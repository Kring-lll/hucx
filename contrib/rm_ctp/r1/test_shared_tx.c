/* CPU-only regression: executes the actual RM sources with mocked URMA posts.
 * This is not a provider, hardware, packet-loss or performance test. */
#include <assert.h>
#include <stdio.h>
#include <uct/base/uct_worker.h>
#include "../../../src/uct/urma/rm_urma/rm_urma_ep.c"
#include "../../../src/uct/urma/rm_urma/rm_urma_iface.c"

static struct {
    uint64_t ctx;
    urma_target_jetty_t *target;
    uint64_t header_addr;
    unsigned posts, fail_next, unimports, errors;
    unsigned am_calls, destroy_on_error, purge_on_error, purged;
    urma_status_t fail_status;
} mock;

urma_status_t urma_init(urma_init_attr_t *attr) { return URMA_SUCCESS; }
urma_status_t urma_uninit(void) { return URMA_SUCCESS; }

urma_status_t urma_post_jetty_send_wr(urma_jetty_t *j, urma_jfs_wr_t *wr,
                                      urma_jfs_wr_t **bad)
{
    assert(j != NULL);
    assert(wr->flag.bs.complete_enable == URMA_COMPLETE_ENABLE);
    if (mock.fail_next) {
        mock.fail_next = 0;
        *bad = wr;
        return mock.fail_status ? mock.fail_status : URMA_ENOMEM;
    }
    mock.ctx = wr->user_ctx;
    mock.target = wr->tjetty;
    mock.header_addr = wr->send.src.sge[0].addr;
    ++mock.posts;
    return URMA_SUCCESS;
}

urma_status_t urma_modify_jetty(urma_jetty_t *j, urma_jetty_attr_t *a)
{
    assert(j && a->state == URMA_JETTY_STATE_ERROR);
    return URMA_SUCCESS;
}
urma_status_t urma_unimport_jetty(urma_target_jetty_t *j)
{
    assert(j != NULL);
    ++mock.unimports;
    return URMA_SUCCESS;
}
urma_status_t urma_unimport_seg(urma_target_seg_t *s)
{
    assert(s != NULL);
    return URMA_SUCCESS;
}

static void pool_obj(ucs_mpool_t *mp, void *obj, void *chunk)
{
    uct_ub_iface_send_desc_t *desc = obj;
    desc->super.flags = 0;
    desc->tseg = (urma_target_seg_t *)(uintptr_t)1;
}
static ucs_mpool_ops_t pool_ops = {
    .chunk_alloc = ucs_mpool_chunk_malloc,
    .chunk_release = ucs_mpool_chunk_free,
    .obj_init = pool_obj
};

static void pool_init(ucs_mpool_t *mp, size_t size, int descriptor)
{
    ucs_mpool_params_t p;
    void *obj;
    static ucs_mpool_ops_t plain = {
        .chunk_alloc = ucs_mpool_chunk_malloc,
        .chunk_release = ucs_mpool_chunk_free
    };
    ucs_mpool_params_reset(&p);
    p.elem_size = size;
    p.elems_per_chunk = 16;
    p.alignment = 8;
    p.ops = descriptor ? &pool_ops : &plain;
    p.name = "r1-mock";
    assert(ucs_mpool_init(&p, mp) == UCS_OK);
    obj = ucs_mpool_get(mp);
    assert(obj != NULL);
    ucs_mpool_put(obj);
}

static void on_purge(uct_pending_req_t *req, void *arg)
{
    ++mock.purged;
}

static ucs_status_t on_error(void *arg, uct_ep_h ep, ucs_status_t status)
{
    ++mock.errors;
    if (mock.destroy_on_error) {
        uct_rm_urma_ep_destroy(ep);
    } else if (mock.purge_on_error) {
        uct_rm_urma_ep_pending_purge(ep, on_purge, NULL);
    }
    return UCS_OK;
}

static uct_rm_urma_ep_t *new_ep(uct_rm_urma_iface_t *iface, unsigned remote)
{
    uct_rm_urma_ep_t *ep = ucs_calloc(1, sizeof(*ep), "r1-mock-ep");
    assert(ep != NULL);
    ep->super.super.super.iface = &iface->super.super.super;
    assert(uct_ub_iface_add_ep(&iface->super, &ep->super) == UCS_OK);
    uct_ub_ep_tx_outstanding_queue_init(&ep->super.txqp);
    ep->super.flags = UCT_RM_URMA_EP_FLAG_CONNECTED;
    ep->super.dep_id = remote;
    ep->super.fc.fc_wnd = INT16_MAX;
    ep->rm_urma_jetty = &iface->rm_urma_jetty;
    ep->tjetty = (urma_target_jetty_t *)(uintptr_t)(remote + 1);
    ucs_arbiter_group_init(&ep->arb_group);
    ucs_list_head_init(&ep->retired_list);
    return ep;
}

