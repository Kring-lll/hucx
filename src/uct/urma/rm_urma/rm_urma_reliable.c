/**
 * Copyright (C) Huawei Technologies Co., Ltd. 2026. ALL RIGHTS RESERVED.
 * See file LICENSE for terms.
 */
#include "rm_urma_ep.h"
#include "rm_urma_reliable.h"
#include <endian.h>

#define RM_REL_MAGIC 0x33524d53u

/* Never dereference unaligned wire integers. Encoding is little endian. */
static void rel_put32(void *p, unsigned offset, uint32_t value)
{
    value = htole32(value);
    memcpy((uint8_t *)p + offset, &value, sizeof(value));
}

static void rel_put64(void *p, unsigned offset, uint64_t value)
{
    value = htole64(value);
    memcpy((uint8_t *)p + offset, &value, sizeof(value));
}

static uint32_t rel_get32(const void *p, unsigned offset)
{
    uint32_t value;
    memcpy(&value, (const uint8_t *)p + offset, sizeof(value));
    return le32toh(value);
}

static uint64_t rel_get64(const void *p, unsigned offset)
{
    uint64_t value;
    memcpy(&value, (const uint8_t *)p + offset, sizeof(value));
    return le64toh(value);
}

ucs_status_t uct_rm_urma_reliable_address_pack(void *buffer, size_t capacity,
                        const uct_rm_urma_iface_addr_v2_t *address)
{
    ucs_status_t status = uct_rm_urma_iface_addr_v2_pack(buffer, capacity, address);
    if (status == UCS_OK) {
        ((uint8_t *)buffer)[28] = 3;
        ((uint8_t *)buffer)[30] = 2;
    }
    return status;
}

ucs_status_t uct_rm_urma_reliable_address_unpack(const void *buffer, size_t length,
                        uct_rm_urma_iface_addr_v2_t *address)
{
    uint8_t wire[UCT_RM_URMA_IFACE_ADDR_V2_SIZE];
    if ((buffer == NULL) || (length != sizeof(wire)) ||
        (((const uint8_t *)buffer)[28] != 3) ||
        (((const uint8_t *)buffer)[30] != 2) ||
        (((const uint8_t *)buffer)[31] != 0)) {
        return UCS_ERR_INVALID_PARAM;
    }
    memcpy(wire, buffer, sizeof(wire));
    wire[28] = UCT_RM_URMA_ADDR_VERSION;
    wire[30] = UCT_RM_URMA_ADDR_PEER_FC;
    return uct_rm_urma_iface_addr_v2_unpack(wire, sizeof(wire), address);
}

void uct_rm_urma_reliable_peer_init(uct_rm_urma_peer_t *peer)
{
    ucs_list_head_init(&peer->reliable.replay);
    peer->reliable.tx_next = 1;
    peer->reliable.rx_next = 1;
}

static uct_rm_urma_iface_t *rel_iface(uct_rm_urma_ep_t *ep)
{
    return ucs_derived_of(ep->super.super.super.iface, uct_rm_urma_iface_t);
}

int uct_rm_urma_reliable_can_send(uct_rm_urma_ep_t *ep)
{
    uct_rm_urma_iface_t *iface = rel_iface(ep);
    return (ep->peer != NULL) &&
           (ep->peer->reliable.count < ep->peer->remote.iface.initial_credit) &&
           (ep->rm_urma_jetty->available > 1) &&
           (iface->super.tx.jfc_available > 1) &&
           uct_rm_urma_iface_pending_can_send(iface, &ep->arb_group);
}

