/* CPU-only regression: executes the actual RM sources with mocked URMA posts.
 * This is not a provider, hardware, packet-loss or performance test. */
#include <assert.h>
#include <stdio.h>
#include <uct/base/uct_worker.h>
#include <ucs/async/async.h>
#include "../../../src/uct/urma/rm_urma/rm_urma_ep.c"

static unsigned fail_target_alloc;
static void *target_test_malloc(size_t size, const char *name);
#define ucs_malloc target_test_malloc
#include "../../../src/uct/urma/rm_urma/rm_urma_iface.c"
#undef ucs_malloc

static void *target_test_malloc(size_t size, const char *name)
{
    if (fail_target_alloc) {
        --fail_target_alloc;
        return NULL;
    }
    return ucs_malloc(size, name);
}

static struct {
    uint64_t ctx;
    urma_target_jetty_t *target;
    uint64_t header_addr;
    unsigned posts, fail_next, unimports, errors;
    unsigned imports, seg_imports, seg_unimports;
    unsigned fail_import, fail_seg_import, fail_unimport;
    unsigned live_targets, live_segments;
    unsigned require_quiesce, hardware_stopped;
    unsigned am_calls, destroy_on_error, purge_on_error, purged;
    urma_status_t fail_status;
} mock;

urma_status_t urma_init(urma_init_attr_t *attr) { return URMA_SUCCESS; }
urma_status_t urma_uninit(void) { return URMA_SUCCESS; }

urma_status_t urma_delete_jetty(urma_jetty_t *jetty)
{
    assert(jetty != NULL);
    mock.hardware_stopped = 1;
    return URMA_SUCCESS;
}

urma_status_t urma_delete_jfr(urma_jfr_t *jfr)
{
    assert(jfr != NULL && mock.hardware_stopped);
    return URMA_SUCCESS;
}

urma_target_jetty_t *urma_import_jetty(urma_context_t *ctx,
                                      urma_rjetty_t *remote,
                                      urma_token_t *token)
{
    urma_target_jetty_t *target;

    ++mock.imports;
    if (mock.fail_import) {
        --mock.fail_import;
        return NULL;
    }
    target = calloc(1, sizeof(*target));
    assert(target != NULL);
    ++mock.live_targets;
    target->id         = remote->jetty_id;
    target->trans_mode = remote->trans_mode;
    return target;
}

urma_target_seg_t *urma_import_seg(urma_context_t *ctx, urma_seg_t *seg,
                                  urma_token_t *token, uint64_t addr,
                                  urma_import_seg_flag_t flag)
{
    urma_target_seg_t *target;

    ++mock.seg_imports;
    if (mock.fail_seg_import) {
        --mock.fail_seg_import;
        return NULL;
    }
    target = calloc(1, sizeof(*target));
    assert(target != NULL);
    ++mock.live_segments;
    target->seg = *seg;
    return target;
}

