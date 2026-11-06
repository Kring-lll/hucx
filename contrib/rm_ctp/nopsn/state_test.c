/* Exercise actual EP/iface routines. URMA posts are intercepted, not modeled. */
#include "../../../src/uct/urma/rm_urma/rm_urma_ep.c"
#include <uct/base/uct_worker.h>
#include <assert.h>
#include <sys/resource.h>
extern unsigned uct_rm_urma_iface_dispatch_pending_rx_do(uct_rm_urma_iface_t *iface);

static unsigned char wire[8192], expected[8192];
static unsigned wire_len, posts, delivered, expected_len;
static unsigned last_inline, last_sges, last_first;
static int hold_receive;
static void *held;

urma_status_t urma_post_jetty_send_wr(urma_jetty_t *j, urma_jfs_wr_t *w,
                                    urma_jfs_wr_t **bad)
{
    unsigned i;
    (void)j; (void)bad;
    assert(w->opcode == URMA_OPC_SEND);
    wire_len = 0;
    for (i = 0; i < w->send.src.num_sge; ++i) {
        urma_sge_t *s = &w->send.src.sge[i];
        assert(wire_len + s->len <= sizeof(wire));
        memcpy(wire + wire_len, (void *)(uintptr_t)s->addr, s->len);
        wire_len += s->len;
    }
    last_inline = w->flag.bs.inline_flag;
    last_sges = w->send.src.num_sge;
    last_first = w->send.src.sge[0].len;
    ++posts;
    return URMA_SUCCESS;
}

typedef struct fixture {
    uct_rm_urma_iface_t iface;
    uct_rm_urma_ep_t ep;
    uct_priv_worker_t worker;
    ucs_async_context_t async;
    ucs_mpool_t rx_pool;
} fixture_t;

static void tx_init(ucs_mpool_t *mp, void *object, void *chunk)
{
    uct_rm_urma_send_skb_t *skb = object;
    (void)mp; (void)chunk;
    memset(skb, 0, sizeof(*skb));
    skb->tseg = (void *)1;
}

static void pool_init(ucs_mpool_t *mp, const char *name, int tx)
{
    static const ucs_mpool_ops_t tx_ops = {
        .chunk_alloc=ucs_mpool_chunk_malloc, .chunk_release=ucs_mpool_chunk_free,
        .obj_init=tx_init
    };
    static const ucs_mpool_ops_t rx_ops = {
        .chunk_alloc=ucs_mpool_chunk_malloc, .chunk_release=ucs_mpool_chunk_free
    };
    ucs_mpool_params_t p;
    ucs_mpool_params_reset(&p);
    p.elem_size=8192; p.alignment=64; p.elems_per_chunk=32;
    p.align_offset=tx ? 0 : sizeof(uct_rm_urma_recv_skb_t)+64;
    p.max_elems=128; p.ops=tx ? &tx_ops : &rx_ops; p.name=name;
    assert(ucs_mpool_init(&p, mp)==UCS_OK);
}

static ucs_status_t on_am(void *arg, void *data, size_t length, unsigned flags)
{
    fixture_t *f=arg;
    assert(flags & UCT_CB_PARAM_FLAG_DESC);
    assert(length==expected_len);
    assert(memcmp(data,expected,length)==0);
    ++delivered;
    if (hold_receive) {
        held=(char *)data-32;
        assert(f->iface.super.config.rx_payload_offset-
               f->iface.super.config.rx_headroom_offset==32);
        return UCS_INPROGRESS;
    }
    return UCS_OK;
}

