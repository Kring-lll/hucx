/* Public UCP API integration. TCP carries bootstrap and markers only; the
 * 8B..64KiB payload under test travels through ucp_put_nbi/ucp_get_nbi. */
#define main unused_uct_write_probe_main
#include "../native_write/write_probe.c"
#undef main
#include <ucp/api/ucp.h>

enum { CAPACITY = 65536, GUARD = 128, ALLOCATION = CAPACITY + 2 * GUARD };
static ucp_worker_h uworker;
static double udeadline;

static void uprogress(void)
{
    ucp_worker_progress(uworker);
    require(seconds() < udeadline, "UCP progress deadline");
}

static void umarker(int fd, char expected)
{
    char value;
    ssize_t n;
    for (;;) {
        uprogress();
        n = recv(fd, &value, 1, MSG_DONTWAIT);
        if (n == 1) {
            require(value == expected, "UCP phase marker");
            return;
        }
        require(n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR),
                "UCP marker socket");
    }
}

static void uwait(void *request)
{
    ucs_status_t status;
    require(!UCS_PTR_IS_ERR(request), "UCP request initiation");
    if (request == NULL) {
        return;
    }
    do {
        uprogress();
        status = ucp_request_check_status(request);
    } while (status == UCS_INPROGRESS);
    checked(status, "UCP request completion");
    ucp_request_free(request);
}

static unsigned char value(unsigned round, size_t index)
{
    return (unsigned char)(round * 37u + index * 13u + (index >> 8));
}

