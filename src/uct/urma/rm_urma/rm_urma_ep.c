/**
* Copyright (C) Huawei Technologies Co., Ltd. 2026. ALL RIGHTS RESERVED.
* Description: Data plane interface
* See file LICENSE for terms.
*/

#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include "rm_urma_ep.h"

#include <ucs/time/time.h>
#include <uct/urma/base/urma_md.h>
#include <uct/api/uct.h>
#include <uct/base/uct_iface.h>
#include <ucs/arch/bitops.h>
#include <ucs/arch/cpu.h>
#include <ucs/type/class.h>
#include <ucs/type/cpu_set.h>
#include <ucs/debug/log.h>
#include <uct/base/uct_iov.inl>

static void UCS_F_ALWAYS_INLINE uct_rm_urma_init_base_wr(urma_jfs_wr_t *wr, uct_rm_urma_iface_t *iface,
                                                         urma_opcode_t opcode, uct_rm_urma_ep_t *ep,
                                                         uint32_t force_complete_enable, uint32_t inline_flag,
                                                         uint32_t solicited_flag)
{
    /* R1 uses one CQE per WR. Per-EP moderation can strand a shared SQ. */
    uct_ub_init_base_wr(wr, &iface->super, opcode, &ep->super, 1, inline_flag, solicited_flag);

    wr->user_ctx = ep->super.ep_id;
    /* the upper 32bits is ep_id, and the lower 32 bit is sn */
    wr->user_ctx = (wr->user_ctx << UCT_UB_EP_ID_OFFSET) | (ep->super.status.pi + 1);
    wr->tjetty = ep->tjetty;

    ucs_trace("Init wr, ep_id:%u, pi:%u, ci:%u, opcode:%u\n",
              ep->super.ep_id, ep->super.status.pi, ep->super.status.ci, opcode);
}

static inline void uct_rm_urma_update_tx_resources_status(uct_rm_urma_ep_t *ep, uct_rm_urma_iface_t *iface,
                                                          uint32_t complete_flag)
{
    if (complete_flag == 1) {
        ep->super.status.uncompltes = 0;
    } else {
        ep->super.status.uncompltes += 1;
    }

    ep->super.status.pi++;
    ep->rm_urma_jetty->available--;
    iface->super.tx.jfc_available--;
}

bool uct_rm_urma_ep_can_send(uct_rm_urma_ep_t *ep)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(ep->super.super.super.iface,
                                                uct_rm_urma_iface_t);

    return ((iface->tx.error == UCS_OK) &&
            !(ep->super.flags & (UCT_RM_URMA_EP_FLAG_CLOSING |
                                 UCT_RM_URMA_EP_FLAG_FLUSH_CANCEL |
                                 UCT_RM_URMA_EP_FLAG_ERR_HANDLER_INVOKED)) &&
            ((iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) ?
             uct_rm_urma_link_can_send(ep) :
             uct_ub_fc_has_resources(&iface->super, &ep->super.fc)) &&
            (uct_rm_urma_outstanding_queue_available(ep) > 0));
}

ucs_status_t uct_rm_urma_ep_am_short(uct_ep_h tl_ep, uint8_t id, uint64_t hdr,
                                     const void *buffer, unsigned length)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    uct_ub_am_short_hdr_t *am = &iface->super.am_inl_hdr;
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    urma_jfs_wr_t *bad_wr = NULL;
    ucs_status_t ret = UCS_OK;
    urma_status_t status;

    UCT_UB_CHECK_AM_SHORT(id, length, uct_ub_am_short_hdr_t, iface->super.config.max_inline);
    UCT_RM_URMA_CHECK_RES_AND_FC(iface, ep, id);
    UCT_TL_EP_STAT_OP(&ep->super.super, AM, SHORT, sizeof(hdr) + length);

    am->am_hdr = hdr;
    am->ub_hdr.am_id = id;
    am->ub_hdr.ep_id = ep->super.dep_id;
    uct_ub_iface_fill_inl_sge(&iface->super, am, sizeof(*am), buffer, length);

    UCT_UB_FILL_SRC_SGL_BUF(&wr->send.src, iface->super.inl_sge, iface->super.inline_num_sge);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_SEND, ep, 0, URMA_INLINE_ENABLE, URMA_SOLICITED_ENABLE);
    uct_ub_ep_fm(&iface->super, &ep->super, wr);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        ucs_warn("In function ep_am_short, urma_post_jetty_send_wr() failed, status:0x%x.\n", status);
        ret = (status == URMA_ENOMEM || status == URMA_EAGAIN) ?
              UCS_ERR_NO_RESOURCE : UCS_ERR_IO_ERROR;
        goto out;
    }

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    UCT_RM_URMA_UPDATE_FC(iface, ep, id);

out:
    (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
    return ret;
}

ssize_t uct_rm_urma_ep_am_bcopy(uct_ep_h tl_ep, uint8_t id,
                                uct_pack_callback_t pack_cb, void *arg,
                                unsigned flags)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    uct_ub_iface_send_desc_t *desc = NULL;
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t status;
    urma_sge_t sge;
    size_t length;

    UCT_CHECK_AM_ID(id);
    UCT_RM_URMA_CHECK_RES_AND_FC(iface, ep, id);
    UCT_UB_IFACE_GET_TX_AM_BCOPY_DESC(&iface->super, &ep->super, &iface->super.tx.mp, desc,
                                      id, uct_ub_am_hdr_fill, uct_ub_hdr_t,
                                      pack_cb, arg, &length);
    UCT_TL_EP_STAT_OP(&ep->super.super, AM, BCOPY, length);

    UCT_UB_FILL_WR_SG_BUF(&wr->send.src, (uint64_t)(desc + 1),
                          length + sizeof(uct_ub_hdr_t), desc->tseg, 1, &sge);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_SEND, ep, 0, URMA_INLINE_DISABLE, URMA_SOLICITED_ENABLE);
    uct_ub_ep_fm(&iface->super, &ep->super, wr);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_mpool_put(desc);
        return (status == URMA_ENOMEM || status == URMA_EAGAIN) ?
               UCS_ERR_NO_RESOURCE : UCS_ERR_IO_ERROR;
    }

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    uct_ub_outstanding_queue_add_send_op_sn(&ep->super.txqp, &desc->super, ep->super.status.pi);
    UCT_RM_URMA_UPDATE_FC(iface, ep, id);
    (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
    return length;
}

ucs_status_t uct_rm_urma_ep_am_zcopy(uct_ep_h tl_ep, uint8_t id, const void *header,
                                     unsigned header_length, const uct_iov_t *iov,
                                     size_t iovcnt, unsigned flags,
                                     uct_completion_t *comp)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    uct_ub_iface_send_desc_t *desc = NULL;
    uint32_t force_complete_enable;
    urma_sge_t sge[UCT_UB_MAX_IOV];
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t status;
    size_t sge_cnt;

    /* 1 iov consumed by am header */
    UCT_CHECK_IOV_SIZE(iovcnt, iface->super.config.max_send_sge - 1, "uct_rm_urma_ep_am_zcopy");

    UCT_UB_CHECK_AM_ZCOPY(id, header_length, uct_iov_total_length(iov, iovcnt),
                          iface->super.config.short_desc_size, iface->super.config.seg_size);
    UCT_RM_URMA_CHECK_RES_AND_FC(iface, ep, id);

    UCT_UB_IFACE_GET_TX_AM_ZCOPY_DESC(&iface->super, &ep->super, &iface->super.tx.short_desc_mp,
                                      desc, id, header, header_length, comp,
                                      &force_complete_enable);
    UCT_TL_EP_STAT_OP(&ep->super.super, AM, ZCOPY, header_length +
                      uct_iov_total_length(iov, iovcnt));
    sge[0].len = sizeof(uct_ub_hdr_t) + header_length;
    sge[0].addr = (uint64_t)(desc + 1);
    sge[0].tseg = desc->tseg;
    sge_cnt = uct_ub_sge_fill_iov(sge + 1, iov, iovcnt);

    UCT_UB_FILL_SRC_SGL_BUF(&wr->send.src, sge, sge_cnt + 1);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_SEND, ep, force_complete_enable,
                             URMA_INLINE_DISABLE, URMA_SOLICITED_ENABLE);
    uct_ub_ep_fm(&iface->super, &ep->super, wr);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function ep am zcopy, urma_post_jetty_send_wr() failed, status:0x%x", status);
    }

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    uct_ub_outstanding_queue_add_send_op_sn(&ep->super.txqp, &desc->super, ep->super.status.pi);
    UCT_RM_URMA_UPDATE_FC(iface, ep, id);
    (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
    return UCS_INPROGRESS;
}

