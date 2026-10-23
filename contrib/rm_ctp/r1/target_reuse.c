/* Two-process UCP correctness probe, not an MPI or latency benchmark.
 * TCP exchanges worker addresses and completion markers only. The four
 * tagged payloads use UCP with the UCX_TLS selected by the caller. */
#include <ucp/api/ucp.h>
#include <ucs/type/status.h>
#include <arpa/inet.h>
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

enum { EP_COUNT = 4, PAYLOAD_BYTES = 64, DEADLINE_SECONDS = 60 };

static void fail(const char *where)
{
    fprintf(stderr, "TARGET_REUSE_FAILED: %s errno=%d\n", where, errno);
    exit(1);
}

static double now(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        fail("clock_gettime");
    }
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static void check(ucs_status_t status, const char *where)
{
    if (status != UCS_OK) {
        fprintf(stderr, "%s: %s\n", where, ucs_status_string(status));
        fail(where);
    }
}

static void transfer(int fd, void *buffer, size_t length, int sending)
{
    char *ptr = buffer;
    ssize_t count;

    while (length != 0) {
        count = sending ? send(fd, ptr, length, MSG_NOSIGNAL) :
                          recv(fd, ptr, length, 0);
        if ((count < 0) && (errno == EINTR)) {
            continue;
        }
        if (count <= 0) {
            fail("bootstrap transfer");
        }
        ptr += count;
        length -= count;
    }
}

static int bootstrap(int server, const char *ip, unsigned port)
{
    struct sockaddr_in address = {};
    struct timeval timeout = { .tv_sec = DEADLINE_SECONDS };
    struct timespec pause = { .tv_nsec = 100000000 };
    double deadline = now() + DEADLINE_SECONDS;
    int fd, listener, yes = 1;

    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    if (inet_pton(AF_INET, ip, &address.sin_addr) != 1) {
        fail("numeric IPv4 address required");
    }
    if (server) {
        listener = socket(AF_INET, SOCK_STREAM, 0);
        if ((listener < 0) ||
            (setsockopt(listener, SOL_SOCKET, SO_REUSEADDR,
                        &yes, sizeof(yes)) != 0) ||
            (bind(listener, (struct sockaddr *)&address, sizeof(address)) != 0) ||
            (listen(listener, 1) != 0)) {
            fail("bootstrap listen");
        }
        /* The caller also runs each process under an external timeout. */
        fd = accept(listener, NULL, NULL);
        close(listener);
    } else {
        do {
            fd = socket(AF_INET, SOCK_STREAM, 0);
            if (fd < 0) {
                fail("bootstrap socket");
            }
            if (connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0) {
                break;
            }
            close(fd);
            fd = -1;
            nanosleep(&pause, NULL);
        } while (now() < deadline);
    }
    if ((fd < 0) ||
        (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0) ||
        (setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) != 0)) {
        fail("bootstrap connection");
    }
    return fd;
}

static void wait_request(ucp_worker_h worker, ucs_status_ptr_t request)
{
    ucs_status_t status;
    double deadline = now() + DEADLINE_SECONDS;

    if (UCS_PTR_IS_ERR(request)) {
        check(UCS_PTR_STATUS(request), "request submission");
    }
    if (request == NULL) {
        return;
    }
    while ((status = ucp_request_check_status(request)) == UCS_INPROGRESS) {
        ucp_worker_progress(worker);
        if (now() >= deadline) {
            fail("UCP request timeout");
        }
    }
    ucp_request_free(request);
    check(status, "request completion");
}