static void rel_release(uct_rm_urma_iface_t *iface, uct_rm_urma_replay_t *entry)
{
    uct_rm_urma_ep_t *ep = entry->owner;
    ucs_assert(!entry->in_flight && !entry->quarantined);
    if (entry->kind == UCT_RM_URMA_REL_DATA) {
        ucs_list_del(&entry->list);
        --entry->peer->reliable.count;
        ucs_assert(ep->reliable_outstanding > 0);
        --ep->reliable_outstanding;
        if (!(ep->super.flags & UCT_RM_URMA_EP_FLAG_CLOSING)) {
            ucs_arbiter_group_schedule(&iface->tx.arbiter, &ep->arb_group);
        }
    } else {
        entry->peer->reliable.ack_in_flight = 0;
    }
    ucs_mpool_put(entry->desc);
    ucs_mpool_put(entry);
}

void uct_rm_urma_reliable_complete(uct_urma_iface_send_op_t *op,
                                  const void *response)
{
    uct_rm_urma_replay_t *entry = ucs_container_of(op, uct_rm_urma_replay_t, op);
    uct_rm_urma_iface_t *iface = ucs_derived_of(op->iface, uct_rm_urma_iface_t);
    entry->in_flight = 0;
    if ((entry->kind == UCT_RM_URMA_REL_ACK) || entry->acknowledged ||
        (iface->tx.error != UCS_OK)) {
        rel_release(iface, entry);
    }
}

static uct_rm_urma_replay_t *rel_alloc(uct_rm_urma_iface_t *iface,
                                      uct_rm_urma_peer_t *peer, unsigned kind)
{
    uct_rm_urma_replay_t *entry;
    entry = ucs_mpool_get(&iface->tx.pending_mp);
    if (entry == NULL) {
        return NULL;
    }
    /* Reused entries must not inherit ACK, CQE, retry or quarantine state. */
    memset(entry, 0, sizeof(*entry));
    entry->desc = ucs_mpool_get(&iface->super.tx.mp);
    if (entry->desc == NULL) {
        ucs_mpool_put(entry);
        return NULL;
    }
    entry->peer = peer;
    entry->kind = kind;
    entry->op.iface = &iface->super;
    entry->op.flags = 0;
    entry->op.user_comp = NULL;
    entry->op.handler = uct_rm_urma_reliable_complete;
    ucs_list_head_init(&entry->list);
    return entry;
}

static void rel_header(uct_rm_urma_iface_t *iface,
                        uct_rm_urma_replay_t *entry, uint8_t id, size_t length)
{
    uct_ub_hdr_t *ub = (uct_ub_hdr_t *)(entry->desc + 1);
    uint8_t *wire = (void *)(ub + 1);
    ub->am_id = UCT_RM_URMA_EP_FC_PURE_GRANT;
    ub->ep_id = 0;
    memset(wire, 0, UCT_RM_URMA_REL_PREFIX);
    rel_put32(wire, 0, RM_REL_MAGIC);
    wire[4] = 3;
    wire[5] = entry->kind;
    wire[6] = id;
    rel_put64(wire, 8, iface->link.instance);
    rel_put64(wire, 16, entry->peer->remote.iface.instance);
    rel_put64(wire, 24, entry->psn);
    rel_put32(wire, 32, length);
    entry->length = sizeof(*ub) + UCT_RM_URMA_REL_PREFIX;
    if (entry->kind == UCT_RM_URMA_REL_DATA) {
        memcpy(wire + 36, iface->super.eid.raw, 16);
        rel_put32(wire, 52, iface->super.uasid);
        /* Validated once in link_ready; encode the return address on EVERY
         * DATA, so losing the first DATA does not lose bootstrap state. */
        uct_rm_urma_reliable_address_pack(wire + 56, 48, &iface->link.address);
        entry->length = sizeof(*ub) + UCT_RM_URMA_REL_HEADER + length;
    }
}

