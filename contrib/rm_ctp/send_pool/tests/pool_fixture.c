/* CPU-only ownership model; verbatim production functions are included below.
 * This is not a URMA ABI, CQE, packet-loss or performance test. */
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#define UCS_OK 0
#define UCS_ERR_NO_RESOURCE -1
#define UCS_ERR_IO_ERROR -2
#define UCT_RM_URMA_REL_DATA 1
#define UCT_RM_URMA_REL_ACK 2
#define UCT_RM_URMA_EP_FLAG_CLOSING 1
#define UCT_RM_URMA_LINK_RELIABLE_V3 2
#define ucs_container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define ucs_derived_of(p,t) ((t *)(p))
#define ucs_max(a,b) ((a)>(b)?(a):(b))
#if PRODUCT_ASSERT
#define ucs_assert(x) assert(x)
#else
#define ucs_assert(x) ((void)0)
#endif
typedef int ucs_status_t;
typedef uint64_t ucs_time_t;
typedef struct list {struct list *prev,*next;} ucs_list_link_t;
static void ucs_list_head_init(ucs_list_link_t *h){h->next=h->prev=h;}
static int ucs_list_is_empty(ucs_list_link_t *h){return h->next==h;}
static void ucs_list_add_tail(ucs_list_link_t *h,ucs_list_link_t *e){e->prev=h->prev;e->next=h;h->prev->next=e;h->prev=e;}
static void ucs_list_del(ucs_list_link_t *e){e->prev->next=e->next;e->next->prev=e->prev;}
#define ucs_list_for_each(v,h,m) for(ucs_list_link_t *ln=(h)->next;ln!=(h) && ((v)=ucs_container_of(ln,__typeof__(*(v)),m),1);ln=ln->next)
#define ucs_list_for_each_safe(v,n,h,m) for(ucs_list_link_t *ln=(h)->next,*nx=ln->next;ln!=(h) && ((v)=ucs_container_of(ln,__typeof__(*(v)),m),(n)=nx==(h)?NULL:ucs_container_of(nx,__typeof__(*(n)),m),1);ln=nx,nx=ln->next)
static void *extract(ucs_list_link_t *h,size_t offset){ucs_list_link_t *p=h->next;ucs_list_del(p);return (char *)p-offset;}
#define ucs_list_extract_head(h,t,m) ((t *)extract(h,offsetof(t,m)))
typedef struct {unsigned char *slots[2];unsigned used[2],gets,puts,fail;size_t size;} ucs_mpool_t;
static ucs_mpool_t *pools[2];
static unsigned pools_count;
static void pool_init(ucs_mpool_t *p,size_t size){memset(p,0,sizeof(*p));p->size=size;for(unsigned i=0;i<2;i++){p->slots[i]=malloc(size+32);assert(p->slots[i]);memset(p->slots[i],0xa5,size+32);}pools[pools_count++]=p;}
static void *ucs_mpool_get(ucs_mpool_t *p){++p->gets;if(p->fail)return NULL;for(unsigned i=0;i<2;i++)if(!p->used[i]){p->used[i]=1;return p->slots[i]+16;}return NULL;}
static void ucs_mpool_put(void *v){for(unsigned p=0;p<pools_count;p++)for(unsigned i=0;i<2;i++)if(v==pools[p]->slots[i]+16){ucs_mpool_t *m=pools[p];assert(m->used[i]);for(unsigned j=0;j<16;j++)assert(m->slots[i][j]==0xa5 && m->slots[i][m->size+16+j]==0xa5);m->used[i]=0;++m->puts;memset(v,0xa5,m->size);return;}assert(!"unknown pool return");}
static unsigned live(ucs_mpool_t *p){return p->used[0]+p->used[1];}
static void destroy(ucs_mpool_t *p){assert(!live(p));for(unsigned i=0;i<2;i++)free(p->slots[i]);}
typedef struct {void *tseg;unsigned char payload[64];} uct_ub_iface_send_desc_t;
typedef struct op {void *iface;unsigned flags;void *user_comp;void (*handler)(struct op *,const void *);} uct_urma_iface_send_op_t;
typedef struct uct_rm_urma_ep {struct {unsigned flags;} super;unsigned reliable_outstanding;int arb_group;struct uct_rm_urma_peer *peer;} uct_rm_urma_ep_t;
typedef struct uct_rm_urma_peer uct_rm_urma_peer_t;
typedef struct uct_rm_urma_replay {
 ucs_list_link_t list;uct_urma_iface_send_op_t op;uct_rm_urma_peer_t *peer;uct_rm_urma_ep_t *owner;
 uct_ub_iface_send_desc_t *desc;uint64_t psn;ucs_time_t posted_at,accepted_at;
 unsigned length,retries,in_flight,acknowledged,kind,quarantined;
} uct_rm_urma_replay_t;
struct uct_rm_urma_peer {ucs_list_link_t list;struct {unsigned count,ack_in_flight;uint64_t tx_next;ucs_list_link_t replay;} reliable;};
typedef struct {struct {struct {ucs_mpool_t mp;} tx;} super;struct {ucs_mpool_t pending_mp;int arbiter,error;} tx;
 struct {unsigned mode;ucs_list_link_t peers;} link;struct {ucs_list_link_t quarantine;} reliable;} uct_rm_urma_iface_t;
