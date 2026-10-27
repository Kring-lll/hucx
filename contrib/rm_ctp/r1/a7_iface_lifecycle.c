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

enum { STREAMS = 4, PER_STREAM = 64, DATA_BYTES = 64, AM_ID = 1 };
typedef struct {
    uint32_t stream;
    uint32_t sequence;
    unsigned char body[DATA_BYTES - 8];
} message_t;
typedef struct {
    ucs_async_context_t *async;
    uct_worker_h worker;
    uct_md_h md;
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

/* A worker is SINGLE-threaded: "overlap" below means WRs outstanding together,
 * not unsupported concurrent calls from multiple application threads. */
enum { CYCLES = 2, GENERATIONS = 16, PER_GENERATION = 4 };
typedef struct {
    uct_pending_req_t request;
    uct_ep_h ep;
    message_t message;
    unsigned completed;
    unsigned invoked;
    unsigned purged;
    int bcopy;
} pending_message_t;

static ucs_status_t post(uct_ep_h ep, message_t *message, int bcopy)
{
    uint64_t header;
    ssize_t length;
    if (bcopy) {
        length = uct_ep_am_bcopy(ep, AM_ID, pack, message, 0);
        require(length < 0 || length == sizeof(*message), "bcopy packed length");
        return length < 0 ? (ucs_status_t)length : UCS_OK;
    }
    memcpy(&header, message, sizeof(header));
    return uct_ep_am_short(ep, AM_ID, header, message->body, sizeof(message->body));
}

static ucs_status_t pending_send(uct_pending_req_t *request)
{
    pending_message_t *p = (pending_message_t *)request;
    ucs_status_t status;
    ++p->invoked;
    require(p->completed == 0, "pending called after successful completion");
    status = post(p->ep, &p->message, p->bcopy);
    require(status == UCS_OK || status == UCS_ERR_NO_RESOURCE, "pending post status");
    if (status == UCS_OK) {
        ++p->completed;
    }
    return status;
}

static void pending_purge(uct_pending_req_t *request, void *argument)
{
    pending_message_t *p = (pending_message_t *)request;
    require(argument == p && !p->completed && !p->invoked, "purge ownership");
    ++p->purged;
}

static void create(state_t *s, uct_ep_params_t *params, uct_ep_h *ep)
{
    checked(uct_ep_create(params, ep), "create logical EP");
    ++s->created;
}

static void close_state(state_t *s)
{
    checked(uct_iface_set_am_handler(s->iface, AM_ID, NULL, NULL, 0),
            "remove AM handler");
    uct_iface_close(s->iface);
    uct_md_close(s->md);
    uct_worker_destroy(s->worker);
    ucs_async_context_destroy(s->async);
}

int main(int argc, char **argv)
{
    unsigned cycle, generation, stream, within, i, port;
    unsigned total_created = 0, total_destroyed = 0, total_sent = 0;
    unsigned total_received = 0, total_purged = 0, total_pending = 0;
    unsigned early_destroyed = 0;
    char final, map_path[4096];
    int server, fd;
    require(argc == 5 && (!strcmp(argv[1], "listen") || !strcmp(argv[1], "connect")),
            "usage: lifecycle listen|connect IP PORT MAPS_PREFIX");
    server = !strcmp(argv[1], "listen");
    port = (unsigned)strtoul(argv[3], NULL, 10);
    require(port > 0 && port <= 65535, "TCP port");
    require(sizeof(message_t) == DATA_BYTES, "message padding");
    setvbuf(stdout, NULL, _IOLBF, 0);
    fd = socket_open(server, argv[2], port);
    for (cycle = 0; cycle < CYCLES; ++cycle) {
        state_t s = {0};
        uct_ep_params_t params = {0};
        uct_ep_h eps[STREAMS] = {0};
        unsigned char local[560] = {0}, remote[560] = {0};
        uint32_t header[2], peer_header[2];
        unsigned sent = 0, purged = 0, pending = 0, destroyed = 0;
        s.role = server ? "receiver" : "sender";
        s.deadline = seconds() + 65;
        open_iface(&s);
        require(s.attr.device_addr_len <= 512, "device address capacity");
        checked(uct_iface_get_device_address(s.iface, (uct_device_addr_t *)local),
                "local device address");
        checked(uct_iface_get_address(s.iface, (uct_iface_addr_t *)(local + 512)),
                "local iface address");
        printf("LIFECYCLE_BEGIN ROLE=%s CYCLE=%u\n", s.role, cycle);
        address_line(&s, "LOCAL_BEFORE_EP", local + 512);
        header[0] = htonl((uint32_t)s.attr.device_addr_len);
        header[1] = htonl(48);
        if (!server) {
            bytes(fd, header, sizeof(header), 1);
            bytes(fd, local, sizeof(local), 1);
        }
        bytes(fd, peer_header, sizeof(peer_header), 0);
        require(ntohl(peer_header[0]) == s.attr.device_addr_len &&
                ntohl(peer_header[1]) == 48, "peer address lengths");
        bytes(fd, remote, sizeof(remote), 0);
        if (server) {
            bytes(fd, header, sizeof(header), 1);
            bytes(fd, local, sizeof(local), 1);
        }
        address_line(&s, "REMOTE_BEFORE_EP", remote + 512);
        if (!server) {
            params.field_mask = UCT_EP_PARAM_FIELD_IFACE | UCT_EP_PARAM_FIELD_DEV_ADDR |
                                UCT_EP_PARAM_FIELD_IFACE_ADDR;
            params.iface = s.iface;
            params.dev_addr = (uct_device_addr_t *)remote;
            params.iface_addr = (uct_iface_addr_t *)(remote + 512);
            for (generation = 0; generation < GENERATIONS; ++generation) {
                unsigned used_pending = 0;
                for (stream = 0; stream < STREAMS; ++stream) {
                    create(&s, &params, &eps[stream]);
                    for (i = 0; i < stream; ++i) {
                        require(eps[i] != eps[stream], "distinct live business EPs");
                    }
                }
                for (within = 0; within < PER_GENERATION; ++within) {
                    unsigned seq = generation * PER_GENERATION + within;
                    for (stream = 0; stream < STREAMS; ++stream) {
                        message_t message;
                        ucs_status_t status;
                        int bcopy = (stream + seq) & 1;
                        fill(&message, stream, seq);
                        status = post(eps[stream], &message, bcopy);
                        if (within == 0 && stream == 0) {
                            /* Retire only after a direct post, never after a progress callback. */
                            while (status == UCS_ERR_NO_RESOURCE) {
                                tick(&s);
                                status = post(eps[stream], &message, bcopy);
                            }
                        }
                        if (status == UCS_ERR_NO_RESOURCE && !used_pending) {
                            pending_message_t p = {0};
                            p.request.func = pending_send;
                            p.ep = eps[stream];
                            p.message = message;
                            p.bcopy = bcopy;
                            checked(uct_ep_pending_add(p.ep, &p.request, 0),
                                    "pending enqueue before progress");
                            uct_ep_pending_purge(p.ep, pending_purge, &p);
                            require(p.purged == 1 && p.completed == 0, "pending purged once");
                            ++purged;
                            checked(uct_ep_pending_add(p.ep, &p.request, 0),
                                    "pending enqueue after purge");
                            while (!p.completed) {
                                tick(&s);
                            }
                            require(p.invoked > 0 && p.completed == 1, "pending completed once");
                            ++pending;
                            used_pending = 1;
                            status = UCS_OK;
                        } else {
                            while (status == UCS_ERR_NO_RESOURCE) {
                                tick(&s);
                                status = post(eps[stream], &message, bcopy);
                            }
                        }
                        checked(status, "payload post");
                        ++sent;
                        if (within == 0 && stream == 0) {
                            uct_ep_h retired = eps[0];
                            /* No worker progress occurred since this successful post. */
                            require(uct_ep_flush(retired, 0, NULL) == UCS_INPROGRESS,
                                    "retired EP must have an unconsumed send completion");
                            uct_ep_destroy(retired);
                            ++destroyed;
                            create(&s, &params, &eps[0]);
                            require(eps[0] != retired, "retired EP reused before its CQE");
                            ++early_destroyed;
                        }
                    }
                }
                require(used_pending, "generation did not exercise pending ownership");
                flush(&s);
                for (stream = 0; stream < STREAMS; ++stream) {
                    uct_ep_destroy(eps[stream]);
                    eps[stream] = NULL;
                    ++destroyed;
                }
                flush(&s);
                printf("LIFECYCLE_GENERATION ROLE=sender CYCLE=%u GENERATION=%u "
                       "SENT=%u CREATED=%u DESTROYED=%u PURGED=%u PENDING=%u\n",
                       cycle, generation, sent, s.created, destroyed, purged, pending);
            }
            marker(&s, fd, 'R');
            flush(&s);
            final = 'D';
            bytes(fd, &final, 1, 1);
            marker(&s, fd, 'C');
        } else {
            while (s.received < STREAMS * PER_STREAM) {
                tick(&s);
            }
            flush(&s);
            final = 'R';
            bytes(fd, &final, 1, 1);
            marker(&s, fd, 'D');
            flush(&s);
            final = 'C';
            bytes(fd, &final, 1, 1);
        }
        require(snprintf(map_path, sizeof(map_path), "%s.%u", argv[4], cycle) > 0,
                "maps filename");
        maps(map_path);
        close_state(&s);
        total_created += s.created;
        total_destroyed += destroyed;
        total_sent += sent;
        total_received += s.received;
        total_purged += purged;
        total_pending += pending;
        printf("LIFECYCLE_CYCLE_PASS ROLE=%s CYCLE=%u CREATED=%u DESTROYED=%u "
               "SENT=%u RECEIVED=%u PURGED=%u PENDING=%u\n",
               s.role, cycle, s.created, destroyed, sent, s.received, purged, pending);
        /* Both old ifaces must be fully closed before the next address exchange. */
        final = 'Q';
        bytes(fd, &final, 1, 1);
        bytes(fd, &final, 1, 0);
        require(final == 'Q', "both ifaces closed");
    }
    close(fd);
    printf("LIFECYCLE_PASS ROLE=%s CYCLES=2 GENERATIONS=16 CREATED=%u DESTROYED=%u "
           "SENT=%u RECEIVED=%u PURGED=%u PENDING=%u EARLY_DESTROY=%u BYTES=64\n",
           server ? "receiver" : "sender", total_created, total_destroyed,
           total_sent, total_received, total_purged, total_pending, early_destroyed);
    return 0;
}
