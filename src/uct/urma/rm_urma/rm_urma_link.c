/**
 * Copyright (C) Huawei Technologies Co., Ltd. 2026. ALL RIGHTS RESERVED.
 * See file LICENSE for terms.
 */
#include "rm_urma_ep.h"
#include <ucs/sys/sys.h>

void uct_rm_urma_link_init(uct_rm_urma_iface_t *iface,
                          const uct_rm_urma_iface_config_t *config)
{
    memset(&iface->link, 0, sizeof(iface->link));
    iface->link.mode = config->link_mode;
    iface->link.max_peers = config->max_peers;
    ucs_list_head_init(&iface->link.peers);
    ucs_list_head_init(&iface->link.pending);
    kh_init_inplace(uct_rm_urma_requests, &iface->link.requests);
    kh_init_inplace(uct_rm_urma_peers, &iface->link.peers_by_address);
}

ucs_status_t uct_rm_urma_link_ready(uct_rm_urma_iface_t *iface)
{
    uct_rm_urma_iface_addr_v2_t *address = &iface->link.address;
    const urma_seg_t *segment;
    unsigned char wire[UCT_RM_URMA_IFACE_ADDR_V2_SIZE];
    uint32_t reserve;
    ucs_status_t status;

    if (iface->link.mode == UCT_RM_URMA_LINK_LEGACY) {
        return UCS_OK;
    }
    if ((iface->link.mode != UCT_RM_URMA_LINK_IFACE_V2) ||
        !iface->super.config.fc_enabled ||
        (iface->link.max_peers == 0) ||
        (iface->link.max_peers > UINT16_MAX) ||
        (iface->rm_urma_jetty.available < 2) ||
        (iface->super.tx.jfc_available < 2) ||
        (iface->super.config.seg_size <
         UCT_RM_URMA_FC_REQ_V2_SIZE + sizeof(uct_ub_hdr_t))) {
        return UCS_ERR_INVALID_PARAM;
    }

    segment = &iface->super.ka_local_seg->tseg->seg;
    address->segment_va = segment->ubva.va;
    address->segment_length = segment->len;
    address->segment_flags = segment->attr.value;
    address->segment_token = segment->token_id;
    address->jetty_id = iface->rm_urma_jetty.jetty->jetty_id.id;
    address->max_peers = iface->link.max_peers;
    address->initial_credit = iface->super.config.fc_wnd_size;
    address->rx_depth = iface->super.config.rx_queue_depth -
                        iface->super.rx.jfr_que_available;
    address->instance = ucs_generate_uuid((uintptr_t)iface);
    if (address->instance == 0) {
        address->instance = 1;
    }
    status = uct_rm_urma_iface_addr_v2_pack(wire, sizeof(wire), address);
    if (status != UCS_OK) {
        ucs_error("RM iface RX budget invalid: R=%u K=%u W=%u",
                  address->rx_depth, address->max_peers,
                  address->initial_credit);
        return status;
    }
    iface->link.instance = address->instance;
    ucs_debug("RM iface_v2 ready iface=%p R=%u K=%u W=%u instance=%lu",
              iface, address->rx_depth, address->max_peers,
              address->initial_credit, address->instance);
    reserve = 2u * address->max_peers + 2;
    iface->link.rx_free = address->rx_depth - reserve -
                          (uint64_t)address->max_peers * address->initial_credit;
    return UCS_OK;
}

ucs_status_t uct_rm_urma_link_get_address(uct_iface_h tl_iface,
                                         uct_iface_addr_t *address)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_iface,
                                               uct_rm_urma_iface_t);

    if (iface->link.mode != UCT_RM_URMA_LINK_IFACE_V2) {
        return UCS_ERR_UNSUPPORTED;
    }
    ucs_debug("RM iface address export iface=%p instance=%lu", iface,
              iface->link.instance);
    return uct_rm_urma_iface_addr_v2_pack(address,
                 UCT_RM_URMA_IFACE_ADDR_V2_SIZE, &iface->link.address);
}