urma_status_t urma_post_jetty_send_wr(urma_jetty_t *j, urma_jfs_wr_t *wr,
                                      urma_jfs_wr_t **bad)
{
    assert(j != NULL);
    assert(!mock.require_quiesce || mock.hardware_stopped);
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
    assert(!mock.require_quiesce || mock.hardware_stopped);
    ++mock.unimports;
    if (mock.fail_unimport) {
        --mock.fail_unimport;
        return URMA_FAIL;
    }
    free(j);
    assert(mock.live_targets > 0);
    --mock.live_targets;
    return URMA_SUCCESS;
}
urma_status_t urma_unimport_seg(urma_target_seg_t *s)
{
    assert(s != NULL);
    assert(!mock.require_quiesce || mock.hardware_stopped);
    ++mock.seg_unimports;
    free(s);
    assert(mock.live_segments > 0);
    --mock.live_segments;
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

static uct_rm_urma_ep_t *new_unconnected_ep(uct_rm_urma_iface_t *iface)
{
    static uct_ub_md_t md;
    uct_rm_urma_ep_t *ep = ucs_calloc(1, sizeof(*ep), "r1-mock-ep");
    assert(ep != NULL);
    ep->super.super.super.iface = &iface->super.super.super;
    assert(uct_ub_iface_add_ep(&iface->super, &ep->super) == UCS_OK);
    uct_ub_ep_tx_outstanding_queue_init(&ep->super.txqp);
    ep->super.fc.fc_wnd = INT16_MAX;
    ep->rm_urma_jetty = &iface->rm_urma_jetty;
    iface->super.super.md = &md.super;
    ucs_arbiter_group_init(&ep->arb_group);
    ucs_list_head_init(&ep->retired_list);
    return ep;
}

static uct_rm_urma_ep_t *new_ep(uct_rm_urma_iface_t *iface, unsigned remote)
{
    urma_rjetty_t target = {};
    urma_token_t token = {};
    uct_rm_urma_ep_t *ep = new_unconnected_ep(iface);

    ep->super.flags       = UCT_RM_URMA_EP_FLAG_CONNECTED;
    ep->super.dep_id      = remote;
    target.jetty_id.id    = remote + 1;
    target.trans_mode     = URMA_TM_RM;
    target.type           = URMA_JETTY;
    target.tp_type        = URMA_CTP;
    assert(uct_rm_urma_iface_target_get(iface, &target, &token,
                                       &ep->target) == UCS_OK);
    ep->tjetty = ep->target->jetty;
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
        kh_init_inplace(uct_rm_urma_targets, &iface.targets);
        ucs_arbiter_init(&iface.tx.arbiter);
        pool_init(&iface.super.tx.mp, sizeof(uct_ub_iface_send_desc_t) + 2048, 1);
        pool_init(&iface.super.tx.short_desc_mp, sizeof(uct_ub_iface_send_desc_t) + 256, 1);
        pool_init(&iface.super.tx.send_op_mp, sizeof(uct_urma_iface_send_op_t), 0);
        pool_init(&iface.tx.pending_mp, sizeof(uct_rm_urma_fc_pending_req_t), 0);
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
            assert(ucs_arbiter_group_num_elems(&iface.tx.ctrl_ep->arb_group) == 1);
            complete(&iface, a1); grant = mock.ctx;
            assert(mock.posts == 3 && b->super.fc.fc_wnd == 3);
            complete(&iface, a2); complete(&iface, grant);
        } else {
            mock.fail_next = 1;
            mock.fail_status = URMA_EAGAIN;
            receive_fc(&iface, a, UCT_RM_URMA_EP_FLAG_FC_HARD_REQ);
            assert(ucs_arbiter_group_num_elems(&iface.tx.ctrl_ep->arb_group) == 1);
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
        assert(kh_size(&iface.targets) == 0);
        uct_rm_urma_iface_targets_cleanup(&iface);
        assert(mock.live_targets == 0 && mock.live_segments == 0);
        ucs_ptr_array_cleanup(&iface.super.eps, 1);
        ucs_mpool_cleanup(&iface.tx.pending_mp, 1);
        ucs_mpool_cleanup(&iface.super.tx.mp, 1);
        ucs_mpool_cleanup(&iface.super.tx.short_desc_mp, 1);
        ucs_mpool_cleanup(&iface.super.tx.send_op_mp, 1);
        ucs_callbackq_cleanup(&worker.super.progress_q);
    }
    puts("PASS late FC, immediate/queued errors, retry, cancel, grant wakeup, OOM and callback destruction/purge (11 CPU cases)");
}

static ucs_status_t connect_remote(uct_rm_urma_ep_t *ep, unsigned remote_ep)
{
    uint8_t device[32] = {};
    urma_eid_t eid = {};
    uint32_t uasid = 17;
    uct_rm_urma_ep_wire_addr_t wire = {
        .jetty_id = 771,
        .ep_id = remote_ep,
        .seg_va = 0x1000,
        .seg_len = 4096,
        .seg_token_id = 123,
        .trans_mode = URMA_TM_RM
    };

    eid.raw[0] = 0x45;
    eid.raw[15] = 0x97;
    memcpy(device + 1, &eid, sizeof(eid));
    memcpy(device + 1 + sizeof(eid), &uasid, sizeof(uasid));
    uint8_t packed[sizeof(wire) + 1];

    memcpy(packed + 1, &wire, sizeof(wire));
    return uct_ep_connect_to_ep(&ep->super.super.super,
                                (const uct_device_addr_t *)device,
                                (const uct_ep_addr_t *)(packed + 1));
}

