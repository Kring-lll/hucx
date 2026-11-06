"""Compile the unchanged resend function text with CPU-only external dependencies.
This isolates selection/skip ordering; it is not a URMA, timer, or MPI test.
"""
import argparse
import hashlib
from pathlib import Path
import resource
import signal
import subprocess
import tempfile

PREFIX=r'''
#include <assert.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define UCS_F_ALWAYS_INLINE inline
#define UCS_PTR_BYTE_OFFSET(p,o) ((void *)((char *)(p)+(o)))
#define ucs_derived_of(p,t) ((t *)(p))
#define ucs_assert assert
#define ucs_assertv_always(c,...) assert(c)
#define ucs_debug(...) ((void)0)
#define UCT_RM_URMA_SKB_ALIGN 64
#define UCT_RM_URMA_PSN_COMPARE(a,op,b) ((int16_t)((uint16_t)(a)-(uint16_t)(b)) op 0)
#define UCT_RM_URMA_EP_FLAG_TX_NACKED (1u<<10)
#define UCT_RM_URMA_SEND_SKB_FLAG_RESENDING (1u<<3)
#define UCT_RM_URMA_SEND_SKB_FLAG_ZCOPY (1u<<2)
#define UCT_RM_URMA_SEND_SKB_FLAG_CTL_RESEND (1u<<6)
#define UCT_RM_URMA_PACKET_FLAG_AM (1u<<24)
#define UCT_RM_URMA_PACKET_FLAG_ACK_REQ (1u<<25)
#define UCT_RM_URMA_PACKET_FLAG_CTL (1u<<29)
#define UCT_RM_URMA_EP_NULL_ID 0xffffffu
#define UCT_RM_URMA_RESENDS_PER_ACK 4
#define UCT_RM_URMA_IFACE_SEND_CTL_FLAG_SIGNALED 4
#define UCT_RM_URMA_IFACE_SEND_CTL_FLAG_SOLICITED 2
#define ucs_queue_iter_end(q,p) (*(p)==NULL)
#define ucs_queue_iter_elem(e,p,m) ((uct_rm_urma_send_skb_t *)(*(p)))
#define ucs_queue_iter_next(p) (&(*(p))->next)
typedef uint16_t uct_rm_urma_psn_t;
typedef struct elem {struct elem *next;} ucs_queue_elem_t;
typedef ucs_queue_elem_t **ucs_queue_iter_t;
typedef struct {ucs_queue_elem_t *head;} qhead_t;
typedef struct __attribute__((packed)) {uint32_t packet_type;uint16_t psn,ack_psn;} uct_rm_urma_neth_t;
typedef struct __attribute__((packed)) {uint32_t packet_type;uint16_t ack_psn;} uct_rm_urma_data_neth_t;
typedef struct __attribute__((packed,aligned(64))) {
 ucs_queue_elem_t queue;void *tseg;uint16_t len,flags,local_psn;uct_rm_urma_neth_t neth[0];
} uct_rm_urma_send_skb_t;
typedef struct {unsigned char opaque[40];} uct_rm_urma_ctl_hdr_t;
typedef struct {struct {size_t addr_size;} super;} uct_rm_urma_iface_t;
typedef struct {
 struct {struct {struct {void *iface;} super;} super;uint32_t ep_id,dep_id;} super;
 struct {uint16_t psn,max_psn,acked_psn,resend_count;qhead_t window;uint64_t resend_time;struct {unsigned ops;} pending;} tx;
 struct {struct {uint16_t head_sn;} ooo_pkts;uint16_t acked_psn;} rx;
 struct {ucs_queue_iter_t pos;uint16_t psn,max_psn;} resend;unsigned flags;
} uct_rm_urma_ep_t;
typedef struct {void *buffer,*tseg;uint16_t length;} uct_rm_urma_iov_t;
typedef struct {uct_rm_urma_iov_t iov[8];uint16_t iovcnt;} uct_rm_urma_zcopy_desc_t;
typedef struct {uct_rm_urma_send_skb_t *self_skb,*resent_skb;uct_rm_urma_ep_t *ep;uint16_t sn;} uct_rm_urma_ctl_desc_t;
static unsigned ended,posted;
static unsigned char txspace[8192] __attribute__((aligned(64)));
static uct_rm_urma_ctl_desc_t ctl;
static uct_rm_urma_zcopy_desc_t zcopy;
static void ucs_fatal(const char *fmt,...) __attribute__((noreturn));
static void ucs_fatal(const char *fmt,...) {va_list a;va_start(a,fmt);vfprintf(stderr,fmt,a);va_end(a);fputc('\n',stderr);abort();}
static void uct_rm_urma_ep_resend_end(uct_rm_urma_ep_t *ep){(void)ep;++ended;}
static uint32_t uct_rm_urma_neth_get_dest_id(uct_rm_urma_neth_t *n){return n->packet_type&0xffffffu;}
static int uct_rm_urma_ep_is_connected(uct_rm_urma_ep_t *ep){return ep->super.dep_id!=UCT_RM_URMA_EP_NULL_ID;}
static uct_rm_urma_send_skb_t *uct_rm_urma_iface_ctl_skb_get(uct_rm_urma_iface_t *f){(void)f;return (void *)txspace;}
static uint64_t uct_rm_urma_iface_get_time(uct_rm_urma_iface_t *f){(void)f;return 1;}
static uct_rm_urma_zcopy_desc_t *uct_rm_urma_get_zcopy_desc(uct_rm_urma_send_skb_t *s){(void)s;return &zcopy;}
static uct_rm_urma_ctl_desc_t *uct_rm_urma_get_ctl_desc(uct_rm_urma_send_skb_t *s){(void)s;return &ctl;}
static uint16_t uct_rm_urma_ep_send_ctl(uct_rm_urma_ep_t *e,uct_rm_urma_send_skb_t *s,const uct_rm_urma_iov_t *i,uint16_t n,int f){(void)e;(void)s;(void)i;(void)n;(void)f;++posted;return 1;}
static void uct_rm_urma_iface_add_ctl_desc(uct_rm_urma_iface_t *f,uct_rm_urma_ctl_desc_t *c){(void)f;(void)c;}
'''
MAIN=r'''
int main(int argc,char **argv) {
 unsigned char input[8192] __attribute__((aligned(64)))={0};
 uct_rm_urma_send_skb_t *skb=(void *)input;
 uct_rm_urma_iface_t iface={0};uct_rm_urma_ep_t ep={0};
 assert(argc==2);iface.super.addr_size=20;ep.super.super.super.iface=&iface;
 ep.super.ep_id=7;ep.super.dep_id=9;ep.tx.psn=8;ep.tx.max_psn=32;ep.tx.acked_psn=6;
 ep.rx.ooo_pkts.head_sn=99;ep.resend.max_psn=7;ep.tx.window.head=&skb->queue;
 ep.resend.pos=&ep.tx.window.head;skb->local_psn=7;skb->len=22;
 skb->neth->packet_type=UCT_RM_URMA_PACKET_FLAG_AM|9;
 ((uct_rm_urma_data_neth_t *)skb->neth)->ack_psn=0x7000;
 if(!strcmp(argv[1],"large_no_nack")) skb->len=4094;
 else if(!strcmp(argv[1],"out_of_window")) {skb->local_psn=32;ep.tx.max_psn=32;}
 else if(!strcmp(argv[1],"already_resending")) skb->flags=UCT_RM_URMA_SEND_SKB_FLAG_RESENDING;
 else if(!strcmp(argv[1],"large_nack")) {skb->len=4094;ep.flags=UCT_RM_URMA_EP_FLAG_TX_NACKED;}
 else if(!strcmp(argv[1],"boundary_skip")) skb->len=6+sizeof(uct_rm_urma_ctl_hdr_t)+20+1;
 else if(!strcmp(argv[1],"boundary_retry")) skb->len=6+sizeof(uct_rm_urma_ctl_hdr_t)+20;
 else if(!strcmp(argv[1],"wrap_already_resending")) {skb->local_psn=0;ep.tx.acked_psn=65535;ep.tx.psn=1;ep.tx.max_psn=31;ep.resend.max_psn=0;skb->flags=UCT_RM_URMA_SEND_SKB_FLAG_RESENDING;}
 else if(!strcmp(argv[1],"acked")) ep.tx.acked_psn=7;
 else if(!strcmp(argv[1],"empty")) ep.tx.window.head=NULL;
 else if(!strcmp(argv[1],"dummy")) {skb->neth->packet_type=UCT_RM_URMA_EP_NULL_ID;skb->neth->psn=7;skb->len=8;}
 else if(!strcmp(argv[1],"control")) {skb->neth->packet_type=UCT_RM_URMA_PACKET_FLAG_CTL|9;skb->neth->psn=7;skb->len=8;}
 else assert(!strcmp(argv[1],"small_retry"));
 uct_rm_urma_ep_resend(&ep);
 if(!strcmp(argv[1],"control")) assert(posted==1 && ep.tx.resend_count==1);
 else assert(posted==0);
 printf("RESEND_SELECTION_CASE=%s PASS EXTERNALS_STUBBED=YES\n",argv[1]);return 0;
}
'''


