/**
* Copyright (C) Huawei Technologies Co., Ltd. 2026. ALL RIGHTS RESERVED.
*
* See file LICENSE for terms.
*/

#ifdef HAVE_CONFIG_H
#  include "config.h"
#endif

#include <uct/urma/rm_urma/rm_urma_iface.h>
#include <uct/urma/base/urma_device.h>
#include <uct/urma/rm_urma/rm_urma_ep.h>

#include <uct/api/uct.h>
#include <uct/base/uct_md.h>
#include <uct/base/uct_iface.h>
#include <ucs/arch/bitops.h>
#include <ucs/arch/cpu.h>
#include <ucs/type/class.h>
#include <ucs/type/cpu_set.h>
#include <ucs/debug/log.h>
#include <ucs/sys/string.h>

static void UCS_CLASS_DELETE_FUNC_NAME(uct_rm_urma_iface_t)(uct_iface_t*);

static const char *uct_rm_urma_link_mode_names[] = {
    "legacy_ep", "iface_v2", NULL
};

static const char *uct_rm_urma_flush_mode_names[] = {
    [UCT_RM_URMA_FLUSH_MODE_RDMA_WRITE_0] = "write0",
    [UCT_RM_URMA_FLUSH_MODE_FLOW_CONTROL] = "fc",
    [UCT_RM_URMA_FLUSH_MODE_AUTO]         = "auto",
    [UCT_RM_URMA_FLUSH_MODE_LAST]         = NULL
};

#ifdef ENABLE_STATS
static ucs_stats_class_t uct_rm_urma_iface_stats_class = {
    .name = "rm_urma_iface",
    .num_counters = UCT_RM_URMA_IFACE_STAT_LAST,
    .counter_names = {
        [UCT_RM_URMA_IFACE_STAT_RX_DL_CR_ERR]       = "rx_deal_cr_err",
        [UCT_RM_URMA_IFACE_STAT_TX_DL_CR_ERR]       = "tx_deal_cr_err",
        [UCT_RM_URMA_IFACE_STAT_RX_FC_REQ]          = "rx_fc_req",
        [UCT_RM_URMA_IFACE_STAT_TX_FC_GRANT]        = "tx_fc_grant",
        [UCT_RM_URMA_IFACE_STAT_TX_KA_SUCCESS]      = "tx_ka_success",
        [UCT_RM_URMA_IFACE_STAT_TX_KA_FAIL]         = "tx_ka_fail",
        [UCT_RM_URMA_IFACE_STAT_NO_CQE]             = "no_cqe"
    }
};
#endif /* ENABLE_STATS */

ucs_config_field_t uct_rm_urma_iface_config_table[] = {
    /* Use the validated AM segment size by default. This is a software default,
     * not a device-wide CTP limit; explicit SEG_SIZE settings still apply. */
    {"URMA_", "SEG_SIZE=4096", NULL,
     ucs_offsetof(uct_rm_urma_iface_config_t, super),
     UCS_CONFIG_TYPE_TABLE(uct_urma_iface_config_table)},

    {"TX_JFC_LEN", "4096",
     "Length of send completion queue. This limits the total number of outstanding signaled sends.",
     ucs_offsetof(uct_rm_urma_iface_config_t, tx_jfc_len), UCS_CONFIG_TYPE_UINT},

    {"FLUSH_MODE", "auto",
     "Method to use for posting flush operation:\n"
     " - write0 : Post empty RDMA_WRITE\n"
     " - fc     : Send flow control message\n"
     " - auto   : Select automatically based on device support",
     ucs_offsetof(uct_rm_urma_iface_config_t, flush_mode),
     UCS_CONFIG_TYPE_ENUM(uct_rm_urma_flush_mode_names)},

    {"FC_SOFT_THRESH", "0.5",
     "Threshold for sending soft request for FC credits to the peer. This value\n"
     "refers to the percentage of the FC_WND_SIZE value. (must be > HARD_THRESH and < 1)",
     ucs_offsetof(uct_rm_urma_iface_config_t, soft_thresh), UCS_CONFIG_TYPE_DOUBLE},

    {"LINK_MODE", "legacy_ep",
     "RM connection mode: legacy_ep or bounded iface_v2.",
     ucs_offsetof(uct_rm_urma_iface_config_t, link_mode),
     UCS_CONFIG_TYPE_ENUM(uct_rm_urma_link_mode_names)},

    {"MAX_PEERS", "0",
     "Static lifetime bound on distinct incoming/outgoing remote ifaces.\n"
     "Required for iface_v2; every participant must honor the job bound.",
     ucs_offsetof(uct_rm_urma_iface_config_t, max_peers), UCS_CONFIG_TYPE_UINT},

    {"NATIVE_WRITE", "n",
     "Enable the candidate single-IOV PUT_ZCOPY path (up to 4096 bytes).\n"
     "Default off; READ has its own switch. Other PUT paths stay off.",
     ucs_offsetof(uct_rm_urma_iface_config_t, native_write), UCS_CONFIG_TYPE_BOOL},

    {"NATIVE_READ", "n",
     "Enable single-IOV GET_ZCOPY (up to 4096 bytes).\n"
     "Default off; this does not enable GET_BCOPY or MPI RMA.",
     ucs_offsetof(uct_rm_urma_iface_config_t, native_read), UCS_CONFIG_TYPE_BOOL},

    {"NATIVE_RMA", "n",
     "Enable bounded native PUT_SHORT/PUT_BCOPY/GET_BCOPY and zcopy.\n"
     "Default off; host RMA integration candidate, not atomic support.",
     ucs_offsetof(uct_rm_urma_iface_config_t, native_rma), UCS_CONFIG_TYPE_BOOL},

    {NULL}
};

ucs_status_t uct_rm_urma_iface_flush(uct_iface_h tl_iface, unsigned flags, uct_completion_t *comp)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = NULL;
    ucs_status_t status;
    int count = 0;
    int i;

    if (comp != NULL) {
        return UCS_ERR_UNSUPPORTED;
    }
    if (iface->tx.error != UCS_OK) {
        return iface->tx.error;
    }

    if ((iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) &&
        !ucs_list_is_empty(&iface->link.pending)) {
        ++count;
    }
    if ((iface->tx.ctrl_ep != NULL) &&
        !ucs_arbiter_group_is_empty(&iface->tx.ctrl_ep->arb_group)) {
        ++count;
    }

    for (i = 0; i < iface->super.eps.size; i++) {
        if (__ucs_ptr_array_is_free(iface->super.eps.start[i])) {
            continue;
        }
        ep = ucs_derived_of((uct_ub_ep_t *)iface->super.eps.start[i], uct_rm_urma_ep_t);
        status = uct_ep_flush(&ep->super.super.super, flags, NULL);
        if ((status == UCS_ERR_NO_RESOURCE) || (status == UCS_INPROGRESS)) {
            ++count;
        } else if (status != UCS_OK) {
            return status;
        }
    }

    if (count != 0) {
        UCT_TL_IFACE_STAT_FLUSH_WAIT(&iface->super.super);
        return UCS_INPROGRESS;
    }

    UCT_TL_IFACE_STAT_FLUSH(&iface->super.super);

    return UCS_OK;
}

