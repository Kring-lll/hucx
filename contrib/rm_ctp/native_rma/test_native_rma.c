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

static unsigned unpacked;
static void unpack_data(void *arg, const void *data, size_t length)
{
    memcpy(arg, data, length);
    ++unpacked;
}

static size_t pack_data(void *data, void *arg)
{
    memcpy(data, arg, 64);
    return 64;
}

static size_t pack_oversize(void *data, void *arg)
{
    (void)data;
    (void)arg;
    return 4097;
}

static void basic_init(read_fixture_t *r)
{
    read_init(r);
    r->fixture.iface.config.native_read = 0;
    r->fixture.iface.config.native_rma = 1;
    r->fixture.iface.super.config.max_inline = 128;
    unpacked = 0;
}

static void test_basic_completions(void)
{
    read_fixture_t r;
    for (unsigned mode = 0; mode < 5; ++mode) {
        basic_init(&r);
        assert(uct_rm_urma_ep_get_bcopy(&r.ep->super.super.super, unpack_data,
                r.dst + 3, 64, (uint64_t)r.src + 5,
                (uct_rkey_t)&r.key, mode == 1 ? NULL : &r.completion) == UCS_INPROGRESS);
        assert(unpacked == 0 && r.dst[3] == 0xa5);
        assert(r.fixture.iface.super.tx.reads_available == 0);
        assert(uct_rm_urma_ep_flush(&r.ep->super.super.super, 0, NULL) == UCS_INPROGRESS);
        if (mode == 2) {
            uct_rm_urma_ep_flush(&r.ep->super.super.super, UCT_FLUSH_FLAG_CANCEL, NULL);
        }
        if (mode == 3) {
            uct_ep_destroy(&r.ep->super.super.super);
        }
        if (mode == 4) {
            urma_cr_t cr = { .status = URMA_CR_LOC_LEN_ERR, .user_ctx = mock.ctx };
            uct_rm_urma_deal_with_send_cr(&r.fixture.iface, &cr, 1);
        } else {
            complete(&r.fixture.iface, mock.ctx);
        }
        assert(unpacked == (mode < 2 ? 1 : 0));
        assert(r.fixture.iface.super.tx.reads_available == 64);
        assert(r.completion.count == (mode == 1 ? 1 : 0));
        if (mode < 2) {
            assert(!memcmp(r.src + 5, r.dst + 3, 64));
        } else {
            assert(r.completion.status != UCS_OK && r.dst[3] == 0xa5);
        }
        assert(r.dst[2] == 0xa5 && r.dst[67] == 0xa5);
        control_fixture_cleanup(&r.fixture);
    }
    puts("PASS GET_BCOPY callback timing, NULL completion, cancel, destroy, error and byte return");
}

static void test_basic_writes(void)
{
    read_fixture_t r;
    for (unsigned mode = 0; mode < 2; ++mode) {
        basic_init(&r);
        memset(r.src, 0x44, sizeof(r.src));
        assert((mode ? uct_rm_urma_ep_put_bcopy(&r.ep->super.super.super,
                          pack_data, r.dst + 3, (uint64_t)r.src + 5, (uct_rkey_t)&r.key) :
                      uct_rm_urma_ep_put_short(&r.ep->super.super.super,
                          r.dst + 3, 64, (uint64_t)r.src + 5, (uct_rkey_t)&r.key)) ==
               (mode ? 64 : UCS_OK));
        assert(!memcmp(r.dst + 3, r.src + 5, 64));
        assert(r.src[4] == 0x44 && r.src[69] == 0x44);
        assert(r.ep->super.fc.fc_wnd == 0);
        assert(uct_rm_urma_ep_flush(&r.ep->super.super.super, 0, NULL) == UCS_INPROGRESS);
        complete(&r.fixture.iface, mock.ctx);
        assert(uct_rm_urma_ep_flush(&r.ep->super.super.super, 0, NULL) == UCS_OK);
        control_fixture_cleanup(&r.fixture);
    }
    puts("PASS inline/bcopy WRITE data, guards, no AM credit and local flush");
}

