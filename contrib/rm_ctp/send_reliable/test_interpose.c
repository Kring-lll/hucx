/* Local ELF forwarding check against the built transport DSO, no device. */
#define _GNU_SOURCE
#include <assert.h>
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <uct/urma/rm_urma/rm_urma_ep.h>

int main(int argc, char **argv)
{
    assert(argc == 2);
    void *handle = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (handle == NULL) {
        fprintf(stderr, "MODULE_OPEN_FAILED=%s\n", dlerror());
        return 2;
    }
    ucs_status_t (*handler)(uct_urma_iface_t *, void *, urma_cr_t *, unsigned);
    handler = dlsym(handle, "uct_rm_urma_iface_fc_handler");
    assert(handler != NULL);
    uct_rm_urma_iface_t iface = {0};
    unsigned char frame[sizeof(uct_ub_hdr_t) + UCT_RM_URMA_REL_PREFIX] = {0};
    uct_ub_hdr_t *hdr = (void *)frame;
    unsigned char *body = frame + sizeof(*hdr);
    urma_cr_t cr = { .completion_len = sizeof(frame) };
    hdr->am_id = UCT_RM_URMA_EP_FC_PURE_GRANT;
    body[4] = 3;
    body[5] = UCT_RM_URMA_REL_ACK;
    iface.link.mode = UCT_RM_URMA_LINK_RELIABLE_V3;
    iface.tx.error = UCS_ERR_CANCELED;
    /* Two software drops, then forward into real receive's error-state no-op. */
    for (unsigned i = 0; i < 3; ++i) {
        assert(handler(&iface.super, frame, &cr, 0) == UCS_OK);
    }
    puts("ACTUAL_DSO_LOCAL_INTERPOSITION_FORWARDING=PASS NO_DEVICE");
    return 0;
}
