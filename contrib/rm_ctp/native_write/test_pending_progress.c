#include <unistd.h>
#define main existing_rm_regression_main
#include "../r1/test_shared_tx.c"
#undef main
int main(void)
{
    control_fixture_t f;
    uct_rm_urma_ep_t *ep;
    pending_t pending = { .req.func = pending_send };
    control_fixture_init(&f, 128);
    f.iface.config.native_write = 1;
    ep = new_ep(&f.iface, 1);
    ep->super.fc.fc_wnd = 0;
    pending.ep = ep;
    assert(uct_rm_urma_ep_pending_add(&ep->super.super.super, &pending.req, 0) == UCS_OK);
    puts("BEFORE_DISPATCH: queued AM without credit, native_write=y");
    fflush(stdout);
    alarm(2);
    uct_rm_urma_iface_progress_pending(&f.iface);
    alarm(0);
    assert(pending.called == 1);
    assert(!ucs_arbiter_group_is_empty(&ep->arb_group));
    ep->super.fc.fc_wnd = 8;
    uct_rm_urma_iface_progress_pending(&f.iface);
    assert(pending.called == 2 && mock.posts == 1);
    complete(&f.iface, mock.ctx);
    control_fixture_cleanup(&f);
    puts("PENDING_DISPATCH_RETURNS_AND_RECOVERS=PASS CPU_ONLY");
    return 0;
}