static void test_basic_failures(void)
{
    read_fixture_t r;
    basic_init(&r);
    for (unsigned op = 0; op < 3; ++op) {
        for (unsigned failure = 0; failure < 4; ++failure) {
            ssize_t status;
            uint64_t fence = r.ep->super.fi.fence_beat;
            mock.fail_next = failure < 2;
            mock.fail_status = failure ? URMA_EINVAL : URMA_EAGAIN;
            r.key.remote_seg.len = failure == 2 ? 32 : sizeof(r.src);
            r.fixture.iface.config.native_rma = failure != 3;
            if (op == 0) {
                status = uct_rm_urma_ep_put_short(&r.ep->super.super.super,
                            r.dst, 64, (uint64_t)r.src, (uct_rkey_t)&r.key);
            } else if (op == 1) {
                status = uct_rm_urma_ep_put_bcopy(&r.ep->super.super.super,
                            pack_data, r.dst, (uint64_t)r.src, (uct_rkey_t)&r.key);
            } else {
                status = uct_rm_urma_ep_get_bcopy(&r.ep->super.super.super,
                            unpack_data, r.dst, 64, (uint64_t)r.src,
                            (uct_rkey_t)&r.key, &r.completion);
            }
            assert(status == (failure == 0 ? UCS_ERR_NO_RESOURCE :
                              failure == 1 ? UCS_ERR_IO_ERROR :
                              failure == 2 ? UCS_ERR_INVALID_PARAM : UCS_ERR_UNSUPPORTED));
            assert(mock.posts == 0 && unpacked == 0 && r.completion.count == 1);
            assert(r.ep->super.status.pi == r.ep->super.status.ci);
            assert(r.ep->super.fi.fence_beat == fence);
            assert(r.fixture.iface.super.tx.reads_available == 64);
        }
    }
    r.fixture.iface.config.native_rma = 1;
    r.key.remote_seg.len = sizeof(r.src);
    assert(uct_rm_urma_ep_put_bcopy(&r.ep->super.super.super, pack_oversize,
                                    NULL, (uint64_t)r.src, (uct_rkey_t)&r.key) == UCS_ERR_INVALID_PARAM);
    assert(uct_rm_urma_ep_put_short(&r.ep->super.super.super, NULL, 0, 0, 0) == UCS_OK);
    assert(uct_rm_urma_ep_get_bcopy(&r.ep->super.super.super, NULL, NULL,
                                   0, 0, 0, NULL) == UCS_OK);
    r.fixture.iface.super.tx.reads_available = 63;
    assert(uct_rm_urma_ep_get_bcopy(&r.ep->super.super.super, unpack_data, r.dst,
                      64, (uint64_t)r.src, (uct_rkey_t)&r.key, NULL) == UCS_ERR_NO_RESOURCE);
    assert(mock.posts == 0);
    control_fixture_cleanup(&r.fixture);
    puts("PASS three basic ops disabled/bounds/temporary+permanent post rollback/zero/oversize/read budget");
}

static size_t full_pack(void *destination, void *arg)
{
    memcpy(destination, arg, 4096);
    return 4096;
}