ucs_status_t uct_rm_urma_ep_put_0bytes(uct_rm_urma_ep_t *ep, uct_rm_urma_iface_t *iface)
{
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    urma_jfs_wr_t *bad_wr = NULL;
    uint64_t dummy_paylaod;
    urma_status_t status;
    urma_sge_t dst_sge;
    urma_sge_t src_sge;
    ucs_status_t ret;

    UCT_UB_FILL_WR_SG_BUF(&wr->rw.src, (uint64_t)&dummy_paylaod, 0, NULL, 1, &src_sge);
    UCT_UB_FILL_WR_SG_BUF(&wr->rw.dst, (uint64_t)ep->ka_va_addr, 0, ep->ka_tseg, 1, &dst_sge);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_WRITE, ep, 1, URMA_INLINE_ENABLE, URMA_SOLICITED_DISABLE);
    uct_ub_ep_fm(&iface->super, &ep->super, wr);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function ep put_0bytes, urma_post_jetty_send_wr() failed, status:0x%x", status);
    }

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    ret = UCS_OK;

    return ret;
}

ucs_status_t uct_rm_urma_ep_put_short(uct_ep_h tl_ep, const void *payload,
                                      unsigned length, uint64_t remote_addr,
                                      uct_rkey_t rkey)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    urma_target_seg_t *target_seg = NULL;
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t status;
    urma_sge_t dst_sge;
    urma_sge_t src_sge;
    ucs_status_t ret;

    UCT_CHECK_LENGTH(length, 0, iface->super.config.max_inline, "put_short");
    UCT_RM_URMA_CHECK_RES(iface, ep);
    UCT_TL_EP_STAT_OP(&ep->super.super, PUT, SHORT, length);

    if (ucs_unlikely(length == 0)) {
        ret = uct_rm_urma_ep_put_0bytes(ep, iface);
        goto out;
    }

    ret = uct_ub_ep_get_tseg(&iface->super, rkey, &target_seg);
    if (ucs_unlikely(ret != UCS_OK)) {
        ucs_error("In function ep put short, get target seg failed");
        goto out;
    }

    UCT_UB_FILL_WR_SG_BUF(&wr->rw.src, (uint64_t)payload, length, NULL, 1, &src_sge);
    UCT_UB_FILL_WR_SG_BUF(&wr->rw.dst, remote_addr, length, target_seg, 1, &dst_sge);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_WRITE, ep, 1, URMA_INLINE_ENABLE, URMA_SOLICITED_DISABLE);
    uct_ub_ep_fm(&iface->super, &ep->super, wr);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function ep put short, urma_post_jetty_send_wr() failed, status:0x%x.\n", status);
    }

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    ret = UCS_OK;

out:
    (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
    return ret;
}

ssize_t uct_rm_urma_ep_put_bcopy(uct_ep_h tl_ep, uct_pack_callback_t pack_cb,
                                 void *arg, uint64_t remote_addr, uct_rkey_t rkey)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    uct_ub_iface_send_desc_t *desc = NULL;
    urma_target_seg_t *target_seg = NULL;
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t status;
    urma_sge_t dst_sge;
    urma_sge_t src_sge;
    ucs_status_t ret;
    size_t length;

    UCT_RM_URMA_CHECK_RES(iface, ep);
    UCT_UB_IFACE_GET_TX_PUT_BCOPY_DESC(&iface->super, &iface->super.tx.mp,
                                       desc, pack_cb, arg, length);
    UCT_TL_EP_STAT_OP(&ep->super.super, PUT, BCOPY, length);

    ret = uct_ub_ep_get_tseg(&iface->super, rkey, &target_seg);
    if (ucs_unlikely(ret != UCS_OK)) {
        ucs_error("In function ep put bcopy, get target seg failed");
        goto err_free_desc;
    }

    UCT_UB_FILL_WR_SG_BUF(&(wr->rw.src), (uint64_t)(desc + 1), length, desc->tseg, 1, &src_sge);
    UCT_UB_FILL_WR_SG_BUF(&(wr->rw.dst), remote_addr, length, target_seg, 1, &dst_sge);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_WRITE, ep, 1, URMA_INLINE_DISABLE, URMA_SOLICITED_DISABLE);
    uct_ub_ep_fm(&iface->super, &ep->super, wr);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function ep put bcopy, urma_post_jetty_send_wr() failed, status:0x%x.\n", status);
    }

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    uct_ub_outstanding_queue_add_send_op_sn(&ep->super.txqp, &desc->super, ep->super.status.pi);

    (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
    return length;

err_free_desc:
    _UCT_RELEASE_DESC(_, desc);
    return ret;
}

ucs_status_t uct_rm_urma_ep_put_zcopy(uct_ep_h tl_ep, const uct_iov_t *iov, size_t iovcnt,
                                      uint64_t remote_addr, uct_rkey_t rkey,
                                      uct_completion_t *comp)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    size_t total_length = uct_iov_total_length(iov, iovcnt);
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_sge_t src_sge_buf[UCT_UB_MAX_IOV] = {0};
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    urma_target_seg_t *target_seg = NULL;
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t status;
    urma_sge_t dst_sge;
    ucs_status_t ret;
    size_t sge_cnt;

    ucs_trace_func("");

    UCT_CHECK_IOV_SIZE(iovcnt, iface->super.config.max_send_sge, "uct_rm_urma_ep_put_zcopy");
    UCT_CHECK_LENGTH(total_length, 0, UCT_UB_MAX_MESSAGE_SIZE, "put_zcopy");
    UCT_RM_URMA_CHECK_RES(iface, ep);
    UCT_SKIP_ZERO_LENGTH(total_length);
    UCT_TL_EP_STAT_OP(&ep->super.super, PUT, ZCOPY, total_length);

    ret = uct_ub_ep_get_tseg(&iface->super, rkey, &target_seg);
    if (ucs_unlikely(ret != UCS_OK)) {
        ucs_error("In function ep put zcopy, get target tseg failed");
        goto out;
    }

    sge_cnt = uct_ub_sge_fill_iov(src_sge_buf, iov, iovcnt);

    UCT_UB_FILL_SRC_SGL_BUF(&wr->rw.src, src_sge_buf, sge_cnt);
    UCT_UB_FILL_WR_SG_BUF(&wr->rw.dst, remote_addr, total_length, target_seg, 1, &dst_sge);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_WRITE, ep, 1, URMA_INLINE_DISABLE, URMA_SOLICITED_DISABLE);
    uct_ub_ep_fm(&iface->super, &ep->super, wr);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        /* if status is URMA_ENOMEM, This error occurs in the following two scenarios:
            1.the status of qp is incorrect(only for 1822 card)
            2. the sq/cq queue overflows(1822 card and Mellanox card ) */
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function ep put zcopy, urma_post_jetty_send_wr() failed, status:0x%x.\n", status);
    }

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    uct_ub_tx_outstanding_queue_add_send_comp(&iface->super, &ep->super.txqp, uct_ub_send_op_completion_handler,
                                              comp, ep->super.status.pi,
                                              0 | UCT_UB_IFACE_SEND_OP_FLAG_ZCOPY, total_length);
    ret = UCS_INPROGRESS;

out:
    return ret;
}

ucs_status_t uct_rm_urma_ep_get_zero_bytes(uct_rm_urma_ep_t *ep, uct_rm_urma_iface_t *iface,
                                       uct_ub_iface_send_desc_t *desc)
{
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t status;
    urma_sge_t dst_sge;
    urma_sge_t src_sge;
    ucs_status_t ret;

    UCT_UB_FILL_WR_SG_BUF(&wr->rw.dst, (uint64_t)(desc + 1), 0, desc->tseg, 1, &src_sge);
    UCT_UB_FILL_WR_SG_BUF(&wr->rw.src, (uint64_t)ep->ka_va_addr, 0, ep->ka_tseg, 1, &dst_sge);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_READ, ep, 1, URMA_INLINE_DISABLE, URMA_SOLICITED_DISABLE);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function ep put_0bytes, urma_post_jetty_send_wr() failed, status:0x%x.\n", status);
    }

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    uct_ub_outstanding_queue_add_send_op_sn(&ep->super.txqp, &desc->super, ep->super.status.pi);
    ret = UCS_INPROGRESS;

    return ret;
}

