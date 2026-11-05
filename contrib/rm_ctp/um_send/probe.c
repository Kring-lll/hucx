/* Test-only observer and RX fault injection. Never linked into libuct_ub. */
#define _GNU_SOURCE
#ifdef HAVE_CONFIG_H
#include "config.h"
#endif
#include <uct/urma/rm_urma/rm_urma_ep.h>
#include <ucs/datastruct/mpool.inl>
/* Reuse the reviewed WR forwarding/size/inline observer. */
urma_status_t wr_forward_send(urma_jetty_t *, urma_jfs_wr_t *, urma_jfs_wr_t **);
#define urma_post_jetty_send_wr wr_forward_send
#include "../wr_probe/send_probe.c"
#undef urma_post_jetty_send_wr

#include "probe_layout.h"
static uint64_t data_attempts, data_success, retry_data_posts, unknown_data_layout;
static uint64_t rx_data, rx_ack;
static unsigned data_drop, ack_drop, duplicate, duplicate_failed;
typedef void (*rx_fn)(uct_rm_urma_iface_t *, uct_rm_urma_neth_t *, unsigned,
                      uct_rm_urma_recv_skb_t *, int);
static rx_fn real_rx;
static pthread_once_t rx_once = PTHREAD_ONCE_INIT;

static void resolve_rx(void)
{
    *(void **)(&real_rx) = dlsym(RTLD_NEXT, "uct_rm_urma_ep_process_rx");
    if (!real_rx) {
        const char *module = getenv("HUCX_UM_SEND_MODULE");
        void *handle = module ? dlopen(module, RTLD_NOW | RTLD_NOLOAD | RTLD_LOCAL) : NULL;
        if (handle) *(void **)(&real_rx) = dlsym(handle, "uct_rm_urma_ep_process_rx");
    }
    if (!real_rx || real_rx == uct_rm_urma_ep_process_rx) _exit(126);
}

/* Snapshot layouts before forwarding; never infer retries from repeated 16-bit PSNs. */
urma_status_t urma_post_jetty_send_wr(urma_jetty_t *jetty, urma_jfs_wr_t *wr,
                                    urma_jfs_wr_t **bad)
{
    uint64_t data = 0, retries = 0, unknown = 0;
    if (jetty && jetty->jetty_cfg.jfs_cfg.trans_mode == URMA_TM_RM) {
        urma_jfs_wr_t *p = wr;
        for (unsigned n = 0; p && n < MAX_CHAIN; ++n, p = p->next) {
            if (p->opcode != URMA_OPC_SEND || !p->send.src.num_sge ||
                p->send.src.num_sge > MAX_SGE || !p->send.src.sge ||
                p->send.src.sge[0].len < sizeof(uct_rm_urma_neth_t)) continue;
            uct_rm_urma_neth_t h;
            memcpy(&h, (void *)(uintptr_t)p->send.src.sge[0].addr, sizeof(h));
            if (!(h.packet_type & UCT_RM_URMA_PACKET_FLAG_AM)) continue;
            enum um_probe_layout layout = um_probe_classify(p->send.src.num_sge,
                p->send.src.sge[0].len, p->flag.bs.complete_enable,
                p->flag.bs.solicited_enable, p->flag.bs.inline_flag);
            ++data;
            retries += layout == UM_PROBE_RETRY;
            unknown += layout == UM_PROBE_UNKNOWN;
        }
    }
    urma_status_t status = wr_forward_send(jetty, wr, bad);
    pthread_mutex_lock(&lock);
    data_attempts += data;
    unknown_data_layout += unknown;
    if (status == URMA_SUCCESS) { data_success += data; retry_data_posts += retries; }
    pthread_mutex_unlock(&lock);
    return status;
}

void uct_rm_urma_ep_process_rx(uct_rm_urma_iface_t *iface, uct_rm_urma_neth_t *neth,
                              unsigned length, uct_rm_urma_recv_skb_t *skb, int async)
{
    pthread_once(&rx_once, resolve_rx);
    int drop = 0, dup = 0;
    const char *fault = getenv("HUCX_UM_SEND_FAULT");
    pthread_mutex_lock(&lock);
    if (neth->packet_type & UCT_RM_URMA_PACKET_FLAG_AM) {
        ++rx_data;
        if (fault && !strcmp(fault, "loss_dup") && rx_data == 3) { ++data_drop; drop = 1; }
        if (fault && !strcmp(fault, "loss_dup") && rx_data == 7) { ++duplicate; dup = 1; }
    } else if (length == sizeof(*neth)) {
        ++rx_ack;
        if (fault && !strcmp(fault, "loss_dup") && ack_drop < 2) { ++ack_drop; drop = 1; }
    }
    pthread_mutex_unlock(&lock);
    if (drop) { ucs_mpool_put(skb); return; }
    uct_rm_urma_recv_skb_t *copy = NULL;
    size_t offset = (char *)neth - (char *)skb;
    if (dup) {
        copy = ucs_mpool_get(ucs_mpool_obj_owner(skb));
        if (copy) memcpy(copy, skb, offset + length);
        else ++duplicate_failed;
    }
    real_rx(iface, neth, length, skb, async);
    if (copy) real_rx(iface, (void *)((char *)copy + offset), length, copy, async);
}

__attribute__((destructor)) static void save_protocol(void)
{
    const char *dir = getenv("HUCX_OSU_PROBE_DIR"), *rank = getenv("OMPI_COMM_WORLD_RANK");
    const char *nonce = getenv("A89_RUN_NONCE");
    char path[PATH_MAX];
    if (!dir || !rank || !nonce || strspn(rank, "0123456789") != strlen(rank) ||
        strlen(nonce) != 32 || strspn(nonce,"0123456789abcdef") != 32) return;
    if (snprintf(path, sizeof(path), "%s/protocol_rank%s_pid%ld.json", dir, rank,
                 (long)getpid()) >= (int)sizeof(path)) return;
    int fd = open(path, O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC, 0600);
    if (fd < 0) return;
    FILE *f = fdopen(fd, "w");
    if (!f) { close(fd); return; }
    fprintf(f, "{\"schema\":2,\"retry_method\":\"PINNED_UM_SGE_LAYOUT\","
        "\"rank\":%s,\"pid\":%ld,\"nonce\":\"%s\",\"header_bytes\":%zu,"
        "\"rx_data\":%llu,\"rx_ack\":%llu,\"data_drop\":%u,\"ack_drop\":%u,"
        "\"duplicate\":%u,\"duplicate_failed\":%u,\"data_attempts\":%llu,"
        "\"data_success\":%llu,\"retry_data_posts\":%llu,\"unknown_data_layout\":%llu}\n",
        rank, (long)getpid(), nonce, sizeof(uct_rm_urma_neth_t),
        (unsigned long long)rx_data, (unsigned long long)rx_ack, data_drop, ack_drop,
        duplicate, duplicate_failed, (unsigned long long)data_attempts,
        (unsigned long long)data_success, (unsigned long long)retry_data_posts,
        (unsigned long long)unknown_data_layout);
    fclose(f);
}
