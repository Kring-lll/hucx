/* NODE SOFTWARE INJECTION ONLY. Drop after a successful hardware receive and
 * before the production reliable decoder. This does not emulate provider CQ
 * errors, link failures, hardware corruption or actual on-wire loss. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef int (*receive_t)(void *, const void *, size_t);
static receive_t real_receive;
static unsigned data_drops, ack_drops, duplicated, calls;
static uint64_t read64(const unsigned char *p)
{
    uint64_t n = 0;
    for (unsigned i = 0; i < 8; ++i) {
        n |= (uint64_t)p[i] << (8 * i);
    }
    return n;
}

int uct_rm_urma_reliable_receive(void *iface, const void *buffer, size_t length)
{
    const unsigned char *wire = buffer;
    if (real_receive == NULL) {
        const char *module = getenv("HUCX_SEND_TEST_MODULE");
        void *handle = module ? dlopen(module, RTLD_NOW | RTLD_NOLOAD) : NULL;
        if (handle != NULL) {
            real_receive = (receive_t)dlsym(handle, "uct_rm_urma_reliable_receive");
        }
        if ((real_receive == NULL) || (real_receive == uct_rm_urma_reliable_receive)) {
            fputs("SEND_INJECTION_RESOLVE_FAILED\n", stderr);
            exit(72);
        }
    }
    ++calls;
    if ((length >= 36) && (wire[4] == 3)) {
        uint64_t psn = read64(wire + 24);
        if ((wire[5] == 1) && (psn == 3) && !data_drops) {
            ++data_drops;
            fputs("SEND_SOFTWARE_DROP DATA PSN=3\n", stderr);
            return 0;
        }
        if ((wire[5] == 2) && (ack_drops < 2)) {
            ++ack_drops;
            fputs("SEND_SOFTWARE_DROP ACK\n", stderr);
            return 0;
        }
        if ((wire[5] == 1) && (psn == 2) && !duplicated) {
            int status = real_receive(iface, buffer, length);
            ++duplicated;
            if (status != 0) {
                return status;
            }
            fputs("SEND_SOFTWARE_DUPLICATE DATA PSN=2\n", stderr);
        }
    }
    return real_receive(iface, buffer, length);
}

__attribute__((destructor)) static void report(void)
{
    fprintf(stderr, "SEND_SOFTWARE_INJECTION CALLS=%u DATA_DROPS=%u ACK_DROPS=%u DUPLICATES=%u\n",
            calls, data_drops, ack_drops, duplicated);
}
