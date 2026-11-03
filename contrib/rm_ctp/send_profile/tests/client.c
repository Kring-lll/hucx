#define _GNU_SOURCE
#include "urma_api.h"
#include <assert.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
int MPI_Init(int *,char ***);
int MPI_Init_thread(int *,char ***,int,int *);
int MPI_Finalize(void);
static void put(unsigned char *p,unsigned n,uint64_t v)
{for(unsigned i=0;i<n;++i)p[i]=(unsigned char)(v>>(8*i));}
int main(int argc,char **argv)
{
 assert(argc==2);
 void *h=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);assert(h);
 urma_status_t (*post)(urma_jetty_t *,urma_jfs_wr_t *,urma_jfs_wr_t **)=dlsym(RTLD_DEFAULT,"urma_post_jetty_send_wr");
 int (*poll)(urma_jfc_t *,int,urma_cr_t *)=dlsym(RTLD_DEFAULT,"urma_poll_jfc");
 unsigned (*calls)(void)=dlsym(h,"provider_calls");assert(post && poll && calls);
 urma_jetty_t j={0};j.jetty_cfg.jfs_cfg.trans_mode=URMA_TM_RM;
 unsigned char wire[128]={0};put(wire+3,4,0x33524d53);wire[7]=3;wire[8]=1;
 put(wire+11,8,101);put(wire+19,8,202);put(wire+27,8,1);
 urma_sge_t s={.addr=(uint64_t)(uintptr_t)wire,.len=128};
 urma_jfs_wr_t wr={0},*bad=NULL;
 wr.opcode=URMA_OPC_SEND;wr.send.src.sge=&s;wr.send.src.num_sge=1;
 (void)post(&j,&wr,&bad); /* Startup excluded. */
 if(getenv("INIT_THREAD")){int provided;assert(MPI_Init_thread(&argc,&argv,0,&provided)==0);}
 else assert(MPI_Init(&argc,&argv)==0);
 for(unsigned i=0;i<4;++i){
  wr.flag.bs.inline_flag=i/2;wr.flag.bs.complete_enable=i%2;
  if(i==2)wire[8]=2;
  if(i==3){wire[8]=1;put(wire+19,8,303);}
  int code=post(&j,&wr,&bad);
  assert(code==(getenv("PROVIDER_ERROR")?URMA_EAGAIN:URMA_SUCCESS));
  assert(errno==(getenv("PROVIDER_ERROR")?37:36));
 }
 urma_cr_t cr[2];urma_jfc_t c;assert(poll(&c,2,cr)==2 && errno==35);
 assert(MPI_Finalize()==0);
 (void)post(&j,&wr,&bad); /* Finalize excluded. */
 assert(calls()==6);
 return 0;
}
