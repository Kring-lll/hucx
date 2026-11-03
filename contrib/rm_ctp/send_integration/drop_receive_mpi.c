/* Test-only receive interposer. Faults occur after hardware receive success,
 * before the unchanged reliable_v3 decoder. Never a hardware-loss experiment. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef int (*receive_t)(void *, const void *, size_t);
static receive_t real_receive;
static unsigned data_drops;
static unsigned ack_drops;
static unsigned duplicated;
static unsigned calls;
static uint64_t message_id;
static uint64_t total_bytes;
static uint64_t source_instance;
static uint64_t duplicate_offset;
static uint64_t drop_offset;
static uint64_t duplicate_psn;
static uint64_t drop_psn;
static int tracking;

static uint64_t get_le(const unsigned char *p, unsigned count)
{
    uint64_t value = 0;
    for (unsigned i = 0; i < count; ++i) {
        value |= (uint64_t)p[i] << (8 * i);
    }
    return value;
}

int uct_rm_urma_reliable_receive(void *, const void *, size_t);

static void resolve(void)
{
    const char *module = getenv("HUCX_SEND_TEST_MODULE");
    const char *profile = getenv("HUCX_SEND_INJECT_PROFILE");
    if (profile == NULL || strcmp(profile, "eager_middle") != 0) {
        fputs("SEND_INJECTION_INVALID_PROFILE\n", stderr);
        exit(72);
    }
    void *handle = module ? dlopen(module, RTLD_NOW | RTLD_NOLOAD) : NULL;
    if (handle != NULL) {
        real_receive = (receive_t)dlsym(handle, "uct_rm_urma_reliable_receive");
    }
    if (real_receive == NULL || real_receive == uct_rm_urma_reliable_receive) {
        fputs("SEND_INJECTION_RESOLVE_FAILED\n", stderr);
        exit(73);
    }
}

/* Forward declaration is needed by resolve(), which rejects self-resolution. */
int uct_rm_urma_reliable_receive(void *iface, const void *buffer, size_t length)
{
    const unsigned char *wire = buffer;
    if (real_receive == NULL) {
        resolve();
    }
    ++calls;
    if (wire == NULL || length < 36 || get_le(wire, 4) != 0x33524d53u ||
        wire[4] != 3 || wire[7] != 0) {
        return real_receive(iface, buffer, length);
    }
    uint64_t payload = get_le(wire + 32, 4);
    if (wire[5] == 2 && length == 36 && payload == 0 && ack_drops < 2) {
        ++ack_drops;
        return 0;
    }
    if (wire[5] != 1 || length < 104 || payload != length - 104) {
        return real_receive(iface, buffer, length);
    }
    const unsigned char *ucp = wire + 104;
    uint64_t source = get_le(wire + 8, 8);
    uint64_t psn = get_le(wire + 24, 8);
    /* Pinned UCP tag IDs/layout: FIRST=3, tag/total_len/msg_id=8/8/8;
     * MIDDLE=4, msg_id/offset=8/8. Validate these against product source in CPU tests. */
    if (!tracking && wire[6] == 3 && payload >= 24 && get_le(ucp + 8, 8) == 65536) {
        int status = real_receive(iface, buffer, length);
        if (status == 0) {
            message_id = get_le(ucp + 16, 8);
            total_bytes = get_le(ucp + 8, 8);
            source_instance = source;
            tracking = 1;
        }
        return status;
    }
    if (tracking && wire[6] == 4 && payload > 16 && source == source_instance &&
        get_le(ucp, 8) == message_id) {
        uint64_t offset = get_le(ucp + 8, 8);
        uint64_t fragment_bytes = payload - 16;
        /* Only interior fragments: neither the first nor the last fragment. */
        if (offset > 0 && offset < total_bytes && fragment_bytes < total_bytes - offset) {
            if (!duplicated) {
                int status = real_receive(iface, buffer, length);
                if (status != 0) {
                    return status;
                }
                duplicated = 1;
                duplicate_offset = offset;
                duplicate_psn = psn;
                return real_receive(iface, buffer, length);
            }
            if (!data_drops && offset > duplicate_offset && psn > duplicate_psn) {
                data_drops = 1;
                drop_offset = offset;
                drop_psn = psn;
                return 0;
            }
        }
    }
    return real_receive(iface, buffer, length);
}

__attribute__((destructor)) static void report(void)
{
    const char *directory = getenv("HUCX_OSU_PROBE_DIR");
    const char *nonce = getenv("A89_RUN_NONCE");
    const char *rank = getenv("OMPI_COMM_WORLD_RANK");
    /* timeout/env wrappers can inherit LD_PRELOAD; never count their counters. */
    if (calls == 0 || directory == NULL || nonce == NULL || rank == NULL) {
        return;
    }
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/inject_rank%d_pid%ld.json",
                 directory, atoi(rank), (long)getpid()) >= (int)sizeof(path)) {
        return;
    }
    FILE *file = fopen(path, "wx");
    if (file == NULL) {
        fputs("SEND_INJECTION_PROOF_WRITE_FAILED\n", stderr);
        return;
    }
    fprintf(file, "{\"rank\":%d,\"pid\":%ld,\"nonce\":\"%s\",\"calls\":%u,"
                  "\"data_drops\":%u,\"ack_drops\":%u,\"duplicates\":%u,"
                  "\"message_id\":%llu,\"total_bytes\":%llu,"
                  "\"duplicate_offset\":%llu,\"drop_offset\":%llu,"
                  "\"duplicate_psn\":%llu,\"drop_psn\":%llu}\n",
            atoi(rank), (long)getpid(), nonce, calls, data_drops, ack_drops, duplicated,
            (unsigned long long)message_id, (unsigned long long)total_bytes,
            (unsigned long long)duplicate_offset, (unsigned long long)drop_offset,
            (unsigned long long)duplicate_psn, (unsigned long long)drop_psn);
    if (fclose(file) != 0) {
        fputs("SEND_INJECTION_PROOF_CLOSE_FAILED\n", stderr);
    }
}
