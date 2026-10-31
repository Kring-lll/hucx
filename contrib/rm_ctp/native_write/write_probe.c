/* Direct UCT test only. TCP bootstraps addresses and final synchronization.
 * Payloads and credit grants use RM+CTP; receiver creates zero business EPs. */
#include <uct/api/uct.h>
#include <ucs/async/async_fwd.h>
#include <ucs/type/thread_mode.h>
#include "rm_urma_addr.h"
#include <arpa/inet.h>
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

enum { STREAMS = 4, PER_STREAM = 16, DATA_BYTES = 64, AM_ID = 1 };
typedef struct {
    uint32_t stream;
    uint32_t sequence;
    unsigned char body[DATA_BYTES - 8];
} message_t;
typedef struct {
    ucs_async_context_t *async;
    uct_worker_h worker;
    uct_md_h md;
    uct_component_h component;
    uct_iface_h iface;
    uct_iface_attr_t attr;
    unsigned seen[STREAMS][PER_STREAM];
    unsigned received;
    unsigned created;
    const char *role;
    double deadline;
} state_t;

static double seconds(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) {
        perror("clock_gettime");
        exit(2);
    }
    return t.tv_sec + t.tv_nsec * 1e-9;
}

static void require(int ok, const char *where)
{
    if (!ok) {
        fprintf(stderr, "DIRECT_IFACE_FAILED: %s errno=%d\n", where, errno);
        exit(2);
    }
}

static void checked(ucs_status_t status, const char *where)
{
    if (status != UCS_OK) {
        fprintf(stderr, "DIRECT_IFACE_STATUS: %s: %s\n", where,
                ucs_status_string(status));
        exit(2);
    }
}

static unsigned char pattern(unsigned stream, unsigned seq, unsigned off)
{
    return (unsigned char)(stream * 17u + seq * 29u + off * 13u);
}

static void fill(message_t *m, unsigned stream, unsigned seq)
{
    unsigned i;
    m->stream = htonl(stream);
    m->sequence = htonl(seq);
    for (i = 0; i < sizeof(m->body); ++i) {
        m->body[i] = pattern(stream, seq, i);
    }
}

static ucs_status_t receive(void *arg, void *data, size_t length, unsigned flags)
{
    state_t *s = arg;
    message_t m;
    unsigned stream, seq, i;
    (void)flags;
    require(length == sizeof(m), "received length");
    memcpy(&m, data, sizeof(m));
    stream = ntohl(m.stream);
    seq = ntohl(m.sequence);
    require(stream < STREAMS && seq < PER_STREAM, "received identity");
    require(!s->seen[stream][seq], "duplicate payload");
    for (i = 0; i < sizeof(m.body); ++i) {
        require(m.body[i] == pattern(stream, seq, i), "payload bytes");
    }
    s->seen[stream][seq] = 1;
    ++s->received;
    return UCS_OK;
}

static size_t pack(void *destination, void *arg)
{
    memcpy(destination, arg, sizeof(message_t));
    return sizeof(message_t);
}

static void tick(state_t *s)
{
    uct_worker_progress(s->worker);
    require(seconds() < s->deadline, "progress deadline");
}

static void bytes(int fd, void *data, size_t length, int sending)
{
    unsigned char *p = data;
    ssize_t n;
    while (length) {
        n = sending ? send(fd, p, length, MSG_NOSIGNAL) : recv(fd, p, length, 0);
        if (n < 0 && errno == EINTR) {
            continue;
        }
        require(n > 0, "TCP bootstrap transfer");
        p += n;
        length -= (size_t)n;
    }
}