ucs_status_t uct_rm_urma_ep_get_bcopy(uct_ep_h tl_ep,
                                      uct_unpack_callback_t unpack_cb,
                                      void *arg, size_t length,
                                      uint64_t remote_addr, uct_rkey_t rkey,
                                      uct_completion_t *comp)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    uct_ub_iface_send_desc_t *desc = NULL;
    urma_target_seg_t *target_seg = NULL;
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t status;
    urma_sge_t dst_sge;
    urma_sge_t src_sge;
    ucs_status_t ret;

    UCT_CHECK_LENGTH(length, 0, iface->super.config.seg_size, "get_bcopy");
    UCT_RM_URMA_CHECK_RES(iface, ep);
    UCT_UB_IFACE_GET_TX_GET_BCOPY_DESC(iface, &iface->super.tx.mp,
                                       desc, unpack_cb, comp, arg, length);
    UCT_TL_EP_STAT_OP(&ep->super.super, GET, BCOPY, length);

    if (ucs_unlikely(length == 0)) {
        ret = uct_rm_urma_ep_get_zero_bytes(ep, iface, desc);
        if (ret != UCS_INPROGRESS) {
            goto err_free_desc;
        }
        return ret;
    }

    ret = uct_ub_ep_get_tseg(&iface->super, rkey, &target_seg);
    if (ucs_unlikely(ret != UCS_OK)) {
        ucs_error("In function ep get bcopy, get target seg failed");
        goto err_free_desc;
    }

    UCT_UB_FILL_WR_SG_BUF(&wr->rw.dst, (uint64_t)(desc + 1), length, desc->tseg, 1, &src_sge);
    UCT_UB_FILL_WR_SG_BUF(&wr->rw.src, remote_addr, length, target_seg, 1, &dst_sge);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_READ, ep, 1, URMA_INLINE_DISABLE, URMA_SOLICITED_DISABLE);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function ep get bcopy, urma_post_jetty_send_wr() failed, status:0x%x.\n", status);
    }

    UCT_UB_READ_POSTED(iface, length);
    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    uct_ub_outstanding_queue_add_send_op_sn(&ep->super.txqp, &desc->super, ep->super.status.pi);

    (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
    return UCS_INPROGRESS;

err_free_desc:
    _UCT_RELEASE_DESC(_, desc);
    return ret;
}

ucs_status_t uct_rm_urma_ep_get_zcopy(uct_ep_h tl_ep, const uct_iov_t *iov,
                                      size_t iovcnt, uint64_t remote_addr,
                                      uct_rkey_t rkey, uct_completion_t *comp)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    size_t total_length = uct_iov_total_length(iov, iovcnt);
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_sge_t src_sge_buf[UCT_UB_MAX_IOV] = {0};
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    urma_target_seg_t *target_seg = NULL;
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t status;
    urma_sge_t dst_sge;
    ucs_status_t ret;
    size_t sge_cnt;

    UCT_CHECK_IOV_SIZE(iovcnt, iface->super.config.max_send_sge, "uct_rm_urma_ep_get_zcopy");
    UCT_CHECK_LENGTH(total_length, 0, iface->super.config.max_get_zcopy, "get_zcopy");
    UCT_SKIP_ZERO_LENGTH(total_length);
    UCT_RM_URMA_CHECK_RES(iface, ep);

    ret = uct_ub_ep_get_tseg(&iface->super, rkey, &target_seg);
    if (ucs_unlikely(ret != UCS_OK)) {
        ucs_error("In function ep_get_zcopy, get target seg failed");
        goto out;
    }

    sge_cnt = uct_ub_sge_fill_iov(src_sge_buf, iov, iovcnt);

    UCT_UB_FILL_SRC_SGL_BUF(&wr->rw.dst, src_sge_buf, sge_cnt);
    UCT_UB_FILL_WR_SG_BUF(&wr->rw.src, remote_addr, total_length, target_seg, 1, &dst_sge);
    UCT_TL_EP_STAT_OP(&ep->super.super, GET, ZCOPY, total_length);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_READ, ep, 1, URMA_INLINE_DISABLE, URMA_SOLICITED_DISABLE);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function ep_get_zcopy, urma_post_jetty_send_wr() failed, status:0x%x.\n", status);
    }

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    uct_ub_tx_outstanding_queue_add_send_comp(&iface->super, &ep->super.txqp, uct_ub_ep_get_zcopy_completion_handler,
                                              comp, ep->super.status.pi,
                                              0 | UCT_UB_IFACE_SEND_OP_FLAG_IOV, total_length);
    UCT_UB_READ_POSTED(iface, total_length);
    ret = UCS_INPROGRESS;

out:
    return ret;
}

static UCS_F_ALWAYS_INLINE ucs_status_t
uct_rm_urma_ep_atomic_cswap(uct_rm_urma_ep_t *ep, unsigned opcode, void *result,
                            unsigned length, uint64_t remote_addr, uct_rkey_t rkey,
                            uint64_t compare_mask, uint64_t compare,
                            uint64_t swap_mask, uint64_t swap_add, uct_completion_t *comp)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(ep->super.super.super.iface, uct_rm_urma_iface_t);
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    uct_ub_iface_send_desc_t *desc = NULL;
    urma_target_seg_t *target_seg = NULL;
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t status;
    ucs_status_t ret;
    urma_sge_t dst;
    urma_sge_t src;

    UCT_RM_URMA_CHECK_RES(iface, ep);

    ret = uct_ub_ep_get_tseg(&iface->super, rkey, &target_seg);
    if (ucs_unlikely(ret != UCS_OK)) {
        ucs_error("In function ep_get_zcopy, get target seg failed");
        return ret;
    }

    if (comp == NULL) {
        UCT_UB_IFACE_GET_TX_ATOMIC_DESC(&iface->super, &iface->super.tx.short_desc_mp, desc);
    } else {
        UCT_UB_IFACE_GET_TX_ATOMIC_FETCH_DESC(&iface->super, &iface->super.tx.short_desc_mp,
                                              desc, uct_ub_iface_atomic_handler(&iface->super, length),
                                              result, comp);
    }

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_CAS, ep, 1, URMA_INLINE_DISABLE, URMA_SOLICITED_DISABLE);
    uct_ub_ep_fm(&iface->super, &ep->super, wr);

    UCT_UB_FILL_ATOMIC_WR(wr, (uint64_t)(desc + 1), remote_addr, compare, swap_add, length,
                          target_seg, desc->tseg, &dst, &src);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function uct_rm_urma_ep_atomic_cswap, urma_post_jetty_send_wr() failed, status:0x%x", status);
    }

    UCT_TL_EP_STAT_ATOMIC(&ep->super.super);

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    uct_ub_outstanding_queue_add_send_op_sn(&ep->super.txqp, &desc->super, ep->super.status.pi);
    (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
    return UCS_INPROGRESS;

}

ucs_status_t uct_rm_urma_ep_atomic_cswap64(uct_ep_h tl_ep, uint64_t compare, uint64_t swap,
                                           uint64_t remote_addr, uct_rkey_t rkey,
                                           uint64_t *result, uct_completion_t *comp)
{
    return uct_rm_urma_ep_atomic_cswap(ucs_derived_of(tl_ep, uct_rm_urma_ep_t),
                                       UCT_ATOMIC_OP_CSWAP, result,
                                       sizeof(uint64_t), remote_addr, rkey, 0,
                                       compare, UINT64_MAX, swap, comp);
}

ucs_status_t uct_rm_urma_ep_atomic_cswap32(uct_ep_h tl_ep, uint32_t compare, uint32_t swap,
                                           uint64_t remote_addr, uct_rkey_t rkey,
                                           uint32_t *result, uct_completion_t *comp)
{
    return uct_rm_urma_ep_atomic_cswap(ucs_derived_of(tl_ep, uct_rm_urma_ep_t),
                                       UCT_ATOMIC_OP_CSWAP, result,
                                       sizeof(uint32_t), remote_addr, rkey, UCS_MASK(32),
                                       compare, UINT64_MAX, swap, comp);
}

static ucs_status_t UCS_F_ALWAYS_INLINE
uct_rm_urma_ep_atomic_opcode_post(uct_ep_h tl_ep, unsigned opcode, unsigned size,
                                  uint64_t value, uint64_t remote_addr, uct_rkey_t rkey)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    uct_ub_iface_send_desc_t *desc = NULL;
    urma_target_seg_t *target_seg = NULL;
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t status;
    urma_sge_t src_buff;
    urma_sge_t dst_buff;
    ucs_status_t ret;

    UCT_RM_URMA_CHECK_RES(iface, ep);
    UCT_UB_CHECK_ATOMIC_OPS(opcode, size, UCT_UB_ATOMIC_OPS);

    ret = uct_ub_ep_get_tseg(&iface->super, rkey, &target_seg);
    if (ucs_unlikely(ret != UCS_OK)) {
        ucs_error("In function ep_atomic_opcode_post, get target seg failed");
        return ret;
    }

    UCT_UB_IFACE_GET_TX_ATOMIC_DESC(&iface->super, &iface->super.tx.short_desc_mp, desc);

    UCT_UB_FILL_FAA(wr, remote_addr, value, size,
                    (uint64_t)(desc + 1), desc->tseg, target_seg, &dst_buff, &src_buff);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_FADD, ep, 1, URMA_INLINE_DISABLE, URMA_SOLICITED_DISABLE);
    uct_ub_ep_fm(&iface->super, &ep->super, wr);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function ep_atomic_opcode_post, urma_post_jetty_send_wr() failed, status:0x%x.\n", status);
    }

    UCT_TL_EP_STAT_ATOMIC(&ep->super.super);

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    uct_ub_outstanding_queue_add_send_op_sn(&ep->super.txqp, &desc->super, ep->super.status.pi);
    (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
    return UCS_OK;
}