int main(int argc, char **argv)
{
    ucp_config_t *config;
    ucp_context_h context;
    ucp_worker_params_t wp = { .field_mask = UCP_WORKER_PARAM_FIELD_THREAD_MODE,
                               .thread_mode = UCS_THREAD_MODE_SINGLE };
    ucp_params_t cp = { .field_mask = UCP_PARAM_FIELD_FEATURES,
                        .features = UCP_FEATURE_RMA };
    ucp_ep_params_t ep_params = {0};
    ucp_request_param_t request_params = {0};
    ucp_mem_map_params_t mp = {0};
    ucp_mem_h memory;
    ucp_ep_h ep;
    ucp_rkey_h key;
    ucp_address_t *worker_address;
    size_t worker_length, key_length;
    uint64_t remote_length, remote_address;
    void *peer_worker, *packed_key, *peer_key;
    unsigned char *buffer;
    int provider, fd;
    char ack;
    const size_t sizes[] = {8, 65536, 64, 16385, 4096, 8193, 8, 65536};

    require(argc == 5, "UCP listen|connect IP PORT MAPS");
    provider = !strcmp(argv[1], "listen");
    require(provider || !strcmp(argv[1], "connect"), "UCP role");
    setvbuf(stdout, NULL, _IOLBF, 0);
    fd = socket_open(provider, argv[2], strtoul(argv[3], NULL, 10));
    udeadline = seconds() + 130;
    checked(ucp_config_read(NULL, NULL, &config), "UCP config");
    checked(ucp_init(&cp, config, &context), "UCP init RMA only");
    ucp_config_release(config);
    checked(ucp_worker_create(context, &wp, &uworker), "UCP worker");
    checked(ucp_worker_get_address(uworker, &worker_address, &worker_length), "UCP address");
    /* Exchange sequentially in opposite directions to avoid filling a TCP
     * socket with a variable-sized address before the peer can receive it. */
    for (unsigned side = 0; side < 2; ++side) {
        if (provider == (int)side) {
            remote_length = worker_length;
            bytes(fd, &remote_length, sizeof(remote_length), 1);
            bytes(fd, worker_address, worker_length, 1);
        } else {
            bytes(fd, &remote_length, sizeof(remote_length), 0);
            require(remote_length > 0 && remote_length <= 1048576, "UCP address length");
            peer_worker = malloc(remote_length);
            require(peer_worker != NULL, "peer address allocation");
            bytes(fd, peer_worker, remote_length, 0);
        }
    }
    ep_params.field_mask = UCP_EP_PARAM_FIELD_REMOTE_ADDRESS;
    ep_params.address = peer_worker;
    checked(ucp_ep_create(uworker, &ep_params, &ep), "UCP create RMA endpoint");
    free(peer_worker);
    ucp_worker_release_address(uworker, worker_address);
    buffer = malloc(ALLOCATION);
    require(buffer != NULL, "UCP guarded buffer");
    mp.field_mask = UCP_MEM_MAP_PARAM_FIELD_ADDRESS | UCP_MEM_MAP_PARAM_FIELD_LENGTH;
    mp.address = buffer;
    mp.length = ALLOCATION;
    checked(ucp_mem_map(context, &mp, &memory), "UCP memory map");
    checked(ucp_rkey_pack(context, memory, &packed_key, &key_length), "UCP key pack");
    if (provider) {
        remote_address = (uint64_t)(buffer + GUARD);
        remote_length = key_length;
        bytes(fd, &remote_length, sizeof(remote_length), 1);
        bytes(fd, packed_key, key_length, 1);
        bytes(fd, &remote_address, sizeof(remote_address), 1);
    } else {
        bytes(fd, &remote_length, sizeof(remote_length), 0);
        require(remote_length > 0 && remote_length <= 1048576, "UCP rkey length");
        peer_key = malloc(remote_length);
        require(peer_key != NULL, "UCP peer key buffer");
        bytes(fd, peer_key, remote_length, 0);
        bytes(fd, &remote_address, sizeof(remote_address), 0);
        checked(ucp_ep_rkey_unpack(ep, peer_key, &key), "UCP rkey unpack");
        free(peer_key);
    }
    ucp_rkey_buffer_release(packed_key);
    for (unsigned round = 0; round < 16; ++round) {
        size_t n = sizes[round % 8];
        int read = round >= 8;
        memset(buffer, 0xa5, ALLOCATION);
        if ((provider && read) || (!provider && !read)) {
            for (size_t i = 0; i < n; ++i) {
                buffer[GUARD + i] = value(round, i);
            }
        }
        if (provider) {
            ack = 'R';
            bytes(fd, &ack, 1, 1);
            umarker(fd, 'D');
        } else {
            ucs_status_t status;
            umarker(fd, 'R');
            status = read ? ucp_get_nbi(ep, buffer + GUARD, n, remote_address, key) :
                            ucp_put_nbi(ep, buffer + GUARD, n, remote_address, key);
            require(status == UCS_OK || status == UCS_INPROGRESS, "UCP PUT/GET submit");
            uwait(ucp_ep_flush_nbx(ep, &request_params));
            ack = 'D';
            bytes(fd, &ack, 1, 1);
        }
        for (size_t i = 0; i < ALLOCATION; ++i) {
            unsigned char expected = (i >= GUARD && i < GUARD + n) ?
                                     value(round, i - GUARD) : 0xa5;
            if (buffer[i] != expected) {
                fprintf(stderr, "UCP_RMA_MISMATCH round=%u op=%s bytes=%zu role=%s "
                        "index=%zu region=%s expected=%u actual=%u\n",
                        round, read ? "GET" : "PUT", n,
                        provider ? "provider" : "initiator", i,
                        (i >= GUARD && i < GUARD + n) ? "payload" : "guard",
                        (unsigned)expected, (unsigned)buffer[i]);
                require(0, "UCP full data and guards");
            }
        }
        if (provider) {
            ack = 'V';
            bytes(fd, &ack, 1, 1);
        } else {
            umarker(fd, 'V');
        }
        printf("UCP_RMA_ROUND=%u OP=%s BYTES=%zu ROLE=%s DATA=PASS\n",
               round, read ? "GET" : "PUT", n, provider ? "provider" : "initiator");
    }
    ucp_ep_print_info(ep, stdout);
    maps(argv[4]);
    if (!provider) {
        ucp_rkey_destroy(key);
    }
    uwait(ucp_ep_close_nbx(ep, &request_params));
    checked(ucp_mem_unmap(context, memory), "UCP memory unmap");
    free(buffer);
    ucp_worker_destroy(uworker);
    ucp_cleanup(context);
    close(fd);
    puts("UCP_RMA_PASS ROUNDS=16 STRICT_SIZE_ALTERNATION=YES GUARDS=PASS CLEANUP=PASS");
    return 0;
}