static void uct_rm_urma_iface_common_progress_enable(uct_iface_h tl_iface, unsigned flags)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_iface, uct_rm_urma_iface_t);

    uct_base_iface_progress_enable_cb(&iface->super.super, iface->progress, flags);
}

static ucs_status_t uct_rm_urma_iface_query(uct_iface_h tl_iface, uct_iface_attr_t *iface_attr)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_iface, uct_rm_urma_iface_t);

    ucs_status_t status = uct_ub_base_iface_query(&(iface->super), iface_attr);
    if (status != UCS_OK) {
        return status;
    }

    status = uct_ub_iface_cap_query(&(iface->super), iface_attr);
    if (status != UCS_OK) {
        return status;
    }

    iface_attr->ep_addr_len = sizeof(uct_rm_urma_ep_addr_params_t);
    /* R1 validates AM short/bcopy. RMA, atomic and peer-check await their own
     * RM/CTP contracts and acceptance; do not expose copied RC capabilities. */
    iface_attr->cap.flags &= ~(UCT_IFACE_FLAG_PUT_SHORT |
                               UCT_IFACE_FLAG_PUT_BCOPY |
                               UCT_IFACE_FLAG_PUT_ZCOPY |
                               UCT_IFACE_FLAG_GET_BCOPY |
                               UCT_IFACE_FLAG_GET_ZCOPY |
                               UCT_IFACE_FLAG_AM_ZCOPY |
                               UCT_IFACE_FLAG_EP_CHECK |
                               UCT_IFACE_FLAG_ERRHANDLE_PEER_FAILURE |
                               UCT_IFACE_FLAG_ATOMIC_DEVICE);
    if (iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) {
        iface_attr->iface_addr_len = UCT_RM_URMA_IFACE_ADDR_V2_SIZE;
        iface_attr->ep_addr_len = 0;
        iface_attr->cap.flags &= ~UCT_IFACE_FLAG_CONNECT_TO_EP;
        iface_attr->cap.flags |= UCT_IFACE_FLAG_CONNECT_TO_IFACE;
    } else {
        iface_attr->iface_addr_len = 0;
        iface_attr->cap.flags |= UCT_IFACE_FLAG_CONNECT_TO_EP;
    }
    if (iface->config.native_write || iface->config.native_rma) {
        iface_attr->cap.flags |= UCT_IFACE_FLAG_PUT_ZCOPY;
        iface_attr->cap.put.min_zcopy = 1;
        iface_attr->cap.put.max_zcopy = ucs_min(4096, iface_attr->cap.put.max_zcopy);
        iface_attr->cap.put.max_iov = 1;
    }
    if (iface->config.native_read || iface->config.native_rma) {
        iface_attr->cap.flags |= UCT_IFACE_FLAG_GET_ZCOPY;
        iface_attr->cap.get.min_zcopy = 1;
        iface_attr->cap.get.max_zcopy = ucs_min(4096, iface_attr->cap.get.max_zcopy);
        iface_attr->cap.get.max_iov = 1;
    }
    if (iface->config.native_rma) {
        iface_attr->cap.flags |= UCT_IFACE_FLAG_PUT_BCOPY |
                                 UCT_IFACE_FLAG_GET_BCOPY;
        iface_attr->cap.put.max_short = ucs_min(4096, iface->super.config.max_inline);
        if (iface_attr->cap.put.max_short > 0) {
            iface_attr->cap.flags |= UCT_IFACE_FLAG_PUT_SHORT;
        }
        iface_attr->cap.put.max_bcopy = ucs_min(4096, iface->super.config.seg_size);
        iface_attr->cap.get.max_bcopy = ucs_min(4096, iface->super.config.seg_size);
    }
    iface_attr->cap.atomic32.op_flags = 0;
    iface_attr->cap.atomic32.fop_flags = 0;
    iface_attr->cap.atomic64.op_flags = 0;
    iface_attr->cap.atomic64.fop_flags = 0;
    iface_attr->overhead = 75e-9; /* Software overhead */

    return UCS_OK;
}

static UCS_F_ALWAYS_INLINE void uct_rm_urma_update_tx_res(uct_rm_urma_iface_t *iface, uct_rm_urma_ep_t *ep,
                                                          unsigned count)
{
    /*
     * PI (Previous Instruction) and CI (Current Instruction) values
     * simulate hardware-level instruction registers. These values are designed
     * to support single-wrap-around behavior (e.g., max_value + 1 = min_value),
     * thus explicit wrap-around checks are omitted for performance.
     *
     * Note: Values are expected to wrap at most once between state updates.
     *        No overflow/underflow validation is performed per design.
     */
    ep->super.status.ci += count;
    uct_ub_iface_update_reads(&(iface->super));
    ep->rm_urma_jetty->available += count;
    iface->super.tx.jfc_available += count;
}

static UCS_F_ALWAYS_INLINE uct_ub_iface_find_ep_t
uct_rm_urma_get_ep_by_cr(uct_rm_urma_iface_t *iface, urma_cr_t *cr,
                       uct_rm_urma_ep_t **ep)
{
    uint32_t id = cr->user_ctx >> UCT_UB_EP_ID_OFFSET;
    uct_ub_ep_t *base_ep;

    if (id >= iface->super.eps.size) {
        return UCT_UB_EP_NOT_EXIST;
    }
    base_ep = (uct_ub_ep_t *)iface->super.eps.start[id];
    if (__ucs_ptr_array_is_free(base_ep)) {
        return UCT_UB_EP_NOT_EXIST;
    }
    *ep = ucs_derived_of(base_ep, uct_rm_urma_ep_t);
    return UCT_UB_FIND_DATA_EP;
}

