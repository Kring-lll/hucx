#include "urma_api.h"
#include <errno.h>
#include <stdlib.h>
static unsigned post_calls;
urma_status_t urma_post_jetty_send_wr(urma_jetty_t *j,urma_jfs_wr_t *w,urma_jfs_wr_t **bad)
{
 (void)j; ++post_calls;
 if(getenv("PROVIDER_ERROR")){*bad=w;errno=37;return URMA_EAGAIN;}
 *bad=NULL;errno=36;return URMA_SUCCESS;
}
int urma_poll_jfc(urma_jfc_t *j,int n,urma_cr_t *cr)
{(void)j;if(n<2)return -1;cr[0].status=0;cr[1].status=0;errno=35;return 2;}
unsigned provider_calls(void){return post_calls;}