static int socket_open(int server, const char *ip, unsigned port)
{
    struct sockaddr_in a = {0};
    struct timeval timeout = { .tv_sec = 20 };
    struct timespec pause = { .tv_nsec = 100000000 };
    double until = seconds() + 20;
    int fd = -1, listener, yes = 1;
    a.sin_family = AF_INET;
    a.sin_port = htons(port);
    require(inet_pton(AF_INET, ip, &a.sin_addr) == 1, "numeric server IP");
    if (server) {
        listener = socket(AF_INET, SOCK_STREAM, 0);
        require(listener >= 0, "listener socket");
        require(!setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes)),
                "listener option");
        require(!bind(listener, (void *)&a, sizeof(a)) && !listen(listener, 1),
                "bind/listen (do not kill another process if port is occupied)");
        fd = accept(listener, NULL, NULL);
        close(listener);
    } else {
        while (seconds() < until) {
            fd = socket(AF_INET, SOCK_STREAM, 0);
            require(fd >= 0, "client socket");
            if (!connect(fd, (void *)&a, sizeof(a))) {
                break;
            }
            close(fd);
            fd = -1;
            nanosleep(&pause, NULL);
        }
    }
    require(fd >= 0, "bootstrap connection");
    require(!setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) &&
            !setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)),
            "bootstrap timeouts");
    return fd;
}

static void marker(state_t *s, int fd, char expected)
{
    char value;
    ssize_t n;
    for (;;) {
        tick(s);
        n = recv(fd, &value, 1, MSG_DONTWAIT);
        if (n == 1) {
            require(value == expected, "final synchronization marker");
            return;
        }
        require(n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR),
                "final synchronization socket");
    }
}

static void flush(state_t *s)
{
    ucs_status_t status;
    do {
        tick(s);
        status = uct_iface_flush(s->iface, 0, NULL);
        require(status == UCS_OK || status == UCS_INPROGRESS ||
                status == UCS_ERR_NO_RESOURCE, "iface flush");
    } while (status != UCS_OK);
}

static void open_iface(state_t *s)
{
    uct_component_h *components;
    uct_component_attr_t component = { .field_mask = UCT_COMPONENT_ATTR_FIELD_NAME };
    uct_md_config_t *md_config;
    uct_iface_config_t *config;
    uct_iface_params_t p = {0};
    uct_md_resource_desc_t *resources;
    unsigned count, i, j;
    checked(uct_query_components(&components, &count), "query components");
    for (i = 0; i < count; ++i) {
        checked(uct_component_query(components[i], &component), "component name");
        if (!strcmp(component.name, "ub")) {
            component.field_mask = UCT_COMPONENT_ATTR_FIELD_MD_RESOURCE_COUNT;
            checked(uct_component_query(components[i], &component), "UB MD count");
            require(component.md_resource_count > 0 && component.md_resource_count < 4096,
                    "UB MD count bounds");
            resources = calloc(component.md_resource_count, sizeof(*resources));
            require(resources != NULL, "UB MD resources allocation");
            component.md_resources = resources;
            component.field_mask = UCT_COMPONENT_ATTR_FIELD_MD_RESOURCES;
            checked(uct_component_query(components[i], &component), "UB MD resources");
            for (j = 0; j < component.md_resource_count; ++j) {
                if (!strcmp(resources[j].md_name, "udmac0d1e2")) {
                    break;
                }
            }
            require(j < component.md_resource_count, "selected UB MD is unavailable");
            checked(uct_md_config_read(components[i], NULL, NULL, &md_config),
                    "read UB MD config");
            s->component = components[i];
            s->component = components[i];
            checked(uct_md_open(components[i], "udmac0d1e2", md_config, &s->md),
                    "open selected UB MD");
            uct_config_release(md_config);
            free(resources);
            break;
        }
    }
    uct_release_component_list(components);
    require(s->md != NULL, "UB component missing");
    checked(ucs_async_context_create(UCS_ASYNC_MODE_POLL, &s->async), "async create");
    checked(uct_worker_create(s->async, UCS_THREAD_MODE_SINGLE, &s->worker), "worker create");
    checked(uct_md_iface_config_read(s->md, "rm_urma", NULL, NULL, &config),
            "read RM iface config");
    p.field_mask = UCT_IFACE_PARAM_FIELD_OPEN_MODE | UCT_IFACE_PARAM_FIELD_DEVICE |
                   UCT_IFACE_PARAM_FIELD_STATS_ROOT | UCT_IFACE_PARAM_FIELD_RX_HEADROOM |
                   UCT_IFACE_PARAM_FIELD_CPU_MASK;
    p.open_mode = UCT_IFACE_OPEN_MODE_DEVICE;
    p.mode.device.tl_name = "rm_urma";
    p.mode.device.dev_name = "udmac0d1e2:1";
    UCS_CPU_ZERO(&p.cpu_mask);
    checked(uct_iface_open(s->md, s->worker, &p, config, &s->iface), "open RM iface");
    uct_config_release(config);
    uct_iface_progress_enable(s->iface, UCT_PROGRESS_SEND | UCT_PROGRESS_RECV);
    checked(uct_iface_query(s->iface, &s->attr), "iface query");
    require((s->attr.cap.flags & UCT_IFACE_FLAG_CONNECT_TO_IFACE) &&
            !(s->attr.cap.flags & UCT_IFACE_FLAG_CONNECT_TO_EP) &&
            s->attr.iface_addr_len == 48 && s->attr.ep_addr_len == 0,
            "iface_v2 capability/address lengths");
    require((s->attr.cap.flags & UCT_IFACE_FLAG_AM_SHORT) &&
            (s->attr.cap.flags & UCT_IFACE_FLAG_AM_BCOPY) &&
            s->attr.cap.am.max_short >= sizeof(message_t) &&
            s->attr.cap.am.max_bcopy >= sizeof(message_t), "64B short/bcopy capability");
    /* Zero flags selects a synchronous callback from worker progress. */
    checked(uct_iface_set_am_handler(s->iface, AM_ID, receive, s, 0),
            "AM receive handler");
}