static ucs_status_t
uct_rm_urma_link_peer_get(uct_rm_urma_iface_t *iface,
                          const uct_rm_urma_return_addr_t *remote,
                          uct_rm_urma_peer_t **peer_p)
{
    uct_rm_urma_peer_t *peer;
    uct_ub_md_t *md = uct_ub_iface_md(&iface->super);
    urma_rjetty_t target = {0};
    urma_seg_t segment = {0};
    urma_import_seg_flag_t flags = {0};
    ucs_status_t status;

    khiter_t slot;
    int inserted;

    slot = kh_get(uct_rm_urma_peers, &iface->link.peers_by_address, remote);
    if (slot != kh_end(&iface->link.peers_by_address)) {
        unsigned char existing[48];
        unsigned char incoming[48];

        peer = kh_value(&iface->link.peers_by_address, slot);
        if (peer == NULL) {
            return UCS_ERR_BUSY;
        }
        uct_rm_urma_iface_addr_v2_pack(existing, sizeof(existing), &peer->remote.iface);
        uct_rm_urma_iface_addr_v2_pack(incoming, sizeof(incoming), &remote->iface);
        if (memcmp(existing, incoming, sizeof(existing))) {
            return UCS_ERR_INVALID_PARAM;
        }
        *peer_p = peer;
        return UCS_OK;
    }
    if (iface->link.peer_count >= iface->link.max_peers) {
        return UCS_ERR_EXCEEDS_LIMIT;
    }
    peer = ucs_calloc(1, sizeof(*peer), "rm_iface_peer");
    if (peer == NULL) {
        return UCS_ERR_NO_MEMORY;
    }
    peer->remote = *remote;
    slot = kh_put(uct_rm_urma_peers, &iface->link.peers_by_address,
                   &peer->remote, &inserted);
    if (inserted < 0) {
        ucs_free(peer);
        return UCS_ERR_NO_MEMORY;
    }
    ucs_assert(inserted != 0);
    kh_value(&iface->link.peers_by_address, slot) = NULL;
    peer->credits = remote->iface.initial_credit;
    /* Reuse the configured hard-threshold ratio with the remote window. */
    peer->hard_threshold = ucs_max(1u,
        (uint64_t)remote->iface.initial_credit * iface->super.config.fc_hard_thresh /
        iface->super.config.fc_wnd_size);
    ucs_list_head_init(&peer->eps);
    memcpy(target.jetty_id.eid.raw, remote->eid, sizeof(remote->eid));
    target.jetty_id.uasid = remote->uasid;
    target.jetty_id.id = remote->iface.jetty_id;
    target.trans_mode = URMA_TM_RM;
    target.type = URMA_JETTY;
    target.flag.bs.order_type = URMA_DEF_ORDER;
    target.tp_type = URMA_CTP;
    status = uct_rm_urma_iface_target_get(iface, &target, &md->dev.token,
                                         &peer->target);
    if (status != UCS_OK) {
        goto err_free;
    }
    if (peer->target->references > 1) {
        /* A new incarnation cannot reuse the old incarnation's pinned
         * hardware target. Dynamic restart is outside bounded iface_v2. */
        uct_rm_urma_iface_target_put(iface, peer->target);
        status = UCS_ERR_UNSUPPORTED;
        goto err_free;
    }
    segment.ubva.eid = target.jetty_id.eid;
    segment.ubva.uasid = remote->uasid;
    segment.ubva.va = remote->iface.segment_va;
    segment.len = remote->iface.segment_length;
    segment.attr.value = remote->iface.segment_flags;
    segment.token_id = remote->iface.segment_token;
    flags.bs.cacheable = URMA_NON_CACHEABLE;
    flags.bs.access = URMA_ACCESS_READ | URMA_ACCESS_WRITE | URMA_ACCESS_ATOMIC;
    flags.bs.mapping = URMA_SEG_NOMAP;
    peer->segment = urma_import_seg(md->dev.urma_ctx, &segment,
                                    &md->dev.token, 0, flags);
    if (peer->segment == NULL) {
        status = UCS_ERR_IO_ERROR;
        uct_rm_urma_iface_target_put(iface, peer->target);
        goto err_free;
    }
    kh_value(&iface->link.peers_by_address, slot) = peer;
    ucs_list_add_tail(&iface->link.peers, &peer->list);
    ++iface->link.peer_count;
    *peer_p = peer;
    return UCS_OK;

err_free:
    kh_del(uct_rm_urma_peers, &iface->link.peers_by_address, slot);
    ucs_free(peer);
    return status;
}

