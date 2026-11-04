/* Synthetic provider for probe forwarding/drop/duplicate tests; no device. */
#include <uct/urma/rm_urma/rm_urma_ep.h>
#include <ucs/datastruct/mpool.inl>
#include <stdlib.h>
#include <stdio.h>
#include <assert.h>
static unsigned delivered, freed;
void *ucs_mpool_get(ucs_mpool_t *mp)
{
    ucs_mpool_elem_t *e=calloc(1,sizeof(*e)+8192);
    if (!e) abort();e->mpool=mp;return e+1;
}
void ucs_mpool_put(void *p) { ++freed;free((ucs_mpool_elem_t *)p-1); }
void uct_rm_urma_ep_process_rx(uct_rm_urma_iface_t *f,uct_rm_urma_neth_t *h,
    unsigned len,uct_rm_urma_recv_skb_t *s,int async)
{
    (void)f;(void)len;(void)async;
    if(h->packet_type & UCT_RM_URMA_PACKET_FLAG_AM)++delivered;
    ucs_mpool_put(s);
}
urma_status_t urma_post_jetty_send_wr(urma_jetty_t *j,urma_jfs_wr_t *w,urma_jfs_wr_t **b)
{(void)j;(void)w;(void)b;return URMA_SUCCESS;}
void fixture_check(void){assert(delivered==10);assert(freed==14);puts("PROBE_FORWARD_DROP_DUPLICATE_FREE=PASS");}