static void address_line(state_t *s, const char *phase, const void *wire)
{
    uct_rm_urma_iface_addr_v2_t a;
    checked(uct_rm_urma_iface_addr_v2_unpack(wire, 48, &a), "decode actual iface address");
    require(a.rx_depth == 64 && a.max_peers == 5 && a.initial_credit == 8,
            "actual R64/K5/W8");
    printf("DIRECT_ADDRESS ROLE=%s PHASE=%s APP_EPS=%u IFACE_LEN=48 EP_LEN=0 "
           "JETTY=%u INSTANCE=%" PRIu64 " R=%u K=%u W=%u SEG_LEN=%" PRIu64 "\n",
           s->role, phase, s->created, a.jetty_id, a.instance, a.rx_depth,
           a.max_peers, a.initial_credit, a.segment_length);
}

static void maps(const char *path)
{
    char line[4096];
    FILE *in = fopen("/proc/self/maps", "r");
    FILE *out = fopen(path, "w");
    require(in && out, "maps files");
    while (fgets(line, sizeof(line), in)) {
        if (strstr(line, "libuct") || strstr(line, "libucs") ||
            strstr(line, "libucm") || strstr(line, "liburma")) {
            fputs(line, out);
        }
    }
    fclose(in);
    fclose(out);
}