ucs_status_t uct_rm_urma_link_connect(uct_rm_urma_ep_t *ep,
                                     const uct_device_addr_t *device,
                                     const uct_iface_addr_t *address)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(ep->super.super.super.iface,
                                               uct_rm_urma_iface_t);
    uct_ub_address_pack_params_t dev = {0};
    uct_rm_urma_return_addr_t remote = {0};
    uct_rm_urma_peer_t *peer;
    ucs_status_t status;

    status = uct_rm_urma_iface_addr_v2_unpack(address, 48, &remote.iface);
    if (status != UCS_OK) {
        return status;
    }
    uct_ub_address_unpack((const uct_ub_address_t *)device, &dev);
    memcpy(remote.eid, dev.eid.raw, sizeof(remote.eid));
    remote.uasid = dev.uasid;
    status = uct_rm_urma_link_peer_get(iface, &remote, &peer);
    if (status != UCS_OK) {
        return status;
    }
    status = uct_rm_urma_iface_target_get(iface, &peer->target->key.remote,
                                         &peer->target->key.token, &ep->target);
    if (status != UCS_OK) {
        return status;
    }
    ep->peer = peer;
    ep->ka_tseg = peer->segment;
    ep->ka_va_addr = remote.iface.segment_va;
    ep->tjetty = peer->target->jetty;
    ep->super.dep_id = 0;
    ep->super.flags |= UCT_RM_URMA_EP_FLAG_CONNECTED;
    ucs_list_add_tail(&peer->eps, &ep->peer_list);
    ucs_debug("RM iface connect iface=%p peer=%p ep=%p", iface, peer, ep);
    return UCS_OK;
}

int uct_rm_urma_link_can_send(uct_rm_urma_ep_t *ep)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(ep->super.super.super.iface,
                                               uct_rm_urma_iface_t);

    return (ep->peer != NULL) && (ep->peer->credits > 0) &&
           (ep->rm_urma_jetty->available > 1) &&
           (iface->super.tx.jfc_available > 1);
}

ucs_status_t uct_rm_urma_link_check(uct_rm_urma_ep_t *ep)
{
    return uct_rm_urma_link_can_send(ep) ? UCS_OK : UCS_ERR_NO_RESOURCE;
}

static ucs_status_t
uct_rm_urma_link_queue(uct_rm_urma_iface_t *iface, uct_rm_urma_peer_t *peer,
                       uct_rm_urma_ctrl_kind_t kind, uint64_t id,
                       uint32_t credits)
{
    uct_rm_urma_link_req_t *req;
    uct_rm_urma_ctrl_v2_t control = {0};
    uct_rm_urma_return_addr_t address = {0};
    ucs_status_t status;

    req = ucs_mpool_get(&iface->tx.pending_mp);
    if (req == NULL) {
        return UCS_ERR_NO_MEMORY;
    }
    memset(req, 0, sizeof(*req));
    req->peer = peer;
    req->kind = kind;
    req->id = id;
    req->credits = credits;
    req->length = (kind == UCT_RM_URMA_CTRL_REQUEST) ? 100 : 32;
    control.kind = kind;
    control.request_id = id;
    control.credits = credits;
    control.requester_instance = (kind == UCT_RM_URMA_CTRL_REQUEST) ?
                                iface->link.instance : peer->remote.iface.instance;
    control.responder_instance = (kind == UCT_RM_URMA_CTRL_REQUEST) ?
                                peer->remote.iface.instance : iface->link.instance;
    memcpy(address.eid, iface->super.eid.raw, sizeof(address.eid));
    address.uasid = iface->super.uasid;
    address.iface = iface->link.address;
    status = uct_rm_urma_ctrl_v2_pack(req->payload, sizeof(req->payload),
                                     &control, &address);
    if (status != UCS_OK) {
        ucs_mpool_put(req);
        return status;
    }
    status = uct_rm_urma_iface_target_get(iface, &peer->target->key.remote,
                                         &peer->target->key.token, &req->target);
    if (status != UCS_OK) {
        ucs_mpool_put(req);
        return status;
    }
    ucs_list_add_tail(&iface->link.pending, &req->list);
    return UCS_OK;
}