def snippet(text,start,end):
    a=text.index(start);return text[a:text.index(end,a)]


def main():
    p=argparse.ArgumentParser();p.add_argument('--ep-source',type=Path);p.add_argument('--legacy',action='store_true');args=p.parse_args()
    root=Path(__file__).resolve().parents[3]
    source=args.ep_source or root/'src/uct/urma/rm_urma/rm_urma_ep.c'
    ep=source.read_text();header=(root/'src/uct/urma/rm_urma/rm_urma_ep.h').read_text()
    helpers=snippet(header,'static UCS_F_ALWAYS_INLINE size_t\nuct_rm_urma_neth_size','ucs_status_t uct_rm_urma_ep_am_short')
    function=snippet(ep,'static void uct_rm_urma_ep_resend(uct_rm_urma_ep_t *ep)','static void uct_rm_urma_ep_send_ack')
    with tempfile.TemporaryDirectory() as temp:
        c=Path(temp)/'actual_resend.c';c.write_text(PREFIX+helpers+function+MAIN);binary=c.with_suffix('')
        subprocess.run(['gcc','-std=gnu99','-O1','-g','-Wall','-Wextra','-Werror','-Wno-unused-function','-fsanitize=undefined',str(c),'-o',str(binary)],check=True)
        cases=('large_no_nack','out_of_window','already_resending','wrap_already_resending','boundary_skip','acked','empty','dummy','control','small_retry','large_nack','boundary_retry')
        def no_core():resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        for case in cases:
            r=subprocess.run([str(binary),case],capture_output=True,text=True,preexec_fn=no_core)
            blocked=case in ('small_retry','large_nack','boundary_retry')
            if args.legacy and case in ('large_no_nack','out_of_window','already_resending','wrap_already_resending','boundary_skip'):blocked=True
            assert r.returncode==(-signal.SIGABRT if blocked else 0),(case,r.returncode,r.stdout,r.stderr)
            if blocked:assert 'attempted DATA retransmission' in r.stderr,(case,r.stderr)
            assert 'runtime error:' not in r.stderr,(case,r.stderr)
        print('REAL_RESEND_BODY_%s=%d/%d PASS FUNCTION_SHA256=%s NO_HARDWARE=YES'%('LEGACY_FALSE_ABORT_REPRODUCED' if args.legacy else 'SKIP_ORDER_AND_TRUE_RETRY_GUARD',len(cases),len(cases),hashlib.sha256(function.encode()).hexdigest()))


if __name__=='__main__':main()