void uct_rm_urma_iface_set_failed(uct_rm_urma_iface_t *iface, ucs_status_t status)
{
    urma_jetty_attr_t attr = { .mask = JETTY_STATE,
                               .state = URMA_JETTY_STATE_ERROR };
    uct_rm_urma_ep_t *ep;
    unsigned i;

    if (iface->tx.error != UCS_OK) {
        return;
    }
    iface->tx.error = status;
    ++iface->tx.progress_depth;
    if (urma_modify_jetty(iface->rm_urma_jetty.jetty, &attr) != URMA_SUCCESS) {
        ucs_warn("Failed to stop RM shared Jetty; retaining outstanding buffers");
    }
    /* A shared-Jetty failure conservatively affects all its EPs. No recovery
     * or per-peer fault isolation is claimed by R1. This scan is error-only. */
    for (i = 0; i < iface->super.eps.size; ++i) {
        if (__ucs_ptr_array_is_free(iface->super.eps.start[i])) {
            continue;
        }
        ep = (uct_rm_urma_ep_t *)iface->super.eps.start[i];
        if (ep == iface->tx.ctrl_ep) {
            /* This EP is owned by the iface, never by an upper-layer user. */
            ucs_arbiter_group_desched(&iface->tx.arbiter, &ep->arb_group);
            continue;
        }
        if (ep->super.flags & (UCT_RM_URMA_EP_FLAG_CLOSING |
                              UCT_RM_URMA_EP_FLAG_ERR_HANDLER_INVOKED)) {
            continue;
        }
        ep->super.flags |= UCT_RM_URMA_EP_FLAG_ERR_HANDLER_INVOKED;
        ucs_arbiter_group_desched(&iface->tx.arbiter, &ep->arb_group);
        uct_iface_handle_ep_err(&iface->super.super.super,
                               &ep->super.super.super, status);
    }
    --iface->tx.progress_depth;
}

static void uct_rm_urma_handle_failure(uct_urma_iface_t *ub_iface, urma_cr_t *cr)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(ub_iface, uct_rm_urma_iface_t);
    ucs_status_t status = (cr->status == URMA_CR_ACK_TIMEOUT_ERR ||
                          cr->status == URMA_CR_RNR_RETRY_CNT_EXC_ERR) ?
                         UCS_ERR_ENDPOINT_TIMEOUT : UCS_ERR_IO_ERROR;
    uct_rm_urma_iface_set_failed(iface, status);
}

ucs_status_t uct_rm_urma_iface_target_get(uct_rm_urma_iface_t *iface,
                                         const urma_rjetty_t *remote,
                                         const urma_token_t *token,
                                         uct_rm_urma_target_t **target_p)
{
    uct_rm_urma_target_key_t key = {*remote, *token};
    uct_rm_urma_target_t *target;
    uct_ub_md_t *md = uct_ub_iface_md(&iface->super);
    khiter_t iter;
    int inserted;

    *target_p = NULL;
    if (iface->tx.error != UCS_OK) {
        return iface->tx.error;
    }

    iter = kh_get(uct_rm_urma_targets, &iface->targets, &key);
    if (iter != kh_end(&iface->targets)) {
        target = kh_value(&iface->targets, iter);
        /* A failed unimport is quarantined, never handed to a new EP. */
        if (target->references == 0) {
            return UCS_ERR_IO_ERROR;
        }
        if (target->references == UINT_MAX) {
            return UCS_ERR_NO_RESOURCE;
        }
        ++target->references;
        ucs_debug("RM target reuse iface=%p target=%p refs=%u", iface,
                  target, target->references);
        *target_p = target;
        return UCS_OK;
    }

    target = ucs_malloc(sizeof(*target), "rm_target");
    if (target == NULL) {
        return UCS_ERR_NO_MEMORY;
    }
    target->key        = key;
    target->references = 0;
    target->jetty      = NULL;

    /* Reserve the slot before import, but never publish an incomplete value.
     * UCT iface access is serialized by the existing worker contract. */
    iter = kh_put(uct_rm_urma_targets, &iface->targets, &target->key, &inserted);
    if (inserted < 0) {
        ucs_free(target);
        return UCS_ERR_NO_MEMORY;
    }
    ucs_assert(inserted != 0);
    target->jetty = urma_import_jetty(md->dev.urma_ctx, &key.remote,
                                     &key.token);
    if (target->jetty == NULL) {
        kh_del(uct_rm_urma_targets, &iface->targets, iter);
        ucs_free(target);
        return UCS_ERR_IO_ERROR;
    }

    target->references             = 1;
    kh_value(&iface->targets, iter) = target;
    *target_p                      = target;
    ucs_debug("RM target import iface=%p target=%p jetty_id=%u", iface,
              target, remote->jetty_id.id);
    return UCS_OK;
}

void uct_rm_urma_iface_target_put(uct_rm_urma_iface_t *iface,
                                 uct_rm_urma_target_t *target)
{
    khiter_t iter;

    ucs_assert(target->references > 0);
    if (--target->references != 0) {
        return;
    }
    if (urma_unimport_jetty(target->jetty) != URMA_SUCCESS) {
        ucs_warn("Failed to unimport RM target; quarantining until iface close");
        return;
    }

    iter = kh_get(uct_rm_urma_targets, &iface->targets, &target->key);
    ucs_assert(iter != kh_end(&iface->targets));
    ucs_assert(kh_value(&iface->targets, iter) == target);
    kh_del(uct_rm_urma_targets, &iface->targets, iter);
    ucs_debug("RM target release iface=%p target=%p", iface, target);
    ucs_free(target);
}

