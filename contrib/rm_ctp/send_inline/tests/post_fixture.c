/* Local CPU model only: compile the verbatim production function extracted by
 * test_inline.py. This is not a real URMA ABI/provider or full UCX build. */
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
typedef int ucs_status_t;
typedef int urma_status_t;
enum { UCS_OK=0,UCS_ERR_NO_RESOURCE=-1,UCS_ERR_IO_ERROR=-2,
       URMA_SUCCESS=0,URMA_EAGAIN=11,URMA_ENOMEM=12,URMA_ERROR=13,
       URMA_INLINE_DISABLE=0,URMA_INLINE_ENABLE=1,URMA_COMPLETE_ENABLE=1,
       URMA_SOLICITED_ENABLE=1,URMA_OPC_SEND=1,UCT_EP_PARAM_FIELD_IFACE=1 };
typedef void *uct_ep_h;
typedef struct {unsigned field_mask;void *iface;} uct_ep_params_t;
typedef struct {uint64_t addr;unsigned len;void *tseg;} urma_sge_t;
typedef struct urma_jfs_wr {struct {struct {urma_sge_t *sge;unsigned num_sge;} src;} send;
    struct {struct {unsigned inline_flag,complete_enable;} bs;} flag;void *tjetty;} urma_jfs_wr_t;
typedef struct {void *tseg;unsigned char data[4096];} uct_ub_iface_send_desc_t;
typedef struct {struct {void *jetty;} *target;} peer_t;
typedef struct {void *jetty;int available;} jetty_t;
typedef struct {struct {struct {int fc_wnd;} fc;struct {unsigned pi;} status;
    struct {int unused;} txqp;} super;jetty_t *rm_urma_jetty;} uct_rm_urma_ep_t;
typedef struct {struct {struct {int super;} super;struct {unsigned max_inline;} config;
    struct {int jfc_available;} tx;urma_jfs_wr_t send_wr;} super;
    struct {uct_rm_urma_ep_t *ctrl_ep;} tx;struct {int creating_control;} link;
    jetty_t rm_urma_jetty;} uct_rm_urma_iface_t;
typedef struct {uct_ub_iface_send_desc_t *desc;unsigned length,in_flight,quarantined;
    peer_t *peer;int op;} uct_rm_urma_replay_t;
static unsigned posts,queued,updated,observed_inline,observed_complete,observed_length;
static void *observed_tseg,*observed_target;
static urma_status_t provider_status;
static ucs_status_t constructor_status;
static uct_rm_urma_ep_t constructor_ep;
static unsigned char observed_bytes[4096];
#define ucs_derived_of(pointer,type) ((type *)(pointer))
static int new_ep(uct_ep_h *ep,uct_ep_params_t *params)
{assert(params->field_mask==UCT_EP_PARAM_FIELD_IFACE);*ep=&constructor_ep;return constructor_status;}
#define UCS_CLASS_NEW(type,result,params) new_ep(result,params)
#define UCT_UB_FILL_WR_SG_BUF(dst,address,length,segment,count,sg) do { \
    (sg)->addr=(address);(sg)->len=(length);(sg)->tseg=(segment); \
    (dst)->sge=(sg);(dst)->num_sge=(count); } while(0)
static void uct_rm_urma_init_base_wr(urma_jfs_wr_t *wr,uct_rm_urma_iface_t *iface,
    int opcode,uct_rm_urma_ep_t *ep,unsigned complete,unsigned inl,unsigned solicited)
{(void)iface;(void)ep;assert(opcode==URMA_OPC_SEND && solicited==1);
 wr->flag.bs.inline_flag=inl;wr->flag.bs.complete_enable=complete;}