static void test_ep_wire_address(uct_rm_urma_iface_t *iface,
                                  uct_rm_urma_ep_t *ep)
{
    urma_target_seg_t segment = {};
    uct_ub_iface_ka_desc_t mem = { .tseg = &segment };
    uct_rm_urma_ep_wire_addr_t expected = {
        .jetty_id = 100,
        .tpn = 17,
        .ep_id = ep->super.ep_id,
        .seg_va = 0x123456789abULL,
        .seg_len = 4096,
        .seg_flag = 0x123,
        .seg_token_id = 321,
        .trans_mode = URMA_TM_RM
    };
    uct_rm_urma_ep_addr_params_t decoded = {};
    uint8_t packed[sizeof(expected) + 2];

    segment.seg.ubva.va = expected.seg_va;
    segment.seg.len = expected.seg_len;
    segment.seg.attr.value = expected.seg_flag;
    segment.seg.token_id = expected.seg_token_id;
    iface->super.ka_local_seg = &mem;
    iface->rm_urma_jetty.jetty->jetty_id.id = expected.jetty_id;
    iface->rm_urma_jetty.jetty->jetty_cfg.jfs_cfg.trans_mode = URMA_TM_RM;
    ep->tp.tpn = expected.tpn;
    memset(packed, 0xa5, sizeof(packed));
    assert(uct_ep_get_address(&ep->super.super.super,
                              (uct_ep_addr_t *)(packed + 1)) == UCS_OK);
    assert(packed[0] == 0xa5 && packed[sizeof(packed) - 1] == 0xa5);
    assert(!memcmp(packed + 1, &expected, sizeof(expected)));
    uct_rm_urma_ep_addr_unpack((const uct_rm_urma_ep_addr_t *)(packed + 1),
                               &decoded);
    assert(decoded.jetty_id == expected.jetty_id);
    assert(decoded.ep_id == expected.ep_id && decoded.seg_va == expected.seg_va);
    assert(decoded.seg_len == expected.seg_len);
    assert(decoded.seg_flag == expected.seg_flag);
    assert(decoded.seg_token_id == expected.seg_token_id);
    assert(decoded.trans_mode == expected.trans_mode);
    iface->super.ka_local_seg = NULL;
    puts("PASS unchanged 40-byte EP wire address with unaligned export/import and canaries");
}

