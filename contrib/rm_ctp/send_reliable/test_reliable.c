/* CPU-only tests of the production RM SEND protocol. No simulated result is
 * evidence of device packet loss, device CQE semantics or performance. */
#define main prior_shared_main
#include "../r1/test_shared_tx.c"
#undef main
#include "../../../src/uct/urma/rm_urma/rm_urma_reliable.c"

static unsigned delivered;
static uint64_t expected_value;
static ucs_status_t ordered_receive(void *arg, void *buffer, size_t length,
                                     unsigned flags)
{
    uint64_t value;
    assert(flags == 0 && length == 8);
    memcpy(&value, buffer, 8);
    assert(value == expected_value++);
    ++delivered;
    return UCS_OK;
}

typedef struct {
    unsigned char data[4096];
    unsigned length;
    uint64_t ctx;
} saved_frame_t;

static saved_frame_t save_frame(void)
{
    saved_frame_t frame;
    memcpy(frame.data, mock.frame, mock.frame_length);
    frame.length = mock.frame_length;
    frame.ctx = mock.ctx;
    return frame;
}

static void deliver(uct_rm_urma_iface_t *iface, saved_frame_t *frame)
{
    urma_cr_t cr = { .completion_len = frame->length };
    assert(uct_rm_urma_iface_fc_handler(&iface->super, frame->data, &cr, 0) == UCS_OK);
}

static void fixture(control_fixture_t *f, unsigned id)
{
    iface_v2_fixture_init(f, id, 128);
    f->iface.link.mode = UCT_RM_URMA_LINK_RELIABLE_V3;
    f->iface.super.config.seg_size = 4096;
    ucs_mpool_cleanup(&f->iface.super.tx.mp, 1);
    pool_init(&f->iface.super.tx.mp, sizeof(uct_ub_iface_send_desc_t) + 4096, 1);
    f->iface.reliable.rto = ucs_time_from_sec(0.01);
    f->iface.reliable.timeout = ucs_time_from_sec(30);
    f->iface.reliable.max_retries = 4;
    assert(uct_rm_urma_link_ready(&f->iface) == UCS_OK);
    f->iface.super.super.am[1].cb = ordered_receive;
    f->iface.super.super.am[1].arg = NULL;
}

static void pair_cleanup(control_fixture_t *a, control_fixture_t *b)
{
    fixture_allow_other_live = 1;
    control_fixture_cleanup(a);
    fixture_allow_other_live = 0;
    control_fixture_cleanup(b);
}

static saved_frame_t ack(uct_rm_urma_iface_t *receiver)
{
    saved_frame_t frame;
    uct_rm_urma_reliable_progress(receiver, ucs_get_time());
    frame = save_frame();
    assert(frame.data[sizeof(uct_ub_hdr_t) + 5] == UCT_RM_URMA_REL_ACK);
    return frame;
}