typedef struct {char x[64];} uct_rm_urma_fc_pending_req_t;
typedef struct {char x[96];} uct_rm_urma_link_req_t;
typedef struct {size_t elem_size,alignment;unsigned elems_per_chunk;void *ops;const char *name;} ucs_mpool_params_t;
static int uct_rm_urma_pending_mpool_ops;
static ucs_mpool_params_t captured;
static void ucs_mpool_params_reset(ucs_mpool_params_t *p){memset(p,0,sizeof(*p));}
static int ucs_mpool_init(ucs_mpool_params_t *p,ucs_mpool_t *m){captured=*p;pool_init(m,p->elem_size);return UCS_OK;}
static unsigned scheduled,failed,post_quarantine;
static int post_error;
static uct_rm_urma_iface_t *current;
static void ucs_arbiter_group_schedule(int *a,int *b){(void)a;(void)b;++scheduled;}
static uct_rm_urma_iface_t *rel_iface(uct_rm_urma_ep_t *ep){(void)ep;return current;}
static void rel_header(uct_rm_urma_iface_t *i,uct_rm_urma_replay_t *e,uint8_t id,size_t len){(void)i;(void)id;e->length=(unsigned)len;}
static ucs_status_t uct_rm_urma_ep_reliable_post(uct_rm_urma_iface_t *i,uct_rm_urma_replay_t *e){(void)i;e->quarantined=post_quarantine;if(!post_error)e->in_flight=1;return (int)post_error;}
static void uct_rm_urma_iface_set_failed(uct_rm_urma_iface_t *i,int status){++failed;i->tx.error=status;}
static ucs_time_t ucs_get_time(void){return 123;}
void uct_rm_urma_reliable_complete(uct_urma_iface_send_op_t *,const void *);
#include "production.inc"
static void fixture(uct_rm_urma_iface_t *i,uct_rm_urma_peer_t *p,uct_rm_urma_ep_t *e)
{memset(i,0,sizeof(*i));memset(p,0,sizeof(*p));memset(e,0,sizeof(*e));pools_count=0;scheduled=failed=post_error=post_quarantine=0;current=i;e->peer=p;
 i->link.mode=UCT_RM_URMA_LINK_RELIABLE_V3;assert(uct_rm_urma_iface_pending_mpool_init(i)==UCS_OK);
 assert(captured.elem_size>=sizeof(uct_rm_urma_replay_t) && captured.elems_per_chunk==128 && captured.alignment==8);
 pool_init(&i->super.tx.mp,sizeof(uct_ub_iface_send_desc_t));ucs_list_head_init(&i->link.peers);ucs_list_head_init(&i->reliable.quarantine);
 ucs_list_head_init(&p->reliable.replay);p->reliable.tx_next=1;ucs_list_add_tail(&i->link.peers,&p->list);}
static void finish(uct_rm_urma_iface_t *i){uct_rm_urma_reliable_cleanup(i);destroy(&i->super.tx.mp);destroy(&i->tx.pending_mp);}
static void test_pool_failure_and_dirty_reuse(void)
{uct_rm_urma_iface_t i;uct_rm_urma_peer_t p;uct_rm_urma_ep_t e;fixture(&i,&p,&e);
 i.tx.pending_mp.fail=1;assert(!rel_alloc(&i,&p,1) && !i.super.tx.mp.gets);i.tx.pending_mp.fail=0;
 i.super.tx.mp.fail=1;assert(!rel_alloc(&i,&p,1) && !live(&i.tx.pending_mp));i.super.tx.mp.fail=0;
 for(unsigned n=0;n<1000;n++){
  uct_rm_urma_replay_t *r=rel_alloc(&i,&p,UCT_RM_URMA_REL_ACK);assert(r && !r->owner && !r->acknowledged && !r->in_flight && !r->quarantined && !r->retries && !r->psn);
  assert(r->op.iface==&i.super && r->op.handler==uct_rm_urma_reliable_complete && !r->op.flags && !r->op.user_comp);
  r->in_flight=1;p.reliable.count=0;uct_rm_urma_reliable_complete(&r->op,NULL);assert(!live(&i.tx.pending_mp) && !live(&i.super.tx.mp));
 }finish(&i);}