static ucs_status_t send_short(uct_rm_urma_ep_t *ep)
{
    return uct_rm_urma_ep_am_short(&ep->super.super.super, 0, 7, "payload", 7);
}
static void complete(uct_rm_urma_iface_t *iface, uint64_t ctx)
{
    urma_cr_t cr = { .status = URMA_CR_SUCCESS, .user_ctx = ctx };
    uct_rm_urma_deal_with_send_cr(iface, &cr, 1);
}

typedef struct {
    uct_pending_req_t req;
    uct_rm_urma_ep_t *ep;
    unsigned called;
} pending_t;
static ucs_status_t pending_send(uct_pending_req_t *req)
{
    pending_t *p = ucs_container_of(req, pending_t, req);
    ++p->called;
    return send_short(p->ep);
}
static unsigned callbacks;
static uct_rm_urma_ep_t *callback_ep;
static void on_complete(uct_completion_t *c)
{
    ++callbacks;
    if (callback_ep) {
        uct_rm_urma_ep_destroy(&callback_ep->super.super.super);
        callback_ep = NULL;
    }
}
static size_t pack(void *dst, void *arg) { memcpy(dst, arg, 32); return 32; }

static unsigned pool_free_count(ucs_mpool_t *mp)
{
    ucs_mpool_elem_t *p;
    unsigned count = 0;
    for (p = mp->freelist; p != NULL; p = p->next) {
        assert(++count < 1024); /* Detect a double-put cycle as well. */
    }
    return count;
}

static ucs_status_t on_am(void *arg, void *data, size_t length, unsigned flags)
{
    ++mock.am_calls;
    return UCS_OK;
}

static void receive_fc(uct_rm_urma_iface_t *iface, uct_rm_urma_ep_t *ep,
                       uint8_t id)
{
    uct_ub_hdr_t hdr = { .am_id = id, .ep_id = ep->super.ep_id };
    urma_cr_t cr = { .completion_len = sizeof(hdr) };
    assert(uct_rm_urma_iface_fc_handler(&iface->super, &hdr, &cr, 0) == UCS_OK);
}