static void test_loss_reorder_and_lifetime(void)
{
    control_fixture_t a, b;
    uct_rm_urma_ep_t *ep, *other;
    saved_frame_t first, second, reply, replay;
    ucs_time_t now;
    fixture(&a, 1);
    fixture(&b, 2);
    ep = iface_v2_new_ep(&a, &b);
    other = iface_v2_new_ep(&a, &b);
    assert(ep->peer == other->peer);
    delivered = 0;
    expected_value = 11;
    assert(uct_rm_urma_ep_am_short(&ep->super.super.super, 1, 11, NULL, 0) == UCS_OK);
    first = save_frame();
    assert(uct_rm_urma_ep_am_short(&other->super.super.super, 1, 12, NULL, 0) == UCS_OK);
    second = save_frame();
    /* First DATA lost; second arrives ahead of it and is not delivered. */
    deliver(&b.iface, &second);
    deliver(&b.iface, &second);
    assert(delivered == 0 && b.iface.reliable.discarded == 2);
    complete(&a.iface, first.ctx);
    complete(&a.iface, second.ctx);
    assert(ep->reliable_outstanding == 1);
    assert(uct_rm_urma_ep_flush(&ep->super.super.super, 0, NULL) == UCS_ERR_NO_RESOURCE);
    /* Receive replenishment debt MUST block even a cumulative ACK. */
    b.iface.reliable.rx_debt = 1;
    unsigned posts = mock.posts;
    uct_rm_urma_reliable_progress(&b.iface, ucs_get_time());
    assert(mock.posts == posts);
    b.iface.reliable.rx_debt = 0;
    reply = ack(&b.iface); /* ACK 0 lost, but its local CQE succeeds. */
    complete(&b.iface, reply.ctx);
    now = ucs_get_time() + a.iface.reliable.rto + 1;
    /* Limit the SQ so that exactly the oldest entry is retried. */
    a.iface.rm_urma_jetty.available = 2;
    uct_rm_urma_reliable_progress(&a.iface, now);
    replay = save_frame();
    assert(!memcmp(first.data, replay.data, first.length));
    deliver(&b.iface, &replay);
    assert(delivered == 1);
    reply = ack(&b.iface);
    /* ACK before retransmit CQE: cannot free DMA storage or destroy EP yet. */
    deliver(&a.iface, &reply);
    uct_rm_urma_ep_destroy(&ep->super.super.super);
    assert(!ucs_list_is_empty(&a.iface.tx.retired_eps));
    complete(&a.iface, replay.ctx);
    uct_rm_urma_iface_gc(&a.iface);
    assert(ucs_list_is_empty(&a.iface.tx.retired_eps));
    complete(&b.iface, reply.ctx);
    a.iface.rm_urma_jetty.available = 2;
    uct_rm_urma_reliable_progress(&a.iface, now + 1);
    replay = save_frame();
    assert(!memcmp(second.data, replay.data, second.length));
    deliver(&b.iface, &replay);
    assert(delivered == 2);
    complete(&a.iface, replay.ctx);
    reply = ack(&b.iface); /* Drop this ACK as well. */
    complete(&b.iface, reply.ctx);
    uct_rm_urma_reliable_progress(&a.iface, now + a.iface.reliable.rto + 2);
    replay = save_frame();
    deliver(&b.iface, &replay); /* duplicate must trigger ACK without callback */
    assert(delivered == 2);
    reply = ack(&b.iface);
    deliver(&a.iface, &reply);
    complete(&a.iface, replay.ctx);
    complete(&b.iface, reply.ctx);
    assert(other->reliable_outstanding == 0 && other->peer->reliable.count == 0);
    assert(uct_rm_urma_ep_flush(&other->super.super.super, 0, NULL) == UCS_OK);
    assert(a.iface.reliable.retries == 3);
    pair_cleanup(&a, &b);
    puts("CPU_SEND_LOSS_REORDER_ACK_LOSS_EP_DESTROY_FLUSH=PASS");
}

static void test_reject_timeout_and_close(void)
{
    control_fixture_t a, b;
    uct_rm_urma_ep_t *ep;
    saved_frame_t data, response;
    uint8_t wire[48];
    uint64_t next;
    fixture(&a, 3);
    fixture(&b, 4);
    ep = iface_v2_new_ep(&a, &b);
    a.iface.super.config.seg_size = 2048;
    assert(uct_rm_urma_link_ready(&a.iface) == UCS_ERR_INVALID_PARAM);
    a.iface.super.config.seg_size = 4096;
    assert(uct_rm_urma_link_ready(&a.iface) == UCS_OK);
    assert(uct_rm_urma_link_get_address(&b.iface.super.super.super,
                                      (uct_iface_addr_t *)wire) == UCS_OK);
    uct_rm_urma_iface_addr_v2_t decoded;
    assert(uct_rm_urma_iface_addr_v2_unpack(wire, 48, &decoded) != UCS_OK);
    wire[28] = 2;
    assert(uct_rm_urma_reliable_address_unpack(wire, 48, &decoded) != UCS_OK);
    next = ep->peer->reliable.tx_next;
    mock.fail_next = 1;
    assert(uct_rm_urma_ep_am_short(&ep->super.super.super, 1, 11, NULL, 0) == UCS_ERR_NO_RESOURCE);
    assert(ep->peer->reliable.tx_next == next && ep->reliable_outstanding == 0);
    assert(uct_rm_urma_ep_am_short(&ep->super.super.super, 1, 11, NULL, 0) == UCS_OK);
    data = save_frame();
    delivered = 0;
    expected_value = 11;
    deliver(&b.iface, &data);
    response = ack(&b.iface);
    /* Old-instance ACK must not open a current stream window. */
    response.data[sizeof(uct_ub_hdr_t) + 16] ^= 1;
    deliver(&a.iface, &response);
    assert(ep->reliable_outstanding == 1);
    complete(&b.iface, response.ctx);
    /* No CQE arrives. Timer must terminate without resubmitting live DMA. */
    unsigned posts = mock.posts;
    uct_rm_urma_reliable_progress(&a.iface, ucs_get_time() + a.iface.reliable.timeout + 1);
    assert(a.iface.tx.error == UCS_ERR_ENDPOINT_TIMEOUT && mock.posts == posts);
    assert(ep->reliable_outstanding == 1);
    pair_cleanup(&a, &b); /* Jetty deleted before that outstanding descriptor. */
    puts("CPU_VERSION_NORESOURCE_NO_PSN_HOLE_STALE_ACK_TIMEOUT_DMA_CLOSE=PASS");
}

