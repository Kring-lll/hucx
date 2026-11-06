#include <urma_api.h>
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#if !defined(WIRE_CLIENT) && !defined(WIRE_LOCAL_CLIENT)
static unsigned posts;
urma_status_t urma_post_jetty_send_wr(urma_jetty_t *j,urma_jfs_wr_t *w,urma_jfs_wr_t **bad)
{(void)j;++posts;if(getenv("FAKE_ERROR")){*bad=w;return URMA_EAGAIN;}return URMA_SUCCESS;}
unsigned fixture_posts(void){return posts;}
#else
#ifndef WIRE_LOCAL_CLIENT
extern unsigned fixture_posts(void);
#endif
typedef urma_status_t (*post_fn)(urma_jetty_t *,urma_jfs_wr_t *,urma_jfs_wr_t **);
static void forward(post_fn post,urma_jfs_wr_t *wr)
{
    urma_jfs_wr_t before=*wr,*bad=NULL;
    urma_status_t expected=getenv("FAKE_ERROR") ? URMA_EAGAIN : URMA_SUCCESS;
    assert(post(NULL,wr,&bad)==expected);
    assert(memcmp(&before,wr,sizeof(before))==0);
    assert(bad==(getenv("FAKE_ERROR") ? wr : NULL));
}
int main(int argc,char **argv)
{
    unsigned h=argc>1 ? (unsigned)atoi(argv[1]) : 6;
    post_fn post;
#ifdef WIRE_LOCAL_CLIENT
    void *handle=NULL;
    assert(argc==3);
    if(strcmp(argv[2],"missing")){
        handle=dlopen(argv[2],RTLD_NOW|RTLD_LOCAL);assert(handle);
    }
    *(void **)(&post)=dlsym(RTLD_DEFAULT,"urma_post_jetty_send_wr");assert(post);
#else
    post=urma_post_jetty_send_wr;
#endif
    unsigned char bytes[4096]={0};uint32_t type=1u<<24;memcpy(bytes,&type,4);
    urma_sge_t sg[2]={{0}};urma_jfs_wr_t wr={0};
    wr.opcode=URMA_OPC_SEND;wr.send.src.num_sge=2;wr.send.src.sge=sg;
    sg[0].addr=(uintptr_t)bytes;sg[0].len=h+8;sg[1].addr=(uintptr_t)(bytes+128);sg[1].len=8;
    wr.flag.bs.inline_flag=1;
    forward(post,&wr);
    wr.flag.bs.inline_flag=0;wr.send.src.num_sge=1;sg[0].len=h+4088;
    forward(post,&wr);
    type=0;memcpy(bytes,&type,4);sg[0].len=8;wr.flag.bs.inline_flag=1;
    forward(post,&wr);
#ifdef WIRE_LOCAL_CLIENT
    unsigned (*count)(void);*(void **)(&count)=dlsym(handle,"fixture_posts");assert(count);
    assert(count()==3);
#else
    assert(fixture_posts()==3);
#endif
    return 0;
}
#endif
