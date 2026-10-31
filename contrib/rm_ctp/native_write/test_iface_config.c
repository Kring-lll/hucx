/* Actual RM link_ready validation with URMA mocked; no device acceptance. */
#define main existing_rm_regression_main
#include "../r1/test_shared_tx.c"
#undef main
int main(void)
{
    control_fixture_t f;
    iface_v2_fixture_init(&f, 1, 128);
    f.iface.super.config.fc_enabled = 0;
    assert(uct_rm_urma_link_ready(&f.iface) == UCS_ERR_INVALID_PARAM);
    f.iface.super.config.fc_enabled = 1;
    f.iface.link.max_peers = 5;
    f.iface.super.config.rx_queue_depth = 64;
    f.iface.super.config.fc_wnd_size = 8;
    assert(uct_rm_urma_link_ready(&f.iface) == UCS_OK);
    assert(f.iface.link.rx_free == 12);
    control_fixture_cleanup(&f);
    puts("ACTUAL_RM_READY: FC_OFF_REJECTED FC_ON_R64_K5_W8_PASS CPU_ONLY");
    return 0;
}