static void test_fc_errors(void)
{
    unsigned scenario, i, free_before;
    for (scenario = 0; scenario < 11; ++scenario) {
        uct_rm_urma_iface_t iface = {};
        uct_priv_worker_t worker = {};
        urma_jetty_t local = {};
        uct_rm_urma_ep_t *a, *b, *ep;
        pending_t user = { .req.func = pending_send };
        uint64_t a1, a2, grant;

        memset(&mock, 0, sizeof(mock));
        assert(ucs_callbackq_init(&worker.super.progress_q) == UCS_OK);
        iface.super.super.worker = &worker;
        iface.super.super.err_handler = on_error;
        iface.super.super.am[0].cb = on_am;
        iface.super.super.super.ops = uct_rm_urma_iface_tl_ops;
        iface.super.config.max_inline = 128;
        iface.super.config.seg_size = 2048;
        iface.super.config.tx_max_wr = 3;
        iface.super.config.fc_enabled = 1;
        iface.super.config.fc_wnd_size = 3;
        iface.super.config.fc_hard_thresh = 1;
        iface.config.fc_soft_thresh = 2;
        iface.super.tx.jfc_available = 64;
        iface.super.tx.reads_available = 1048576;
        iface.rm_urma_jetty.jetty = &local;
        iface.rm_urma_jetty.available = 2;
        ucs_ptr_array_init(&iface.super.eps, "fc-error-EPs");
        ucs_list_head_init(&iface.tx.retired_eps);
        ucs_arbiter_init(&iface.tx.arbiter);
        pool_init(&iface.super.tx.mp, sizeof(uct_ub_iface_send_desc_t) + 2048, 1);
        pool_init(&iface.super.tx.short_desc_mp, sizeof(uct_ub_iface_send_desc_t) + 256, 1);
        pool_init(&iface.super.tx.send_op_mp, sizeof(uct_urma_iface_send_op_t), 0);
        pool_init(&iface.tx.pending_mp, sizeof(uct_rm_urma_pending_req_t), 0);
        free_before = pool_free_count(&iface.tx.pending_mp);
        a = new_ep(&iface, 11);
        b = new_ep(&iface, 22);
        a->super.fc.fc_wnd = b->super.fc.fc_wnd = 3;

        if (scenario == 0) {
            uct_rm_urma_iface_set_failed(&iface, UCS_ERR_IO_ERROR);
            a->super.fc.fc_wnd = 0;
            receive_fc(&iface, a, UCT_RM_URMA_EP_FLAG_FC_HARD_REQ);
            receive_fc(&iface, a, UCT_RM_URMA_EP_FC_PURE_GRANT);
            assert(mock.posts == 0 && mock.errors == 2 && mock.am_calls == 0);
            assert(a->super.fc.fc_wnd == 0);
        } else if ((scenario == 1) || (scenario == 2)) {
            mock.fail_next = 1;
            mock.fail_status = URMA_EINVAL;
            mock.destroy_on_error = (scenario == 2);
            receive_fc(&iface, a, UCT_RM_URMA_EP_FLAG_FC_HARD_REQ);
            assert(iface.tx.error == UCS_ERR_IO_ERROR && mock.errors == 2);
            assert(mock.am_calls == 0);
        } else if (scenario == 9) {
            for (i = 0; i < 3; ++i) {
                assert(send_short(a) == UCS_OK); complete(&iface, mock.ctx);
            }
            assert(a->super.fc.fc_wnd == 0);
            assert(send_short(a) == UCS_ERR_NO_RESOURCE);
            user.ep = a;
            assert(uct_rm_urma_ep_pending_add(&a->super.super.super,
                                              &user.req, 0) == UCS_OK);
            assert(user.called == 0);
            receive_fc(&iface, a, UCT_RM_URMA_EP_FC_PURE_GRANT);
            assert(user.called == 1 && a->super.fc.fc_wnd == 2);
            complete(&iface, mock.ctx);
        } else if (scenario == 10) {
            void *held[16];
            assert(free_before == 16);
            for (i = 0; i < 16; ++i) held[i] = ucs_mpool_get(&iface.tx.pending_mp);
            iface.tx.pending_mp.data->quota = 0;
            mock.destroy_on_error = 1;
            receive_fc(&iface, a, UCT_RM_URMA_EP_FLAG_FC_HARD_REQ);
            assert(iface.tx.error == UCS_ERR_NO_MEMORY && mock.errors == 2);
            assert(mock.posts == 0 && mock.am_calls == 0);
            for (i = 0; i < 16; ++i) ucs_mpool_put(held[i]);
        } else if (scenario == 6) {
            assert(send_short(a) == UCS_OK); a1 = mock.ctx;
            assert(send_short(a) == UCS_OK); a2 = mock.ctx;
            receive_fc(&iface, b, UCT_RM_URMA_EP_FLAG_FC_HARD_REQ);
            assert(ucs_arbiter_group_num_elems(&b->arb_group) == 1);
            complete(&iface, a1); grant = mock.ctx;
            assert(mock.posts == 3 && b->super.fc.fc_wnd == 3);
            complete(&iface, a2); complete(&iface, grant);
        } else {
            mock.fail_next = 1;
            mock.fail_status = URMA_EAGAIN;
            receive_fc(&iface, a, UCT_RM_URMA_EP_FLAG_FC_HARD_REQ);
            assert(ucs_arbiter_group_num_elems(&a->arb_group) == 1);
            assert(pool_free_count(&iface.tx.pending_mp) + 1 == free_before);
            if (scenario == 3) {
                uct_rm_urma_iface_progress_pending(&iface);
                assert(mock.posts == 1 && mock.errors == 0);
                complete(&iface, mock.ctx);
            } else if (scenario == 5) {
                assert(uct_rm_urma_ep_flush(&a->super.super.super,
                                            UCT_FLUSH_FLAG_CANCEL, NULL) == UCS_OK);
                uct_rm_urma_iface_progress_pending(&iface);
                assert(mock.posts == 0 && mock.errors == 0);
                assert(send_short(b) == UCS_OK); complete(&iface, mock.ctx);
            } else {
                mock.fail_next = 1;
                mock.fail_status = URMA_EINVAL;
                mock.destroy_on_error = (scenario != 8);
                if (scenario == 8) {
                    mock.purge_on_error = 1;
                    user.ep = b;
                    uct_pending_req_arb_group_push(&b->arb_group, &user.req);
                    ucs_arbiter_group_schedule(&iface.tx.arbiter, &b->arb_group);
                }
                if (scenario == 7) {
                    a->super.fc.fc_wnd = 0;
                    receive_fc(&iface, a, UCT_RM_URMA_EP_FC_PURE_GRANT);
                } else {
                    uct_rm_urma_iface_progress_pending(&iface);
                }
                assert(iface.tx.error == UCS_ERR_IO_ERROR && mock.errors == 2);
                assert(mock.posts == 0 && user.called == 0);
                if (scenario == 8) assert(mock.purged == 1);
            }
        }
        assert(pool_free_count(&iface.tx.pending_mp) == free_before);
        for (i = 0; i < iface.super.eps.size; ++i) {
            if (__ucs_ptr_array_is_free(iface.super.eps.start[i])) continue;
            ep = (uct_rm_urma_ep_t *)iface.super.eps.start[i];
            assert(ep->super.status.pi == ep->super.status.ci);
            uct_rm_urma_ep_destroy(&ep->super.super.super);
        }
        uct_rm_urma_iface_gc(&iface);
        assert(mock.unimports == 2);
        ucs_arbiter_cleanup(&iface.tx.arbiter);
        ucs_ptr_array_cleanup(&iface.super.eps, 1);
        ucs_mpool_cleanup(&iface.tx.pending_mp, 1);
        ucs_mpool_cleanup(&iface.super.tx.mp, 1);
        ucs_mpool_cleanup(&iface.super.tx.short_desc_mp, 1);
        ucs_mpool_cleanup(&iface.super.tx.send_op_mp, 1);
        ucs_callbackq_cleanup(&worker.super.progress_q);
    }
    puts("PASS late FC, immediate/queued errors, retry, cancel, grant wakeup, OOM and callback destruction/purge (11 CPU cases)");
}

