/* Reproduce a provider invisible to the preload object's RTLD_NEXT scope. */
#define _GNU_SOURCE
#include <urma_api.h>
#include <dlfcn.h>
#include <assert.h>
#include <stdlib.h>
#include <string.h>
typedef urma_status_t (*post_fn)(urma_jetty_t *, urma_jfs_wr_t *, urma_jfs_wr_t **);
int main(int argc, char **argv)
{
    assert(argc == 2);
    void *handle = NULL;
    if (strcmp(argv[1], "missing") != 0) {
        handle = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
        assert(handle != NULL);
    }
    post_fn call;
    *(void **)(&call) = dlsym(RTLD_DEFAULT, "urma_post_jetty_send_wr");
    assert(call != NULL);
    urma_jetty_t jetty = {0};
    jetty.jetty_cfg.jfs_cfg.trans_mode = URMA_TM_RM;
    urma_sge_t sge = {.len = 4096};
    urma_jfs_wr_t wr = {0};
    wr.opcode = URMA_OPC_SEND;
    wr.send.src.sge = &sge;
    wr.send.src.num_sge = 1;
    urma_jfs_wr_t before = wr;
    urma_jfs_wr_t *bad = NULL;
    urma_status_t status = call(&jetty, &wr, &bad);
    assert(memcmp(&before, &wr, sizeof(wr)) == 0);
    assert(status == (getenv("FAKE_ERROR") ? URMA_EAGAIN : URMA_SUCCESS));
    assert(bad == (getenv("FAKE_ERROR") ? &wr : NULL));
    if (handle != NULL) dlclose(handle);
    return 0;
}