static void test_target_reuse(void)
{
    uct_rm_urma_iface_t iface = {};
    uct_priv_worker_t worker = {};
    urma_jetty_t local = {};
    uct_rm_urma_ep_t *a, *b, *c;
    urma_jfr_t jfr = {};
    ucs_async_context_t async;
    uct_rm_urma_target_t *target, *again, *targets[128];
    uct_rm_urma_target_key_t key = {}, changed;
    uint64_t a_ctx, b_ctx;
    unsigned before, i;

    memset(&mock, 0, sizeof(mock));
    assert(ucs_async_context_init(&async, UCS_ASYNC_MODE_POLL) == UCS_OK);
    assert(ucs_callbackq_init(&worker.super.progress_q) == UCS_OK);
    iface.super.super.worker = &worker;
    worker.async = &async;
    iface.super.super.err_handler = on_error;
    iface.super.super.super.ops = uct_rm_urma_iface_tl_ops;
    iface.super.config.max_inline = 128;
    iface.super.config.seg_size = 2048;
    iface.super.config.tx_max_wr = 3;
    iface.super.tx.jfc_available = 64;
    iface.super.tx.reads_available = 1048576;
    iface.rm_urma_jetty.jetty = &local;
    iface.rm_urma_jetty.available = 2;
    ucs_ptr_array_init(&iface.super.eps, "target-EPs");
    ucs_list_head_init(&iface.tx.retired_eps);
    kh_init_inplace(uct_rm_urma_targets, &iface.targets);
    ucs_arbiter_init(&iface.tx.arbiter);
    pool_init(&iface.super.tx.mp, sizeof(uct_ub_iface_send_desc_t) + 2048, 1);
    pool_init(&iface.super.tx.short_desc_mp,
              sizeof(uct_ub_iface_send_desc_t) + 256, 1);
    pool_init(&iface.super.tx.send_op_mp, sizeof(uct_urma_iface_send_op_t), 0);
    a = new_unconnected_ep(&iface);
    b = new_unconnected_ep(&iface);
    c = new_unconnected_ep(&iface);

    test_ep_wire_address(&iface, a);

    assert(connect_remote(a, 101) == UCS_OK);
    assert(connect_remote(b, 102) == UCS_OK);
    target = a->target;
    assert(a->target == b->target && target->references == 2);
    assert(a->tjetty == b->tjetty && a->ka_tseg != b->ka_tseg);
    assert(a->super.dep_id == 101 && b->super.dep_id == 102);
    assert(mock.imports == 1 && mock.seg_imports == 2);

    /* Failure on a second EP must not unimport the first EP's target. */
    mock.fail_seg_import = 1;
    assert(connect_remote(c, 103) == UCS_ERR_IO_ERROR);
    assert(c->target == NULL && c->tjetty == NULL && c->ka_tseg == NULL);
    assert(!(c->super.flags & UCT_RM_URMA_EP_FLAG_CONNECTED));
    assert(target->references == 2 && mock.unimports == 0);
    assert(connect_remote(c, 103) == UCS_OK);
    assert(target->references == 3 && mock.imports == 1);
    assert(connect_remote(c, 103) == UCS_ERR_INVALID_PARAM);
    assert(target->references == 3);
    uct_rm_urma_ep_destroy(&c->super.super.super);
    assert(target->references == 2);

    assert(send_short(a) == UCS_OK);
    a_ctx = mock.ctx;
    assert(send_short(b) == UCS_OK);
    b_ctx = mock.ctx;
    uct_rm_urma_ep_destroy(&a->super.super.super);
    complete(&iface, b_ctx);
    uct_rm_urma_iface_gc(&iface);
    assert(target->references == 2 && mock.unimports == 0);
    complete(&iface, a_ctx);
    uct_rm_urma_iface_gc(&iface);
    assert(target->references == 1 && mock.unimports == 0);
    assert(send_short(b) == UCS_OK && mock.target == target->jetty);
    complete(&iface, mock.ctx);
    uct_rm_urma_ep_destroy(&b->super.super.super);
    assert(mock.unimports == 1 && kh_size(&iface.targets) == 0);
    puts("PASS actual connect: one target import, independent EP/segment ownership, CQE-delayed last release");

    c = new_unconnected_ep(&iface);
    before = mock.imports;
    fail_target_alloc = 1;
    assert(connect_remote(c, 201) == UCS_ERR_NO_MEMORY);
    assert(c->target == NULL && kh_size(&iface.targets) == 0);
    assert(mock.imports == before);
    mock.fail_import = 1;
    assert(connect_remote(c, 201) == UCS_ERR_IO_ERROR);
    assert(c->target == NULL && c->tjetty == NULL);
    assert(kh_size(&iface.targets) == 0);
    mock.fail_seg_import = 1;
    assert(connect_remote(c, 201) == UCS_ERR_IO_ERROR);
    assert(c->target == NULL && c->ka_tseg == NULL);
    assert(kh_size(&iface.targets) == 0);
    before = mock.imports;
    assert(connect_remote(c, 201) == UCS_OK);
    assert(mock.imports == before + 1);
    uct_rm_urma_ep_destroy(&c->super.super.super);
    puts("PASS target/segment import failure rollback and retry without a stale cache entry");

    key.remote.jetty_id.id = 515;
    key.remote.trans_mode = URMA_TM_RM;
    key.remote.type = URMA_JETTY;
    key.remote.tp_type = URMA_CTP;
    assert(uct_rm_urma_iface_target_get(&iface, &key.remote, &key.token,
                                       &target) == UCS_OK);
    for (i = 0; i < 9; ++i) {
        changed = key;
        switch (i) {
        case 0: changed.remote.jetty_id.eid.raw[0] = 1; break;
        case 1: changed.remote.jetty_id.uasid = 1; break;
        case 2: changed.remote.jetty_id.id = 516; break;
        case 3: changed.remote.trans_mode = URMA_TM_RC; break;
        case 4: changed.remote.policy = 1; break;
        case 5: changed.remote.type = URMA_JETTY_GROUP; break;
        case 6: changed.remote.flag.value = 1; break;
        case 7: changed.remote.tp_type = URMA_UTP; break;
        case 8: changed.token.token = 1; break;
        }
        assert(!uct_rm_urma_target_equal(&key, &changed));
        assert(uct_rm_urma_iface_target_get(&iface, &changed.remote,
                                           &changed.token, &again) == UCS_OK);
        assert(again != target);
        uct_rm_urma_iface_target_put(&iface, again);
    }
    before = mock.imports;
    for (i = 0; i < 128; ++i) {
        changed = key;
        changed.remote.jetty_id.id = 1000 + i;
        assert(uct_rm_urma_iface_target_get(&iface, &changed.remote,
                                           &changed.token,
                                           &targets[i]) == UCS_OK);
    }
    assert(uct_rm_urma_iface_target_get(&iface, &key.remote, &key.token,
                                       &again) == UCS_OK);
    assert(again == target && target->references == 2);
    assert(mock.imports == before + 128);
    uct_rm_urma_iface_target_put(&iface, again);
    for (i = 0; i < 128; ++i) {
        uct_rm_urma_iface_target_put(&iface, targets[i]);
    }
    target->references = UINT_MAX;
    assert(uct_rm_urma_iface_target_get(&iface, &key.remote, &key.token,
                                       &again) == UCS_ERR_NO_RESOURCE);
    assert(again == NULL);
    target->references = 1;
    uct_rm_urma_iface_target_put(&iface, target);
    assert(kh_size(&iface.targets) == 0);
    puts("PASS full 128-bit EID/import identity, stable targets across rehash and reference overflow guard");

    assert(uct_rm_urma_iface_target_get(&iface, &key.remote, &key.token,
                                       &target) == UCS_OK);
    before = mock.imports;
    iface.tx.error = UCS_ERR_IO_ERROR;
    assert(uct_rm_urma_iface_target_get(&iface, &key.remote, &key.token,
                                       &again) == UCS_ERR_IO_ERROR);
    assert(mock.imports == before && again == NULL);
    iface.tx.error = UCS_OK;
    mock.fail_unimport = 1;
    uct_rm_urma_iface_target_put(&iface, target);
    assert(target->references == 0 && kh_size(&iface.targets) == 1);
    assert(uct_rm_urma_iface_target_get(&iface, &key.remote, &key.token,
                                       &again) == UCS_ERR_IO_ERROR);
    assert(mock.imports == before && again == NULL);
    uct_rm_urma_iface_targets_cleanup(&iface);
    puts("PASS failed iface rejection and failed-unimport quarantine/close retry (CPU injection only)");

    assert(mock.seg_unimports == 4);
    assert(mock.live_targets == 0 && mock.live_segments == 0);

    kh_init_inplace(uct_rm_urma_targets, &iface.targets);
    pool_init(&iface.tx.pending_mp, sizeof(uct_rm_urma_fc_pending_req_t), 0);
    iface.super.rx.jfr = &jfr;
    a = new_unconnected_ep(&iface);
    b = new_unconnected_ep(&iface);
    assert(connect_remote(a, 301) == UCS_OK);
    assert(connect_remote(b, 302) == UCS_OK);
    target = b->target;
    assert(send_short(b) == UCS_OK);
    uct_rm_urma_ep_destroy(&a->super.super.super);
    uct_rm_urma_ep_destroy(&b->super.super.super);
    assert(target->references == 1 && mock.live_targets == 1);
    mock.require_quiesce = 1;
    _UCS_CLASS_CLEANUP_NAME(uct_rm_urma_iface_t)(&iface);
    assert(mock.hardware_stopped && iface.rm_urma_jetty.jetty == NULL);
    assert(ucs_list_is_empty(&iface.tx.retired_eps));
    assert(mock.live_targets == 0 && mock.live_segments == 0);
    puts("PASS actual iface cleanup: stop shared Jetty before releasing retired EP target/segment");

    ucs_ptr_array_cleanup(&iface.super.eps, 1);
    ucs_mpool_cleanup(&iface.super.tx.mp, 1);
    ucs_mpool_cleanup(&iface.super.tx.short_desc_mp, 1);
    ucs_mpool_cleanup(&iface.super.tx.send_op_mp, 1);
    ucs_callbackq_cleanup(&worker.super.progress_q);
    ucs_async_context_cleanup(&async);
}