static ucs_status_t
uct_rm_urma_iface_send_fc_grant_owned(uct_rm_urma_iface_t *iface,
                                     const urma_rjetty_t *remote,
                                     const urma_token_t *token,
                                     uint32_t remote_ep_id,
                                     uct_rm_urma_ep_t *legacy_ep)
{
    uct_ep_params_t params = {
        .field_mask = UCT_EP_PARAM_FIELD_IFACE,
        .iface      = &iface->super.super.super
    };
    uct_rm_urma_fc_pending_req_t *req;
    uct_ep_t *tl_ep;
    ucs_status_t status;

    if (iface->tx.error != UCS_OK) {
        return iface->tx.error;
    }
    if (!iface->super.config.fc_enabled ||
        (remote_ep_id >= UCT_UB_IFACE_MAX_EPS) ||
        (remote->trans_mode != URMA_TM_RM) || (remote->type != URMA_JETTY) ||
        (remote->tp_type != URMA_CTP)) {
        return UCS_ERR_INVALID_PARAM;
    }

    req = ucs_mpool_get(&iface->tx.pending_mp);
    if (req == NULL) {
        return UCS_ERR_NO_MEMORY;
    }
    status = uct_rm_urma_iface_target_get(iface, remote, token, &req->target);
    if (status != UCS_OK) {
        ucs_mpool_put(req);
        return status;
    }
    req->desc         = NULL;
    req->remote_ep_id = remote_ep_id;
    req->legacy_ep    = legacy_ep;

    if (iface->tx.ctrl_ep == NULL) {
        /* Reuse the ordinary software EP constructor and CQE routing. It
         * borrows the existing Jetty and has no connected business peer. */
        status = UCS_CLASS_NEW(uct_rm_urma_ep_t, &tl_ep, &params);
        if (status != UCS_OK) {
            uct_rm_urma_iface_target_put(iface, req->target);
            ucs_mpool_put(req);
            return status;
        }
        iface->tx.ctrl_ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
        iface->tx.ctrl_ep->super.fc.fc_wnd = INT16_MAX;
        ucs_debug("RM control sender created iface=%p ep=%p", iface,
                  iface->tx.ctrl_ep);
    }

    req->super.ep         = &iface->tx.ctrl_ep->super.super.super;
    req->super.super.func = uct_rm_urma_ep_fc_grant;
    if (ucs_arbiter_group_is_empty(&iface->tx.ctrl_ep->arb_group)) {
        status = uct_rm_urma_ep_fc_grant(&req->super.super);
        if (status != UCS_ERR_NO_RESOURCE) {
            return status;
        }
    }

    /* A shortage retains the same target and request. Append to preserve
     * control order, even when a later request could be posted immediately. */
    uct_pending_req_arb_group_push(&iface->tx.ctrl_ep->arb_group,
                                   &req->super.super);
    ucs_arbiter_group_schedule(&iface->tx.arbiter,
                               &iface->tx.ctrl_ep->arb_group);
    return UCS_OK;
}

ucs_status_t uct_rm_urma_iface_send_fc_grant(uct_rm_urma_iface_t *iface,
                                           const urma_rjetty_t *remote,
                                           const urma_token_t *token,
                                           uint32_t remote_ep_id)
{
    return uct_rm_urma_iface_send_fc_grant_owned(iface, remote, token,
                                                remote_ep_id, NULL);
}

static void uct_rm_urma_iface_targets_cleanup(uct_rm_urma_iface_t *iface)
{
    uct_rm_urma_target_t *target;

    /* UCT requires all EPs to be destroyed before closing their iface. The
     * shared Jetty is already stopped; retry only quarantined imports. */
    kh_foreach_value(&iface->targets, target, {
        ucs_assert_always(target->references == 0);
        if (urma_unimport_jetty(target->jetty) != URMA_SUCCESS) {
            ucs_warn("RM target unimport still failed at iface close");
        }
        ucs_free(target);
    });
    kh_destroy_inplace(uct_rm_urma_targets, &iface->targets);
}

void uct_rm_urma_iface_gc(uct_rm_urma_iface_t *iface)
{
    uct_rm_urma_ep_t *ep, *tmp;

    ++iface->tx.progress_depth;
    ucs_list_for_each_safe(ep, tmp, &iface->tx.retired_eps, retired_list) {
        if (ep->super.status.pi != ep->super.status.ci) {
            continue;
        }
        ucs_list_del(&ep->retired_list);
        UCS_CLASS_DELETE(uct_rm_urma_ep_t, ep);
    }
    --iface->tx.progress_depth;
}

static void uct_rm_urma_iface_progress_pending(uct_rm_urma_iface_t *iface)
{
    ucs_status_t status = UCS_OK;

    if ((iface->tx.error != UCS_OK) || ucs_arbiter_is_empty(&iface->tx.arbiter)) {
        return;
    }
    ++iface->tx.progress_depth;
    ucs_arbiter_dispatch(&iface->tx.arbiter, 1,
                         uct_rm_urma_ep_process_pending, &status);
    if (status != UCS_OK) {
        /* An error callback may purge pending or destroy an EP. The arbiter
         * must have finished removing the failed grant before notifying it. */
        uct_rm_urma_iface_set_failed(iface, status);
    }
    --iface->tx.progress_depth;
}

ucs_status_t uct_rm_urma_iface_fc_handler(uct_urma_iface_t *ub_iface, void *arg, urma_cr_t *cr, unsigned flags)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(ub_iface, uct_rm_urma_iface_t);
    uct_ub_hdr_t *hdr = (uct_ub_hdr_t *)arg;
    uct_rm_urma_ep_t *ep = NULL;
    uint32_t ep_id = hdr->ep_id;
    ucs_status_t status;
    int16_t cur_wnd;
    unsigned length;
    uint8_t fc_hdr;

    if (iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) {
        if ((cr->completion_len < sizeof(*hdr)) ||
            (hdr->am_id != UCT_RM_URMA_EP_FC_PURE_GRANT)) {
            uct_rm_urma_iface_set_failed(iface, UCS_ERR_INVALID_PARAM);
            return UCS_ERR_INVALID_PARAM;
        }
        status = uct_rm_urma_link_receive(iface, hdr + 1,
                                          cr->completion_len - sizeof(*hdr));
        if (status != UCS_OK) {
            uct_rm_urma_iface_set_failed(iface, status);
        }
        return status;
    }
    length = cr->completion_len - sizeof(*hdr);
    fc_hdr = uct_rm_urma_fc_get_fc_hdr(hdr->am_id);

    ucs_assert(iface->super.config.fc_enabled);

    if ((iface->tx.error != UCS_OK) || (ep_id >= iface->super.eps.size)) {
        return UCS_OK;
    }
    ep = (uct_rm_urma_ep_t *)(iface->super.eps.start[ep_id]);
    if (__ucs_ptr_array_is_free(ep) || (ep == iface->tx.ctrl_ep) ||
        (ep->super.flags & (UCT_RM_URMA_EP_FLAG_CLOSING |
                            UCT_RM_URMA_EP_FLAG_FLUSH_CANCEL |
                            UCT_RM_URMA_EP_FLAG_ERR_HANDLER_INVOKED))) {
        return UCS_OK;
    }

    if (fc_hdr & UCT_RM_URMA_EP_FLAG_FC_GRANT) {
        UCS_STATS_UPDATE_COUNTER(ep->super.fc.stats, UCT_UB_FC_STAT_RX_GRANT, 1);

        /* Got either grant flag or special FC grant message */
        cur_wnd = ep->super.fc.fc_wnd;

        /* Peer granted resources, so update wnd */
        ep->super.fc.fc_wnd = iface->super.config.fc_wnd_size;
        UCS_STATS_SET_COUNTER(ep->super.fc.stats, UCT_UB_FC_STAT_FC_WND, ep->super.fc.fc_wnd);

        /* To preserve ordering we have to dispatch all pending
         * operations if current fc_wnd is <= 0
         * (otherwise it will be dispatched by tx progress) */
        if (cur_wnd <= 0) {
            ucs_arbiter_group_schedule(&iface->tx.arbiter, &ep->arb_group);
            uct_rm_urma_iface_progress_pending(iface);
            if ((iface->tx.error != UCS_OK) ||
                (ep->super.flags & (UCT_RM_URMA_EP_FLAG_CLOSING |
                                    UCT_RM_URMA_EP_FLAG_FLUSH_CANCEL |
                                    UCT_RM_URMA_EP_FLAG_ERR_HANDLER_INVOKED))) {
                return UCS_OK;
            }
        }
        if (fc_hdr == UCT_RM_URMA_EP_FC_PURE_GRANT) {
            /* Special FC grant message can't be bundled with any other FC
             * request. Stop processing this AM and do not invoke AM handler */
            UCS_STATS_UPDATE_COUNTER(ep->super.fc.stats, UCT_UB_FC_STAT_RX_PURE_GRANT, 1);
            return UCS_OK;
        }
    }

    if (fc_hdr & UCT_RM_URMA_EP_FLAG_FC_SOFT_REQ) {
        UCS_STATS_UPDATE_COUNTER(ep->super.fc.stats, UCT_UB_FC_STAT_RX_SOFT_REQ, 1);

        /* Got soft credit request. Mark ep that it needs to grant
         * credits to the peer in outgoing AM (if any). */
        ep->super.flags |= UCT_RM_URMA_EP_FLAG_FC_GRANT;

    } else if (fc_hdr & UCT_RM_URMA_EP_FLAG_FC_HARD_REQ) {
        UCS_STATS_UPDATE_COUNTER(ep->super.fc.stats, UCT_UB_FC_STAT_RX_HARD_REQ, 1);
        /* Until instance/request identity is carried on the wire, preserve
         * cancellation of this old connection's unposted replies. */
        status = uct_rm_urma_iface_send_fc_grant_owned(iface,
                                                      &ep->target->key.remote,
                                                      &ep->target->key.token,
                                                      ep->super.dep_id, ep);
        if (status != UCS_OK) {
            uct_rm_urma_iface_set_failed(iface, status);
            return UCS_OK;
        }
    }

    return uct_iface_invoke_am(&iface->super.super,
                               (hdr->am_id & ~UCT_RM_URMA_EP_FC_MASK),
                               hdr + 1, length, flags);
}

