/* Read-only diagnostic, NOT a timing observer. Never alter WR, completion,
 * error, errno, provider selection or buffers. Counts Init->Finalize only. */
#define _GNU_SOURCE
#include <urma_api.h>
#include <dlfcn.h>
#include <link.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

extern int hucx_profile_active;
static pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER;
typedef urma_status_t (*post_fn)(urma_jetty_t *,urma_jfs_wr_t *,urma_jfs_wr_t **);
static post_fn post;
static int (*poll_real)(urma_jfc_t *,int,urma_cr_t *);
static pthread_once_t once=PTHREAD_ONCE_INIT;
static uint64_t send_ok[2][2], send_attempt[2][2], bytes_ok, errors, incomplete;
static uint64_t data_ok, ack_ok, repeated_data, unknown_ok, max_bytes;
static uint64_t polls, empty_polls, cqes, poll_errors, cq_errors;
static uint64_t short_calls, bcopy_calls;
static int (*short_real)(void *,uint8_t,uint64_t,const void *,unsigned);
static ssize_t (*bcopy_real)(void *,uint8_t,size_t (*)(void *,void *),void *,unsigned);
static pthread_once_t short_once=PTHREAD_ONCE_INIT,bcopy_once=PTHREAD_ONCE_INIT;
typedef struct {uint64_t source,target,highest;} peer_t;
static peer_t peers[256];
static unsigned peer_count;
static void *handles[8];
static unsigned handle_count;

