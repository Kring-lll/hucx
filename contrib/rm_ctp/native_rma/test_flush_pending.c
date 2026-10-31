/* Reproduce flush overtaking an unfinished fragmented GET using actual RM C.
 * URMA DMA/CQE are mocked; this is not a device READ or retransmission test. */
#include <unistd.h>
#define main prior_rm_test_main
#include "../r1/test_shared_tx.c"
#undef main

enum { FRAGMENTS = 16, FRAGMENT_BYTES = 4096, GUARD_BYTES = 128,
       WHOLE_BYTES = FRAGMENTS * FRAGMENT_BYTES + 2 * GUARD_BYTES };

typedef struct {
    control_fixture_t control;
    uct_rm_urma_ep_t *ep;
    uct_ub_rkey_info_t key;
    urma_target_seg_t remote;
    unsigned char src[WHOLE_BYTES], dst[WHOLE_BYTES];
    uct_pending_req_t get_pending, flush_pending;
    uct_completion_t completion;
    uint64_t contexts[FRAGMENTS];
    unsigned posted, flush_calls, flush_seen_posted, flush_seen_unpacked;
} fragmented_t;

static void fragmented_unpack(void *arg, const void *data, size_t length)
{
    memcpy(arg, data, length);
}

static ucs_status_t fragment_progress(uct_pending_req_t *req)
{
    fragmented_t *f = ucs_container_of(req, fragmented_t, get_pending);
    size_t offset = GUARD_BYTES + f->posted * FRAGMENT_BYTES;
    ucs_status_t status;
    status = uct_rm_urma_ep_get_bcopy(&f->ep->super.super.super,
              fragmented_unpack, f->dst + offset, FRAGMENT_BYTES,
              (uint64_t)f->src + offset, (uct_rkey_t)&f->key, NULL);
    if (status != UCS_INPROGRESS) {
        return status;
    }
    f->contexts[f->posted++] = mock.ctx;
    return f->posted == FRAGMENTS ? UCS_OK : UCS_INPROGRESS;
}

static void flush_completion(uct_completion_t *comp)
{
    fragmented_t *f = ucs_container_of(comp, fragmented_t, completion);
    ++f->flush_calls;
    f->flush_seen_posted = f->posted;
    /* All destination fragments must have been unpacked before this callback. */
    for (unsigned i = 0; i < FRAGMENTS; ++i) {
        size_t offset = GUARD_BYTES + i * FRAGMENT_BYTES;
        if (!memcmp(f->src + offset, f->dst + offset, FRAGMENT_BYTES)) {
            ++f->flush_seen_unpacked;
        }
    }
}

static ucs_status_t flush_progress(uct_pending_req_t *req)
{
    fragmented_t *f = ucs_container_of(req, fragmented_t, flush_pending);
    ucs_status_t status = uct_rm_urma_ep_flush(&f->ep->super.super.super, 0,
                                              &f->completion);
    if (status == UCS_OK) {
        /* UCP accounts immediate completion itself, outside the UCT callback. */
        flush_completion(&f->completion);
        return UCS_OK;
    }
    return status == UCS_INPROGRESS ? UCS_OK : status;
}

static void fragmented_init(fragmented_t *f)
{
    memset(f, 0, sizeof(*f));
    control_fixture_init(&f->control, 128);
    f->control.iface.config.native_rma = 1;
    f->control.iface.super.config.seg_size = FRAGMENT_BYTES;
    ucs_mpool_cleanup(&f->control.iface.super.tx.mp, 1);
    pool_init(&f->control.iface.super.tx.mp,
              sizeof(uct_ub_iface_send_desc_t) + FRAGMENT_BYTES, 1);
    f->ep = new_ep(&f->control.iface, 42);
    f->ep->super.fc.fc_wnd = 0;
    f->control.iface.super.tx.reads_available = 2 * FRAGMENT_BYTES;
    f->remote.seg.ubva.va = (uint64_t)f->src;
    f->remote.seg.len = sizeof(f->src);
    f->key.remote_seg = f->remote.seg;
    f->key.import_tseg = &f->remote;
    f->get_pending.func = fragment_progress;
    f->flush_pending.func = flush_progress;
    f->completion.func = flush_completion;
    f->completion.count = 1;
    f->completion.status = UCS_OK;
    memset(f->dst, 0xa5, sizeof(f->dst));
    memset(f->src, 0xa5, sizeof(f->src));
    for (unsigned i = GUARD_BYTES; i < WHOLE_BYTES - GUARD_BYTES; ++i) {
        f->src[i] = (unsigned char)(i * 13u + (i >> 8));
    }
}

