#include <urma_api.h>
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#ifndef WIRE_CLIENT
static unsigned posts;
urma_status_t urma_post_jetty_send_wr(urma_jetty_t *j,urma_jfs_wr_t *w,urma_jfs_wr_t **bad)
{(void)j;(void)w;(void)bad;++posts;return URMA_SUCCESS;}
unsigned fixture_posts(void){return posts;}
#else
extern unsigned fixture_posts(void);
int main(int argc,char **argv)
{
    unsigned h=argc>1 ? (unsigned)atoi(argv[1]) : 6;
    unsigned char bytes[4096]={0};uint32_t type=1u<<24;memcpy(bytes,&type,4);
    urma_sge_t sg[2]={{0}};urma_jfs_wr_t wr={0},*bad=NULL;
    wr.opcode=URMA_OPC_SEND;wr.send.src.num_sge=2;wr.send.src.sge=sg;
    sg[0].addr=(uintptr_t)bytes;sg[0].len=h+8;sg[1].addr=(uintptr_t)(bytes+128);sg[1].len=8;
    wr.flag.bs.inline_flag=1;
    assert(urma_post_jetty_send_wr(NULL,&wr,&bad)==URMA_SUCCESS);
    wr.flag.bs.inline_flag=0;wr.send.src.num_sge=1;sg[0].len=h+4088;
    assert(urma_post_jetty_send_wr(NULL,&wr,&bad)==URMA_SUCCESS);
    type=0;memcpy(bytes,&type,4);sg[0].len=8;wr.flag.bs.inline_flag=1;
    assert(urma_post_jetty_send_wr(NULL,&wr,&bad)==URMA_SUCCESS);
    assert(fixture_posts()==3);return 0;
}
#endif