static void init(fixture_t *f)
{
    memset(f,0,sizeof(*f));
    assert(ucs_async_context_init(&f->async,UCS_ASYNC_MODE_POLL)==UCS_OK);
    f->worker.async=&f->async;
    f->iface.super.super.worker=&f->worker;
    f->ep.super.super.super.iface=&f->iface.super.super.super;
    ucs_ptr_array_init(&f->iface.super.eps,"nopsn_test");
    f->ep.super.ep_id=ucs_ptr_array_insert(&f->iface.super.eps,&f->ep);
    f->ep.super.dep_id=f->ep.super.ep_id;
    f->ep.flags=UCT_RM_URMA_EP_FLAG_CONNECTED;
    f->ep.tx.psn=1; f->ep.tx.max_psn=33; f->ep.ca.cwnd=32; f->ep.ca.wmax=32;
    assert(ucs_frag_list_init(0,&f->ep.rx.ooo_pkts,0 UCS_STATS_ARG(NULL))==UCS_OK);
    ucs_queue_head_init(&f->ep.tx.window);
    ucs_queue_head_init(&f->iface.rx.pending_q);
    ucs_queue_head_init(&f->iface.tx.async_comp_q);
    ucs_queue_head_init(&f->iface.tx.outstanding_q);
    ucs_arbiter_init(&f->iface.tx.pending_q);
    ucs_arbiter_group_init(&f->ep.tx.pending.group);
    ucs_arbiter_elem_init(&f->ep.tx.pending.elem);
    pool_init(&f->iface.tx.mp,"nopsn_tx",1);
    pool_init(&f->rx_pool,"nopsn_rx",0);
    f->iface.tx.skb=ucs_mpool_get(&f->iface.tx.mp);
    f->iface.super.config.seg_size=4096;
    f->iface.super.config.max_inline=128;
    f->iface.super.config.tx_jfc_moderation=64;
    f->iface.super.config.rx_max_poll=16;
    f->iface.super.config.rx_payload_offset=sizeof(uct_rm_urma_recv_skb_t)+64+8;
    f->iface.super.config.rx_hdr_offset=f->iface.super.config.rx_payload_offset-6;
    f->iface.super.config.rx_headroom_offset=f->iface.super.config.rx_payload_offset-32;
    f->iface.super.release_desc.cb=uct_ub_iface_release_desc;
    f->iface.async.disable=1;
    f->iface.super.super.am[0].cb=on_am;
    f->iface.super.super.am[0].arg=f;
    f->iface.super.super.am[0].flags=UCT_CB_FLAG_ASYNC;
    f->iface.tx.wr_inl.opcode=f->iface.tx.wr_skb.opcode=URMA_OPC_SEND;
    f->iface.tx.wr_inl.send.src.num_sge=2;
    f->iface.tx.wr_skb.send.src.num_sge=1;
    f->iface.tx.wr_inl.send.src.sge=f->iface.tx.wr_skb.send.src.sge=f->iface.tx.sge;
}

static void finish(fixture_t *f)
{
    assert(ucs_queue_is_empty(&f->ep.tx.window));
    assert(ucs_queue_is_empty(&f->iface.rx.pending_q));
    ucs_arbiter_group_desched(&f->iface.tx.pending_q,&f->ep.tx.pending.group);
    ucs_arbiter_group_purge(&f->iface.tx.pending_q,&f->ep.tx.pending.group,
                          uct_rm_urma_ep_arbiter_purge_cb,NULL);
    ucs_mpool_put(f->iface.tx.skb);
    ucs_mpool_cleanup(&f->iface.tx.mp,1);
    ucs_mpool_cleanup(&f->rx_pool,1);
    ucs_frag_list_cleanup(&f->ep.rx.ooo_pkts);
    ucs_ptr_array_remove(&f->iface.super.eps,f->ep.super.ep_id);
    ucs_ptr_array_cleanup(&f->iface.super.eps,0);
    ucs_async_context_cleanup(&f->async);
}

static void receive_wire(fixture_t *f,int async)
{
    uct_rm_urma_recv_skb_t *rx=ucs_mpool_get(&f->rx_pool);
    uct_rm_urma_neth_t *neth=(void *)((char *)rx+f->iface.super.config.rx_hdr_offset);
    unsigned before=delivered;
    memcpy(neth,wire,wire_len);
    f->iface.super.super.am[0].flags=async ? 0 : UCT_CB_FLAG_ASYNC;
    uct_rm_urma_ep_process_rx(&f->iface,neth,wire_len,rx,async);
    if (async) {
        assert(delivered==before);
        assert(uct_rm_urma_iface_dispatch_pending_rx_do(&f->iface)==1);
    }
    assert(delivered==before+1);
    if (hold_receive) {
        assert(held!=NULL);
        uct_recv_desc(held)->cb(uct_recv_desc(held),held);
        held=NULL;
    }
}

static size_t pack(void *dest,void *arg)
{
    size_t n=*(size_t *)arg;
    memcpy(dest,expected,n);
    return n;
}