static void test_full_fragment_and_fifo(void)
{
    read_fixture_t r;
    unsigned char source[4352], target[4352], result[4352];
    pending_t am = { .req.func = pending_send };
    void *held[16];
    unsigned count;
    basic_init(&r);
    ucs_mpool_cleanup(&r.fixture.iface.super.tx.mp, 1);
    pool_init(&r.fixture.iface.super.tx.mp,
              sizeof(uct_ub_iface_send_desc_t) + 4096, 1);
    r.fixture.iface.super.config.seg_size = 4096;
    memset(source, 0x5b, sizeof(source));
    memset(target, 0xa5, sizeof(target));
    memset(result, 0xa5, sizeof(result));
    r.key.remote_seg.ubva.va = (uint64_t)target;
    r.key.remote_seg.len = sizeof(target);
    assert(uct_rm_urma_ep_put_bcopy(&r.ep->super.super.super, full_pack,
            source + 128, (uint64_t)target + 128, (uct_rkey_t)&r.key) == 4096);
    complete(&r.fixture.iface, mock.ctx);
    r.fixture.iface.super.tx.reads_available = 4096;
    assert(uct_rm_urma_ep_get_bcopy(&r.ep->super.super.super, unpack_data,
            result + 128, 4096, (uint64_t)target + 128, (uct_rkey_t)&r.key,
            &r.completion) == UCS_INPROGRESS);
    complete(&r.fixture.iface, mock.ctx);
    for (unsigned i = 0; i < sizeof(target); ++i) {
        assert(target[i] == ((i >= 128 && i < 4224) ? 0x5b : 0xa5));
        assert(result[i] == target[i]);
    }
    count = pool_free_count(&r.fixture.iface.super.tx.mp);
    assert(count <= 16);
    for (unsigned i = 0; i < count; ++i) {
        held[i] = ucs_mpool_get(&r.fixture.iface.super.tx.mp);
    }
    r.fixture.iface.super.tx.mp.data->quota = 0;
    assert(uct_rm_urma_ep_put_bcopy(&r.ep->super.super.super, full_pack,
            source + 128, (uint64_t)target + 128, (uct_rkey_t)&r.key) == UCS_ERR_NO_RESOURCE);
    assert(uct_rm_urma_ep_get_bcopy(&r.ep->super.super.super, unpack_data,
            result + 128, 4096, (uint64_t)target + 128, (uct_rkey_t)&r.key,
            NULL) == UCS_ERR_NO_RESOURCE);
    for (unsigned i = 0; i < count; ++i) {
        ucs_mpool_put(held[i]);
    }
    am.ep = r.ep;
    assert(uct_rm_urma_ep_pending_add(&r.ep->super.super.super, &am.req, 0) == UCS_OK);
    assert(uct_rm_urma_ep_put_bcopy(&r.ep->super.super.super, full_pack,
            source + 128, (uint64_t)target + 128, (uct_rkey_t)&r.key) == UCS_ERR_NO_RESOURCE);
    alarm(2);
    uct_rm_urma_iface_progress_pending(&r.fixture.iface);
    alarm(0);
    assert(am.called == 1);
    r.ep->super.fc.fc_wnd = 8;
    uct_rm_urma_iface_progress_pending(&r.fixture.iface);
    complete(&r.fixture.iface, mock.ctx);
    control_fixture_cleanup(&r.fixture);
    puts("PASS full 4096B bcopy PUT/GET guards, descriptor exhaustion, FIFO and dispatch return");
}

static void test_basic_shared_ep_completion(void)
{
    read_fixture_t r;
    unsigned char second[96];
    uct_rm_urma_ep_t *other;
    uct_completion_t done = { .func = on_complete, .count = 1, .status = UCS_OK };
    uint64_t first_context;
    basic_init(&r);
    memset(second, 0xa5, sizeof(second));
    r.fixture.iface.super.tx.reads_available = 128;
    other = new_ep(&r.fixture.iface, 43);
    other->super.fc.fc_wnd = 0;
    assert(uct_rm_urma_ep_get_bcopy(&r.ep->super.super.super, unpack_data,
            r.dst + 3, 64, (uint64_t)r.src + 5, (uct_rkey_t)&r.key,
            &r.completion) == UCS_INPROGRESS);
    first_context = mock.ctx;
    assert(uct_rm_urma_ep_get_bcopy(&other->super.super.super, unpack_data,
            second + 3, 64, (uint64_t)r.src + 5, (uct_rkey_t)&r.key,
            &done) == UCS_INPROGRESS);
    complete(&r.fixture.iface, mock.ctx);
    assert(done.count == 0 && r.completion.count == 1 && unpacked == 1);
    assert(r.fixture.iface.super.tx.reads_available == 64);
    complete(&r.fixture.iface, first_context);
    assert(r.completion.count == 0 && unpacked == 2);
    assert(r.fixture.iface.super.tx.reads_available == 128);
    assert(!memcmp(second + 3, r.dst + 3, 64));
    assert(second[2] == 0xa5 && second[67] == 0xa5);
    control_fixture_cleanup(&r.fixture);
    puts("PASS GET_BCOPY shared Jetty reverse EP completions keep callback and credits separate");
}

int main(void)
{
    test_basic_completions();
    test_basic_writes();
    test_basic_failures();
    test_full_fragment_and_fifo();
    test_basic_shared_ep_completion();
    puts("NATIVE_RMA_CPU=PASS NOT_DEVICE_ACCEPTANCE");
    return 0;
}