static int find_provider(struct dl_phdr_info *info,size_t size,void *arg)
{
    (void)size; (void)arg;
    const char *name=strrchr(info->dlpi_name,'/'); name=name ? name+1:info->dlpi_name;
    if (strcmp(name,"liburma.so") && strncmp(name,"liburma.so.",11)) return 0;
    if (handle_count==8) return 0;
    void *handle=dlopen(info->dlpi_name,RTLD_NOW|RTLD_LOCAL|RTLD_NOLOAD);
    if (!handle) return 0;
    handles[handle_count++]=handle;
    if (!post) *(void **)(&post)=dlsym(handle,"urma_post_jetty_send_wr");
    if (!poll_real) *(void **)(&poll_real)=dlsym(handle,"urma_poll_jfc");
    return post && poll_real;
}
static void resolve(void)
{
    *(void **)(&post)=dlsym(RTLD_NEXT,"urma_post_jetty_send_wr");
    *(void **)(&poll_real)=dlsym(RTLD_NEXT,"urma_poll_jfc");
    if (!post || !poll_real) dl_iterate_phdr(find_provider,NULL);
    if (!post || !poll_real) { const char s[]="SEND_PROFILE_URMA_RESOLVE_ERROR\n"; ssize_t ignored=write(2,s,sizeof(s)-1); (void)ignored; _exit(127); }
}
static uint64_t little(const uint8_t *p,unsigned n)
{
    uint64_t value=0;
    for (unsigned i=0;i<n;++i) value|=(uint64_t)p[i]<<(8*i);
    return value;
}
typedef struct {uint64_t bytes,source,target,psn; unsigned inl,complete,kind;} sample_t;
urma_status_t urma_post_jetty_send_wr(urma_jetty_t *jetty,urma_jfs_wr_t *wr,urma_jfs_wr_t **bad)
{
    int entry_errno=errno;
    pthread_once(&once,resolve);
    sample_t sample[256]; unsigned count=0,bad_meta=0;
    int active=__atomic_load_n(&hucx_profile_active,__ATOMIC_ACQUIRE);
    if (active && jetty && jetty->jetty_cfg.jfs_cfg.trans_mode==URMA_TM_RM) {
        urma_jfs_wr_t *item=wr;
        for (unsigned i=0;item && i<256;++i,item=item->next) {
            if (item->opcode!=URMA_OPC_SEND) continue;
            if (item->send.src.num_sge>64 || (item->send.src.num_sge && !item->send.src.sge)) {++bad_meta;continue;}
            sample_t *s=&sample[count++]; memset(s,0,sizeof(*s));
            s->inl=!!item->flag.bs.inline_flag; s->complete=!!item->flag.bs.complete_enable;
            for (unsigned j=0;j<item->send.src.num_sge;++j) s->bytes+=item->send.src.sge[j].len;
            /* Fixed accepted97b ABI: packed UB AM header=3 bytes; reliable
             * prefix=36 bytes LE. Only examine this header, never user payload.
             * Generic control SENDs and split-SGE headers remain unclassified. */
            if (item->send.src.num_sge && item->send.src.sge[0].len>=39) {
                const uint8_t *p=(const void *)(uintptr_t)item->send.src.sge[0].addr;
                if (p && little(p+3,4)==0x33524d53u && p[7]==3 && p[10]==0 &&
                    (p[8]==1 || p[8]==2)) {
                    s->kind=p[8]; s->source=little(p+11,8); s->target=little(p+19,8); s->psn=little(p+27,8);
                }
            }
        }
        if (item) ++bad_meta;
    }
    errno=entry_errno;
    urma_status_t status=post(jetty,wr,bad); int saved=errno;
    if (active) {
        pthread_mutex_lock(&lock); incomplete+=bad_meta;
        errors+=(count && status!=URMA_SUCCESS);
        for (unsigned i=0;i<count;++i) {
            sample_t *s=&sample[i]; ++send_attempt[s->inl][s->complete];
            if (status!=URMA_SUCCESS) continue; /* No accepted-prefix inference. */
            ++send_ok[s->inl][s->complete]; bytes_ok+=s->bytes;
            if (s->bytes>max_bytes) max_bytes=s->bytes;
            if (s->kind==1) {
                ++data_ok;
                unsigned j;
                for(j=0;j<peer_count;++j) if(peers[j].source==s->source && peers[j].target==s->target) break;
                if(j==peer_count) {
                    if(j==256) {++incomplete;continue;}
                    peers[peer_count++]=(peer_t){s->source,s->target,0};
                }
                if(s->psn<=peers[j].highest) ++repeated_data;
                else peers[j].highest=s->psn;
            } else if (s->kind==2) ++ack_ok; else ++unknown_ok;
        }
        pthread_mutex_unlock(&lock);
    }
    errno=saved; return status;
}
int urma_poll_jfc(urma_jfc_t *jfc,int capacity,urma_cr_t *cr)
{
    int entry_errno=errno; pthread_once(&once,resolve); errno=entry_errno;
    int result=poll_real(jfc,capacity,cr),saved=errno;
    if (__atomic_load_n(&hucx_profile_active,__ATOMIC_ACQUIRE)) {
        pthread_mutex_lock(&lock); ++polls;
        empty_polls+=result==0; poll_errors+=result<0;
        if(result>capacity || (result>0 && !cr)) ++incomplete;
        else if(result>0) {cqes+=(unsigned)result; for(int i=0;i<result;++i) cq_errors+=cr[i].status!=URMA_CR_SUCCESS;}
        pthread_mutex_unlock(&lock);
    }
    errno=saved; return result;
}
static void *transport_symbol(const char *name)
{
    void *symbol=dlsym(RTLD_NEXT,name);
    if (!symbol) {
        const char *path=getenv("HUCX_SEND_PROFILE_MODULE");
        void *handle=path ? dlopen(path,RTLD_NOW|RTLD_LOCAL|RTLD_NOLOAD):NULL;
        if (handle) symbol=dlsym(handle,name); /* Keep loaded handle until exit. */
    }
    if(!symbol) _exit(127);
    return symbol;
}
static void resolve_short(void)
{ *(void **)(&short_real)=transport_symbol("uct_rm_urma_ep_am_short"); }
static void resolve_bcopy(void)
{ *(void **)(&bcopy_real)=transport_symbol("uct_rm_urma_ep_am_bcopy"); }
int uct_rm_urma_ep_am_short(void *ep,uint8_t id,uint64_t header,const void *buffer,unsigned length)
{
    int saved=errno;
    pthread_once(&short_once,resolve_short);
    if(__atomic_load_n(&hucx_profile_active,__ATOMIC_ACQUIRE)) __atomic_add_fetch(&short_calls,1,__ATOMIC_RELAXED);
    errno=saved; return short_real(ep,id,header,buffer,length);
}
ssize_t uct_rm_urma_ep_am_bcopy(void *ep,uint8_t id,size_t (*pack)(void *,void *),void *arg,unsigned flags)
{
    int saved=errno;
    pthread_once(&bcopy_once,resolve_bcopy);
    if(__atomic_load_n(&hucx_profile_active,__ATOMIC_ACQUIRE)) __atomic_add_fetch(&bcopy_calls,1,__ATOMIC_RELAXED);
    errno=saved; return bcopy_real(ep,id,pack,arg,flags);
}
void hucx_profile_counters(FILE *stream)
{
    pthread_mutex_lock(&lock);
    fprintf(stream,",\"rm_send_bytes\":%llu,\"max_send_bytes\":%llu,\"error_calls\":%llu,\"incomplete\":%llu,"
        "\"reliable_data\":%llu,\"reliable_ack\":%llu,\"repeated_successful_data_psn\":%llu,\"unclassified_send\":%llu,"
        "\"am_short_calls\":%llu,\"am_bcopy_calls\":%llu,\"process_jfc_poll_calls\":%llu,\"process_empty_polls\":%llu,"
        "\"process_cqes\":%llu,\"process_poll_errors\":%llu,\"process_cqe_errors\":%llu,\"send_flags\":[",
        (unsigned long long)bytes_ok,(unsigned long long)max_bytes,(unsigned long long)errors,(unsigned long long)incomplete,
        (unsigned long long)data_ok,(unsigned long long)ack_ok,(unsigned long long)repeated_data,(unsigned long long)unknown_ok,
        (unsigned long long)__atomic_load_n(&short_calls,__ATOMIC_RELAXED),(unsigned long long)__atomic_load_n(&bcopy_calls,__ATOMIC_RELAXED),(unsigned long long)polls,(unsigned long long)empty_polls,
        (unsigned long long)cqes,(unsigned long long)poll_errors,(unsigned long long)cq_errors);
    for(unsigned i=0;i<2;++i) for(unsigned c=0;c<2;++c)
        fprintf(stream,"%s{\"inline\":%u,\"complete_enable\":%u,\"attempts\":%llu,\"success\":%llu}",
            i||c ? ",":"",i,c,(unsigned long long)send_attempt[i][c],(unsigned long long)send_ok[i][c]);
    fprintf(stream,"]"); pthread_mutex_unlock(&lock);
}