static void uct_rm_urma_iface_jfc_destroy(uct_urma_iface_t *iface, uint8_t direction)
{
    uct_ub_destroy_jfc(iface->jfc[direction]);
}

static uct_ub_iface_ops_t uct_rm_urma_iface_ops = {
    .super = {
        .iface_estimate_perf   = uct_base_iface_estimate_perf,
        .iface_vfs_refresh     = (uct_iface_vfs_refresh_func_t)ucs_empty_function,
        .ep_query              = (uct_ep_query_func_t)ucs_empty_function_return_unsupported,
        .ep_invalidate         = (uct_ep_invalidate_func_t)ucs_empty_function_return_unsupported,
        .ep_connect_to_ep_v2   = uct_rm_urma_ep_connect_to_ep_v2,
        .iface_is_reachable_v2 = uct_ub_iface_is_reachable
    },
    .create_jfc     = uct_ub_create_jfc,
    .arm_jfc        = uct_ub_iface_arm_jfc,
    .handle_failure = uct_rm_urma_handle_failure,
    .fc_handler     = uct_rm_urma_iface_fc_handler,
    .destroy_jfc    = uct_rm_urma_iface_jfc_destroy,
};

static unsigned uct_rm_urma_iface_do_progress(uct_iface_h tl_iface)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_iface, uct_rm_urma_iface_t);
    return iface->progress(iface);
}

static ucs_status_t uct_rm_urma_iface_event_arm(uct_iface_h tl_iface,
                                               unsigned events)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_iface,
                                               uct_rm_urma_iface_t);

    if (iface->tx.error != UCS_OK) {
        return iface->tx.error;
    }
    if ((iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) &&
        (iface->link.control_debt ||
         !ucs_list_is_empty(&iface->link.pending))) {
        return UCS_ERR_BUSY;
    }
    return uct_ub_iface_event_arm(tl_iface, events);
}

static uct_iface_ops_t uct_rm_urma_iface_tl_ops = {
    .ep_put_short             = uct_rm_urma_ep_put_short,
    .ep_put_bcopy             = uct_rm_urma_ep_put_bcopy,
    .ep_put_zcopy             = uct_rm_urma_ep_put_zcopy,
    .ep_get_bcopy             = uct_rm_urma_ep_get_bcopy,
    .ep_get_zcopy             = uct_rm_urma_ep_get_zcopy,
    .ep_am_short              = uct_rm_urma_ep_am_short,
    .ep_am_bcopy              = uct_rm_urma_ep_am_bcopy,
    .ep_am_zcopy              = uct_rm_urma_ep_am_zcopy,
    .ep_atomic_cswap64        = uct_rm_urma_ep_atomic_cswap64,
    .ep_atomic_cswap32        = uct_rm_urma_ep_atomic_cswap32,
    .ep_atomic32_post         = uct_rm_urma_ep_atomic32_post,
    .ep_atomic64_post         = uct_rm_urma_ep_atomic64_post,
    .ep_atomic32_fetch        = uct_rm_urma_ep_atomic32_fetch,
    .ep_atomic64_fetch        = uct_rm_urma_ep_atomic64_fetch,
    .ep_pending_add           = uct_rm_urma_ep_pending_add,
    .ep_pending_purge         = uct_rm_urma_ep_pending_purge,
    .ep_flush                 = uct_rm_urma_ep_flush,
    .ep_fence                 = uct_rm_urma_ep_fence,
    .ep_check                 = uct_rm_urma_ep_check,
    .ep_create                = UCS_CLASS_NEW_FUNC_NAME(uct_rm_urma_ep_t),
    .ep_destroy               = uct_rm_urma_ep_destroy,
    .ep_get_address           = uct_rm_urma_ep_get_address,
    .ep_connect_to_ep         = uct_rm_urma_ep_connect_to_ep,
    .iface_flush              = uct_rm_urma_iface_flush,
    .iface_fence              = uct_ub_iface_fence,
    .iface_progress_enable    = uct_rm_urma_iface_common_progress_enable,
    .iface_progress_disable   = uct_base_iface_progress_disable,
    .iface_progress           = uct_rm_urma_iface_do_progress,
    .iface_event_fd_get       = uct_ub_iface_event_fd_get,
    .iface_event_arm          = uct_rm_urma_iface_event_arm,
    .iface_close              = UCS_CLASS_DELETE_FUNC_NAME(uct_rm_urma_iface_t),
    .iface_query              = uct_rm_urma_iface_query,
    .iface_get_device_address = uct_ub_iface_get_device_address,
    .iface_get_address        = uct_rm_urma_link_get_address,
    .iface_is_reachable       = uct_ub_base_iface_is_reachable
};

