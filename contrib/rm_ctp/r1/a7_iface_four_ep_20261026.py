#!/usr/bin/env python3
"""Node89: one focused direct-UCT probe, using the already tested installation.

No MPI collective or data ACK is used. The receiver never calls uct_ep_create;
64 AM payloads must cross an 8-credit window before final TCP synchronization.
This is not complete A7, a performance benchmark, or proof of CTP fault recovery.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile

LAB = Path('/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019')
PEER = 'root@9.88.43.97'
COMMIT = '2a8e3b771211e5af491c8ca483ebcc01a03c2f01'
MODULE = '26b9dac631af27622d0f04f3f86758332bb59874f4cf21743d2820c5783e3aa6'
CODEC_HASHES = {
    "rm_urma_addr.h": "d3f127384282707dab9f5626961e7f02dd2df0177246d99f95fe170703281250",
    "rm_urma_addr.c": "109a554ca40b88d0c9824634fe172eb51c6496a32cabb480e518aead6d2d0162"
}
C_SOURCE = r'''/* Direct UCT test only. TCP bootstraps addresses and final synchronization.
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

int main(int argc, char **argv)
{
    state_t s = {0};
    uct_ep_h eps[STREAMS] = {0};
    uct_ep_params_t ep_params = {0};
    unsigned char local[560] = {0}, remote[560] = {0};
    uint32_t header[2], peer_header[2];
    uint64_t short_header;
    message_t m;
    unsigned stream, seq, i, sent = 0, shorts = 0, bcopies = 0;
    unsigned port;
    int server, fd;
    char final;
    ssize_t length;
    ucs_status_t status;
    require(argc == 5 && (!strcmp(argv[1], "listen") || !strcmp(argv[1], "connect")),
            "usage: probe listen|connect IP PORT MAPS_PATH");
    require(sizeof(message_t) == DATA_BYTES, "message structure padding");
    server = !strcmp(argv[1], "listen");
    port = (unsigned)strtoul(argv[3], NULL, 10);
    require(port > 0 && port <= 65535, "TCP port");
    s.role = server ? "receiver" : "sender";
    setvbuf(stdout, NULL, _IOLBF, 0);
    fd = socket_open(server, argv[2], port);
    s.deadline = seconds() + 65;
    open_iface(&s);
    require(s.attr.device_addr_len <= 512, "device address capacity");
    checked(uct_iface_get_device_address(s.iface, (uct_device_addr_t *)local),
            "local device address");
    checked(uct_iface_get_address(s.iface, (uct_iface_addr_t *)(local + 512)),
            "local iface address");
    address_line(&s, "LOCAL_BEFORE_EP", local + 512);
    header[0] = htonl((uint32_t)s.attr.device_addr_len);
    header[1] = htonl(48);
    if (!server) {
        bytes(fd, header, sizeof(header), 1);
        bytes(fd, local, 560, 1);
    }
    bytes(fd, peer_header, sizeof(peer_header), 0);
    require(ntohl(peer_header[0]) == s.attr.device_addr_len && ntohl(peer_header[1]) == 48,
            "peer address lengths");
    bytes(fd, remote, 560, 0);
    if (server) {
        bytes(fd, header, sizeof(header), 1);
        bytes(fd, local, 560, 1);
    }
    address_line(&s, "REMOTE_BEFORE_EP", remote + 512);
    if (!server) {
        ep_params.field_mask = UCT_EP_PARAM_FIELD_IFACE | UCT_EP_PARAM_FIELD_DEV_ADDR |
                               UCT_EP_PARAM_FIELD_IFACE_ADDR;
        ep_params.iface = s.iface;
        ep_params.dev_addr = (uct_device_addr_t *)remote;
        ep_params.iface_addr = (uct_iface_addr_t *)(remote + 512);
        for (stream = 0; stream < STREAMS; ++stream) {
            checked(uct_ep_create(&ep_params, &eps[stream]), "create logical EP");
            for (i = 0; i < stream; ++i) {
                require(eps[i] != eps[stream], "distinct logical EP handles");
            }
            ++s.created;
        }
        for (seq = 0; seq < PER_STREAM; ++seq) {
            for (stream = 0; stream < STREAMS; ++stream) {
                fill(&m, stream, seq);
                do {
                    if ((stream + seq) & 1) {
                        length = uct_ep_am_bcopy(eps[stream], AM_ID, pack, &m, 0);
                        status = length < 0 ? (ucs_status_t)length : UCS_OK;
                        require(length < 0 || length == sizeof(m), "bcopy packed length");
                    } else {
                        memcpy(&short_header, &m, sizeof(short_header));
                        status = uct_ep_am_short(eps[stream], AM_ID, short_header,
                                                m.body, sizeof(m.body));
                    }
                    require(status == UCS_OK || status == UCS_ERR_NO_RESOURCE,
                            "payload post status");
                    if (status == UCS_ERR_NO_RESOURCE) {
                        tick(&s);
                    }
                } while (status != UCS_OK);
                ++sent;
                if ((stream + seq) & 1) {
                    ++bcopies;
                } else {
                    ++shorts;
                }
            }
        }
        marker(&s, fd, 'R');
        flush(&s);
        maps(argv[4]);
        for (stream = 0; stream < STREAMS; ++stream) {
            uct_ep_destroy(eps[stream]);
        }
        final = 'D';
        bytes(fd, &final, 1, 1);
        marker(&s, fd, 'C');
    } else {
        while (s.received < STREAMS * PER_STREAM) {
            tick(&s);
        }
        flush(&s);
        maps(argv[4]);
        final = 'R';
        bytes(fd, &final, 1, 1);
        marker(&s, fd, 'D');
        flush(&s);
        final = 'C';
        bytes(fd, &final, 1, 1);
    }
    checked(uct_iface_set_am_handler(s.iface, AM_ID, NULL, NULL, 0), "remove AM handler");
    uct_iface_close(s.iface);
    uct_md_close(s.md);
    uct_worker_destroy(s.worker);
    ucs_async_context_destroy(s.async);
    close(fd);
    printf("DIRECT_IFACE_PASS ROLE=%s APP_EPS=%u SENT=%u RECEIVED=%u "
           "SHORT=%u BCOPY=%u STREAMS=4 PER_STREAM=16 BYTES=64\n",
           s.role, s.created, sent, s.received, shorts, bcopies);
    return 0;
}
'''


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def remote(script, *args):
    command = 'bash -s -- ' + shlex.join([str(value) for value in args])
    p = subprocess.run(['ssh', PEER, command], input=script, text=True,
                       capture_output=True, timeout=35)
    if p.returncode:
        raise RuntimeError('SSH失败：' + p.stderr[-1200:] + p.stdout[-1200:])
    return p.stdout


def stop_own_process(p):
    if p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        except ProcessLookupError:
            p.wait()


def check_maps(text, install):
    for name in ('libuct', 'libucs', 'libucm'):
        if not any(str(install / 'lib') + '/' in line and name in line
                   for line in text.splitlines()):
            raise RuntimeError('候选核心库maps缺失：' + name)
    if not any(str(install / 'lib/ucx') + '/' in line and 'libuct_ub' in line
               for line in text.splitlines()):
        raise RuntimeError('候选URMA模块maps缺失')


def targets(text):
    active = set()
    totals = {'import': 0, 'reuse': 0, 'release': 0}
    ifaces = set()
    for action, iface, target in re.findall(
            r'RM target (import|reuse|release) iface=(\S+) target=(\S+)', text):
        key = (iface, target)
        ifaces.add(iface)
        if action == 'import':
            if key in active:
                raise RuntimeError('重复导入仍存活的目标')
            active.add(key)
        else:
            if key not in active:
                raise RuntimeError('目标复用/释放缺少匹配导入')
            if action == 'release':
                active.remove(key)
        totals[action] += 1
    if active or len(ifaces) != 1 or totals['import'] != 1 or totals['release'] != 1:
        raise RuntimeError('单目标导入/退出释放证据不符：' + str(totals))
    return totals


def validate(sender, receiver):
    logs = {'sender': sender, 'receiver': receiver}
    addresses = {}
    post_rows = {}
    result = {'roles': {}, 'maps': 'checked separately'}
    for role, text in logs.items():
        if re.search(r'UCX\s+(ERROR|FATAL)|DIRECT_IFACE_FAILED|DIRECT_IFACE_STATUS:', text):
            raise RuntimeError(role + '日志含终态错误')
        rows = re.findall(
            r'DIRECT_ADDRESS ROLE=(\w+) PHASE=(\w+) APP_EPS=(\d+) '
            r'IFACE_LEN=48 EP_LEN=0 JETTY=(\d+) INSTANCE=(\d+) '
            r'R=(\d+) K=(\d+) W=(\d+) SEG_LEN=(\d+)', text)
        if len(rows) != 2 or any(row[0] != role or row[2] != '0' or
                tuple(map(int, row[5:8])) != (64, 5, 8) or
                any(int(row[index]) <= 0 for index in (3, 4, 8)) for row in rows):
            raise RuntimeError(role + '地址/实例/预算证据不符')
        phases = {row[1]: row for row in rows}
        if set(phases) != {'LOCAL_BEFORE_EP', 'REMOTE_BEFORE_EP'}:
            raise RuntimeError(role + '地址时序标记不全')
        addresses[role] = phases
        marker = re.search(
            r'DIRECT_IFACE_PASS ROLE=(\w+) APP_EPS=(\d+) SENT=(\d+) '
            r'RECEIVED=(\d+) SHORT=(\d+) BCOPY=(\d+) '
            r'STREAMS=4 PER_STREAM=16 BYTES=64', text)
        expected = ('sender', '4', '64', '0', '32', '32') if role == 'sender' else \
                   ('receiver', '0', '0', '64', '0', '0')
        if marker is None or marker.groups() != expected:
            raise RuntimeError(role + '四EP/数据校验/关闭标记不符')
        connections = text.count('RM iface connect')
        if connections != (4 if role == 'sender' else 0):
            raise RuntimeError(role + '实际iface连接次数不符')
        if 'RM legacy EP address export' in text or 'RM legacy connect_to_ep' in text:
            raise RuntimeError('误走旧EP地址或连接路径')
        if text.count('RM iface address export') != 1:
            raise RuntimeError(role + 'iface地址导出次数不是1')
        if text.index('PHASE=LOCAL_BEFORE_EP') > text.index('PHASE=REMOTE_BEFORE_EP'):
            raise RuntimeError('本地发布/远端可获得地址顺序不符')
        if role == 'sender' and text.index('PHASE=REMOTE_BEFORE_EP') > text.index('RM iface connect'):
            raise RuntimeError('未先获得对端地址就创建了业务EP')
        post_rows[role] = [(int(kind), int(identity), int(credits))
                          for kind, identity, credits in re.findall(
            r'RM iface control post kind=(\d+) id=(\d+) credits=(\d+)', text)]
        result['roles'][role] = {'app_ep_count': int(expected[1]),
            'payload_count': 64, 'targets': targets(text),
            'iface_connections': connections,
            'local_address': {'jetty': int(phases['LOCAL_BEFORE_EP'][3]),
                              'instance': int(phases['LOCAL_BEFORE_EP'][4]),
                              'rx_depth': 64, 'max_peers': 5, 'window': 8}}
    for role, other in (('sender', 'receiver'), ('receiver', 'sender')):
        if addresses[role]['REMOTE_BEFORE_EP'][3:] != addresses[other]['LOCAL_BEFORE_EP'][3:]:
            raise RuntimeError('两端交换的实际iface字段不一致')
    if result['roles']['sender']['targets']['reuse'] < 3:
        raise RuntimeError('四个发送EP没有复用同一导入目标')
    requests = post_rows['sender']
    grants = post_rows['receiver']
    if (any(kind != 1 for kind, _, _ in requests) or
            any(kind != 2 for kind, _, _ in grants) or len(requests) < 7 or
            sorted((i, n) for _, i, n in requests) != sorted((i, n) for _, i, n in grants) or
            len({i for _, i, _ in requests}) != len(requests) or
            sum(n for _, _, n in grants) < 64 - 8):
        raise RuntimeError('无反向业务EP的跨窗FC请求/回复/补充额度证据不完整')
    result.update(requests=len(requests), grants=len(grants),
                  granted_credits=sum(n for _, _, n in grants),
                  receiver_reverse_business_eps=0,
                  address_publication_before_application_eps=True)
    return result


def main():
    parser = argparse.ArgumentParser(description='A5/A6/A7定向UCT四EP与独立流控，复用已测库')
    parser.add_argument('case', type=Path)
    parser.add_argument('--port', type=int, default=19947)
    args = parser.parse_args()
    case = args.case.resolve()
    if case.parent != LAB or not 1 <= args.port <= 65535:
        parser.error('必须是实验根下的隔离CASE及有效端口')
    install = case / 'install'
    manifest = json.loads((case / 'build_manifest.json').read_text())
    module = install / 'lib/ucx/libuct_ub.so.0.0.0'
    if manifest['source_commit'] != COMMIT or manifest['module_sha256'] != MODULE or digest(module) != MODULE:
        raise RuntimeError('本轮已测产品身份不符')
    codec = case / 'source/src/uct/urma/rm_urma'
    for name, expected in CODEC_HASHES.items():
        if digest(codec / name) != expected:
            raise RuntimeError('实际地址codec源码不符：' + name)
    out = Path(tempfile.mkdtemp(prefix='a7_direct_four_ep_', dir=str(case)))
    print('DIRECT_DIR=' + str(out), flush=True)
    report = {'product_commit': COMMIT, 'module_sha256': MODULE,
              'helper_sha256': digest(__file__), 'c_source_sha256': hashlib.sha256(C_SOURCE.encode()).hexdigest(),
              'scope': 'direct UCT四EP/零反向业务EP/跨窗口；非完整A7/性能/CTP故障恢复',
              'passed': False}

    def save():
        (out / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    save()
    try:
        peer = remote('set -eu\nsha256sum "$1"\nldd "$1"\n', module)
        if peer.split()[0] != MODULE or 'not found' in peer:
            raise RuntimeError('对端已测模块或依赖不符')
        source = out / 'iface_direct.c'
        source.write_text(C_SOURCE)
        binary = out / 'iface_direct'
        command = ['gcc', '-std=gnu99', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
                   '-I' + str(install / 'include'), '-I' + str(codec), str(source),
                   str(codec / 'rm_urma_addr.c'), '-L' + str(install / 'lib'),
                   '-Wl,-rpath,' + str(install / 'lib'), '-luct', '-lucs', '-lucm',
                   '-lpthread', '-ldl', '-lrt', '-lm', '-o', str(binary)]
        with (out / 'compile.log').open('w') as stream:
            subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True)
        remote('set -eu\ntest ! -e "$1"\nmkdir "$1"\n', out)
        subprocess.run(['scp', '-q', str(binary), PEER + ':' + str(binary)], check=True)
        if remote('sha256sum "$1"\n', binary).split()[0] != digest(binary):
            raise RuntimeError('两端专项可执行文件不一致')
        environment = {
            'PATH': '/usr/bin:/bin', 'UCX_MODULE_DIR': str(install / 'lib/ucx'),
            'LD_LIBRARY_PATH': str(install / 'lib'),
            'LD_PRELOAD': ':'.join(str(install / 'lib' / ('lib' + name + '.so.0'))
                                   for name in ('uct', 'ucs', 'ucm')),
            'UCX_LOG_LEVEL': 'debug', 'UCX_RM_URMA_LINK_MODE': 'iface_v2',
            'UCX_RM_URMA_MAX_PEERS': '5', 'UCX_RM_URMA_FC_ENABLE': 'y',
            'UCX_RM_URMA_FC_WND_SIZE': '8', 'UCX_RM_URMA_TX_DEPTH': '8',
            'UCX_RM_URMA_RX_DEPTH': '64', 'UCX_RM_URMA_FC_SOFT_THRESH': '0.5',
            'UCX_RM_URMA_FC_HARD_THRESH': '0.25'}
        prefix = ['timeout', '-k', '5s', '85s', '/usr/bin/env', '-i'] + \
                 [key + '=' + value for key, value in environment.items()]
        server_cmd = prefix + [str(binary), 'listen', '9.88.43.97', str(args.port), str(out / 'receiver.maps')]
        client_cmd = prefix + [str(binary), 'connect', '9.88.43.97', str(args.port), str(out / 'sender.maps')]
        server_script = 'set -eu\nexec ' + shlex.join(server_cmd) + ' >' + shlex.quote(str(out / 'receiver.log')) + ' 2>&1\n'
        (out / 'server.sh').write_text(server_script)
        report['commands'] = {'sender': client_cmd, 'receiver': server_cmd}
        save()
        server = subprocess.Popen(['ssh', PEER, 'bash -s'], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, start_new_session=True)
        server.stdin.write(server_script)
        server.stdin.close()
        server.stdin = None
        client_rc = None
        client_error = None
        try:
            with (out / 'sender.log').open('w') as stream:
                client = subprocess.Popen(client_cmd, stdout=stream, stderr=subprocess.STDOUT,
                                          start_new_session=True)
                try:
                    client_rc = client.wait(timeout=95)
                except subprocess.TimeoutExpired:
                    stop_own_process(client)
                    client_error = 'sender timed out; own test process stopped'
        finally:
            try:
                stdout, stderr = server.communicate(timeout=100)
            except subprocess.TimeoutExpired:
                stop_own_process(server)
                stdout, stderr = server.communicate()
                client_error = 'server SSH timed out; remote test has its own timeout'
            (out / 'server_ssh.log').write_text(stdout + stderr)
        for name in ('receiver.log', 'receiver.maps'):
            value = remote('if test -f "$1"; then cat "$1"; fi\n', out / name)
            (out / name).write_text(value)
        report['exits'] = {'sender': client_rc, 'receiver_ssh': server.returncode}
        if client_error or client_rc != 0 or server.returncode != 0:
            raise RuntimeError(client_error or '专项进程失败：' + str(report['exits']))
        details = validate((out / 'sender.log').read_text(errors='replace'),
                           (out / 'receiver.log').read_text(errors='replace'))
        for name in ('sender.maps', 'receiver.maps'):
            check_maps((out / name).read_text(), install)
        details['maps'] = 'BOTH_PASS'
        report.update(passed=True, details=details)
        save()
        print('DIRECT_UCT_FOUR_EP_PASS=YES MODULE_BOTH_MATCH=YES', flush=True)
        print('ADDRESS_PUBLISHED_AND_RECEIVED_BEFORE_APP_EP=PASS IFACE_LEN=48 EP_LEN=0 R=64 K=5 W=8', flush=True)
        print('APPLICATION_EPS SENDER=4 RECEIVER=0; PAYLOAD=64x64B SHORT=32 BCOPY=32 DATA_PASS=YES', flush=True)
        print('INDEPENDENT_FC REQUESTS=%d GRANTS=%d CREDITS=%d REVERSE_BUSINESS_EP=0' %
              (details['requests'], details['grants'], details['granted_credits']), flush=True)
        print('TARGETS=' + json.dumps({role: row['targets'] for role, row in details['roles'].items()}), flush=True)
        print('CANDIDATE_MAPS_BOTH=PASS SCOPE=FOCUSED_ONLY NOT_FULL_A7', flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), flush=True)
    except Exception as exc:
        report['error'] = str(exc)
        save()
        print('DIRECT_IFACE_FAILED: ' + str(exc), file=sys.stderr, flush=True)
        print('RESULTS_JSON=' + str(out / 'results.json'), file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
