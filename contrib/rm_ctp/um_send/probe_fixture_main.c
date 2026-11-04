#include <uct/urma/rm_urma/rm_urma_ep.h>
#include <stdlib.h>
extern void fixture_check(void);
int main(void)
{
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
    urma_jetty_t j={0};urma_jfs_wr_t w={0},*bad=NULL;urma_sge_t sge={0};
    uct_rm_urma_neth_t h={.packet_type=UCT_RM_URMA_PACKET_FLAG_AM,.psn=10};
    j.jetty_cfg.jfs_cfg.trans_mode=URMA_TM_RM;w.opcode=URMA_OPC_SEND;
    sge.addr=(uintptr_t)&h;sge.len=8;w.send.src.sge=&sge;w.send.src.num_sge=1;
    urma_post_jetty_send_wr(&j,&w,&bad);urma_post_jetty_send_wr(&j,&w,&bad);
    fixture_check();return 0;
}