typedef struct {
    uct_rm_urma_iface_t iface;
    uct_priv_worker_t worker;
    ucs_async_context_t async;
    uct_ub_md_t md;
    urma_jetty_t local;
    urma_jfr_t jfr;
} control_fixture_t;

static void control_fixture_init(control_fixture_t *f, unsigned max_inline)
{
    uct_rm_urma_iface_t *iface = &f->iface;

    memset(f, 0, sizeof(*f));
    memset(&mock, 0, sizeof(mock));
    assert(ucs_async_context_init(&f->async, UCS_ASYNC_MODE_POLL) == UCS_OK);
    assert(ucs_callbackq_init(&f->worker.super.progress_q) == UCS_OK);
    f->worker.async = &f->async;
    iface->super.super.worker = &f->worker;
    iface->super.super.md = &f->md.super;
    iface->super.super.err_handler = on_error;
    iface->super.super.am[0].cb = on_am;
    iface->super.super.super.ops = uct_rm_urma_iface_tl_ops;
    iface->super.config.max_inline = max_inline;
    iface->super.config.seg_size = 2048;
    iface->super.config.tx_max_wr = 3;
    iface->super.config.fc_enabled = 1;
    iface->super.config.fc_wnd_size = 8;
    iface->super.tx.jfc_available = 64;
    iface->super.tx.reads_available = 1048576;
    iface->super.rx.jfr = &f->jfr;
    iface->rm_urma_jetty.jetty = &f->local;
    iface->rm_urma_jetty.available = 2;
    ucs_ptr_array_init(&iface->super.eps, "control-EPs");
    ucs_list_head_init(&iface->tx.retired_eps);
    kh_init_inplace(uct_rm_urma_targets, &iface->targets);
    ucs_arbiter_init(&iface->tx.arbiter);
    pool_init(&iface->super.tx.mp, sizeof(uct_ub_iface_send_desc_t) + 2048, 1);
    pool_init(&iface->super.tx.short_desc_mp,
              sizeof(uct_ub_iface_send_desc_t) + 256, 1);
    pool_init(&iface->super.tx.send_op_mp, sizeof(uct_urma_iface_send_op_t), 0);
    pool_init(&iface->tx.pending_mp, sizeof(uct_rm_urma_fc_pending_req_t), 0);
}