int main(int argc, char **argv)
{
    ucp_params_t params = { .field_mask = UCP_PARAM_FIELD_FEATURES,
                            .features = UCP_FEATURE_TAG };
    ucp_worker_params_t worker_params = {
        .field_mask = UCP_WORKER_PARAM_FIELD_THREAD_MODE,
        .thread_mode = UCS_THREAD_MODE_SINGLE
    };
    ucp_request_param_t request_params = {};
    ucp_ep_params_t ep_params = { .field_mask = UCP_EP_PARAM_FIELD_REMOTE_ADDRESS };
    ucp_config_t *config;
    ucp_context_h context;
    ucp_worker_h worker;
    ucp_address_t *address;
    ucp_ep_h eps[EP_COUNT];
    ucs_status_ptr_t requests[EP_COUNT];
    unsigned char payload[EP_COUNT][PAYLOAD_BYTES] = {};
    size_t address_length;
    uint32_t length;
    void *remote;
    unsigned i, j, port;
    int fd, server;
    char marker;
    char *end;
    unsigned long parsed;
    double deadline;
    ssize_t count;

    if ((argc != 4) || (strcmp(argv[1], "listen") && strcmp(argv[1], "connect"))) {
        fprintf(stderr, "usage: %s listen|connect SERVER_IPV4 PORT\n", argv[0]);
        return 2;
    }
    parsed = strtoul(argv[3], &end, 10);
    if ((*argv[3] == '\0') || (*end != '\0') || (parsed == 0) || (parsed > 65535)) {
        fail("invalid port");
    }
    port = parsed;
    server = !strcmp(argv[1], "listen");
    setvbuf(stdout, NULL, _IOLBF, 0);
    fd = bootstrap(server, argv[2], port);
    check(ucp_config_read(NULL, NULL, &config), "ucp_config_read");
    check(ucp_init(&params, config, &context), "ucp_init");
    ucp_config_release(config);
    check(ucp_worker_create(context, &worker_params, &worker), "worker create");
    check(ucp_worker_get_address(worker, &address, &address_length), "worker address");
    if (address_length > UINT32_MAX) {
        fail("worker address too large");
    }

    if (!server) {
        length = htonl(address_length);
        transfer(fd, &length, sizeof(length), 1);
        transfer(fd, address, address_length, 1);
    }
    transfer(fd, &length, sizeof(length), 0);
    length = ntohl(length);
    if ((length == 0) || (length > 1024 * 1024)) {
        fail("invalid remote worker address length");
    }
    remote = malloc(length);
    if (remote == NULL) {
        fail("remote address allocation");
    }
    transfer(fd, remote, length, 0);
    if (server) {
        length = htonl(address_length);
        transfer(fd, &length, sizeof(length), 1);
        transfer(fd, address, address_length, 1);
    }
    ucp_worker_release_address(worker, address);

    if (!server) {
        ep_params.address = remote;
        for (i = 0; i < EP_COUNT; ++i) {
            check(ucp_ep_create(worker, &ep_params, &eps[i]), "endpoint create");
        }
    }
    free(remote);
    for (i = 0; i < EP_COUNT; ++i) {
        if (server) {
            requests[i] = ucp_tag_recv_nbx(worker, payload[i], PAYLOAD_BYTES,
                                           0xa400 + i, UINT64_MAX, &request_params);
        } else {
            for (j = 0; j < PAYLOAD_BYTES; ++j) {
                payload[i][j] = (unsigned char)(17 * i + j);
            }
            requests[i] = ucp_tag_send_nbx(eps[i], payload[i], PAYLOAD_BYTES,
                                           0xa400 + i, &request_params);
        }
    }
    for (i = 0; i < EP_COUNT; ++i) {
        wait_request(worker, requests[i]);
    }
    if (server) {
        for (i = 0; i < EP_COUNT; ++i) {
            for (j = 0; j < PAYLOAD_BYTES; ++j) {
                if (payload[i][j] != (unsigned char)(17 * i + j)) {
                    fail("payload validation");
                }
            }
        }
        /* Progress the receiver while the sender closes its endpoints. */
        deadline = now() + DEADLINE_SECONDS;
        do {
            ucp_worker_progress(worker);
            count = recv(fd, &marker, 1, MSG_DONTWAIT);
            if (count == 1) {
                break;
            }
            if ((count == 0) || ((errno != EAGAIN) && (errno != EINTR)) ||
                (now() >= deadline)) {
                fail("peer close marker");
            }
        } while (1);
        if (marker != 'D') {
            fail("invalid close marker");
        }
    } else {
        for (i = 0; i < EP_COUNT; ++i) {
            wait_request(worker, ucp_ep_close_nbx(eps[i], &request_params));
        }
        marker = 'D';
        transfer(fd, &marker, 1, 1);
    }
    ucp_worker_destroy(worker);
    ucp_cleanup(context);
    close(fd);
    printf("TARGET_REUSE_PAYLOAD_PASS ROLE=%s EPS=%d BYTES=%d\n",
           server ? "receiver" : "sender", EP_COUNT, PAYLOAD_BYTES);
    return 0;
}