static ucs_status_t uct_rm_urma_iface_check_validity(const uct_iface_params_t *params,
                                                     const uct_rm_urma_iface_config_t *config,
                                                     const uct_worker_h worker)
{
    ucs_status_t ret;

    ret = uct_ub_iface_check_validity(params, worker);
    if (ret != UCS_OK) {
        return ret;
    }

    return UCS_OK;
}

static ucs_status_t uct_rm_urma_iface_create_jetty(uct_rm_urma_iface_t *iface, uct_rm_urma_jetty_t *jetty)
{
    uct_ub_device_t *dev = uct_ub_iface_device(&iface->super);

    urma_jetty_flag_t jetty_flag = {
        .bs = {.share_jfr = 1}  /* Shared jfr */
    };

    urma_jfs_cfg_t jfs_cfg = {
        .depth                  = iface->super.config.tx_max_wr,
        .flag = {
            .bs = {
                .lock_free      = 1,
                .error_suspend  = 0,
                .outorder_comp  = 0,
                .order_type     = URMA_DEF_ORDER
            }
        },
        .trans_mode             = URMA_TM_RM,
        .priority               = UB_URMA_MAX_PRIORITY, /* Highest priority */
        .max_sge                = iface->super.config.max_send_sge,
        .max_inline_data        = iface->super.config.max_inline,
        .rnr_retry              = URMA_TYPICAL_RNR_RETRY,
        .err_timeout            = URMA_TYPICAL_ERR_TIMEOUT,
        .jfc                    = iface->super.jfc[UCT_UB_DIR_TX],
        .user_ctx               = (uint64_t)NULL
    };

    /* Using shared jfr */
    urma_jetty_cfg_t jetty_cfg = {
        .id         = 0,
        .flag       = jetty_flag,
        .jfs_cfg    = jfs_cfg,
        .shared = {
            .jfr = iface->super.rx.jfr,
            .jfc = iface->super.jfc[UCT_UB_DIR_RX],
        }
    };

    jetty->jetty = urma_create_jetty(dev->urma_ctx, &jetty_cfg);
    if (jetty->jetty == NULL) {
        ucs_error("Failed to create jetty "
                  "TX wr:%d inl:%ld  %m",
                  iface->super.config.tx_max_wr, iface->super.config.max_inline);
        return UCS_ERR_IO_ERROR;
    }
    jetty->available = iface->super.config.tx_max_wr - 1;
    return UCS_OK;
}


static ucs_status_t uct_rm_urma_create_ub_resources(uct_rm_urma_iface_t *iface, const uct_ub_device_t *dev,
                                                    const uct_ub_iface_init_attr_t *init_attr)
{
    ucs_status_t status;

    urma_jfr_cfg_t jfr_cfg = {
        .id                     = 0,
        .depth                  = iface->super.config.rx_queue_depth,
        .flag = {
            .bs = {
                .tag_matching = URMA_NO_TAG_MATCHING,
                .lock_free = 1,
                .order_type = URMA_DEF_ORDER
            }
        },
        .trans_mode             = URMA_TM_RM,
        .max_sge                = 1,
        .min_rnr_timer          = URMA_TYPICAL_MIN_RNR_TIMER,
        .jfc                    = iface->super.jfc[UCT_UB_DIR_RX],
        .token_value            = dev->token
    };

    status = uct_ub_iface_create_jfr(&iface->super, &jfr_cfg);
    if (status != UCS_OK) {
        return status;
    }

    return UCS_OK;
}

static void uct_rm_urma_deal_with_send_cr(uct_rm_urma_iface_t *iface,
                                        urma_cr_t *cr, int count)
{
    uct_rm_urma_ep_t *ep;
    unsigned completed;
    int i;

    ++iface->tx.progress_depth;
    for (i = 0; i < count; ++i) {
        if (((cr[i].status == URMA_CR_WR_SUSPEND_DONE) &&
             !cr[i].flag.bs.suspend_done) ||
            ((cr[i].status == URMA_CR_WR_FLUSH_ERR_DONE) &&
             !cr[i].flag.bs.flush_err_done)) {
            /* These synthetic CRs have no valid user_ctx. */
            uct_rm_urma_handle_failure(&iface->super, &cr[i]);
            continue;
        }
        if (uct_rm_urma_get_ep_by_cr(iface, &cr[i], &ep) == UCT_UB_EP_NOT_EXIST) {
            uct_rm_urma_iface_set_failed(iface, UCS_ERR_IO_ERROR);
            continue;
        }
        completed = uct_ub_iface_get_tx_res_count(&ep->super, &cr[i]);
        /* Every submitted WR requests a CQE and outorder_comp is disabled.
         * Reject stale/out-of-order completions instead of over-crediting. */
        if ((completed != 1) || (ep->super.status.pi == ep->super.status.ci)) {
            uct_rm_urma_iface_set_failed(iface, UCS_ERR_IO_ERROR);
            continue;
        }
        uct_rm_urma_update_tx_res(iface, ep, 1);
        if (cr[i].status != URMA_CR_SUCCESS) {
            uct_rm_urma_handle_failure(&iface->super, &cr[i]);
        }
        if ((cr[i].status != URMA_CR_SUCCESS) ||
            (ep->super.flags & (UCT_RM_URMA_EP_FLAG_CLOSING |
                                UCT_RM_URMA_EP_FLAG_FLUSH_CANCEL))) {
            uct_rm_urma_ep_txqp_purge_outstanding(iface, &ep->super.txqp,
                                                 UCS_ERR_CANCELED,
                                                 ep->super.status.ci, 0);
        } else {
            /* Credit first; callbacks may send or request EP destruction. */
            uct_ub_iface_txqp_completion_desc(&ep->super.txqp,
                                             ep->super.status.ci);
        }
        /* READ completion records return bytes during the callback above.
         * Publish them before retrying pending, including the last CQE. */
        uct_ub_iface_update_reads(&iface->super);
    }
    uct_rm_urma_iface_progress_pending(iface);
    --iface->tx.progress_depth;
}

