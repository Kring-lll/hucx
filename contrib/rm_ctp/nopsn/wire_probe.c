/* Validation-only SEND observer; never linked/preloaded in timing trials. */
#define _GNU_SOURCE
#include <urma_api.h>
#include <dlfcn.h>
#include <link.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <limits.h>

static pthread_once_t once=PTHREAD_ONCE_INIT;
static pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER;
static urma_status_t (*real_post)(urma_jetty_t *,urma_jfs_wr_t *,urma_jfs_wr_t **);
static unsigned long data,shorts,bcopies,retries,controls,errors,max_bytes,headers;
static char provider[PATH_MAX];
static unsigned local_handle_used;
static void *provider_handle;
static int find_loaded_provider(struct dl_phdr_info *info, size_t size, void *arg)
{
    (void)size;
    (void)arg;
    const char *name = strrchr(info->dlpi_name, '/');
    name = name != NULL ? name + 1 : info->dlpi_name;
    if (strcmp(name, "liburma.so") != 0 && strncmp(name, "liburma.so.", 11) != 0) {
        return 0;
    }
    /* Reuse an already loaded object; never introduce a second URMA library. */
    void *handle = dlopen(info->dlpi_name, RTLD_NOW | RTLD_NOLOAD | RTLD_LOCAL);
    if (handle == NULL) {
        return 0;
    }
    void *symbol = dlsym(handle, "urma_post_jetty_send_wr");
    if (symbol != NULL && symbol != (void *)urma_post_jetty_send_wr) {
        provider_handle = handle;
        local_handle_used = 1;
        *(void **)(&real_post) = symbol;
        return 1;
    }
    dlclose(handle);
    return 0;
}

static void resolve(void)
{
    Dl_info info;
    *(void **)(&real_post) = dlsym(RTLD_NEXT, "urma_post_jetty_send_wr");
    if (real_post == NULL) {
        dl_iterate_phdr(find_loaded_provider, NULL);
    }
    if (real_post == NULL) {
        const char message[] = "NOPSN_WIRE_RESOLVE_ERROR: no accessible loaded URMA send symbol\n";
        ssize_t written = write(STDERR_FILENO, message, sizeof(message) - 1);
        (void)written;
        _exit(121);
    }
    if (dladdr((void *)real_post, &info) && info.dli_fname != NULL) {
        (void)snprintf(provider, sizeof(provider), "%s", info.dli_fname);
    }
}


urma_status_t urma_post_jetty_send_wr(urma_jetty_t *j,urma_jfs_wr_t *w,urma_jfs_wr_t **bad)
{
    urma_jfs_wr_t *p;
    urma_status_t rc;
    pthread_once(&once,resolve);
    rc=real_post(j,w,bad);
    if (rc!=URMA_SUCCESS) return rc;
    pthread_mutex_lock(&lock);
    for(p=w;p;p=p->next) {
        size_t total=0;
        uint32_t type=0;
        unsigned i;
        if(p->opcode!=URMA_OPC_SEND) { ++errors; continue; }
        for(i=0;i<p->send.src.num_sge;++i) total+=p->send.src.sge[i].len;
        if(total>max_bytes) max_bytes=total;
        if(total>4096 || p->send.src.num_sge<1 || p->send.src.sge[0].len<4) {
            ++errors;continue;
        }
        memcpy(&type,(void *)(uintptr_t)p->send.src.sge[0].addr,4);
        if(!(type&(1u<<24))) { ++controls;continue; }
        ++data;
        if(p->send.src.num_sge==2 && p->flag.bs.inline_flag && !p->flag.bs.solicited_enable) {
            unsigned first=p->send.src.sge[0].len;
            ++shorts;
            if(first==14) headers|=1u<<6;
            else if(first==16) headers|=1u<<8;
            else ++errors;
        } else if(p->send.src.num_sge==1 && !p->flag.bs.inline_flag && !p->flag.bs.solicited_enable) {
            ++bcopies;
        } else if(p->send.src.num_sge==2 && p->send.src.sge[0].len==8 &&
                  p->flag.bs.complete_enable && p->flag.bs.solicited_enable) {
            ++retries;
        } else ++errors;
    }
    pthread_mutex_unlock(&lock);
    return rc;
}

__attribute__((destructor)) static void receipt(void)
{
    const char *dir=getenv("HUCX_NOPSN_WIRE_DIR"),*nonce=getenv("A89_RUN_NONCE");
    const char *rank=getenv("OMPI_COMM_WORLD_RANK");
    char path[PATH_MAX],body[2048];
    int fd,n;
    if(!dir || !nonce || !rank) return;
    n=snprintf(path,sizeof(path),"%s/nopsn_wire_rank%d_pid%d.json",dir,atoi(rank),getpid());
    if(n<0 || (size_t)n>=sizeof(path)) return;
    n=snprintf(body,sizeof(body),"{\"schema\":1,\"rank\":%d,\"pid\":%d,\"nonce\":\"%s\","
        "\"data_success\":%lu,\"short_success\":%lu,\"bcopy_success\":%lu,\"retry_success\":%lu,"
        "\"control_success\":%lu,\"max_wr_bytes\":%lu,\"header_mask\":%lu,\"errors\":%lu,\"local_handle_used\":%u,\"provider_resolved\":%s}\n",
        atoi(rank),getpid(),nonce,data,shorts,bcopies,retries,controls,max_bytes,headers,errors,local_handle_used,provider[0] ? "true" : "false");
    if(n<0 || (size_t)n>=sizeof(body)) return;
    fd=open(path,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);
    if(fd<0) return;
    (void)write(fd,body,n);close(fd);
}