static void control_fixture_cleanup(control_fixture_t *f)
{
    uct_rm_urma_iface_t *iface = &f->iface;
    uct_rm_urma_ep_t *ep;
    unsigned i;

    for (i = 0; i < iface->super.eps.size; ++i) {
        if (__ucs_ptr_array_is_free(iface->super.eps.start[i])) {
            continue;
        }
        ep = (uct_rm_urma_ep_t *)iface->super.eps.start[i];
        if (ep != iface->tx.ctrl_ep) {
            uct_rm_urma_ep_destroy(&ep->super.super.super);
        }
    }
    /* The real RM cleanup must stop DMA before canceling in-flight grants. */
    mock.require_quiesce = 1;
    _UCS_CLASS_CLEANUP_NAME(uct_rm_urma_iface_t)(iface);
    assert(mock.hardware_stopped && iface->tx.ctrl_ep == NULL);
    assert(mock.live_targets == 0 && mock.live_segments == 0);
    assert(ucs_list_is_empty(&iface->tx.retired_eps));
    for (i = 0; i < iface->super.eps.size; ++i) {
        assert(__ucs_ptr_array_is_free(iface->super.eps.start[i]));
    }
    ucs_ptr_array_cleanup(&iface->super.eps, 1);
    ucs_mpool_cleanup(&iface->super.tx.mp, 1);
    ucs_mpool_cleanup(&iface->super.tx.short_desc_mp, 1);
    ucs_mpool_cleanup(&iface->super.tx.send_op_mp, 1);
    ucs_callbackq_cleanup(&f->worker.super.progress_q);
    ucs_async_context_cleanup(&f->async);
}