static UCS_F_ALWAYS_INLINE unsigned uct_rm_urma_iface_poll_tx(uct_rm_urma_iface_t *iface)
{
    int cr_cnt = iface->super.config.tx_max_poll;
    urma_cr_t cr[cr_cnt];
    int count;

    count = urma_poll_jfc(iface->super.jfc[UCT_UB_DIR_TX], cr_cnt, cr);
    if (count < 0) {
        uct_rm_urma_iface_set_failed(iface, UCS_ERR_IO_ERROR);
        return 0;
    }
    if ((count == 0) && (iface->tx.error != UCS_OK)) {
        count = urma_flush_jetty(iface->rm_urma_jetty.jetty, cr_cnt, cr);
    }
    if (count <= 0) {
        return 0;
    }

    UCS_STATS_UPDATE_COUNTER(iface->super.stats, UCT_UB_IFACE_STAT_TX_COMPLETION, count);
    uct_rm_urma_deal_with_send_cr(iface, cr, count);

    return (unsigned)count;
}

static unsigned uct_rm_urma_iface_progress(void *arg)
{
    uct_rm_urma_iface_t *iface = arg;
    unsigned count;

    /* Reentrant progress must not reclaim an EP still used by an outer CQE. */
    if (iface->tx.progress_depth != 0) {
        return 0;
    }
    ++iface->tx.progress_depth;
    if ((iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) &&
        (iface->tx.error != UCS_OK)) {
        count = 0;
    } else if (iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) {
        uct_ub_iface_rx_result_t rx;

        count = uct_ub_iface_poll_rx_accounted(&iface->super,
                  UCT_RM_URMA_EP_FC_MASK,
                  iface->link.control_debt ||
                  !ucs_list_is_empty(&iface->link.pending), &rx);
        uct_rm_urma_link_account_rx(iface, rx.data, rx.control, rx.posted);
        if (rx.status != UCS_OK) {
            uct_rm_urma_iface_set_failed(iface, rx.status);
        }
    } else {
        count = uct_ub_iface_poll_rx(&iface->super, UCT_RM_URMA_EP_FC_MASK);
    }
    /* Always give a busy shared SQ a bounded TX poll, even with continuous RX. */
    count += uct_rm_urma_iface_poll_tx(iface);
    if (iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) {
        uct_rm_urma_link_progress(iface);
    }
    uct_rm_urma_iface_progress_pending(iface);
    uct_rm_urma_iface_gc(iface);
    --iface->tx.progress_depth;
    return count;
}

static UCS_F_ALWAYS_INLINE ucs_status_t uct_rm_urma_iface_fc_init(uct_rm_urma_iface_t *iface,
                                                                  const uct_rm_urma_iface_config_t *config)
{
    uct_ub_iface_fc_init(&iface->super, &config->super);

    /* Check FC parameters correctness */
    if ((config->soft_thresh <= config->super.fc.hard_thresh) ||
        (config->soft_thresh >= 1)) {
        ucs_error("The factor for soft FC threshold should be bigger"
                  " than FC_HARD_THRESH value and less than 1 (s=%f, h=%f)",
                  config->soft_thresh, config->super.fc.hard_thresh);
        return UCS_ERR_INVALID_PARAM;
    }

    if (config->super.fc.enable) {
        iface->config.fc_soft_thresh = ucs_max((int)(iface->super.config.fc_wnd_size *
                                               config->soft_thresh), 1);
    } else {
        iface->config.fc_soft_thresh = 0;
    }
    return UCS_OK;
}

static UCS_F_ALWAYS_INLINE ucs_status_t uct_rm_urma_init_iface_attr(uct_rm_urma_iface_t *iface,
                                                                    const uct_rm_urma_iface_config_t *config,
                                                                    const uct_iface_params_t *params)
{
    uct_ub_hdr_t *hdr = NULL;
    char *dev_name = NULL;
    ucs_status_t status;

    iface->config.native_write = config->native_write;
    iface->config.native_read = config->native_read;
    iface->config.native_rma = config->native_rma;
    iface->tx.pending_group = NULL;

    uct_rm_urma_link_init(iface, config);
    iface->progress = uct_rm_urma_iface_progress;
    iface->tx.error = UCS_OK;
    iface->tx.progress_depth = 0;
    iface->tx.ctrl_ep = NULL;
    iface->rm_urma_jetty.jetty = NULL;
    iface->fc_desc = NULL;
    iface->super.config.tx_jfc_moderation = 0;
    ucs_list_head_init(&iface->tx.retired_eps);
    kh_init_inplace(uct_rm_urma_targets, &iface->targets);
    ucs_arbiter_init(&iface->tx.arbiter);

    /* Flush mode */
    if (config->flush_mode == UCT_RM_URMA_FLUSH_MODE_AUTO) {
        /* Use flow control for flush on older devices */
        dev_name = &(uct_ub_iface_device(&iface->super)->dev_name[0]);
        iface->config.flush_by_fc = (strstr(dev_name, "mthca") == dev_name);
    } else {
        iface->config.flush_by_fc = (config->flush_mode == UCT_RM_URMA_FLUSH_MODE_FLOW_CONTROL);
    }

    status = uct_rm_urma_iface_fc_init(iface, config);
    if (status != UCS_OK) {
        return status;
    }

    if ((iface->super.config.max_inline < sizeof(*hdr)) || iface->config.flush_by_fc) {
        iface->fc_desc = ucs_mpool_get(&iface->super.tx.short_desc_mp);
        if (iface->fc_desc == NULL) {
            return UCS_ERR_NO_MEMORY;
        }
        hdr = (uct_ub_hdr_t*)(iface->fc_desc + 1);
        hdr->am_id = UCT_RM_URMA_EP_FC_PURE_GRANT;
    } else {
        iface->fc_desc = NULL;
    }

    return UCS_OK;
}

static ucs_mpool_ops_t uct_rm_urma_pending_mpool_ops = {
    .chunk_alloc   = ucs_mpool_chunk_malloc,
    .chunk_release = ucs_mpool_chunk_free,
    .obj_init      = NULL,
    .obj_cleanup   = NULL
};

static ucs_status_t uct_rm_urma_iface_pending_mpool_init(uct_rm_urma_iface_t *iface)
{
    ucs_mpool_params_t mp_params;
    ucs_status_t status;

    /* Create memory pool for flush completions. Can't just alloc a certain
     * size buffer, because number of simultaneous flushes is not limited by
     * CQ or QP resources. */
    ucs_mpool_params_reset(&mp_params);
    mp_params.elem_size = ucs_max(sizeof(uct_rm_urma_fc_pending_req_t),
                                  sizeof(uct_rm_urma_link_req_t));
    mp_params.elems_per_chunk = 128;
    mp_params.alignment = sizeof(uint64_t);
    mp_params.ops = &uct_rm_urma_pending_mpool_ops;
    mp_params.name = "pending-ops";
    status = ucs_mpool_init(&mp_params, &iface->tx.pending_mp);

    return status;
}

