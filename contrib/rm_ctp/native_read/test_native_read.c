/* Actual RM sources, mocked URMA. This is not device READ acceptance. */
#include <unistd.h>
#define main existing_rm_regression_main
#include "../r1/test_shared_tx.c"
#undef main

typedef struct {
    control_fixture_t fixture;
    uct_rm_urma_ep_t *ep;
    uct_ub_mem_t mem;
    uct_ub_rkey_info_t key;
    urma_target_seg_t local, remote;
    uct_urma_iface_send_op_t op;
    uct_completion_t completion;
    uct_iov_t iov;
    unsigned char src[96], dst[96];
} read_fixture_t;

static void read_init(read_fixture_t *r)
{
    memset(r, 0, sizeof(*r));
    control_fixture_init(&r->fixture, 128);
    r->fixture.iface.config.native_read = 1;
    r->fixture.iface.super.config.max_get_zcopy = 4096;
    r->fixture.iface.super.tx.reads_available = 64;
    r->ep = new_ep(&r->fixture.iface, 42);
    r->ep->super.fc.fc_wnd = 0;
    r->local.seg.ubva.va = (uint64_t)r->dst;
    r->local.seg.len = sizeof(r->dst);
    r->remote.seg.ubva.va = (uint64_t)r->src;
    r->remote.seg.len = sizeof(r->src);
    r->key.remote_seg = r->remote.seg;
    r->key.import_tseg = &r->remote;
    r->mem.tseg = &r->local;
    r->iov.buffer = r->dst + 3;
    r->iov.length = 64;
    r->iov.count = 1;
    r->iov.memh = &r->mem;
    r->op.flags = UCT_UB_IFACE_SEND_OP_FLAG_IFACE;
    r->op.iface = &r->fixture.iface.super;
    r->fixture.iface.super.tx.zcopy_free_ops = &r->op;
    r->completion.func = on_complete;
    r->completion.count = 1;
    r->completion.status = UCS_OK;
    memset(r->src, 0x5a, sizeof(r->src));
    memset(r->dst, 0xa5, sizeof(r->dst));
}

static ucs_status_t read_post(read_fixture_t *r, uct_completion_t *comp)
{
    return uct_rm_urma_ep_get_zcopy(&r->ep->super.super.super, &r->iov, 1,
                                    (uint64_t)r->src + 5,
                                    (uct_rkey_t)&r->key, comp);
}

static void test_read_completion(void)
{
    read_fixture_t r;
    for (unsigned mode = 0; mode < 5; ++mode) {
        read_init(&r);
        assert(read_post(&r, mode == 1 ? NULL : &r.completion) == UCS_INPROGRESS);
        assert(r.fixture.iface.super.tx.reads_available == 0);
        assert(r.completion.count == 1 && r.ep->super.fc.fc_wnd == 0);
        assert(memcmp(r.src + 5, r.dst + 3, 64) == 0);
        for (unsigned i = 0; i < sizeof(r.dst); ++i) {
            assert(r.dst[i] == ((i >= 3 && i < 67) ? 0x5a : 0xa5));
        }
        assert(uct_rm_urma_ep_flush(&r.ep->super.super.super, 0, NULL) == UCS_INPROGRESS);
        if (mode == 2) {
            assert(uct_rm_urma_ep_flush(&r.ep->super.super.super,
                        UCT_FLUSH_FLAG_CANCEL, NULL) == UCS_INPROGRESS);
            assert(r.completion.count == 1);
        }
        if (mode == 4) {
            uct_ep_destroy(&r.ep->super.super.super);
            assert(r.completion.count == 1);
        }
        if (mode == 3) {
            urma_cr_t cr = { .status = URMA_CR_LOC_LEN_ERR, .user_ctx = mock.ctx };
            uct_rm_urma_deal_with_send_cr(&r.fixture.iface, &cr, 1);
        } else {
            complete(&r.fixture.iface, mock.ctx);
        }
        assert(r.fixture.iface.super.tx.reads_available == 64);
        assert(r.fixture.iface.super.tx.reads_completed == 0);
        assert(r.fixture.iface.super.tx.zcopy_free_ops == &r.op);
        assert(r.completion.count == (mode == 1 ? 1 : 0));
        if (mode == 2 || mode == 3 || mode == 4) {
            assert(r.completion.status != UCS_OK);
        }
        control_fixture_cleanup(&r.fixture);
    }
    puts("PASS READ completion, NULL callback, cancel/error accounting and tail guards");
}

