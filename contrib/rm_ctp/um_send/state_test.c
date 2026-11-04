/* CPU tests execute the actual RM/UM-derived state routines, not a model. */
#include "../../../src/uct/urma/rm_urma/rm_urma_ep.c"
#include <assert.h>
static unsigned posts;
urma_status_t urma_post_jetty_send_wr(urma_jetty_t *j, urma_jfs_wr_t *w, urma_jfs_wr_t **bad)
{
    (void)j;(void)bad;
    assert(w->opcode == URMA_OPC_SEND);
    assert(w->flag.bs.inline_flag == URMA_INLINE_ENABLE);
    ++posts;return URMA_SUCCESS;
}
int main(void)
{
    assert(sizeof(uct_rm_urma_neth_t)==8);
    assert(sizeof(uct_rm_urma_psn_t)==2);
    assert(UCT_RM_URMA_PSN_COMPARE(0, >, 65535));
    assert(UCT_RM_URMA_PSN_COMPARE(65535, <, 0));
    uct_rm_urma_iface_t iface={0};uct_rm_urma_ep_t ep={0};
    ucs_arbiter_init(&iface.tx.pending_q);
    ucs_arbiter_group_init(&ep.tx.pending.group);
    ucs_arbiter_elem_init(&ep.tx.pending.elem);
    ucs_queue_head_init(&ep.tx.window);
    ep.tx.psn=2;ep.tx.acked_psn=65534;
    uct_rm_urma_ep_resend_start(&iface,&ep);
    assert(ep.resend.psn==65535 && ep.resend.max_psn==1);
    assert(ep.tx.pending.ops & UCT_RM_URMA_EP_OP_RESEND);
    ep.tx.acked_psn=65535;
    uct_rm_urma_ep_resend_ack(&iface,&ep);
    assert(ep.resend.psn==0);
    ep.tx.acked_psn=1;
    uct_rm_urma_ep_resend_ack(&iface,&ep);
    assert(ep.resend.psn==2 && !(ep.tx.pending.ops & UCT_RM_URMA_EP_OP_RESEND));
    ep.ca.cwnd=32;ep.tx.psn=2;ep.tx.acked_psn=1;
    uct_rm_urma_ep_ca_drop(&ep);assert(ep.ca.cwnd==16);
    for (unsigned n=0;n<8;++n)uct_rm_urma_ep_ca_drop(&ep);
    assert(ep.ca.cwnd==2);
    ep.ca.wmax=32;uct_rm_urma_ep_ca_ack(&ep);assert(ep.ca.cwnd==3);
    urma_sge_t sge[2]={{0}};urma_jfs_wr_t wr={0};
    sge[0].len=16;sge[1].len=8;
    wr.opcode=URMA_OPC_SEND;wr.send.src.num_sge=2;wr.send.src.sge=sge;
    iface.super.config.seg_size=4096;iface.super.config.tx_jfc_moderation=8;
    iface.super.tx.jfc_available=64;iface.rm_urma_jetty.available=64;
    uct_rm_urma_post_send(&iface,&ep,&wr,UCT_RM_URMA_IFACE_SEND_CTL_FLAG_INLINE);
    assert(posts==1 && iface.super.tx.jfc_available==63 && iface.rm_urma_jetty.available==63);
    ucs_arbiter_group_desched(&iface.tx.pending_q,&ep.tx.pending.group);
    puts("ACTUAL_HEADER_PSN_WRAP_RESEND_ACK_CONGESTION_INLINE_POST=PASS");
    return 0;
}
