#include <urma_api.h>
#include <stdlib.h>
urma_status_t urma_post_jetty_send_wr(urma_jetty_t *jetty, urma_jfs_wr_t *wr, urma_jfs_wr_t **bad)
{
    if (jetty == NULL) abort();
    if (getenv("FAKE_ERROR")) {
        *bad = wr;
        return URMA_EAGAIN;
    }
    *bad = NULL;
    return URMA_SUCCESS;
}
