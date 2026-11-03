/* Software decoder loss only; actual hardware reception has already succeeded. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef int (*receive_t)(void *, const void *, size_t);
static receive_t forward;
static unsigned calls, dropped_data, dropped_ack;
int uct_rm_urma_reliable_receive(void *iface, const void *buffer, size_t length)
{
    const unsigned char *wire = buffer;
    const char *profile = getenv("HUCX_SEND_FAILURE_PROFILE");
    if (forward == NULL) {
        const char *module = getenv("HUCX_SEND_TEST_MODULE");
        void *handle = module ? dlopen(module, RTLD_NOW | RTLD_NOLOAD) : NULL;
        forward = handle ? (receive_t)dlsym(handle, "uct_rm_urma_reliable_receive") : NULL;
        if (forward == NULL || forward == uct_rm_urma_reliable_receive) {
            fputs("FAILURE_INJECTION_RESOLVE_FAILED\n", stderr);
            exit(72);
        }
    }
    ++calls;
    if (wire != NULL && length >= 36 && wire[0] == 0x53 && wire[1] == 0x4d &&
        wire[2] == 0x52 && wire[3] == 0x33 && wire[4] == 3 && profile != NULL) {
        if (length >= 104 && wire[5] == 1 && !strcmp(profile, "drop_data")) {
            ++dropped_data;
            return 0;
        }
        if (length == 36 && wire[5] == 2 && !strcmp(profile, "drop_ack")) {
            ++dropped_ack;
            return 0;
        }
    }
    return forward(iface, buffer, length);
}
__attribute__((destructor)) static void receipt(void)
{
    fprintf(stderr, "FAILURE_INJECTION CALLS=%u DATA=%u ACK=%u\n",
            calls, dropped_data, dropped_ack);
}