ucs_status_t uct_rm_urma_ep_atomic64_post(uct_ep_h ep, unsigned opcode, uint64_t value,
                                          uint64_t remote_addr, uct_rkey_t rkey)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(ep->iface, uct_rm_urma_iface_t);
    uct_ub_md_t *md = ucs_derived_of(iface->super.super.md, uct_ub_md_t);
    if (md->dev.urma_ctx->dev->type == URMA_TRANSPORT_UB && opcode != UCT_ATOMIC_OP_ADD) {
        return UCS_ERR_UNSUPPORTED;
    }

    if (opcode == UCT_ATOMIC_OP_SWAP) {
        return uct_rm_urma_ep_atomic_cswap(ucs_derived_of(ep, uct_rm_urma_ep_t),
                                           UCT_ATOMIC_OP_SWAP, NULL,
                                           sizeof(uint64_t), remote_addr, rkey, 0,
                                           0, UINT64_MAX, value, NULL);
    }
    return uct_rm_urma_ep_atomic_opcode_post(ep, opcode, sizeof(value), value, remote_addr, rkey);
}

ucs_status_t uct_rm_urma_ep_atomic32_post(uct_ep_h ep, unsigned opcode, uint32_t value,
                                          uint64_t remote_addr, uct_rkey_t rkey)
{
    if (opcode == UCT_ATOMIC_OP_SWAP) {
        return uct_rm_urma_ep_atomic_cswap(ucs_derived_of(ep, uct_rm_urma_ep_t),
                                           UCT_ATOMIC_OP_SWAP, NULL,
                                           sizeof(uint32_t), remote_addr, rkey, 0,
                                           0, UINT64_MAX, value, NULL);
    }

    return uct_rm_urma_ep_atomic_opcode_post(ep, opcode, sizeof(value), value, remote_addr, rkey);
}

static ucs_status_t UCS_F_ALWAYS_INLINE
uct_rm_urma_ep_atomic_opcode_fetch(uct_ep_h tl_ep, unsigned opcode, unsigned size,
                                   uint64_t value, void *result,
                                   uint64_t remote_addr, uct_rkey_t rkey,
                                   uct_completion_t *comp)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    uct_ub_iface_send_desc_t *desc = NULL;
    urma_target_seg_t *target_seg = NULL;
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t status;
    urma_sge_t dst_buff;
    urma_sge_t src_buff;
    ucs_status_t ret;

    UCT_RM_URMA_CHECK_RES(iface, ep);
    UCT_UB_CHECK_ATOMIC_OPS(opcode, size, UCT_UB_ATOMIC_OPS);

    ret = uct_ub_ep_get_tseg(&iface->super, rkey, &target_seg);
    if (ucs_unlikely(ret != UCS_OK)) {
        ucs_error("In function ep_atomic_opcode_fetch, get target seg failed");
        return ret;
    }

    UCT_UB_IFACE_GET_TX_ATOMIC_FETCH_DESC(&iface->super, &iface->super.tx.short_desc_mp,
                                          desc, uct_ub_iface_atomic_handler(&iface->super, size),
                                          result, comp);

    UCT_UB_FILL_FAA(wr, remote_addr, value, size,
                    (uint64_t)(desc + 1), desc->tseg, target_seg, &dst_buff, &src_buff);

    ucs_trace("Atomic fetch, value:%lu, token_id:%u, src_key_id:%u, size:%u\n",
              value,
              desc->tseg->seg.token_id, target_seg->seg.token_id, size);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_FADD, ep, 1, URMA_INLINE_DISABLE, URMA_SOLICITED_DISABLE);
    uct_ub_ep_fm(&iface->super, &ep->super, wr);

    status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (ucs_unlikely(status != URMA_SUCCESS)) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function ep atomic opcode fetch, urma_post_jetty_send_wr() failed, status:0x%x.\n", status);
    }

    UCT_TL_EP_STAT_ATOMIC(&ep->super.super);

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    uct_ub_outstanding_queue_add_send_op_sn(&ep->super.txqp, &desc->super, ep->super.status.pi);
    (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
    return UCS_INPROGRESS;

}

ucs_status_t uct_rm_urma_ep_atomic64_fetch(uct_ep_h ep, uct_atomic_op_t opcode,
                                           uint64_t value, uint64_t *result,
                                           uint64_t remote_addr, uct_rkey_t rkey,
                                           uct_completion_t *comp)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(ep->iface, uct_rm_urma_iface_t);
    uct_ub_md_t *md = ucs_derived_of(iface->super.super.md, uct_ub_md_t);
    if (md->dev.urma_ctx->dev->type == URMA_TRANSPORT_UB && opcode != UCT_ATOMIC_OP_ADD) {
        return UCS_ERR_UNSUPPORTED;
    }

    if (opcode == UCT_ATOMIC_OP_SWAP) {
        return uct_rm_urma_ep_atomic_cswap(ucs_derived_of(ep, uct_rm_urma_ep_t),
                                           UCT_ATOMIC_OP_SWAP, result,
                                           sizeof(uint64_t), remote_addr, rkey, 0,
                                           0, UINT64_MAX, value, comp);
    }

    return uct_rm_urma_ep_atomic_opcode_fetch(ep, opcode, sizeof(value), value, result,
                                              remote_addr, rkey, comp);
}

ucs_status_t uct_rm_urma_ep_atomic32_fetch(uct_ep_h ep, uct_atomic_op_t opcode,
                                           uint32_t value, uint32_t *result,
                                           uint64_t remote_addr, uct_rkey_t rkey,
                                           uct_completion_t *comp)
{

    if (opcode == UCT_ATOMIC_OP_SWAP) {
        return uct_rm_urma_ep_atomic_cswap(ucs_derived_of(ep, uct_rm_urma_ep_t),
                                           UCT_ATOMIC_OP_SWAP, result,
                                           sizeof(uint32_t), remote_addr, rkey, 0,
                                           0, UINT64_MAX, value, comp);
    }

    return uct_rm_urma_ep_atomic_opcode_fetch(ep, opcode, sizeof(value), value, result,
                                              remote_addr, rkey, comp);
}

ucs_status_t uct_rm_urma_ep_pending_add(uct_ep_h tl_ep, uct_pending_req_t *r, unsigned flags)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);

    /* ep can tx if
     * - iface has resources: cqe and tx skb
     * - jfs has resources
     */
    if (uct_ub_iface_has_tx_resources(&iface->super) &&
        uct_rm_urma_ep_can_send(ep) && iface->super.tx.jfc_available > 0) {
        return UCS_ERR_BUSY;
    }

    UCS_STATIC_ASSERT(sizeof(uct_pending_req_priv_arb_t) <=
                      UCT_PENDING_REQ_PRIV_LEN);
    uct_pending_req_arb_group_push(&ep->arb_group, r);
    UCT_TL_EP_STAT_PEND(&ep->super.super);

    if ((iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) ?
        ((ep->peer != NULL) && (ep->peer->credits > 0)) :
        uct_ub_fc_has_resources(&iface->super, &ep->super.fc)) {
        /* Shared-SQ waiters stay scheduled, even if this EP has no CQE. */
        ucs_arbiter_group_schedule(&iface->tx.arbiter, &ep->arb_group);
    }
    return UCS_OK;
}

static void uct_rm_urma_fc_req_release(uct_rm_urma_fc_pending_req_t *req)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(req->super.ep->iface,
                                              uct_rm_urma_iface_t);

    if (req->desc != NULL) {
        ucs_mpool_put(req->desc);
    }
    uct_rm_urma_iface_target_put(iface, req->target);
    ucs_mpool_put(req);
}

static void uct_rm_urma_fc_send_completion(uct_urma_iface_send_op_t *op,
                                          const void *resp)
{
    uct_rm_urma_fc_pending_req_t *req =
        ucs_container_of(op, uct_rm_urma_fc_pending_req_t, send_op);

    uct_rm_urma_fc_req_release(req);
}