int main(void)
{
    uct_rm_urma_iface_t iface = {};
    uct_priv_worker_t worker = {};
    urma_jetty_t local = {};
    uct_rm_urma_ep_t *a, *b;
    uint64_t a1, a2, b1, fc_a, fc_b;
    uct_completion_t done = { .func = on_complete, .count = 1, .status = UCS_OK };
    char data[32] = "bcopy payload";
    unsigned i, destroyed_id, before;
    pending_t p = { .req.func = pending_send };

    assert(ucs_callbackq_init(&worker.super.progress_q) == UCS_OK);
    iface.super.super.worker = &worker;
    iface.super.super.err_handler = on_error;
    iface.super.super.super.ops = uct_rm_urma_iface_tl_ops;
    iface.super.config.max_inline = 128;
    iface.super.config.seg_size = 2048;
    iface.super.config.tx_max_wr = 3;
    iface.super.tx.jfc_available = 64;
    iface.super.tx.reads_available = 1048576;
    iface.rm_urma_jetty.jetty = &local;
    iface.rm_urma_jetty.available = 2;
    ucs_ptr_array_init(&iface.super.eps, "r1-EPs");
    ucs_list_head_init(&iface.tx.retired_eps);
    ucs_arbiter_init(&iface.tx.arbiter);
    pool_init(&iface.super.tx.mp, sizeof(uct_ub_iface_send_desc_t) + 2048, 1);
    pool_init(&iface.super.tx.short_desc_mp, sizeof(uct_ub_iface_send_desc_t) + 256, 1);
    pool_init(&iface.super.tx.send_op_mp, sizeof(uct_urma_iface_send_op_t), 0);
    a = new_ep(&iface, 11);
    b = new_ep(&iface, 22);
    assert(a->rm_urma_jetty == b->rm_urma_jetty);
    assert(send_short(a) == UCS_OK); a1 = mock.ctx;
    assert(send_short(a) == UCS_OK); a2 = mock.ctx;
    assert(send_short(b) == UCS_ERR_NO_RESOURCE);
    p.ep = b;
    assert(uct_rm_urma_ep_pending_add(&b->super.super.super, &p.req, 0) == UCS_OK);
    complete(&iface, a1); b1 = mock.ctx;
    assert(p.called == 1 && (b1 >> 32) == b->super.ep_id);
    assert(mock.target == b->tjetty && iface.rm_urma_jetty.available == 0);
    complete(&iface, a2);
    assert(uct_rm_urma_ep_flush(&a->super.super.super, 0, NULL) == UCS_OK);
    assert(uct_rm_urma_ep_flush(&b->super.super.super, 0, NULL) == UCS_INPROGRESS);
    complete(&iface, b1);
    assert(iface.rm_urma_jetty.available == 2);
    puts("PASS shared credit, target routing, cross-EP pending and independent flush");

    a->super.status.pi = a->super.status.ci = 65536;
    assert(send_short(a) == UCS_OK); a1 = mock.ctx;
    assert(uct_rm_urma_ep_flush(&a->super.super.super, 0, &done) == UCS_INPROGRESS);
    assert(callbacks == 0 && ucs_queue_head_elem_non_empty(
           &a->super.txqp.outstanding, uct_urma_iface_send_op_t, queue)->sn == 65537);
    complete(&iface, a1);
    assert(callbacks == 1 && done.count == 0);
    a->super.status.pi = a->super.status.ci = UINT32_MAX;
    assert(send_short(a) == UCS_OK); complete(&iface, mock.ctx);
    assert(a->super.status.pi == 0 && a->super.status.ci == 0);
    for (i = 0; i < 70000; ++i) {
        assert(send_short(a) == UCS_OK); complete(&iface, mock.ctx);
    }
    puts("PASS 32-bit flush sequence, wrap-around and 70000 completions");

    callback_ep = a;
    done.count = 1;
    assert(send_short(a) == UCS_OK); a1 = mock.ctx;
    assert(uct_rm_urma_ep_flush(&a->super.super.super, 0, &done) == UCS_INPROGRESS);
    complete(&iface, a1);
    assert(callback_ep == NULL && (a->super.flags & UCT_RM_URMA_EP_FLAG_CLOSING));
    uct_rm_urma_iface_gc(&iface);
    assert(mock.unimports == 1);
    a = new_ep(&iface, 11);
    puts("PASS destruction requested from completion callback remains deferred");

    before = mock.posts;
    mock.fail_next = 1;
    assert(uct_rm_urma_ep_am_bcopy(&a->super.super.super, 0, pack, data, 0) == UCS_ERR_NO_RESOURCE);
    assert(mock.posts == before && iface.rm_urma_jetty.available == 2);
    assert(uct_rm_urma_ep_am_bcopy(&a->super.super.super, 0, pack, data, 0) == 32);
    a1 = mock.ctx;
    destroyed_id = a->super.ep_id;
    uct_rm_urma_ep_destroy(&a->super.super.super);
    assert(mock.unimports == 1 && !__ucs_ptr_array_is_free(iface.super.eps.start[destroyed_id]));
    complete(&iface, a1);
    uct_rm_urma_iface_gc(&iface);
    assert(mock.unimports == 2 && __ucs_ptr_array_is_free(iface.super.eps.start[destroyed_id]));
    puts("PASS bcopy post-failure rollback and delayed EP/descriptor retirement");

    a = new_ep(&iface, 33);
    iface.super.config.max_inline = 0;
    assert(uct_rm_urma_ep_fc_ctrl(&a->super.super.super, UCT_RM_URMA_EP_FC_PURE_GRANT, NULL) == UCS_OK);
    a1 = mock.ctx; fc_a = mock.header_addr;
    assert(uct_rm_urma_ep_fc_ctrl(&b->super.super.super, UCT_RM_URMA_EP_FC_PURE_GRANT, NULL) == UCS_OK);
    b1 = mock.ctx; fc_b = mock.header_addr;
    assert(fc_a != fc_b && ((uct_ub_hdr_t *)(uintptr_t)fc_a)->ep_id == 33);
    assert(((uct_ub_hdr_t *)(uintptr_t)fc_b)->ep_id == 22);
    complete(&iface, a1); complete(&iface, b1);
    puts("PASS non-inline FC headers remain distinct until completion");

    {
        urma_cr_t special = { .status = URMA_CR_WR_FLUSH_ERR_DONE,
                              .user_ctx = UINT64_MAX };
        uct_rm_urma_deal_with_send_cr(&iface, &special, 1);
        assert(iface.tx.error != UCS_OK && mock.errors == 2);
        assert(iface.rm_urma_jetty.available == 2);
    }
    uct_rm_urma_ep_destroy(&a->super.super.super);
    uct_rm_urma_ep_destroy(&b->super.super.super);
    assert(mock.unimports == 4);
    ucs_arbiter_cleanup(&iface.tx.arbiter);
    ucs_ptr_array_cleanup(&iface.super.eps, 1);
    ucs_mpool_cleanup(&iface.super.tx.mp, 1);
    ucs_mpool_cleanup(&iface.super.tx.short_desc_mp, 1);
    ucs_mpool_cleanup(&iface.super.tx.send_op_mp, 1);
    ucs_callbackq_cleanup(&worker.super.progress_q);
    puts("PASS synthetic CQE isolation and cleanup; MOCK ONLY, no hardware validation");
    test_fc_errors();
    return 0;
}
