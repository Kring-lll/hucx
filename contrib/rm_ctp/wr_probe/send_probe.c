/* Diagnostic observer only: never reads payload or changes a submitted WR. */
#define _GNU_SOURCE
#include <urma_api.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <limits.h>

#define MAX_CHAIN 256
#define MAX_SGE 64
#define MAX_BUCKETS 512

typedef urma_status_t (*post_fn)(urma_jetty_t *, urma_jfs_wr_t *, urma_jfs_wr_t **);
typedef struct {
    uint64_t bytes;
    uint64_t submitted;
    uint64_t success;
    unsigned inline_flag;
    unsigned num_sge;
} bucket_t;
static post_fn original;
static pthread_once_t resolve_once = PTHREAD_ONCE_INIT;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static bucket_t buckets[MAX_BUCKETS];
static size_t used;
static uint64_t calls, errors, other_wrs, incomplete;
static char provider[PATH_MAX];

static void resolve_original(void)
{
    Dl_info info;
    *(void **)(&original) = dlsym(RTLD_NEXT, "urma_post_jetty_send_wr");
    if (original == NULL) {
        const char message[] = "WR_PROBE_RESOLVE_ERROR: no next URMA send symbol\n";
        ssize_t written = write(STDERR_FILENO, message, sizeof(message) - 1);
        (void)written;
        _exit(127);
    }
    if (dladdr((void *)original, &info) && info.dli_fname != NULL) {
        (void)snprintf(provider, sizeof(provider), "%s", info.dli_fname);
    }
}

urma_status_t urma_post_jetty_send_wr(urma_jetty_t *jetty, urma_jfs_wr_t *wr,
                                    urma_jfs_wr_t **bad_wr)
{
    bucket_t samples[MAX_CHAIN];
    size_t count = 0;
    unsigned skipped = 0;
    unsigned invalid = 0;
    urma_jfs_wr_t *current = wr;
    (void)pthread_once(&resolve_once, resolve_original);
    if (jetty == NULL || jetty->jetty_cfg.jfs_cfg.trans_mode != URMA_TM_RM) {
        return original(jetty, wr, bad_wr);
    }
    /* Snapshot metadata before forwarding: a provider may consume the WR. */
    for (unsigned n = 0; current != NULL && n < MAX_CHAIN; ++n) {
        if (current->opcode == URMA_OPC_SEND) {
            uint64_t bytes = 0;
            if (current->send.src.num_sge > MAX_SGE ||
                (current->send.src.num_sge != 0 && current->send.src.sge == NULL)) {
                ++invalid;
            } else {
                for (unsigned j = 0; j < current->send.src.num_sge; ++j) {
                    bytes += current->send.src.sge[j].len;
                }
                samples[count++] = (bucket_t){bytes, 1, 0,
                    current->flag.bs.inline_flag, current->send.src.num_sge};
            }
        } else {
            ++skipped;
        }
        current = current->next;
    }
    if (current != NULL) {
        ++invalid;
    }
    urma_status_t status = original(jetty, wr, bad_wr);
    (void)pthread_mutex_lock(&lock);
    ++calls;
    errors += status != URMA_SUCCESS;
    other_wrs += skipped;
    incomplete += invalid;
    for (size_t n = 0; n < count; ++n) {
        size_t j;
        for (j = 0; j < used; ++j) {
            if (buckets[j].bytes == samples[n].bytes &&
                buckets[j].inline_flag == samples[n].inline_flag &&
                buckets[j].num_sge == samples[n].num_sge) {
                break;
            }
        }
        if (j == used) {
            if (used == MAX_BUCKETS) {
                ++incomplete;
                continue;
            }
            buckets[used++] = samples[n];
            buckets[j].submitted = 0;
        }
        ++buckets[j].submitted;
        /* On a failed chain, accepted prefix is deliberately NOT inferred. */
        buckets[j].success += status == URMA_SUCCESS;
    }
    (void)pthread_mutex_unlock(&lock);
    return status;
}

__attribute__((destructor)) static void save_summary(void)
{
    const char *directory = getenv("HUCX_OSU_PROBE_DIR");
    const char *rank = getenv("OMPI_COMM_WORLD_RANK");
    const char *nonce = getenv("A89_RUN_NONCE");
    char path[PATH_MAX];
    if (directory == NULL || rank == NULL || nonce == NULL || calls == 0) {
        return;
    }
    /* Numeric rank/hex nonce make the JSON and filename unambiguous. */
    if (*rank == '\0' || strspn(rank, "0123456789") != strlen(rank) ||
        strlen(nonce) != 32 || strspn(nonce, "0123456789abcdef") != 32 ||
        snprintf(path, sizeof(path), "%s/wr_rank%s_pid%ld.json", directory,
                 rank, (long)getpid()) >= (int)sizeof(path)) {
        return;
    }
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (fd < 0) {
        return;
    }
    FILE *stream = fdopen(fd, "w");
    if (stream == NULL) {
        close(fd);
        return;
    }
    /* Provider path is not embedded as unchecked JSON; proof is via /proc maps. */
    fprintf(stream, "{\"schema\":1,\"rank\":%s,\"pid\":%ld,\"nonce\":\"%s\","
            "\"calls\":%llu,\"error_calls\":%llu,\"other_wrs\":%llu,"
            "\"incomplete\":%llu,\"provider_is_liburma\":%s,\"buckets\":[",
            rank, (long)getpid(), nonce, (unsigned long long)calls,
            (unsigned long long)errors, (unsigned long long)other_wrs,
            (unsigned long long)incomplete,
            strstr(provider, "/liburma.so") != NULL ? "true" : "false");
    for (size_t j = 0; j < used; ++j) {
        fprintf(stream, "%s{\"bytes\":%llu,\"num_sge\":%u,\"inline\":%u,"
                "\"submitted\":%llu,\"success\":%llu}", j ? "," : "",
                (unsigned long long)buckets[j].bytes, buckets[j].num_sge,
                buckets[j].inline_flag, (unsigned long long)buckets[j].submitted,
                (unsigned long long)buckets[j].success);
    }
    fprintf(stream, "]}\n");
    (void)fclose(stream);
}