static void send_message(fixture_t *f,size_t n,int short_am)
{
    f->iface.tx.async_before_pending=0;
    f->iface.super.tx.jfc_available=64;
    f->iface.rm_urma_jetty.available=64;
    expected_len=n;
    if (short_am) {
        uint64_t hdr=0xdeadbeef12345678ull;
        expected_len=8+n;
        memcpy(expected,&hdr,8);
        assert(uct_rm_urma_ep_am_short(&f->ep.super.super.super,0,hdr,expected+8,n)==UCS_OK);
        assert(last_inline==URMA_INLINE_ENABLE && last_first==14 && last_sges==2);
    } else {
        assert(uct_rm_urma_ep_am_bcopy(&f->ep.super.super.super,0,pack,&n,0)==(ssize_t)n);
        assert(last_inline==URMA_INLINE_DISABLE && last_sges==1);
    }
    assert(wire_len==6+expected_len);
    assert(memcmp(wire+6,expected,expected_len)==0);
    assert(wire_len<=4096);
    assert(((uct_rm_urma_data_neth_t *)wire)->ack_psn==f->ep.rx.acked_psn);
}

int main(int argc,char **argv)
{
    fixture_t f;
    struct rlimit limit={0,0};setrlimit(RLIMIT_CORE,&limit);
    assert(sizeof(uct_rm_urma_neth_t)==8);
    assert(sizeof(uct_rm_urma_data_neth_t)==6);
    assert(sizeof(uct_rm_urma_send_skb_t)==64);
    init(&f);
    urma_jetty_t jetty={0};
    jetty.jetty_id.id=99;jetty.jetty_cfg.jfs_cfg.trans_mode=URMA_TM_RM;
    f.iface.rm_urma_jetty.jetty=&jetty;
    uct_rm_urma_iface_addr_t advertised={0};
    uct_ub_rm_urma_ep_addr_t ep_addr={0};
    assert(uct_rm_urma_iface_get_address(&f.iface.super.super.super,(void *)&advertised)==UCS_OK);
    assert(uct_rm_urma_ep_get_address(&f.ep.super.super.super,(void *)&ep_addr)==UCS_OK);
    assert(advertised.trans_mode==UCT_RM_URMA_NOPSN_ADDR_MODE && ep_addr.iface_addr.trans_mode==UCT_RM_URMA_NOPSN_ADDR_MODE);
    assert(jetty.jetty_cfg.jfs_cfg.trans_mode==URMA_TM_RM);
    memset(expected,0x5a,sizeof(expected));
    send_message(&f,8,1);
    if (argc>1 && !strcmp(argv[1],"--retry")) {
        uct_rm_urma_ep_resend_start(&f.iface,&f.ep);
        uct_rm_urma_ep_resend(&f.ep);
        assert(!"DATA retry must abort the experimental sample");
    }
    receive_wire(&f,0);
    uct_rm_urma_ep_process_ack(&f.iface,&f.ep,1,0);
    assert(ucs_queue_is_empty(&f.ep.tx.window));
    uct_rm_urma_ep_send_ack(&f.iface,&f.ep);
    assert(wire_len==8 && !( ((uct_rm_urma_neth_t *)wire)->packet_type & UCT_RM_URMA_PACKET_FLAG_AM));
    puts("ACTUAL_SHORT_WIRE22_DATA6_ACK8_WINDOW=PASS");
    for(unsigned i=0;i<70000;i++) {
        size_t sizes[]={1,64,4088,8};
        uct_rm_urma_psn_t sent=f.ep.tx.psn;
        memset(expected,(unsigned char)i,sizeof(expected));
        send_message(&f,sizes[i%4],0);
        hold_receive=(i%17)==0;
        receive_wire(&f,(i%19)==0);
        uct_rm_urma_ep_process_ack(&f.iface,&f.ep,sent,0);
        assert(ucs_queue_is_empty(&f.ep.tx.window));
    }
    puts("ACTUAL_BCOPY_BOUNDARY_ALTERNATING_WRAP70000_ASYNC_RETAIN_RELEASE=PASS");
    uct_rm_urma_iface_addr_t old={.jetty_id=1,.trans_mode=URMA_TM_RM};
    uct_rm_urma_peer_address_t target={0};
    assert(uct_rm_urma_iface_unpack_peer_address(&f.iface,NULL,&old,&target)==UCS_ERR_UNREACHABLE);
    assert(target.tjetty==NULL);
    uct_rm_urma_rjetty_get_peer_address(&f.iface,NULL,&old,&target);
    assert(target.tjetty==NULL);
    puts("LEGACY_ADDRESS_REJECTED_BEFORE_IMPORT=PASS");
    finish(&f);
    printf("POSTS=%u DELIVERED=%u CPU_ONLY_NO_HARDWARE=YES\n",posts,delivered);
    return 0;
}