static ucs_status_t rel_check(uct_rm_urma_ep_t *ep)
{
    uct_rm_urma_iface_t *iface = rel_iface(ep);
    if (iface->tx.error != UCS_OK) {
        return iface->tx.error;
    }
    if (ep->super.flags & (UCT_RM_URMA_EP_FLAG_CLOSING |
                           UCT_RM_URMA_EP_FLAG_FLUSH_CANCEL |
                           UCT_RM_URMA_EP_FLAG_ERR_HANDLER_INVOKED)) {
        return UCS_ERR_CANCELED;
    }
    if (!uct_rm_urma_reliable_can_send(ep)) {
        return UCS_ERR_NO_RESOURCE;
    }
    /* Exhaustion is explicit; no ambiguous modular comparison at wrap. */
    if (ep->peer->reliable.tx_next == UINT64_MAX) {
        uct_rm_urma_iface_set_failed(iface, UCS_ERR_EXCEEDS_LIMIT);
        return UCS_ERR_EXCEEDS_LIMIT;
    }
    return UCS_OK;
}

static ucs_status_t rel_accept(uct_rm_urma_ep_t *ep,
                              uct_rm_urma_replay_t *entry, uint8_t id,
                              size_t length)
{
    uct_rm_urma_iface_t *iface = rel_iface(ep);
    ucs_status_t status;
    entry->owner = ep;
    entry->psn = ep->peer->reliable.tx_next;
    rel_header(iface, entry, id, length);
    status = uct_rm_urma_ep_reliable_post(iface, entry);
    if (status != UCS_OK) {
        if (entry->quarantined) {
            ucs_list_add_tail(&iface->reliable.quarantine, &entry->list);
        } else {
            ucs_mpool_put(entry->desc);
            ucs_mpool_put(entry);
        }
        if (status != UCS_ERR_NO_RESOURCE) {
            uct_rm_urma_iface_set_failed(iface, status);
        }
        return status;
    }
    entry->accepted_at = entry->posted_at = ucs_get_time();
    ++ep->peer->reliable.tx_next;
    ++ep->peer->reliable.count;
    ++ep->reliable_outstanding;
    ucs_list_add_tail(&ep->peer->reliable.replay, &entry->list);
    return UCS_OK;
}

ucs_status_t uct_rm_urma_reliable_short(uct_rm_urma_ep_t *ep, uint8_t id,
                        uint64_t header, const void *buffer, unsigned length)
{
    uct_rm_urma_iface_t *iface = rel_iface(ep);
    uct_rm_urma_replay_t *entry;
    uint8_t *data;
    ucs_status_t status;
    if ((id >= UCT_AM_ID_MAX) ||
        ((size_t)length + 8 + UCT_RM_URMA_REL_HEADER + sizeof(uct_ub_hdr_t) >
         iface->super.config.seg_size)) {
        return UCS_ERR_INVALID_PARAM;
    }
    status = rel_check(ep);
    if (status != UCS_OK) {
        return status;
    }
    entry = rel_alloc(iface, ep->peer, UCT_RM_URMA_REL_DATA);
    if (entry == NULL) {
        return UCS_ERR_NO_RESOURCE;
    }
    data = (uint8_t *)(entry->desc + 1) + sizeof(uct_ub_hdr_t) + UCT_RM_URMA_REL_HEADER;
    memcpy(data, &header, sizeof(header));
    if (length != 0) {
        memcpy(data + sizeof(header), buffer, length);
    }
    return rel_accept(ep, entry, id, length + sizeof(header));
}

ssize_t uct_rm_urma_reliable_bcopy(uct_rm_urma_ep_t *ep, uint8_t id,
                                  uct_pack_callback_t pack, void *arg)
{
    uct_rm_urma_iface_t *iface = rel_iface(ep);
    uct_rm_urma_replay_t *entry;
    size_t length;
    ucs_status_t status;
    if (id >= UCT_AM_ID_MAX) {
        return UCS_ERR_INVALID_PARAM;
    }
    status = rel_check(ep);
    if (status != UCS_OK) {
        return status;
    }
    entry = rel_alloc(iface, ep->peer, UCT_RM_URMA_REL_DATA);
    if (entry == NULL) {
        return UCS_ERR_NO_RESOURCE;
    }
    /* Like other UCT BCOPY pack callbacks, pack must respect queried max_bcopy. */
    length = pack((uint8_t *)(entry->desc + 1) + sizeof(uct_ub_hdr_t) +
                   UCT_RM_URMA_REL_HEADER, arg);
    if (length > iface->super.config.seg_size - sizeof(uct_ub_hdr_t) -
                 UCT_RM_URMA_REL_HEADER) {
        ucs_mpool_put(entry->desc);
        ucs_mpool_put(entry);
        return UCS_ERR_INVALID_PARAM;
    }
    status = rel_accept(ep, entry, id, length);
    return (status == UCS_OK) ? (ssize_t)length : status;
}