static UCS_CLASS_INIT_FUNC(uct_rm_urma_iface_t, uct_md_h tl_md, uct_worker_h worker,
                           const uct_iface_params_t *params,
                           const uct_iface_config_t *tl_config)
{
    uct_rm_urma_iface_config_t *config = ucs_derived_of(tl_config, uct_rm_urma_iface_config_t);
    uct_ub_md_t *ub_md = ucs_derived_of(tl_md, uct_ub_md_t);
    uct_ub_iface_ops_t *ops = &uct_rm_urma_iface_ops;
    uct_ub_iface_init_attr_t init_attr = {};
    uct_ub_device_t *dev = &ub_md->dev;
    ucs_status_t status = UCS_OK;

    ucs_trace_func("");

    status = uct_rm_urma_iface_check_validity(params, config, worker);
    if (status != UCS_OK) {
        goto err;
    }

    init_attr.jfc_depth[UCT_UB_DIR_TX] = ucs_min(config->tx_jfc_len, dev->dev_attr.dev_cap.max_jfc_depth);
    init_attr.jfc_depth[UCT_UB_DIR_RX] = ucs_min(config->super.rx.depth, dev->dev_attr.dev_cap.max_jfc_depth);
    init_attr.rx_hdr_len               = sizeof(uct_ub_hdr_t);
    init_attr.use_min_seg              = 0;

    UCS_CLASS_CALL_SUPER_INIT(uct_urma_iface_t, ops, &uct_rm_urma_iface_tl_ops, tl_md, worker,
                              params, tl_config, &init_attr);

    status = UCS_STATS_NODE_ALLOC(&self->stats, &uct_rm_urma_iface_stats_class, self->super.stats, "-%p", self);
    if (status != UCS_OK) {
        goto err;
    }

    status = uct_rm_urma_init_iface_attr(self, config, params);
    if (status != UCS_OK) {
        goto err_destroy_attr;
    }

    status = uct_rm_urma_create_ub_resources(self, dev, &init_attr);
    if (status != UCS_OK) {
        ucs_error("RM urma create ub resources failed");
        goto err_destroy_attr;
    }

    status = uct_rm_urma_iface_create_jetty(self, &self->rm_urma_jetty);
    if (status != UCS_OK) {
        goto err_destroy_mempool;
    }

    status = uct_rm_urma_iface_pending_mpool_init(self);
    if (status != UCS_OK) {
        ucs_warn("Init pending memory pool err:%m");
        goto err_destroy_jetty;
    }

    uct_ub_iface_init_recv_queue(&self->super, self->super.config.rx_queue_depth);
    status = uct_rm_urma_link_ready(self);
    if (status != UCS_OK) {
        ucs_mpool_cleanup(&self->tx.pending_mp, 1);
        goto err_destroy_jetty;
    }
    return UCS_OK;

err_destroy_jetty:
    if (urma_delete_jetty(self->rm_urma_jetty.jetty) != URMA_SUCCESS) {
        ucs_fatal("Failed to roll back RM shared Jetty");
    }
err_destroy_mempool:
    uct_ub_destroy_jfr(self->super.rx.jfr);
err_destroy_attr:
    kh_destroy_inplace(uct_rm_urma_peers, &self->link.peers_by_address);
    kh_destroy_inplace(uct_rm_urma_requests, &self->link.requests);
    kh_destroy_inplace(uct_rm_urma_targets, &self->targets);
    if (self->fc_desc != NULL) {
        ucs_mpool_put(self->fc_desc);
    }
    ucs_arbiter_cleanup(&self->tx.arbiter);
    UCS_STATS_NODE_FREE(self->stats);
err:
    return status;
}

static UCS_CLASS_CLEANUP_FUNC(uct_rm_urma_iface_t)
{
    uct_rm_urma_ep_t *ep;

    uct_base_iface_progress_disable(&self->super.super.super,
                                    UCT_PROGRESS_SEND | UCT_PROGRESS_RECV);
    ++self->tx.progress_depth;
    self->tx.error = UCS_ERR_CANCELED;
    /* Stop hardware before reclaiming deferred EPs or registered pools. */
    if (urma_delete_jetty(self->rm_urma_jetty.jetty) != URMA_SUCCESS) {
        ucs_fatal("Cannot safely destroy RM shared Jetty");
    }
    self->rm_urma_jetty.jetty = NULL;
    if (self->tx.ctrl_ep != NULL) {
        uct_rm_urma_ep_destroy(&self->tx.ctrl_ep->super.super.super);
    }
    ucs_list_for_each(ep, &self->tx.retired_eps, retired_list) {
        ep->super.status.ci = ep->super.status.pi;
    }
    uct_rm_urma_iface_gc(self);
    self->tx.ctrl_ep = NULL;
    uct_rm_urma_link_cleanup(self);
    uct_rm_urma_iface_targets_cleanup(self);
    --self->tx.progress_depth;
    uct_ub_destroy_jfr(self->super.rx.jfr);
    ucs_arbiter_cleanup(&self->tx.arbiter);
    ucs_mpool_cleanup(&self->tx.pending_mp, 1);
    if (self->fc_desc != NULL) {
        ucs_mpool_put(self->fc_desc);
    }
    UCS_STATS_NODE_FREE(self->stats);
}

UCS_CLASS_DEFINE(uct_rm_urma_iface_t, uct_urma_iface_t);
static UCS_CLASS_DEFINE_NEW_FUNC(uct_rm_urma_iface_t, uct_iface_t, uct_md_h,
                                 uct_worker_h, const uct_iface_params_t*,
                                 const uct_iface_config_t*);

static UCS_CLASS_DEFINE_DELETE_FUNC(uct_rm_urma_iface_t, uct_iface_t);

static ucs_status_t uct_rm_urma_query_tl_devices(uct_md_h md,
                                                 uct_tl_device_resource_t **tl_devices_p,
                                                 unsigned *num_tl_devices_p)
{
    uct_ub_md_t *ub_md = ucs_derived_of(md, uct_ub_md_t);
    int flags = 0;

    return uct_ub_device_query_ports(&ub_md->dev, flags, tl_devices_p,
                                     num_tl_devices_p);
}

UCT_TL_DEFINE(&uct_ub_component, rm_urma, uct_rm_urma_query_tl_devices, uct_rm_urma_iface_t,
              "RM_URMA_", uct_rm_urma_iface_config_table,
              uct_rm_urma_iface_config_t);