static ucs_status_t
uct_rm_urma_ep_fc_ctrl(uct_rm_urma_fc_pending_req_t *req)
{
    uct_ep_t *tl_ep = req->super.ep;
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    urma_jfs_wr_t *wr = &iface->super.send_wr;
    urma_jfs_wr_t *bad_wr = NULL;
    uct_ub_iface_send_desc_t *desc = NULL;
    uct_ub_hdr_t *hdr;
    urma_sge_t sge;
    urma_status_t status;

    if (iface->tx.error != UCS_OK) {
        return iface->tx.error;
    }
    if (ep->super.flags & (UCT_RM_URMA_EP_FLAG_CLOSING |
                           UCT_RM_URMA_EP_FLAG_FLUSH_CANCEL |
                           UCT_RM_URMA_EP_FLAG_ERR_HANDLER_INVOKED)) {
        return UCS_ERR_CANCELED;
    }
    /* A credit grant must not itself consume the peer's FC window. */
    UCT_RM_URMA_CHECK_JFC_RET(iface, ep, UCS_ERR_NO_RESOURCE);
    UCT_RM_URMA_CHECK_TX_QUEUE_RET(ep, UCS_ERR_NO_RESOURCE);
    ucs_assert(ep == iface->tx.ctrl_ep);

    if (iface->super.config.max_inline >= sizeof(*hdr)) {
        hdr = &iface->super.am_inl_hdr.ub_hdr;
        hdr->am_id = UCT_RM_URMA_EP_FC_PURE_GRANT;
        hdr->ep_id = req->remote_ep_id;
        UCT_UB_FILL_WR_SG_BUF(&wr->send.src, (uint64_t)hdr, sizeof(*hdr),
                              NULL, 1, &sge);
        uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_SEND, ep, 1,
                                 URMA_INLINE_ENABLE, URMA_SOLICITED_ENABLE);
    } else {
        /* Each in-flight non-inline control message needs its own header. */
        desc = ucs_mpool_get(&iface->super.tx.short_desc_mp);
        if (desc == NULL) {
            return UCS_ERR_NO_RESOURCE;
        }
        hdr = (uct_ub_hdr_t *)(desc + 1);
        hdr->am_id = UCT_RM_URMA_EP_FC_PURE_GRANT;
        hdr->ep_id = req->remote_ep_id;
        UCT_UB_FILL_WR_SG_BUF(&wr->send.src, (uint64_t)hdr, sizeof(*hdr),
                              desc->tseg, 1, &sge);
        uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_SEND, ep, 1,
                                 URMA_INLINE_DISABLE, URMA_SOLICITED_ENABLE);
    }
    wr->tjetty = req->target->jetty;
    status = urma_post_jetty_send_wr(ep->rm_urma_jetty->jetty, wr, &bad_wr);
    memset(wr, 0, sizeof(*wr));
    if (status != URMA_SUCCESS) {
        if (desc != NULL) {
            ucs_mpool_put(desc);
        }
        return (status == URMA_ENOMEM || status == URMA_EAGAIN) ?
               UCS_ERR_NO_RESOURCE : UCS_ERR_IO_ERROR;
    }
    uct_rm_urma_update_tx_resources_status(ep, iface, URMA_COMPLETE_ENABLE);
    req->desc               = desc;
    req->legacy_ep          = NULL; /* the posted WR owns only its target */
    req->send_op.flags      = 0;
    req->send_op.user_comp  = NULL;
    req->send_op.handler    = uct_rm_urma_fc_send_completion;
    uct_ub_outstanding_queue_add_send_op_sn(&ep->super.txqp, &req->send_op,
                                          ep->super.status.pi);
    return UCS_OK;
}

ucs_status_t uct_rm_urma_ep_fc_grant(uct_pending_req_t *self)
{
    uct_rm_urma_pending_req_t *pending =
        ucs_derived_of(self, uct_rm_urma_pending_req_t);
    uct_rm_urma_fc_pending_req_t *freq =
        ucs_derived_of(pending, uct_rm_urma_fc_pending_req_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(pending->ep, uct_rm_urma_ep_t);
    uct_rm_urma_iface_t *iface = ucs_derived_of(ep->super.super.super.iface,
                                                uct_rm_urma_iface_t);
    ucs_status_t status;

    ucs_assert_always(iface->super.config.fc_enabled);
    status = uct_rm_urma_ep_fc_ctrl(freq);
    if (status == UCS_OK) {
        UCS_STATS_UPDATE_COUNTER(ep->super.fc.stats, UCT_UB_FC_STAT_TX_PURE_GRANT, 1);
    }
    /* A posted request owns its target until CQE, including inline sends.
     * Only an unposted terminal error can release it immediately. */
    if ((status != UCS_OK) && (status != UCS_ERR_NO_RESOURCE)) {
        uct_rm_urma_fc_req_release(freq);
    }
    return (status == UCS_ERR_CANCELED) ? UCS_OK : status;
}

ucs_status_t uct_rm_urma_ep_post_flush_size_zero_bytes(uct_rm_urma_iface_t *iface, uct_rm_urma_ep_t *ep, const void *payload,
                                                 unsigned length, uint64_t remote_addr)
{
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t urma_status;
    urma_sge_t dst_sge;
    urma_sge_t src_sge;
    ucs_status_t ret;

    UCT_UB_FILL_WR_SG_BUF(&wr->rw.src, (uint64_t)payload, length, NULL, 1, &src_sge);
    UCT_UB_FILL_WR_SG_BUF(&wr->rw.dst, remote_addr, length, ep->ka_tseg, 1, &dst_sge);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_WRITE, ep, 1, URMA_INLINE_ENABLE, URMA_SOLICITED_DISABLE);
    wr->next = NULL;

    urma_status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (urma_status != URMA_SUCCESS) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function uct_rm_urma_ep_post_flush_size_zero_bytes, urma_post_jetty_send_wr() failed code(0x%x)", urma_status);
    }

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    ret = UCS_OK;

    return ret;
}

ucs_status_t uct_rm_urma_ep_post_flush_by_fc(uct_rm_urma_iface_t *iface, uct_rm_urma_ep_t *ep)
{
    urma_jetty_t *jetty = ep->rm_urma_jetty->jetty;
    urma_jfs_wr_t *wr = &(iface->super.send_wr);
    urma_jfs_wr_t *bad_wr = NULL;
    urma_status_t urma_status;
    uct_ub_hdr_t *hdr = NULL;
    ucs_status_t ret;
    urma_sge_t sge;

    hdr = (uct_ub_hdr_t*)(iface->fc_desc + 1);
    hdr->ep_id = ep->super.dep_id;
    UCT_UB_FILL_WR_SG_BUF(&wr->send.src, (uint64_t)(iface->fc_desc + 1),
                          sizeof(uct_ub_hdr_t), iface->fc_desc->tseg, 1, &sge);

    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_SEND, ep, 1, URMA_INLINE_ENABLE, URMA_SOLICITED_DISABLE);
    wr->next = NULL;

    urma_status = urma_post_jetty_send_wr(jetty, wr, &bad_wr);
    if (urma_status != URMA_SUCCESS) {
        (void)memset(wr, 0, sizeof(urma_jfs_wr_t));
        ucs_fatal("In function uct_rm_urma_ep_post_flush_by_fc, urma_post_jetty_send_wr() failed code(0x%x)", urma_status);
    }

    uct_rm_urma_update_tx_resources_status(ep, iface, wr->flag.bs.complete_enable);
    ret = UCS_OK;

    return ret;
}

static void uct_rm_urma_ep_post_flush(uct_rm_urma_ep_t *ep, uct_rm_urma_iface_t *iface)
{
    uint64_t dummy = 0;

    if (iface->config.flush_by_fc || (iface->super.config.max_inline == 0)) {
        uct_rm_urma_ep_post_flush_by_fc(iface, ep);
    } else {
        uct_rm_urma_ep_post_flush_size_zero_bytes(iface, ep, &dummy, 0, ep->ka_va_addr);
    }
}

static ucs_status_t uct_rm_urma_ep_check_internal(uct_ep_h tl_ep)
{
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);

    /* in case if no TX resources are available then there is at least
     * one signaled operation which provides actual peer status, in this case
     * just return without any actions */
    UCT_RM_URMA_CHECK_TX_QUEUE_RET(ep, UCS_OK);

    /* in case of not iface resources available then return NO_RESOURCE
     * to add request to pending queue */
    UCT_RM_URMA_CHECK_JFC_RET(iface, ep, UCS_ERR_NO_RESOURCE);

    uct_rm_urma_ep_post_flush(ep, iface);

    return UCS_OK;
}