static void test_both_completion_orders(void)
{for(unsigned ack_first=0;ack_first<2;ack_first++)for(unsigned closing=0;closing<2;closing++){
 uct_rm_urma_iface_t i;uct_rm_urma_peer_t p;uct_rm_urma_ep_t e;fixture(&i,&p,&e);
 uct_rm_urma_replay_t *r=rel_alloc(&i,&p,1);assert(rel_accept(&e,r,1,8)==UCS_OK);
 assert(e.reliable_outstanding==1 && p.reliable.count==1 && p.reliable.tx_next==2 && r->accepted_at==123);
 if(ack_first){r->acknowledged=1;assert(live(&i.tx.pending_mp)==1);}
 uct_rm_urma_reliable_complete(&r->op,NULL);
 if(!ack_first){assert(live(&i.tx.pending_mp)==1 && live(&i.super.tx.mp)==1 && !r->in_flight);r->acknowledged=1;e.super.flags=closing;rel_release(&i,r);}
 assert(!e.reliable_outstanding && !p.reliable.count && ucs_list_is_empty(&p.reliable.replay));finish(&i);
}}
static void test_post_rollback_and_quarantine(void)
{for(unsigned quarantine=0;quarantine<2;quarantine++){
 uct_rm_urma_iface_t i;uct_rm_urma_peer_t p;uct_rm_urma_ep_t e;fixture(&i,&p,&e);post_error=UCS_ERR_IO_ERROR;post_quarantine=quarantine;
 uct_rm_urma_replay_t *r=rel_alloc(&i,&p,1);assert(rel_accept(&e,r,1,8)==UCS_ERR_IO_ERROR && failed==1);
 assert(!p.reliable.count && !e.reliable_outstanding && p.reliable.tx_next==1);
 assert(live(&i.tx.pending_mp)==quarantine && live(&i.super.tx.mp)==quarantine);finish(&i);
 }
 uct_rm_urma_iface_t i;uct_rm_urma_peer_t p;uct_rm_urma_ep_t e;fixture(&i,&p,&e);post_error=UCS_ERR_NO_RESOURCE;
 uct_rm_urma_replay_t *r=rel_alloc(&i,&p,1);assert(rel_accept(&e,r,1,8)==UCS_ERR_NO_RESOURCE && !failed && !live(&i.tx.pending_mp));finish(&i);}
static void test_cancel_cleanup_and_shared_pool(void)
{uct_rm_urma_iface_t i;uct_rm_urma_peer_t p;uct_rm_urma_ep_t e;fixture(&i,&p,&e);
 void *control=ucs_mpool_get(&i.tx.pending_mp);assert(control);
 uct_rm_urma_replay_t *r=rel_alloc(&i,&p,1);assert(r && rel_accept(&e,r,1,8)==UCS_OK);
 assert(!rel_alloc(&i,&p,2));ucs_mpool_put(control);
 r->in_flight=0;e.super.flags=UCT_RM_URMA_EP_FLAG_CLOSING;uct_rm_urma_reliable_cleanup(&i);
 assert(!e.reliable_outstanding && !live(&i.tx.pending_mp) && !scheduled);finish(&i);}
static void test_legacy_pool_size(void)
{for(unsigned mode=0;mode<2;mode++){uct_rm_urma_iface_t i={0};pools_count=0;i.link.mode=mode;
 assert(!uct_rm_urma_iface_pending_mpool_init(&i));assert(captured.elem_size==96);destroy(&i.tx.pending_mp);}}
int main(void){test_pool_failure_and_dirty_reuse();test_both_completion_orders();test_post_rollback_and_quarantine();test_cancel_cleanup_and_shared_pool();test_legacy_pool_size();puts("PRODUCTION_POOL_OWNERSHIP_REUSE_ROLLBACK_ACK_CQE_CLEANUP=PASS");return 0;}