static void test_retry_limit_and_credit(void)
{
    control_fixture_t a, b;
    uct_rm_urma_ep_t *ep;
    saved_frame_t frame;
    fixture(&a, 5);
    fixture(&b, 6);
    ep = iface_v2_new_ep(&a, &b);
    ep->peer->remote.iface.initial_credit = 1;
    assert(uct_rm_urma_ep_am_short(&ep->super.super.super, 1, 11, NULL, 0) == UCS_OK);
    frame = save_frame();
    assert(uct_rm_urma_ep_am_short(&ep->super.super.super, 1, 12, NULL, 0) == UCS_ERR_NO_RESOURCE);
    complete(&a.iface, frame.ctx);
    ucs_time_t now = ucs_get_time();
    for (unsigned i = 1; i <= 4; ++i) {
        uct_rm_urma_reliable_progress(&a.iface, now + i * (a.iface.reliable.rto + 1));
        frame = save_frame();
        complete(&a.iface, frame.ctx);
    }
    uct_rm_urma_reliable_progress(&a.iface, now + 5 * (a.iface.reliable.rto + 1));
    assert(a.iface.tx.error == UCS_ERR_ENDPOINT_TIMEOUT);
    pair_cleanup(&a, &b);
    puts("CPU_BOUNDED_WINDOW_RETRY_EXHAUSTION=PASS");
}


static size_t rel_pack_value(void *buffer, void *arg)
{
    memcpy(buffer, arg, 8);
    return 8;
}

static void test_bcopy_pending_and_terminal(void)
{
    control_fixture_t a, b;
    uct_rm_urma_ep_t *ep;
    saved_frame_t data, response;
    uint64_t value = 91;
    fixture(&a, 7);
    fixture(&b, 8);
    ep = iface_v2_new_ep(&a, &b);
    delivered = 0;
    expected_value = value;
    assert(uct_rm_urma_ep_am_bcopy(&ep->super.super.super, 1,
                                   rel_pack_value, &value, 0) == 8);
    data = save_frame();
    value = 999; /* UCT copied the user data: caller can immediately reuse it. */
    deliver(&b.iface, &data);
    response = ack(&b.iface);
    deliver(&a.iface, &response);
    assert(delivered == 1 && ep->reliable_outstanding == 1);
    /* Even an ACKed WR with a missing local CQE must have a finite deadline. */
    uct_rm_urma_reliable_progress(&a.iface, ucs_get_time() + a.iface.reliable.timeout + 1);
    assert(a.iface.tx.error == UCS_ERR_ENDPOINT_TIMEOUT);
    uct_rm_urma_reliable_progress(&b.iface, ucs_get_time() + b.iface.reliable.timeout + 1);
    assert(b.iface.tx.error == UCS_ERR_ENDPOINT_TIMEOUT);
    complete(&b.iface, response.ctx);
    pair_cleanup(&a, &b);

    fixture(&a, 9);
    fixture(&b, 10);
    ep = iface_v2_new_ep(&a, &b);
    mock.fail_next = 1;
    mock.fail_status = URMA_EINVAL;
    assert(uct_rm_urma_ep_am_short(&ep->super.super.super, 1, 1, NULL, 0) == UCS_ERR_IO_ERROR);
    assert(!ucs_list_is_empty(&a.iface.reliable.quarantine));
    assert(ep->peer->reliable.tx_next == 1 && ep->reliable_outstanding == 0);
    pair_cleanup(&a, &b);
    puts("CPU_BCOPY_COPY_OWNERSHIP_ACKED_CQE_TIMEOUT_TERMINAL_QUARANTINE=PASS");
}