static ucs_status_t uct_rm_urma_ep_check_progress(uct_pending_req_t *self)
{
    uct_rm_urma_pending_req_t *req = ucs_derived_of(self, uct_rm_urma_pending_req_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(req->ep, uct_rm_urma_ep_t);
    ucs_status_t status;

    ucs_assert(ep->super.flags & UCT_RM_URMA_EP_FLAG_KEEPALIVE_PENDING);

    status = uct_rm_urma_ep_check_internal(req->ep);
    if (status == UCS_OK) {
        ep->super.flags &= ~UCT_RM_URMA_EP_FLAG_KEEPALIVE_PENDING;
        ucs_mpool_put(req);
    } else {
        ucs_assert(status == UCS_ERR_NO_RESOURCE);
    }

    return status;
}

ucs_arbiter_cb_result_t uct_rm_urma_ep_arbiter_purge_cb(ucs_arbiter_t *arbiter, ucs_arbiter_group_t *group,
                                                        ucs_arbiter_elem_t *elem, void *arg)
{
    uct_rm_urma_ep_t UCS_V_UNUSED *ep = ucs_container_of(group, uct_rm_urma_ep_t, arb_group);
    uct_pending_req_t *req = ucs_container_of(elem, uct_pending_req_t, priv);
    uct_purge_cb_args_t *cb_args = arg;
    uct_pending_purge_callback_t cb = cb_args->cb;
    uct_rm_urma_pending_req_t *pending;
    uct_rm_urma_fc_pending_req_t *freq;

    if (req->func == uct_rm_urma_ep_check_progress) {
        ep->super.flags &= ~UCT_RM_URMA_EP_FLAG_KEEPALIVE_PENDING;
        ucs_mpool_put(req);
    } else if (ucs_likely(req->func != uct_rm_urma_ep_fc_grant)) {
        /* Invoke user's callback only if it is not internal FC message */
        if (cb != NULL) {
            cb(req, cb_args->arg);
        } else {
            ucs_debug("Ep cancelling user pending request");
        }
    } else {
        pending = ucs_derived_of(req, uct_rm_urma_pending_req_t);
        freq = ucs_derived_of(pending, uct_rm_urma_fc_pending_req_t);
        uct_rm_urma_fc_req_release(freq);
    }
    return UCS_ARBITER_CB_RESULT_REMOVE_ELEM;
}

static ucs_arbiter_cb_result_t
uct_rm_urma_ep_purge_legacy_grant_cb(ucs_arbiter_t *arbiter,
                                    ucs_arbiter_group_t *group,
                                    ucs_arbiter_elem_t *elem, void *arg)
{
    uct_pending_req_t *pending = ucs_container_of(elem, uct_pending_req_t, priv);
    uct_rm_urma_pending_req_t *base =
        ucs_derived_of(pending, uct_rm_urma_pending_req_t);
    uct_rm_urma_fc_pending_req_t *req =
        ucs_derived_of(base, uct_rm_urma_fc_pending_req_t);

    ucs_assert(pending->func == uct_rm_urma_ep_fc_grant);
    if (req->legacy_ep != arg) {
        return UCS_ARBITER_CB_RESULT_NEXT_GROUP;
    }
    uct_rm_urma_fc_req_release(req);
    return UCS_ARBITER_CB_RESULT_REMOVE_ELEM;
}

static void uct_rm_urma_ep_purge_legacy_grants(uct_rm_urma_iface_t *iface,
                                             uct_rm_urma_ep_t *ep)
{
    if ((iface->tx.ctrl_ep != NULL) && (ep != iface->tx.ctrl_ep)) {
        /* No pointer to ep may remain in pending after its ID is recycled.
         * Other destinations' replies stay queued on the control owner. */
        ucs_arbiter_group_purge(&iface->tx.arbiter,
                                &iface->tx.ctrl_ep->arb_group,
                                uct_rm_urma_ep_purge_legacy_grant_cb, ep);
    }
}

void uct_rm_urma_ep_pending_purge(uct_ep_h tl_ep, uct_pending_purge_callback_t cb, void *arg)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    uct_purge_cb_args_t args = { cb, arg };

    ucs_arbiter_group_purge(&iface->tx.arbiter, &ep->arb_group,
                            uct_rm_urma_ep_arbiter_purge_cb, &args);
    uct_rm_urma_ep_purge_legacy_grants(iface, ep);
}

ucs_status_t uct_rm_urma_ep_flush(uct_ep_h tl_ep, unsigned flags,
                                  uct_completion_t *comp)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    uct_urma_iface_send_op_t *op;

    if (flags & UCT_FLUSH_FLAG_REMOTE) {
        return UCS_ERR_UNSUPPORTED;
    }
    if (iface->tx.error != UCS_OK) {
        return iface->tx.error;
    }
    if (flags & UCT_FLUSH_FLAG_CANCEL) {
        /* Cancellation still waits for local DMA completion. */
        ep->super.flags |= UCT_RM_URMA_EP_FLAG_FLUSH_CANCEL;
        uct_rm_urma_ep_purge_legacy_grants(iface, ep);
    }
    if (ep->super.status.pi == ep->super.status.ci) {
        UCT_TL_EP_STAT_FLUSH(&ep->super.super);
        return UCS_OK;
    }
    if (comp != NULL) {
        op = ucs_mpool_get(&iface->super.tx.send_op_mp);
        if (op == NULL) {
            return UCS_ERR_NO_MEMORY;
        }
        uct_ub_ep_init_send_op(op, 0, comp, uct_ub_ep_flush_op_completion_handler);
        op->iface = &iface->super;
        /* The common flush helper takes uint16_t; RM WR sequence is uint32_t. */
        uct_ub_outstanding_queue_add_send_op_sn(&ep->super.txqp, op,
                                              ep->super.status.pi);
    }
    UCT_TL_EP_STAT_FLUSH_WAIT(&ep->super.super);
    return UCS_INPROGRESS;
}

ucs_arbiter_cb_result_t uct_rm_urma_ep_process_pending(ucs_arbiter_t *arbiter, ucs_arbiter_group_t *group,
                                                       ucs_arbiter_elem_t *elem, void *arg)
{
    uct_pending_req_t *req = ucs_container_of(elem, uct_pending_req_t, priv);
    uct_rm_urma_ep_t *ep = ucs_container_of(group, uct_rm_urma_ep_t, arb_group);
    uct_rm_urma_iface_t *iface = ucs_derived_of(ep->super.super.super.iface, uct_rm_urma_iface_t);
    ucs_status_t status;
    ucs_status_t *dispatch_status = arg;
    int is_fc_grant = (req->func == uct_rm_urma_ep_fc_grant);

    /* Stop before invoking another callback after a grant failed. Error
     * notification must wait until the arbiter has removed the current elem. */
    if (*dispatch_status != UCS_OK) {
        return UCS_ARBITER_CB_RESULT_STOP;
    }
    status = uct_rm_urma_iface_invoke_pending_cb(iface, req);
    if (status == UCS_OK) {
        return UCS_ARBITER_CB_RESULT_REMOVE_ELEM;
    } else if (status == UCS_INPROGRESS) {
        return UCS_ARBITER_CB_RESULT_NEXT_GROUP;
    } else if (is_fc_grant && (status != UCS_ERR_NO_RESOURCE)) {
        /* fc_grant has released req; do not dereference it again. */
        *dispatch_status = status;
        return UCS_ARBITER_CB_RESULT_REMOVE_ELEM;
    } else if (!(uct_ub_iface_has_tx_resources(&iface->super) &&
                 (iface->super.tx.jfc_available > 0) &&
                 (iface->rm_urma_jetty.available > 0))) {
        /* No iface resources */
        return UCS_ARBITER_CB_RESULT_STOP;
    }

    /* No any other pending operations (except no-op, flush(CANEL), and others
     * which don't consume TX resources) allowed to be still scheduled on an
     * arbiter group for which flush(CANCEL) was done */
    ucs_assert(!(ep->super.flags & UCT_RM_URMA_EP_FLAG_FLUSH_CANCEL));

    /* No ep resources */
    if ((iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) ?
        ((ep->peer != NULL) && (ep->peer->credits > 0)) :
        uct_ub_fc_has_resources(&iface->super, &ep->super.fc)) {
        /* A provider can report a full shared SQ even before the software WR
         * budget reaches zero. Keep this group scheduled for any TX CQE. */
        return UCS_ARBITER_CB_RESULT_STOP;
    }
    ucs_assertv(!uct_rm_urma_ep_can_send(ep),
                "Pending callback returned error, but send resources are"
                " available");
    return UCS_ARBITER_CB_RESULT_DESCHED_GROUP;
}

ucs_status_t uct_rm_urma_ep_fence(uct_ep_h tl_ep, unsigned flags)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);

    /* in case if fence is requested and enabled by configuration
     * we need to schedule fence for next RDMA operation */
    if (iface->super.config.fence_mode != UCT_UB_FENCE_MODE_NONE) {
        ep->super.fi.fence_beat = iface->super.tx.fi.fence_beat - 1;
    }

    UCT_TL_EP_STAT_FENCE(ucs_derived_of(tl_ep, uct_base_ep_t));
    return UCS_OK;
}

