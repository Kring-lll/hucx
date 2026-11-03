/* Diagnostic phase boundary only. Forward MPI through the existing identity
 * interposer; do not replace any MPI data operation or clock function. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/resource.h>

int hucx_profile_active;
static uint64_t begin_ns;
static struct rusage before;
static struct rusage phase_end;
static uint64_t end_ns;
#ifndef PROFILE_PHASE_ONLY
void hucx_profile_counters(FILE *stream);
#endif
static uint64_t monotonic_ns(void)
{
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value)) _exit(126);
    return (uint64_t)value.tv_sec * 1000000000u + value.tv_nsec;
}
static void *next_symbol(const char *name)
{
    void *symbol = dlsym(RTLD_NEXT, name);
    if (!symbol) { ssize_t ignored=write(2, "SEND_PROFILE_FORWARD_ERROR\n", 27); (void)ignored; _exit(127); }
    return symbol;
}
static void marker(const char *suffix, int finalized)
{
    const char *dir = getenv("HUCX_OSU_PROBE_DIR");
    const char *rank = getenv("OMPI_COMM_WORLD_RANK");
    const char *nonce = getenv("A89_RUN_NONCE");
    char path[4096],temporary[4096];
    struct rusage after;
    if (!dir || !rank || !nonce || !*rank || strspn(rank,"0123456789") != strlen(rank) ||
        strlen(nonce) != 32 || strspn(nonce,"0123456789abcdef") != 32) _exit(126);
    int length = snprintf(path,sizeof(path),"%s/profile_rank%s_pid%ld%s.json",dir,rank,(long)getpid(),suffix);
    if (length < 0 || (size_t)length >= sizeof(path)) _exit(126);
    length=snprintf(temporary,sizeof(temporary),"%s.tmp",path);
    if (length<0 || (size_t)length>=sizeof(temporary)) _exit(126);
    int fd = open(temporary,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);
    if (fd < 0) _exit(126);
    FILE *stream = fdopen(fd,"w");
    if (!stream) _exit(126);
    if (end_ns) after=phase_end;
    else if (getrusage(RUSAGE_SELF,&after)) _exit(126);
    fprintf(stream,"{\"schema\":1,\"rank\":%s,\"pid\":%ld,\"nonce\":\"%s\","
            "\"begin_ns\":%llu,\"at_ns\":%llu,\"finalized\":%s,"
            "\"voluntary_switches\":%ld,\"involuntary_switches\":%ld",
            rank,(long)getpid(),nonce,(unsigned long long)begin_ns,
            (unsigned long long)(end_ns ? end_ns:monotonic_ns()),finalized ? "true":"false",
            after.ru_nvcsw-before.ru_nvcsw,after.ru_nivcsw-before.ru_nivcsw);
#ifndef PROFILE_PHASE_ONLY
    if (!strcmp(suffix,"")) hucx_profile_counters(stream);
#endif
    fprintf(stream,"}\n");
    if (fclose(stream)) _exit(126);
    /* Publish a complete JSON without replacing any existing evidence. */
    if (link(temporary,path) || unlink(temporary)) _exit(126);
}
static void started(int code)
{
    if (code) return;
    if (getrusage(RUSAGE_SELF,&before)) _exit(126);
    begin_ns = monotonic_ns();
    __atomic_store_n(&hucx_profile_active,1,__ATOMIC_RELEASE);
    marker("_ready",0);
}
int MPI_Init(int *argc, char ***argv)
{
    int (*real)(int *,char ***) = next_symbol("MPI_Init");
    int code=real(argc,argv); int saved=errno; started(code); errno=saved; return code;
}
int MPI_Init_thread(int *argc, char ***argv, int required, int *provided)
{
    int (*real)(int *,char ***,int,int *)=next_symbol("MPI_Init_thread");
    int code=real(argc,argv,required,provided); int saved=errno; started(code); errno=saved; return code;
}
int MPI_Finalize(void)
{
    int (*real)(void)=next_symbol("MPI_Finalize");
    __atomic_store_n(&hucx_profile_active,0,__ATOMIC_RELEASE);
    end_ns=monotonic_ns();
    if (getrusage(RUSAGE_SELF,&phase_end)) _exit(126);
    marker("_end",0);
    int code=real(); int saved=errno; marker("",code==0); errno=saved; return code;
}