static void test_peer_isolation_pending_and_wire(void)
{
    control_fixture_t a, b, c;
    uct_rm_urma_ep_t *ab, *ac;
    saved_frame_t one, two, response;
    fixture(&a, 11);
    fixture(&b, 12);
    fixture(&c, 13);
    ab = iface_v2_new_ep(&a, &b);
    ac = iface_v2_new_ep(&a, &c);
    assert(ab->peer != ac->peer);
    assert(uct_rm_urma_ep_am_short(&ab->super.super.super, 1, 21, NULL, 0) == UCS_OK);
    one = save_frame();
    assert(uct_rm_urma_ep_am_short(&ac->super.super.super, 1, 31, NULL, 0) == UCS_OK);
    two = save_frame();
    assert(!memcmp(one.data + sizeof(uct_ub_hdr_t) + 24,
                   two.data + sizeof(uct_ub_hdr_t) + 24, 8)); /* both PSN 1 */
    pending_t pending = { .req.func = pending_send, .ep = ab };
    ab->peer->remote.iface.initial_credit = 1;
    assert(uct_rm_urma_ep_pending_add(&ab->super.super.super, &pending.req, 0) == UCS_OK);
    ab->peer->remote.iface.initial_credit = 8;
    assert(uct_rm_urma_ep_am_short(&ab->super.super.super, 1, 99, NULL, 0) == UCS_ERR_NO_RESOURCE);
    assert(ab->peer->reliable.tx_next == 2);
    uct_rm_urma_iface_progress_pending(&a.iface);
    assert(pending.called == 1 && ucs_arbiter_group_is_empty(&ab->arb_group));
    assert(ab->peer->reliable.tx_next == 3);
    delivered = 0;
    expected_value = 31;
    deliver(&c.iface, &two);
    response = ack(&c.iface);
    complete(&a.iface, one.ctx);
    complete(&a.iface, two.ctx);
    deliver(&a.iface, &response);
    complete(&c.iface, response.ctx);
    assert(ab->reliable_outstanding == 2 && ac->reliable_outstanding == 0);
    /* Truncation and impossible cumulative ACK cannot mutate replay state. */
    assert(uct_rm_urma_reliable_receive(&a.iface, response.data + sizeof(uct_ub_hdr_t), 35) == UCS_ERR_INVALID_PARAM);
    memset(response.data + sizeof(uct_ub_hdr_t) + 24, 0xff, 8);
    assert(uct_rm_urma_reliable_receive(&a.iface, response.data + sizeof(uct_ub_hdr_t), 36) == UCS_ERR_INVALID_PARAM);
    fixture_allow_other_live = 1;
    control_fixture_cleanup(&a);
    control_fixture_cleanup(&b);
    fixture_allow_other_live = 0;
    control_fixture_cleanup(&c);
    puts("CPU_MULTI_PEER_PSN_AND_WINDOW_ISOLATION_MALFORMED_WIRE=PASS");
}

int main(void)
{
    test_loss_reorder_and_lifetime();
    test_reject_timeout_and_close();
    test_retry_limit_and_credit();
    test_bcopy_pending_and_terminal();
    test_peer_isolation_pending_and_wire();
    puts("RELIABLE_SEND_CPU=PASS SCOPE=SOFTWARE_ONLY NOT_DEVICE_OR_PERFORMANCE");
    return 0;
}