ucs_status_t uct_rm_urma_reliable_receive(uct_rm_urma_iface_t *iface,
                                         const void *buffer, size_t length)
{
    const uint8_t *wire = buffer;
    uct_rm_urma_return_addr_t remote = {0};
    uct_rm_urma_peer_t *peer;
    uct_rm_urma_replay_t *entry, *next;
    ucs_status_t status;
    uint64_t psn, source;
    uint32_t payload;
    if (iface->tx.error != UCS_OK) {
        return UCS_OK;
    }
    if ((length < UCT_RM_URMA_REL_PREFIX) ||
        (rel_get32(wire, 0) != RM_REL_MAGIC) || (wire[4] != 3) || wire[7]) {
        return UCS_ERR_INVALID_PARAM;
    }
    /* Stale, well-formed instances cannot complete current transmissions. */
    if (rel_get64(wire, 16) != iface->link.instance) {
        return UCS_OK;
    }
    source = rel_get64(wire, 8);
    psn = rel_get64(wire, 24);
    payload = rel_get32(wire, 32);
    if ((wire[5] == UCT_RM_URMA_REL_ACK) &&
        (length == UCT_RM_URMA_REL_PREFIX) && !wire[6] && !payload) {
        ucs_list_for_each(peer, &iface->link.peers, list) {
            if (peer->remote.iface.instance != source) {
                continue;
            }
            if (psn >= peer->reliable.tx_next) {
                return UCS_ERR_INVALID_PARAM;
            }
            ucs_list_for_each_safe(entry, next, &peer->reliable.replay, list) {
                if (entry->psn > psn) {
                    break;
                }
                entry->acknowledged = 1;
                if (!entry->in_flight && !entry->quarantined) {
                    rel_release(iface, entry);
                }
            }
            return UCS_OK;
        }
        return UCS_OK; /* no matching current stream */
    }
    if ((wire[5] != UCT_RM_URMA_REL_DATA) || (wire[6] >= UCT_AM_ID_MAX) ||
        !psn || (psn == UINT64_MAX) ||
        (length != UCT_RM_URMA_REL_HEADER + (size_t)payload) ||
        (length + sizeof(uct_ub_hdr_t) > iface->super.config.seg_size)) {
        return UCS_ERR_INVALID_PARAM;
    }
    memcpy(remote.eid, wire + 36, 16);
    remote.uasid = rel_get32(wire, 52);
    status = uct_rm_urma_reliable_address_unpack(wire + 56, 48, &remote.iface);
    if ((status != UCS_OK) || (remote.iface.instance != source)) {
        return UCS_ERR_INVALID_PARAM;
    }
    status = uct_rm_urma_link_peer_get(iface, &remote, &peer);
    if (status != UCS_OK) {
        return status;
    }
    peer->reliable.ack_pending = 1;
    if (psn != peer->reliable.rx_next) {
        ++iface->reliable.discarded;
        return UCS_OK; /* future and duplicate DATA never reach UCP */
    }
    ++peer->reliable.rx_next;
    ++iface->reliable.delivered;
    /* The reliable header changes the descriptor offset. Flags=0 explicitly
     * requires synchronous consumption/copy; do not expose an invalid DESC. */
    return uct_iface_invoke_am(&iface->super.super, wire[6],
                    (void *)(wire + UCT_RM_URMA_REL_HEADER), payload, 0);
}