static int urma_post_jetty_send_wr(void *j,urma_jfs_wr_t *wr,urma_jfs_wr_t **bad)
{assert(j);++posts;*bad=NULL;observed_inline=wr->flag.bs.inline_flag;
 observed_complete=wr->flag.bs.complete_enable;observed_length=wr->send.src.sge->len;
 observed_tseg=wr->send.src.sge->tseg;observed_target=wr->tjetty;
 memcpy(observed_bytes,(void *)(uintptr_t)wr->send.src.sge->addr,observed_length);
 return provider_status;}
static void uct_rm_urma_update_tx_resources_status(uct_rm_urma_ep_t *ep,uct_rm_urma_iface_t *iface,unsigned complete)
{(void)ep;(void)iface;assert(complete==1);++updated;}
static void uct_ub_outstanding_queue_add_send_op_sn(void *queue,void *op,unsigned pi)
{(void)queue;(void)op;(void)pi;++queued;}
#include "actual_reliable_post.inc"

static void one(unsigned length,unsigned limit,int status,int create)
{
 uct_rm_urma_iface_t iface={0};uct_rm_urma_ep_t ep={0};jetty_t jetty={(void *)1,8};
 struct {void *jetty;} target={(void *)2};peer_t peer={0};peer.target=(void *)&target;
 uct_ub_iface_send_desc_t *desc=malloc(sizeof(*desc)+4096);assert(desc);
 desc->tseg=(void *)3;memset(desc+1,0xa5,4096);
 uct_rm_urma_replay_t entry={.desc=desc,.length=length,.peer=&peer};
 ep.rm_urma_jetty=&jetty;constructor_ep=ep;constructor_status=UCS_OK;
 iface.tx.ctrl_ep=create ? NULL:&ep;iface.super.config.max_inline=limit;
 iface.rm_urma_jetty.available=8;iface.super.tx.jfc_available=8;
 posts=queued=updated=0;provider_status=status;
 int code=uct_rm_urma_ep_reliable_post(&iface,&entry);
 assert(posts==1 && observed_length==length && observed_inline==(length<=limit));
 assert(observed_complete==1 && observed_target==(void *)2);
 assert(observed_tseg==(length<=limit ? NULL:(void *)3));
 assert(memcmp(observed_bytes,desc+1,length)==0);
 for(unsigned i=0;i<4096;++i)assert(((unsigned char *)(desc+1))[i]==0xa5);
 urma_jfs_wr_t zero={0};assert(memcmp(&iface.super.send_wr,&zero,sizeof(zero))==0);
 if(status==URMA_SUCCESS){
  assert(code==UCS_OK && entry.in_flight==1 && queued==1 && updated==1 && !entry.quarantined);
  /* Re-submission uses the same retained bytes and the same length decision. */
  entry.in_flight=0;assert(uct_rm_urma_ep_reliable_post(&iface,&entry)==UCS_OK);
  assert(posts==2 && queued==2 && memcmp(observed_bytes,desc+1,length)==0);
 }else{
  assert(!queued && !updated && !entry.in_flight);
  assert(code==(status==URMA_EAGAIN||status==URMA_ENOMEM ? UCS_ERR_NO_RESOURCE:UCS_ERR_IO_ERROR));
  assert(entry.quarantined==(status==URMA_ERROR));
 }
 free(desc);
}
int main(void)
{
 unsigned lengths[]={39,115,123,127,128,129,4096},limits[]={0,38,39,64,127,128,256};
 unsigned cases=0;
 for(unsigned i=0;i<sizeof(lengths)/sizeof(lengths[0]);++i)
  for(unsigned j=0;j<sizeof(limits)/sizeof(limits[0]);++j)
   for(unsigned create=0;create<2;++create){one(lengths[i],limits[j],URMA_SUCCESS,create);++cases;}
 for(int error=URMA_EAGAIN;error<=URMA_ERROR;++error){one(123,128,error,0);one(129,128,error,1);cases+=2;}
 puts("PRODUCTION_POST_BOUNDARIES_REPLAY_FLAGS_ERRORS=PASS");
 printf("CPU_MODEL_CASES=%u\n",cases);
 return 0;
}