void uct_rm_urma_link_sent(uct_rm_urma_ep_t *ep)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(ep->super.super.super.iface,
                                               uct_rm_urma_iface_t);
    uct_rm_urma_peer_t *peer = ep->peer;
    ucs_status_t status;
    khiter_t it;
    int inserted;

    ucs_assert(peer != NULL && peer->credits > 0);
    --peer->credits;
    ++peer->spent;
    if ((peer->active_id != 0) ||
        (peer->credits > peer->hard_threshold)) {
        return;
    }
    if (iface->link.sequence == UINT64_MAX) {
        uct_rm_urma_iface_set_failed(iface, UCS_ERR_EXCEEDS_LIMIT);
        return;
    }
    peer->active_id = ++iface->link.sequence;
    peer->requested = peer->spent;
    peer->spent = 0;
    peer->request_posted = 0;
    it = kh_put(uct_rm_urma_requests, &iface->link.requests,
                 peer->active_id, &inserted);
    if (inserted < 0) {
        uct_rm_urma_iface_set_failed(iface, UCS_ERR_NO_MEMORY);
        return;
    }
    kh_value(&iface->link.requests, it) = peer;
    status = uct_rm_urma_link_queue(iface, peer, UCT_RM_URMA_CTRL_REQUEST,
                                    peer->active_id, peer->requested);
    if (status != UCS_OK) {
        uct_rm_urma_iface_set_failed(iface, status);
    }
}

ucs_status_t uct_rm_urma_link_receive(uct_rm_urma_iface_t *iface,
                                     const void *buffer, size_t length)
{
    uct_rm_urma_ctrl_v2_t control;
    uct_rm_urma_return_addr_t address;
    uct_rm_urma_peer_t *peer;
    uct_rm_urma_ep_t *ep;
    khiter_t it;
    ucs_status_t status;

    if (iface->tx.error != UCS_OK) {
        return UCS_OK;
    }
    status = uct_rm_urma_ctrl_v2_unpack(buffer, length, iface->link.instance,
                                       &control, &address);
    if (status == UCS_ERR_NO_ELEM) {
        return UCS_OK; /* A valid old-instance reply must not touch any EP. */
    }
    if (status != UCS_OK) {
        return status;
    }
    if (control.kind == UCT_RM_URMA_CTRL_REQUEST) {
        if (control.credits > iface->link.address.initial_credit) {
            return UCS_ERR_INVALID_PARAM;
        }
        status = uct_rm_urma_link_peer_get(iface, &address, &peer);
        if (status != UCS_OK) {
            return status;
        }
        if (control.request_id <= peer->received_id) {
            return UCS_OK;
        }
        if (peer->grant_pending) {
            return UCS_ERR_INVALID_PARAM;
        }
        status = uct_rm_urma_link_queue(iface, peer, UCT_RM_URMA_CTRL_GRANT,
                                        control.request_id, control.credits);
        if (status == UCS_OK) {
            peer->received_id = control.request_id;
            peer->grant_pending = 1;
        }
        return status;
    }
    it = kh_get(uct_rm_urma_requests, &iface->link.requests, control.request_id);
    if (it == kh_end(&iface->link.requests)) {
        return UCS_OK;
    }
    peer = kh_value(&iface->link.requests, it);
    if ((control.responder_instance != peer->remote.iface.instance) ||
        !peer->request_posted) {
        return UCS_OK;
    }
    if ((control.credits != peer->requested) ||
        ((uint64_t)peer->credits + control.credits >
         peer->remote.iface.initial_credit)) {
        return UCS_ERR_INVALID_PARAM;
    }
    kh_del(uct_rm_urma_requests, &iface->link.requests, it);
    peer->active_id = 0;
    peer->credits += control.credits;
    ucs_list_for_each(ep, &peer->eps, peer_list) {
        if (!(ep->super.flags & (UCT_RM_URMA_EP_FLAG_CLOSING |
                                UCT_RM_URMA_EP_FLAG_FLUSH_CANCEL))) {
            ucs_arbiter_group_schedule(&iface->tx.arbiter, &ep->arb_group);
        }
    }
    return UCS_OK;
}