void uct_rm_urma_reliable_progress(uct_rm_urma_iface_t *iface, ucs_time_t now)
{
    uct_rm_urma_peer_t *peer;
    uct_rm_urma_replay_t *entry, *next;
    ucs_status_t status;
    unsigned budget = 8;
    if (iface->tx.error != UCS_OK) {
        return;
    }
    ucs_list_for_each(peer, &iface->link.peers, list) {
        if (peer->reliable.ack_in_flight &&
            (now - peer->reliable.ack_posted_at >= iface->reliable.timeout)) {
            uct_rm_urma_iface_set_failed(iface, UCS_ERR_ENDPOINT_TIMEOUT);
            return;
        }
        if (!iface->reliable.rx_debt && peer->reliable.ack_pending &&
            !peer->reliable.ack_in_flight && budget) {
            entry = rel_alloc(iface, peer, UCT_RM_URMA_REL_ACK);
            if (entry == NULL) {
                return;
            }
            entry->psn = peer->reliable.rx_next - 1;
            rel_header(iface, entry, 0, 0);
            status = uct_rm_urma_ep_reliable_post(iface, entry);
            if (status != UCS_OK) {
                if (entry->quarantined) {
                    ucs_list_add_tail(&iface->reliable.quarantine, &entry->list);
                } else {
                    ucs_mpool_put(entry->desc);
                    ucs_mpool_put(entry);
                }
                if (status != UCS_ERR_NO_RESOURCE) {
                    uct_rm_urma_iface_set_failed(iface, status);
                }
                return;
            }
            peer->reliable.ack_pending = 0;
            peer->reliable.ack_in_flight = 1;
            peer->reliable.ack_posted_at = now;
            ++iface->reliable.acks;
            --budget;
        }
        ucs_list_for_each_safe(entry, next, &peer->reliable.replay, list) {
            if (now - entry->accepted_at >= iface->reliable.timeout) {
                uct_rm_urma_iface_set_failed(iface, UCS_ERR_ENDPOINT_TIMEOUT);
                return;
            }
            if (entry->acknowledged) {
                continue;
            }
            if (entry->in_flight || (now - entry->posted_at < iface->reliable.rto)) {
                continue;
            }
            if (entry->retries >= iface->reliable.max_retries) {
                uct_rm_urma_iface_set_failed(iface, UCS_ERR_ENDPOINT_TIMEOUT);
                return;
            }
            if (!budget || (iface->rm_urma_jetty.available <= 1) ||
                (iface->super.tx.jfc_available <= 1)) {
                return;
            }
            status = uct_rm_urma_ep_reliable_post(iface, entry);
            if (status == UCS_ERR_NO_RESOURCE) {
                return;
            }
            if (status != UCS_OK) {
                uct_rm_urma_iface_set_failed(iface, status);
                return;
            }
            ++entry->retries;
            ++iface->reliable.retries;
            ucs_debug("RM reliable retry peer=%p psn=%lu attempt=%u",
                      peer, entry->psn, entry->retries);
            entry->posted_at = now;
            --budget;
        }
    }
}

void uct_rm_urma_reliable_cleanup(uct_rm_urma_iface_t *iface)
{
    uct_rm_urma_peer_t *peer;
    uct_rm_urma_replay_t *entry, *next;
    while (!ucs_list_is_empty(&iface->reliable.quarantine)) {
        entry = ucs_list_extract_head(&iface->reliable.quarantine,
                                      uct_rm_urma_replay_t, list);
        ucs_mpool_put(entry->desc);
        ucs_mpool_put(entry);
    }
    ucs_list_for_each(peer, &iface->link.peers, list) {
        ucs_list_for_each_safe(entry, next, &peer->reliable.replay, list) {
            ucs_assert(!entry->in_flight);
            entry->quarantined = 0;
            rel_release(iface, entry);
        }
    }
}