ucs_status_t uct_rm_urma_ep_check(uct_ep_h tl_ep, unsigned flags, uct_completion_t *comp)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    uct_rm_urma_pending_req_t *req = NULL;
    ucs_status_t status;

    UCT_EP_KEEPALIVE_CHECK_PARAM(flags, comp);

    ucs_assert(ep->super.flags & UCT_RM_URMA_EP_FLAG_CONNECTED);

    if (ep->super.flags & UCT_RM_URMA_EP_FLAG_KEEPALIVE_PENDING) {
        /* keepalive request is in pending queue and will be
         * processed when resources are available */
        return UCS_OK;
    }

    status = uct_rm_urma_ep_check_internal(tl_ep);
    if (status != UCS_ERR_NO_RESOURCE) {
        ucs_assert(status == UCS_OK);
        return status;
    }

    /* there are no iface resources, add pending request */
    req = ucs_mpool_get(&iface->tx.pending_mp);
    if (req == NULL) {
        return UCS_ERR_NO_MEMORY;
    }

    req->ep = &ep->super.super.super;
    req->super.func = uct_rm_urma_ep_check_progress;
    status = uct_rm_urma_ep_pending_add(tl_ep, &req->super, 0);
    ep->super.flags |= UCT_RM_URMA_EP_FLAG_KEEPALIVE_PENDING;
    ucs_assert_always(status == UCS_OK);

    return UCS_OK;
}

/* Preserve the existing 40-byte wire format without unaligned typed loads. */
typedef struct uct_rm_urma_ep_wire_addr {
    uint32_t              jetty_id;
    uint32_t              tpn;
    uint32_t              ep_id;
    uint64_t              seg_va;
    uint64_t              seg_len;
    uint32_t              seg_flag;
    uint32_t              seg_token_id;
    urma_transport_mode_t trans_mode;
} UCS_S_PACKED uct_rm_urma_ep_wire_addr_t;

ucs_status_t uct_rm_urma_ep_get_address(uct_ep_h tl_ep, uct_ep_addr_t *addr)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface,
                                               uct_rm_urma_iface_t);
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    const urma_seg_t *seg = &iface->super.ka_local_seg->tseg->seg;
    uct_rm_urma_ep_wire_addr_t wire;

    if (iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) {
        return UCS_ERR_UNSUPPORTED;
    }
    ucs_debug("RM legacy EP address export iface=%p ep=%p", iface, ep);
    UCS_STATIC_ASSERT(sizeof(wire) == 40);
    UCS_STATIC_ASSERT(sizeof(wire) == sizeof(uct_rm_urma_ep_addr_params_t));
    wire.jetty_id     = ep->rm_urma_jetty->jetty->jetty_id.id;
    wire.tpn          = ep->tp.tpn;
    wire.ep_id        = ep->super.ep_id;
    wire.seg_va       = seg->ubva.va;
    wire.seg_len      = seg->len;
    wire.seg_flag     = seg->attr.value;
    wire.seg_token_id = seg->token_id;
    wire.trans_mode   = ep->rm_urma_jetty->jetty->jetty_cfg.jfs_cfg.trans_mode;
    memcpy(addr, &wire, sizeof(wire));
    return UCS_OK;
}

static void
uct_rm_urma_ep_addr_unpack(const uct_rm_urma_ep_addr_t *ep_addr,
                           uct_rm_urma_ep_addr_params_t *params)
{
    uct_rm_urma_ep_wire_addr_t wire;

    memcpy(&wire, ep_addr, sizeof(wire));
    params->jetty_id     = wire.jetty_id;
    params->ep_id        = wire.ep_id;
    params->seg_va       = wire.seg_va;
    params->seg_len      = wire.seg_len;
    params->seg_flag     = wire.seg_flag;
    params->seg_token_id = wire.seg_token_id;
    params->trans_mode   = wire.trans_mode;
}

void uct_rm_urma_ka_seg(uct_rm_urma_ep_addr_params_t *ep_addr,
                        const uct_ub_address_pack_params_t *dev_addr, urma_seg_t *ka_remote_seg)
{
    ka_remote_seg->ubva.eid = dev_addr->eid;
    ka_remote_seg->ubva.uasid = dev_addr->uasid;
    ka_remote_seg->ubva.va = ep_addr->seg_va;
    ka_remote_seg->len = ep_addr->seg_len;
    ka_remote_seg->attr.value = ep_addr->seg_flag;
    ka_remote_seg->token_id = ep_addr->seg_token_id;
}

ucs_status_t uct_rm_urma_ep_ka_import_seg(uct_rm_urma_ep_t *ep, uct_ub_md_t *ub_md, urma_seg_t *ka_remote_seg)
{
    urma_import_seg_flag_t flag = {
        .bs = {
            .cacheable = URMA_NON_CACHEABLE,
            .access    = URMA_ACCESS_READ |
                         URMA_ACCESS_WRITE |
                         URMA_ACCESS_ATOMIC,
            .mapping   = URMA_SEG_NOMAP,
            .reserved  = 0
        }
    };

    ep->ka_tseg = urma_import_seg(ub_md->dev.urma_ctx, ka_remote_seg,
                                  &ub_md->dev.token, 0, flag);
    if (ep->ka_tseg == NULL) {
        ucs_error("Failed to import segment");
        return UCS_ERR_IO_ERROR;
    }

    return UCS_OK;
}

ucs_status_t uct_rm_urma_ep_connect_to_ep(uct_ep_h tl_ep,
                                          const uct_device_addr_t *dev_addr,
                                          const uct_ep_addr_t *ep_addr)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);
    const uct_rm_urma_ep_addr_t *rm_urma_ep_addr =
            (const uct_rm_urma_ep_addr_t *)ep_addr;
    const uct_ub_address_t *ub_addr = (const uct_ub_address_t *)dev_addr;
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    uct_ub_address_pack_params_t ubdev_addr_param = {0};
    uct_ub_md_t *ub_md = uct_ub_iface_md(&iface->super);
    uct_rm_urma_ep_addr_params_t ep_addr_param = {0};
    urma_seg_t ka_remote_seg = {0};
    ucs_status_t status = UCS_OK;
    urma_rjetty_t rjetty = {0};
    urma_token_t token = {0};

    if (ep->super.flags & (UCT_RM_URMA_EP_FLAG_CLOSING |
                          UCT_RM_URMA_EP_FLAG_FLUSH_CANCEL |
                          UCT_RM_URMA_EP_FLAG_ERR_HANDLER_INVOKED)) {
        return UCS_ERR_CANCELED;
    }

    if (iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) {
        return UCS_ERR_UNSUPPORTED;
    }
    ucs_debug("RM legacy connect_to_ep iface=%p ep=%p", iface, ep);
    uct_ub_address_unpack(ub_addr, &ubdev_addr_param);
    uct_rm_urma_ep_addr_unpack(rm_urma_ep_addr, &ep_addr_param);

    if ((ep_addr_param.trans_mode != URMA_TM_RM) ||
        (ep->super.flags & UCT_RM_URMA_EP_FLAG_CONNECTED)) {
        return UCS_ERR_INVALID_PARAM;
    }
    if (ep_addr_param.ep_id >= UCT_UB_IFACE_MAX_EPS) {
        ucs_error("Invalid dep_id:%u received", ep_addr_param.ep_id);
        return UCS_ERR_NO_RESOURCE;
    }

    rjetty.jetty_id.eid = ubdev_addr_param.eid;
    rjetty.jetty_id.uasid = ubdev_addr_param.uasid;
    rjetty.jetty_id.id = ep_addr_param.jetty_id;
    rjetty.trans_mode = ep_addr_param.trans_mode;
    rjetty.type = URMA_JETTY;
    rjetty.flag.bs.order_type = URMA_DEF_ORDER;
    rjetty.tp_type = URMA_CTP;
    token.token = ub_md->dev.token.token;

    status = uct_rm_urma_iface_target_get(iface, &rjetty, &token, &ep->target);
    if (status != UCS_OK) {
        ucs_error("Failed to acquire RM target: %s, eid="EID_FMT
                  ", uasid=%u, jetty_id=%u",
                  ucs_status_string(status), EID_ARGS(rjetty.jetty_id.eid),
                  rjetty.jetty_id.uasid, rjetty.jetty_id.id);
        goto err_import_jetty;
    }

    if (ep->ka_tseg == NULL) {
        uct_rm_urma_ka_seg(&ep_addr_param, &ubdev_addr_param, &ka_remote_seg);
        status = uct_rm_urma_ep_ka_import_seg(ep, ub_md, &ka_remote_seg);
        if (status != UCS_OK) {
            goto err_import_seg;
        }
    }

    ep->tjetty        = ep->target->jetty;
    ep->ka_va_addr    = ep_addr_param.seg_va;
    ep->super.dep_id  = ep_addr_param.ep_id;
    ep->super.flags |= UCT_RM_URMA_EP_FLAG_CONNECTED;
    return UCS_OK;

err_import_seg:
    uct_rm_urma_iface_target_put(iface, ep->target);
    ep->target = NULL;
err_import_jetty:
    return status;
}

ucs_status_t uct_rm_urma_ep_connect_to_ep_v2(uct_ep_h tl_ep, const uct_device_addr_t *dev_addr,
                                             const uct_ep_addr_t *ep_addr,
                                             const uct_ep_connect_to_ep_params_t *param)
{
    return uct_rm_urma_ep_connect_to_ep(tl_ep, dev_addr, ep_addr);
}