void uct_rm_urma_link_account_rx(uct_rm_urma_iface_t *iface,
                                unsigned data, unsigned control,
                                unsigned posted)
{
    unsigned restored;

    iface->link.data_debt += data;
    iface->link.control_debt += control;
    restored = ucs_min(posted, iface->link.control_debt);
    iface->link.control_debt -= restored;
    posted -= restored;
    restored = ucs_min(posted, iface->link.data_debt);
    iface->link.data_debt -= restored;
    iface->link.rx_free += restored;
}

void uct_rm_urma_link_complete(uct_urma_iface_send_op_t *op, const void *response)
{
    uct_rm_urma_link_req_t *req = ucs_container_of(op, uct_rm_urma_link_req_t, op);
    uct_rm_urma_iface_t *iface = ucs_derived_of(op->iface, uct_rm_urma_iface_t);

    if (req->desc != NULL) {
        ucs_mpool_put(req->desc);
    }
    uct_rm_urma_iface_target_put(iface, req->target);
    ucs_mpool_put(req);
}

void uct_rm_urma_link_progress(uct_rm_urma_iface_t *iface)
{
    uct_rm_urma_link_req_t *req;
    ucs_status_t status;
    unsigned limit = 8;

    if ((iface->tx.error != UCS_OK) || iface->link.control_debt) {
        return;
    }
    while (limit-- && !ucs_list_is_empty(&iface->link.pending)) {
        req = ucs_list_head(&iface->link.pending, uct_rm_urma_link_req_t, list);
        if ((req->kind == UCT_RM_URMA_CTRL_GRANT) &&
            (req->credits > iface->link.rx_free)) {
            ucs_list_del(&req->list);
            ucs_list_add_tail(&iface->link.pending, &req->list);
            continue;
        }
        if (req->kind == UCT_RM_URMA_CTRL_GRANT) {
            iface->link.rx_free -= req->credits;
        }
        status = uct_rm_urma_ep_link_post(iface, req);
        if (status != UCS_OK) {
            if ((req->kind == UCT_RM_URMA_CTRL_GRANT) &&
                (status == UCS_ERR_NO_RESOURCE)) {
                iface->link.rx_free += req->credits;
            }
            if (status != UCS_ERR_NO_RESOURCE) {
                uct_rm_urma_iface_set_failed(iface, status);
            }
            return;
        }
        if (req->kind == UCT_RM_URMA_CTRL_REQUEST) {
            req->peer->request_posted = 1;
        } else {
            req->peer->grant_pending = 0;
        }
        ucs_list_del(&req->list);
    }
}

void uct_rm_urma_link_cleanup(uct_rm_urma_iface_t *iface)
{
    uct_rm_urma_link_req_t *req;
    uct_rm_urma_peer_t *peer, *next_peer;

    while (!ucs_list_is_empty(&iface->link.pending)) {
        req = ucs_list_extract_head(&iface->link.pending,
                                    uct_rm_urma_link_req_t, list);
        if (req->desc != NULL) {
            ucs_mpool_put(req->desc);
        }
        uct_rm_urma_iface_target_put(iface, req->target);
        ucs_mpool_put(req);
    }
    ucs_list_for_each_safe(peer, next_peer, &iface->link.peers, list) {
        ucs_assert(ucs_list_is_empty(&peer->eps));
        if (urma_unimport_seg(peer->segment) != URMA_SUCCESS) {
            ucs_warn("RM peer segment unimport failed at stopped iface close");
        }
        uct_rm_urma_iface_target_put(iface, peer->target);
        ucs_list_del(&peer->list);
        ucs_free(peer);
    }
    kh_destroy_inplace(uct_rm_urma_requests, &iface->link.requests);
    kh_destroy_inplace(uct_rm_urma_peers, &iface->link.peers_by_address);
}