static void test_read_rejection(void)
{
    read_fixture_t r;
    read_init(&r);
    r.fixture.iface.config.native_read = 0;
    assert(read_post(&r, &r.completion) == UCS_ERR_UNSUPPORTED);
    r.fixture.iface.config.native_read = 1;
    r.fixture.iface.super.tx.reads_available = 63;
    assert(read_post(&r, &r.completion) == UCS_ERR_NO_RESOURCE);
    r.fixture.iface.super.tx.reads_available = 64;
    r.fixture.iface.super.tx.zcopy_free_ops = NULL;
    assert(read_post(&r, NULL) == UCS_ERR_NO_RESOURCE);
    r.fixture.iface.super.tx.zcopy_free_ops = &r.op;
    r.iov.buffer = r.dst + 33;
    assert(read_post(&r, NULL) == UCS_ERR_INVALID_PARAM);
    r.iov.buffer = r.dst + 3;
    r.iov.count = 2;
    assert(read_post(&r, NULL) == UCS_ERR_INVALID_PARAM);
    r.iov.count = 1;
    r.iov.memh = UCT_MEM_HANDLE_NULL;
    assert(read_post(&r, NULL) == UCS_ERR_INVALID_PARAM);
    r.iov.memh = &r.mem;
    r.key.remote_seg.len = 32;
    assert(read_post(&r, NULL) == UCS_ERR_INVALID_PARAM);
    r.key.remote_seg = r.remote.seg;
    r.iov.length = 4097;
    assert(read_post(&r, NULL) == UCS_ERR_INVALID_PARAM);
    r.iov.length = 0;
    assert(read_post(&r, &r.completion) == UCS_OK);
    assert(mock.posts == 0 && r.completion.count == 1);
    r.iov.length = 64;
    for (unsigned mode = 0; mode < 2; ++mode) {
        mock.fail_next = 1;
        mock.fail_status = mode ? URMA_EINVAL : URMA_EAGAIN;
        assert(read_post(&r, &r.completion) ==
               (mode ? UCS_ERR_IO_ERROR : UCS_ERR_NO_RESOURCE));
        assert(r.fixture.iface.super.tx.reads_available == 64);
        assert(r.fixture.iface.super.tx.zcopy_free_ops == &r.op);
        assert(r.ep->super.status.pi == r.ep->super.status.ci);
    }
    r.key.import_tseg = NULL;
    assert(pthread_rwlock_init(&r.key.import_tseg_lock, NULL) == 0);
    mock.fail_seg_import = 1;
    assert(read_post(&r, NULL) == UCS_ERR_IO_ERROR);
    assert(r.fixture.iface.super.tx.zcopy_free_ops == &r.op);
    pthread_rwlock_destroy(&r.key.import_tseg_lock);
    r.key.import_tseg = &r.remote;
    assert(mock.posts == 0 && r.completion.count == 1);
    control_fixture_cleanup(&r.fixture);
    puts("PASS READ disabled, byte budget, record reservation, bounds, zero and post/import rollback");
}

typedef struct {
    uct_pending_req_t req;
    read_fixture_t *read;
    unsigned calls;
} read_pending_t;

static ucs_status_t pending_read(uct_pending_req_t *req)
{
    read_pending_t *p = ucs_container_of(req, read_pending_t, req);
    ucs_status_t status;
    ++p->calls;
    status = read_post(p->read, NULL);
    return status == UCS_INPROGRESS ? UCS_OK : status;
}

static void test_read_pending(void)
{
    read_fixture_t r;
    read_pending_t pending = { .req.func = pending_read, .read = &r };
    pending_t am = { .req.func = pending_send };
    read_init(&r);
    /* Another EP owns all read bytes. This EP has no outstanding CQE or AM credit. */
    r.fixture.iface.super.tx.reads_available = 0;
    assert(uct_rm_urma_ep_pending_add(&r.ep->super.super.super, &pending.req, 0) == UCS_OK);
    alarm(2);
    uct_rm_urma_iface_progress_pending(&r.fixture.iface);
    alarm(0);
    assert(pending.calls <= 1);
    assert(!ucs_arbiter_group_is_empty(&r.ep->arb_group));
    r.fixture.iface.super.tx.reads_available = 64;
    uct_rm_urma_iface_progress_pending(&r.fixture.iface);
    assert(mock.posts == 1 && ucs_arbiter_group_is_empty(&r.ep->arb_group));
    complete(&r.fixture.iface, mock.ctx);
    /* READ cannot bypass an existing AM head, even when READ has resources. */
    am.ep = r.ep;
    assert(uct_rm_urma_ep_pending_add(&r.ep->super.super.super, &am.req, 0) == UCS_OK);
    assert(read_post(&r, NULL) == UCS_ERR_NO_RESOURCE);
    alarm(2);
    uct_rm_urma_iface_progress_pending(&r.fixture.iface);
    alarm(0);
    assert(am.called == 1);
    r.ep->super.fc.fc_wnd = 8;
    uct_rm_urma_iface_progress_pending(&r.fixture.iface);
    assert(ucs_arbiter_group_is_empty(&r.ep->arb_group));
    complete(&r.fixture.iface, mock.ctx);
    control_fixture_cleanup(&r.fixture);
    puts("PASS READ pending retry without AM credit, FIFO and dispatch return");
}

static void test_read_shared_completion(void)
{
    read_fixture_t r;
    uct_rm_urma_ep_t *other;
    uct_urma_iface_send_op_t second = {0};
    uct_completion_t other_done = { .func = on_complete, .count = 1, .status = UCS_OK };
    uint64_t first_ctx;
    read_init(&r);
    other = new_ep(&r.fixture.iface, 43);
    other->super.fc.fc_wnd = 0;
    second.flags = UCT_UB_IFACE_SEND_OP_FLAG_IFACE;
    second.iface = &r.fixture.iface.super;
    r.op.next = &second;
    r.fixture.iface.super.tx.reads_available = 128;
    assert(read_post(&r, &r.completion) == UCS_INPROGRESS);
    first_ctx = mock.ctx;
    assert(uct_rm_urma_ep_get_zcopy(&other->super.super.super, &r.iov, 1,
                                   (uint64_t)r.src + 5, (uct_rkey_t)&r.key,
                                   &other_done) == UCS_INPROGRESS);
    complete(&r.fixture.iface, mock.ctx);
    assert(other_done.count == 0 && r.completion.count == 1);
    assert(r.fixture.iface.super.tx.reads_available == 64);
    complete(&r.fixture.iface, first_ctx);
    assert(r.completion.count == 0);
    assert(r.fixture.iface.super.tx.reads_available == 128);
    control_fixture_cleanup(&r.fixture);
    puts("PASS READ shared Jetty: per-EP completions and byte credits do not cross");
}

int main(void)
{
    test_read_completion();
    test_read_rejection();
    test_read_pending();
    test_read_shared_completion();
    puts("NATIVE_READ_CPU_REGRESSION=PASS NOT_DEVICE_ACCEPTANCE");
    return 0;
}