int main(int argc, char **argv)
{
    state_t s = {0};
    uct_md_attr_t mdattr;
    uct_mem_h mem;
    uct_rkey_bundle_t key;
    uct_ep_h ep;
    uct_ep_params_t p = {0};
    uct_iov_t iov = {0};
    unsigned char local[560] = {0}, remote[560] = {0};
    unsigned char packed[512] = {0}, peer_key[512] = {0};
    unsigned char *buffer;
    uint64_t addr;
    size_t sizes[] = {8, 64, 4096};
    int server, fd;
    unsigned i, j, round;
    char ack;
    ucs_status_t status;

    require(argc == 5, "probe listen|connect IP PORT MAPS");
    server = !strcmp(argv[1], "listen");
    require(server || !strcmp(argv[1], "connect"), "role");
    s.role = server ? "receiver" : "sender";
    setvbuf(stdout, NULL, _IOLBF, 0);
    fd = socket_open(server, argv[2], strtoul(argv[3], NULL, 10));
    s.deadline = seconds() + 70;
    open_iface(&s);
    require(s.attr.cap.flags & UCT_IFACE_FLAG_PUT_ZCOPY, "native PUT_ZCOPY capability");
    require(!(s.attr.cap.flags & (UCT_IFACE_FLAG_GET_ZCOPY | UCT_IFACE_FLAG_GET_BCOPY)), "GET must stay disabled");
    require(s.attr.cap.put.max_iov == 1 && s.attr.cap.put.max_zcopy >= 4096, "WRITE limit");
    checked(uct_md_query(s.md, &mdattr), "MD query");
    require(mdattr.rkey_packed_size <= sizeof(packed), "key wire capacity");
    buffer = calloc(1, 4096);
    require(buffer != NULL, "allocate registered buffer");
    checked(uct_md_mem_reg(s.md, buffer, 4096, UCT_MD_MEM_ACCESS_ALL, &mem), "register");
    checked(uct_md_mkey_pack(s.md, mem, packed), "pack key");
    checked(uct_iface_get_device_address(s.iface, (uct_device_addr_t *)local), "device addr");
    checked(uct_iface_get_address(s.iface, (uct_iface_addr_t *)(local+512)), "iface addr");
    if (server) {
        addr = (uint64_t)buffer;
        bytes(fd, local, sizeof(local), 1);
        bytes(fd, packed, sizeof(packed), 1);
        bytes(fd, &addr, sizeof(addr), 1);
        bytes(fd, remote, sizeof(remote), 0);
    } else {
        bytes(fd, remote, sizeof(remote), 0);
        bytes(fd, peer_key, sizeof(peer_key), 0);
        bytes(fd, &addr, sizeof(addr), 0);
        bytes(fd, local, sizeof(local), 1);
        checked(uct_rkey_unpack(s.component, peer_key, &key), "unpack remote key");
        p.field_mask = UCT_EP_PARAM_FIELD_IFACE | UCT_EP_PARAM_FIELD_DEV_ADDR | UCT_EP_PARAM_FIELD_IFACE_ADDR;
        p.iface = s.iface;
        p.dev_addr = (uct_device_addr_t *)remote;
        p.iface_addr = (uct_iface_addr_t *)(remote+512);
        checked(uct_ep_create(&p, &ep), "create WRITE EP");
        iov.buffer = buffer;
        iov.memh = mem;
        iov.count = 1;
    }
    /* TCP only exchanges addresses/keys and verification acknowledgements.
     * Every payload byte reaches the receiver through native WRITE. */
    for (round = 0; round < 6; ++round) {
        size_t n = sizes[round % 3];
        unsigned char value = (unsigned char)(0x31 + round);
        if (!server) {
            memset(buffer, value, n);
            iov.length = n;
            do {
                status = uct_ep_put_zcopy(ep, &iov, 1, addr, key.rkey, NULL);
                require(status == UCS_INPROGRESS || status == UCS_ERR_NO_RESOURCE, "native WRITE post");
                tick(&s);
            } while (status == UCS_ERR_NO_RESOURCE);
            flush(&s);
            ack = 'W';
            bytes(fd, &ack, 1, 1);
            marker(&s, fd, 'V');
        } else {
            marker(&s, fd, 'W');
            for (;;) {
                for (i = 0; i < n; ++i) {
                    if (((volatile unsigned char *)buffer)[i] != value) break;
                }
                if (i == n) break;
                tick(&s);
            }
            for (j = (unsigned)n; j < 4096 && round < 2; ++j) {
                require(buffer[j] == 0, "destination tail guard");
            }
            ack = 'V';
            bytes(fd, &ack, 1, 1);
        }
        printf("NATIVE_WRITE_ROUND=%u BYTES=%zu ROLE=%s DATA=PASS\n", round, n, s.role);
    }
    flush(&s);
    maps(argv[4]);
    if (!server) {
        uct_ep_destroy(ep);
        checked(uct_rkey_release(s.component, &key), "release remote key");
    }
    checked(uct_md_mem_dereg(s.md, mem), "deregister");
    free(buffer);
    uct_iface_close(s.iface);
    uct_md_close(s.md);
    uct_worker_destroy(s.worker);
    ucs_async_context_destroy(s.async);
    close(fd);
    puts("NATIVE_WRITE_PASS ROUNDS=6 SIZE=8/64/4096 REGISTRATION_RELEASE=PASS GET=OFF");
    return 0;
}