void uct_rm_urma_ep_txqp_purge_outstanding(uct_rm_urma_iface_t *iface, uct_ub_tx_queue_t *txqp,
                                           ucs_status_t status, uint32_t sn, int warn)
{
    uct_urma_iface_send_op_t *op = NULL;
    ucs_status_t ret = UCS_OK;

    ucs_queue_for_each_extract(op, &txqp->outstanding, queue, UCS_CIRCULAR_COMPARE32(op->sn, <=, sn)) {
        if (op->handler == uct_rm_urma_link_complete) {
            uct_rm_urma_link_complete(op, NULL);
            continue;
        }
        if (op->handler == uct_rm_urma_fc_send_completion) {
            /* The common URMA purge recognizes only its own handlers. A
             * control target must also be released on error/shutdown CQEs. */
            uct_rm_urma_fc_send_completion(op, NULL);
            continue;
        }
        ret = uct_ub_iface_txqp_purge_process(&iface->super, txqp, status, op, warn);
        if (ret != UCS_OK) {
            ucs_warn("rm_urma ub purge outstanding failed");
        }
    }
}

UCS_CLASS_INIT_FUNC(uct_rm_urma_ep_t, const uct_ep_params_t *params)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(params->iface, uct_rm_urma_iface_t);

    UCS_CLASS_CALL_SUPER_INIT(uct_ub_ep_t, &iface->super);
    self->rm_urma_jetty = &iface->rm_urma_jetty;
    ucs_arbiter_group_init(&self->arb_group);
    ucs_list_head_init(&self->retired_list);
    self->ka_tseg = NULL;
    self->ka_va_addr = 0;
    self->tjetty = NULL;
    self->target = NULL;
    self->peer = NULL;
    ucs_list_head_init(&self->peer_list);
    memset(&self->tp, 0, sizeof(self->tp));
    if (iface->link.mode == UCT_RM_URMA_LINK_IFACE_V2) {
        unsigned mask = UCT_EP_PARAM_FIELD_DEV_ADDR | UCT_EP_PARAM_FIELD_IFACE_ADDR;

        if ((params->field_mask & mask) == mask) {
            return uct_rm_urma_link_connect(self, params->dev_addr,
                                            params->iface_addr);
        }
        if (params->field_mask & mask) {
            return UCS_ERR_INVALID_PARAM;
        }
        /* Only the iface-owned control sender uses an unconnected EP. */
        if (!iface->link.creating_control) {
            return UCS_ERR_INVALID_PARAM;
        }
    }
    return UCS_OK;
}

static UCS_CLASS_CLEANUP_FUNC(uct_rm_urma_ep_t)
{
    uct_rm_urma_iface_t *iface = ucs_derived_of(self->super.super.super.iface,
                                              uct_rm_urma_iface_t);

    /* Only GC, or iface shutdown after deleting the shared Jetty, gets here. */
    ucs_assert(self->super.status.pi == self->super.status.ci);
    uct_rm_urma_ep_txqp_purge_outstanding(iface, &self->super.txqp,
                                         UCS_ERR_CANCELED,
                                         self->super.status.pi, 0);
    ucs_arbiter_group_cleanup(&self->arb_group);
    if (self->peer != NULL) {
        ucs_list_del(&self->peer_list);
    }
    if ((self->peer == NULL) && (self->ka_tseg != NULL) &&
        (urma_unimport_seg(self->ka_tseg) != URMA_SUCCESS)) {
        ucs_warn("Failed to unimport RM keepalive segment");
    }
    if (self->target != NULL) {
        uct_rm_urma_iface_target_put(iface, self->target);
    }
}

void uct_rm_urma_ep_destroy(uct_ep_h tl_ep)
{
    uct_rm_urma_ep_t *ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
    uct_rm_urma_iface_t *iface = ucs_derived_of(tl_ep->iface, uct_rm_urma_iface_t);

    if (ep->super.flags & UCT_RM_URMA_EP_FLAG_CLOSING) {
        return;
    }
    ep->super.flags |= UCT_RM_URMA_EP_FLAG_CLOSING;
    uct_rm_urma_ep_pending_purge(tl_ep, NULL, NULL);
    ucs_list_add_tail(&iface->tx.retired_eps, &ep->retired_list);
    /* Keep EP ID, target and DMA descriptors alive until all its CQEs arrive. */
    if (iface->tx.progress_depth == 0) {
        uct_rm_urma_iface_gc(iface);
    }
}

UCS_CLASS_DEFINE(uct_rm_urma_ep_t, uct_ub_ep_t);
UCS_CLASS_DEFINE_NEW_FUNC(uct_rm_urma_ep_t, uct_ep_t, const uct_ep_params_t *);
UCS_CLASS_DEFINE_DELETE_FUNC(uct_rm_urma_ep_t, uct_ep_t);

ucs_status_t uct_rm_urma_ep_link_post(uct_rm_urma_iface_t *iface,
                                     uct_rm_urma_link_req_t *req)
{
    uct_ep_params_t params = {
        .field_mask = UCT_EP_PARAM_FIELD_IFACE,
        .iface = &iface->super.super.super
    };
    uct_ep_h tl_ep;
    uct_rm_urma_ep_t *ep;
    uct_ub_iface_send_desc_t *desc = NULL;
    unsigned char frame[sizeof(uct_ub_hdr_t) + UCT_RM_URMA_FC_REQ_V2_SIZE];
    uct_ub_hdr_t *hdr;
    urma_jfs_wr_t *wr = &iface->super.send_wr;
    urma_jfs_wr_t *bad = NULL;
    urma_sge_t sge;
    urma_status_t result;
    ucs_status_t status;
    unsigned length = sizeof(*hdr) + req->length;
    int use_inline = length <= iface->super.config.max_inline;

    if (iface->tx.ctrl_ep == NULL) {
        iface->link.creating_control = 1;
        status = UCS_CLASS_NEW(uct_rm_urma_ep_t, &tl_ep, &params);
        iface->link.creating_control = 0;
        if (status != UCS_OK) {
            return status;
        }
        iface->tx.ctrl_ep = ucs_derived_of(tl_ep, uct_rm_urma_ep_t);
        iface->tx.ctrl_ep->super.fc.fc_wnd = INT16_MAX;
        ucs_debug("RM control sender created iface=%p ep=%p", iface,
                  iface->tx.ctrl_ep);
    }
    ep = iface->tx.ctrl_ep;
    if ((iface->rm_urma_jetty.available <= 0) ||
        (iface->super.tx.jfc_available <= 0)) {
        return UCS_ERR_NO_RESOURCE;
    }
    if (!use_inline) {
        desc = ucs_mpool_get(&iface->super.tx.mp);
        if (desc == NULL) {
            return UCS_ERR_NO_RESOURCE;
        }
    }
    hdr = use_inline ? (uct_ub_hdr_t *)frame : (uct_ub_hdr_t *)(desc + 1);
    hdr->am_id = UCT_RM_URMA_EP_FC_PURE_GRANT;
    hdr->ep_id = 0;
    memcpy(hdr + 1, req->payload, req->length);
    UCT_UB_FILL_WR_SG_BUF(&wr->send.src, (uint64_t)hdr, length,
                         use_inline ? NULL : desc->tseg, 1, &sge);
    uct_rm_urma_init_base_wr(wr, iface, URMA_OPC_SEND, ep, 1,
                             use_inline ? URMA_INLINE_ENABLE : URMA_INLINE_DISABLE,
                             URMA_SOLICITED_ENABLE);
    wr->tjetty = req->target->jetty;
    result = urma_post_jetty_send_wr(ep->rm_urma_jetty->jetty, wr, &bad);
    memset(wr, 0, sizeof(*wr));
    if (result != URMA_SUCCESS) {
        if ((result == URMA_ENOMEM) || (result == URMA_EAGAIN)) {
            if (desc != NULL) {
                ucs_mpool_put(desc);
            }
            return UCS_ERR_NO_RESOURCE;
        }
        /* A terminal provider error must not recycle a possibly DMA-owned
         * descriptor. Pending cleanup runs only after the Jetty stops. */
        req->desc = desc;
        return UCS_ERR_IO_ERROR;
    }
    ucs_debug("RM iface control post kind=%u id=%lu credits=%u iface=%p",
              req->kind, req->id, req->credits, iface);
    req->desc = desc;
    req->op.flags = 0;
    req->op.iface = &iface->super;
    req->op.user_comp = NULL;
    req->op.handler = uct_rm_urma_link_complete;
    uct_rm_urma_update_tx_resources_status(ep, iface, URMA_COMPLETE_ENABLE);
    uct_ub_outstanding_queue_add_send_op_sn(&ep->super.txqp, &req->op,
                                           ep->super.status.pi);
    return UCS_OK;
}