static void test_flush_tail(unsigned mode)
{
    fragmented_t f;
    ucs_status_t status;
    fragmented_init(&f);
    if (mode != 1) {
        assert(fragment_progress(&f.get_pending) == UCS_INPROGRESS);
        assert(fragment_progress(&f.get_pending) == UCS_INPROGRESS);
    } else {
        f.control.iface.super.tx.reads_available = 0;
    }
    assert(fragment_progress(&f.get_pending) == UCS_ERR_NO_RESOURCE);
    assert(uct_rm_urma_ep_pending_add(&f.ep->super.super.super, &f.get_pending, 0) == UCS_OK);
    status = uct_rm_urma_ep_flush(&f.ep->super.super.super, 0, &f.completion);
#ifdef EXPECT_OLD_FLUSH
    assert(status == (mode == 1 ? UCS_OK : UCS_INPROGRESS));
    if (mode != 1) {
        complete(&f.control.iface, f.contexts[0]);
        complete(&f.control.iface, f.contexts[1]);
        assert(f.flush_calls == 1 && f.flush_seen_posted < FRAGMENTS);
        printf("OLD_FLUSH_EARLY mode=%u posted_at_completion=%u expected=%u\n",
               mode, f.flush_seen_posted, FRAGMENTS);
    } else {
        puts("OLD_FLUSH_EARLY: empty hardware queue with pending GET returned UCS_OK");
    }
    /* Exit without a cleanup race after proving the old bug. */
#else
    assert(status == UCS_ERR_NO_RESOURCE);
    assert(f.flush_calls == 0 && f.completion.count == 1);
    assert(uct_rm_urma_iface_flush(&f.control.iface.super.super.super, 0, NULL) == UCS_INPROGRESS);
    assert(uct_rm_urma_ep_pending_add(&f.ep->super.super.super, &f.flush_pending, 0) == UCS_OK);
    if (mode == 1) {
        f.control.iface.super.tx.reads_available = 2 * FRAGMENT_BYTES;
        uct_rm_urma_iface_progress_pending(&f.control.iface);
    }
    for (unsigned done = 0; done < FRAGMENTS; ++done) {
        assert(f.posted > done);
        assert(f.flush_calls == 0);
        complete(&f.control.iface, f.contexts[done]);
    }
    assert(f.posted == FRAGMENTS && f.flush_calls == 1);
    assert(f.flush_seen_posted == FRAGMENTS && f.flush_seen_unpacked == FRAGMENTS);
    assert(!memcmp(f.src, f.dst, sizeof(f.src)));
    assert(f.control.iface.super.tx.reads_available == 2 * FRAGMENT_BYTES);
    assert(ucs_arbiter_group_is_empty(&f.ep->arb_group));
    assert(uct_rm_urma_ep_flush(&f.ep->super.super.super, 0, NULL) == UCS_OK);
    control_fixture_cleanup(&f.control);
    printf("PASS flush after 64KiB GET pending tail mode=%u fragments=16 guards=PASS\n", mode);
#endif
}

#ifndef EXPECT_OLD_FLUSH
static void test_flush_other_ep_and_cancel(void)
{
    fragmented_t f;
    uct_rm_urma_ep_t *other;
    fragmented_init(&f);
    f.control.iface.super.tx.reads_available = 0;
    assert(uct_rm_urma_ep_pending_add(&f.ep->super.super.super, &f.get_pending, 0) == UCS_OK);
    assert(uct_rm_urma_ep_flush(&f.ep->super.super.super, 0, NULL) == UCS_ERR_NO_RESOURCE);
    other = new_ep(&f.control.iface, 43);
    assert(uct_rm_urma_ep_flush(&other->super.super.super, 0, NULL) == UCS_OK);
    assert(uct_rm_urma_ep_flush(&f.ep->super.super.super,
                               UCT_FLUSH_FLAG_CANCEL, NULL) == UCS_OK);
    uct_rm_urma_ep_pending_purge(&f.ep->super.super.super, NULL, NULL);
    control_fixture_cleanup(&f.control);
    puts("PASS flush: other EP unaffected; CANCEL still bypasses unsent pending");
}
#endif

int main(void)
{
    test_flush_tail(0);
#ifndef EXPECT_OLD_FLUSH
    test_flush_tail(1);
    test_flush_other_ep_and_cancel();
#endif
    return 0;
}