static void test_control_sender(void)
{
    control_fixture_t f;
    uct_rm_urma_iface_t *iface = &f.iface;
    uct_rm_urma_ep_t *a, *b, *ctrl;
    urma_rjetty_t remote = {
        .jetty_id = { .id = 111, .uasid = 17 },
        .trans_mode = URMA_TM_RM,
        .type = URMA_JETTY,
        .tp_type = URMA_CTP
    };
    urma_token_t token = {};
    uint64_t first, second, header;
    unsigned inline_mode, scenario, i, free_before;
    void *held[16];

    for (inline_mode = 0; inline_mode < 2; ++inline_mode) {
        for (scenario = 0; scenario < 15; ++scenario) {
            control_fixture_init(&f, inline_mode ? 128 : 0);
            free_before = pool_free_count(&iface->tx.pending_mp);
            remote.jetty_id.id = 111;
            remote.jetty_id.eid.raw[0] = 0x45;
            if (scenario == 0) {
                /* No business EP exists: full destination identity suffices. */
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                                      &token, 31) == UCS_OK);
                first = mock.ctx;
                header = mock.header_addr;
                ctrl = iface->tx.ctrl_ep;
                assert(ctrl != NULL && mock.target->id.id == 111);
                remote.jetty_id.id = 222;
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                                      &token, 42) == UCS_OK);
                second = mock.ctx;
                assert(ctrl == iface->tx.ctrl_ep && mock.target->id.id == 222);
                assert((first >> 32) == ctrl->super.ep_id);
                assert((second >> 32) == ctrl->super.ep_id);
                assert(ctrl->target == NULL && ctrl->tjetty == NULL);
                assert(mock.live_targets == 2 && mock.live_segments == 0);
                if (!inline_mode) {
                    assert(header != mock.header_addr);
                    assert(((uct_ub_hdr_t *)(uintptr_t)header)->ep_id == 31);
                    assert(((uct_ub_hdr_t *)(uintptr_t)mock.header_addr)->ep_id == 42);
                }
                complete(iface, first);
                complete(iface, second);
                assert(mock.unimports == 2 && mock.errors == 0);
            } else if ((scenario == 1) || (scenario == 2)) {
                a = new_ep(iface, 31);
                if (scenario == 1) {
                    mock.fail_next = 1;
                    mock.fail_status = URMA_EAGAIN;
                }
                assert(uct_rm_urma_iface_send_fc_grant(iface,
                           &a->target->key.remote, &a->target->key.token, 31) == UCS_OK);
                first = mock.ctx;
                header = mock.header_addr;
                assert(a->target->references == 2);
                uct_rm_urma_ep_destroy(&a->super.super.super);
                assert(mock.live_targets == 1 && mock.unimports == 0);
                b = new_ep(iface, 88); /* May reuse the destroyed business EP ID. */
                if (scenario == 1) {
                    uct_rm_urma_iface_progress_pending(iface);
                    first = mock.ctx;
                    header = mock.header_addr;
                }
                assert(mock.target != b->tjetty);
                if (!inline_mode) {
                    assert(((uct_ub_hdr_t *)(uintptr_t)header)->ep_id == 31);
                }
                complete(iface, first);
                assert(mock.unimports == 1 && b->target->references == 1);
            } else if (scenario == 3) {
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                                      &token, 31) == UCS_OK);
                iface->rm_urma_jetty.available = 0;
                remote.jetty_id.id = 222;
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                                      &token, 42) == UCS_OK);
                assert(mock.posts == 1 && mock.live_targets == 2);
                assert(ucs_arbiter_group_num_elems(&iface->tx.ctrl_ep->arb_group) == 1);
                /* Cleanup below has both a posted grant and a pending grant. */
            } else if (scenario == 4) {
                urma_cr_t cr = { .status = URMA_CR_ACK_TIMEOUT_ERR };
                a = new_ep(iface, 31);
                b = new_ep(iface, 42);
                assert(uct_rm_urma_iface_send_fc_grant(iface,
                           &a->target->key.remote, &a->target->key.token, 31) == UCS_OK);
                cr.user_ctx = mock.ctx;
                iface->rm_urma_jetty.available = 0;
                assert(uct_rm_urma_iface_send_fc_grant(iface,
                           &b->target->key.remote, &b->target->key.token, 42) == UCS_OK);
                mock.destroy_on_error = 1;
                uct_rm_urma_deal_with_send_cr(iface, &cr, 1);
                assert(iface->tx.error == UCS_ERR_ENDPOINT_TIMEOUT);
                assert(mock.errors == 2); /* Never call the user with ctrl_ep. */
            } else if (scenario == 5) {
                mock.fail_import = 1;
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                    &token, 31) == UCS_ERR_IO_ERROR);
                assert(iface->tx.ctrl_ep == NULL && mock.live_targets == 0);
                fail_target_alloc = 1;
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                    &token, 31) == UCS_ERR_NO_MEMORY);
                assert(pool_free_count(&iface->tx.pending_mp) == free_before);
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                                      &token, 31) == UCS_OK);
                complete(iface, mock.ctx);
            } else if (scenario == 6) {
                if (!inline_mode) {
                    for (i = 0; i < 16; ++i) {
                        held[i] = ucs_mpool_get(&iface->super.tx.short_desc_mp);
                    }
                    iface->super.tx.short_desc_mp.data->quota = 0;
                } else {
                    iface->rm_urma_jetty.available = 0;
                }
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                                      &token, 31) == UCS_OK);
                assert(mock.posts == 0);
                assert(uct_rm_urma_iface_flush(&iface->super.super.super,
                                                0, NULL) == UCS_INPROGRESS);
                if (!inline_mode) {
                    for (i = 0; i < 16; ++i) {
                        ucs_mpool_put(held[i]);
                    }
                } else {
                    iface->rm_urma_jetty.available = 2;
                }
                uct_rm_urma_iface_progress_pending(iface);
                assert(mock.posts == 1);
                complete(iface, mock.ctx);
                assert(uct_rm_urma_iface_flush(&iface->super.super.super,
                                                0, NULL) == UCS_OK);
            } else if (scenario == 7) {
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                                      &token, 31) == UCS_OK);
                complete(iface, mock.ctx);
                ctrl = iface->tx.ctrl_ep;
                ctrl->super.status.pi = ctrl->super.status.ci = UINT32_MAX;
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                                      &token, 31) == UCS_OK);
                assert((uint32_t)mock.ctx == 0);
                complete(iface, mock.ctx);
                assert(ctrl->super.status.pi == 0 && ctrl->super.status.ci == 0);
            } else if (scenario == 8) {
                mock.fail_next = 1;
                mock.fail_status = URMA_EINVAL;
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                    &token, 31) == UCS_ERR_IO_ERROR);
                assert(mock.posts == 0 && mock.live_targets == 0);
                assert(pool_free_count(&iface->tx.pending_mp) == free_before);
            } else if (scenario == 9) {
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                    &token, 65536) == UCS_ERR_INVALID_PARAM);
                remote.trans_mode = URMA_TM_RC;
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                    &token, 31) == UCS_ERR_INVALID_PARAM);
                remote.trans_mode = URMA_TM_RM;
                iface->tx.error = UCS_ERR_IO_ERROR;
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                    &token, 31) == UCS_ERR_IO_ERROR);
                assert(iface->tx.ctrl_ep == NULL && mock.imports == 0);
            } else if (scenario == 10) {
                uint64_t contexts[64];
                unsigned head = 0, tail = 0, posted;
                for (i = 0; i < 32; ++i) {
                    posted = mock.posts;
                    assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                                          &token, 31) == UCS_OK);
                    if (mock.posts != posted) {
                        contexts[tail++] = mock.ctx;
                    }
                }
                assert(mock.imports == 1 && mock.live_targets == 1);
                while (head < tail) {
                    posted = mock.posts;
                    complete(iface, contexts[head++]);
                    if (mock.posts != posted) {
                        contexts[tail++] = mock.ctx;
                    }
                }
                assert(mock.posts == 32 && mock.unimports == 1);
                assert(pool_free_count(&iface->tx.pending_mp) >= free_before);
            } else if (scenario == 11) {
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                                      &token, 31) == UCS_OK);
                mock.fail_unimport = 1;
                complete(iface, mock.ctx);
                assert(mock.live_targets == 1);
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                    &token, 31) == UCS_ERR_IO_ERROR);
            } else if (scenario == 12) {
                assert(uct_rm_urma_iface_send_fc_grant(iface, &remote,
                                                      &token, 31) == UCS_OK);
                complete(iface, mock.ctx);
                ctrl = iface->tx.ctrl_ep;
                /* A recycled local EP index must not turn a late business
                 * request into a request on the private control owner. */
                receive_fc(iface, ctrl, UCT_RM_URMA_EP_FLAG_FC_HARD_REQ);
                receive_fc(iface, ctrl, UCT_RM_URMA_EP_FLAG_FC_SOFT_REQ);
                receive_fc(iface, ctrl, UCT_RM_URMA_EP_FC_PURE_GRANT);
                assert(mock.posts == 1 && mock.am_calls == 0);
                assert(ctrl->super.fc.fc_wnd == INT16_MAX);
            } else {
                a = new_ep(iface, 31);
                b = new_ep(iface, 42);
                iface->rm_urma_jetty.available = 0;
                receive_fc(iface, a, UCT_RM_URMA_EP_FLAG_FC_HARD_REQ);
                receive_fc(iface, b, UCT_RM_URMA_EP_FLAG_FC_HARD_REQ);
                ctrl = iface->tx.ctrl_ep;
                assert(ucs_arbiter_group_num_elems(&ctrl->arb_group) == 2);
                if (scenario == 13) {
                    uct_rm_urma_ep_destroy(&a->super.super.super);
                    assert(mock.unimports == 1);
                } else {
                    uct_rm_urma_ep_pending_purge(&a->super.super.super, NULL, NULL);
                    assert(a->target->references == 1);
                }
                assert(ucs_arbiter_group_num_elems(&ctrl->arb_group) == 1);
                iface->rm_urma_jetty.available = 2;
                uct_rm_urma_iface_progress_pending(iface);
                assert(mock.posts == 1 && mock.target == b->tjetty);
                first = mock.ctx;
                assert(b->target->references == 2);
                uct_rm_urma_ep_destroy(&b->super.super.super);
                complete(iface, first);
                assert(mock.live_targets == (scenario == 13 ? 0 : 1));
            }
            control_fixture_cleanup(&f);
        }
    }
    puts("PASS iface control sender: 30 CPU cases, inline/non-inline, no business EP,");
    puts("     independent target/CQE lifetime, pending/flush, failure/close and wrap");
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
    kh_init_inplace(uct_rm_urma_targets, &iface.targets);
    ucs_arbiter_init(&iface.tx.arbiter);
    pool_init(&iface.super.tx.mp, sizeof(uct_ub_iface_send_desc_t) + 2048, 1);
    pool_init(&iface.super.tx.short_desc_mp, sizeof(uct_ub_iface_send_desc_t) + 256, 1);
    pool_init(&iface.super.tx.send_op_mp, sizeof(uct_urma_iface_send_op_t), 0);
    pool_init(&iface.tx.pending_mp, sizeof(uct_rm_urma_fc_pending_req_t), 0);
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
    iface.super.config.fc_enabled = 1;
    assert(uct_rm_urma_iface_send_fc_grant(&iface, &a->target->key.remote,
                                         &a->target->key.token, 33) == UCS_OK);
    a1 = mock.ctx; fc_a = mock.header_addr;
    assert(uct_rm_urma_iface_send_fc_grant(&iface, &b->target->key.remote,
                                         &b->target->key.token, 22) == UCS_OK);
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
    uct_rm_urma_ep_destroy(&iface.tx.ctrl_ep->super.super.super);
    iface.tx.ctrl_ep = NULL;
    ucs_arbiter_cleanup(&iface.tx.arbiter);
    assert(kh_size(&iface.targets) == 0);
    uct_rm_urma_iface_targets_cleanup(&iface);
    assert(mock.live_targets == 0 && mock.live_segments == 0);
    ucs_ptr_array_cleanup(&iface.super.eps, 1);
    ucs_mpool_cleanup(&iface.super.tx.mp, 1);
    ucs_mpool_cleanup(&iface.super.tx.short_desc_mp, 1);
    ucs_mpool_cleanup(&iface.super.tx.send_op_mp, 1);
    ucs_callbackq_cleanup(&worker.super.progress_q);
    ucs_mpool_cleanup(&iface.tx.pending_mp, 1);
    puts("PASS synthetic CQE isolation and cleanup; MOCK ONLY, no hardware validation");
    test_fc_errors();
    test_target_reuse();
    test_control_sender();
    return 0;
}
