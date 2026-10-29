#include <urma_api.h>
#include <assert.h>
#include <stdlib.h>
#include <string.h>
int main(int argc, char **argv)
{
    unsigned count = argc > 1 ? (unsigned)atoi(argv[1]) : 1;
    assert(count > 0 && count <= 257);
    urma_sge_t sge[2] = {{.len = 4093}, {.len = 3}};
    urma_jfs_wr_t wr[257] = {0};
    for (unsigned n = 0; n < count; ++n) {
        wr[n].opcode = URMA_OPC_SEND;
        wr[n].send.src.sge = sge;
        wr[n].send.src.num_sge = 2;
        wr[n].flag.bs.inline_flag = n % 2;
        wr[n].next = n + 1 < count ? &wr[n + 1] : NULL;
    }
    if (getenv("FAKE_OTHER")) wr[0].opcode = URMA_OPC_WRITE;
    if (getenv("FAKE_INVALID")) wr[0].send.src.num_sge = 65;
    urma_jfs_wr_t before[257];
    memcpy(before, wr, sizeof(wr));
    urma_jetty_t jetty = {0};
    jetty.jetty_cfg.jfs_cfg.trans_mode = getenv("FAKE_UM") ? URMA_TM_UM : URMA_TM_RM;
    urma_jfs_wr_t *bad = NULL;
    urma_status_t status = urma_post_jetty_send_wr(&jetty, wr, &bad);
    assert(memcmp(before, wr, sizeof(wr)) == 0);
    assert(status == (getenv("FAKE_ERROR") ? URMA_EAGAIN : URMA_SUCCESS));
    assert(bad == (getenv("FAKE_ERROR") ? wr : NULL));
    return 0;
}
