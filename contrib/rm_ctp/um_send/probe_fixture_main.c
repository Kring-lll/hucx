#include <uct/urma/rm_urma/rm_urma_ep.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
extern void fixture_check(void);
int main(int argc,char **argv)
{
    int inject=argc==2 && !strcmp(argv[1],"inject");
    int fail=argc==2 && !strcmp(argv[1],"fail");
    if(inject) {
        for(unsigned i=0;i<10;++i){
            uct_rm_urma_recv_skb_t *s=ucs_mpool_get(NULL);
            uct_rm_urma_neth_t *h=(void *)(s+1);
            h->packet_type=UCT_RM_URMA_PACKET_FLAG_AM;h->psn=i;
            uct_rm_urma_ep_process_rx(NULL,h,24,s,0);
        }
        for(unsigned i=0;i<3;++i){
            uct_rm_urma_recv_skb_t *s=ucs_mpool_get(NULL);
            uct_rm_urma_neth_t *h=(void *)(s+1);h->packet_type=0;
            uct_rm_urma_ep_process_rx(NULL,h,8,s,0);
        }
        fixture_check();
    }
    urma_jetty_t j={0};urma_jfs_wr_t w={0},*bad=NULL;urma_sge_t sge[2]={{0}};
    unsigned char packet[24]={0};uct_rm_urma_neth_t *h=(void *)packet;
    h->packet_type=UCT_RM_URMA_PACKET_FLAG_AM;
    j.jetty_cfg.jfs_cfg.trans_mode=URMA_TM_RM;w.opcode=URMA_OPC_SEND;
    sge[0].addr=(uintptr_t)packet;sge[0].len=24;w.send.src.sge=sge;w.send.src.num_sge=1;
    if(!fail) {
        for(unsigned i=0;i<200000;++i) {
            h->psn=(uint16_t)i;
            assert(urma_post_jetty_send_wr(&j,&w,&bad)==URMA_SUCCESS);
        }
        w.send.src.num_sge=2;sge[0].len=16;sge[1].addr=(uintptr_t)(packet+16);sge[1].len=8;
        w.flag.bs.inline_flag=1;
        for(unsigned i=0;i<4;++i)assert(urma_post_jetty_send_wr(&j,&w,&bad)==URMA_SUCCESS);
    }
    w.send.src.num_sge=2;sge[0].len=8;sge[1].addr=(uintptr_t)(packet+8);sge[1].len=16;
    w.flag.bs.inline_flag=0;w.flag.bs.complete_enable=1;w.flag.bs.solicited_enable=1;
    if(fail) {
        w.user_ctx=0xf00d;assert(urma_post_jetty_send_wr(&j,&w,&bad)!=URMA_SUCCESS);
    } else {
        assert(urma_post_jetty_send_wr(&j,&w,&bad)==URMA_SUCCESS);
        assert(urma_post_jetty_send_wr(&j,&w,&bad)==URMA_SUCCESS);
    }
    return 0;
}
