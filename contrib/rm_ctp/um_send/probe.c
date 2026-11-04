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

#define SEEN_CAP 32768
struct seen_key {
    uintptr_t local, remote;
    uint32_t dest;
    uint16_t psn;
    unsigned occupied;
};
static struct seen_key keys[SEEN_CAP];
static uint64_t repeat_posts, key_overflow, rx_data, rx_ack;
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

/* Record PSN only after a successful provider post; the WR is snapshotted first. */
urma_status_t urma_post_jetty_send_wr(urma_jetty_t *jetty, urma_jfs_wr_t *wr,
                                    urma_jfs_wr_t **bad)
{
    struct seen_key snapshot[MAX_CHAIN];
    unsigned count = 0;
    if (jetty && jetty->jetty_cfg.jfs_cfg.trans_mode == URMA_TM_RM) {
        for (urma_jfs_wr_t *p = wr; p && count < MAX_CHAIN; p = p->next) {
            if (p->opcode != URMA_OPC_SEND || !p->send.src.num_sge ||
                !p->send.src.sge || p->send.src.sge[0].len < sizeof(uct_rm_urma_neth_t)) continue;
            uct_rm_urma_neth_t h;
            memcpy(&h, (void *)(uintptr_t)p->send.src.sge[0].addr, sizeof(h));
            if (!(h.packet_type & UCT_RM_URMA_PACKET_FLAG_AM)) continue;
            snapshot[count++] = (struct seen_key){(uintptr_t)jetty, (uintptr_t)p->tjetty,
                h.packet_type & UCT_RM_URMA_PACKET_DEST_ID_MASK, h.psn, 1};
        }
    }
    urma_status_t status = wr_forward_send(jetty, wr, bad);
    if (status != URMA_SUCCESS) return status;
    pthread_mutex_lock(&lock);
    for (unsigned n = 0; n < count; ++n) {
        struct seen_key *k = &snapshot[n];
        size_t pos = (k->psn * 2654435761u ^ k->dest ^ (k->remote >> 4)) % SEEN_CAP;
        unsigned i;
        for (i = 0; i < SEEN_CAP; ++i, pos = (pos + 1) % SEEN_CAP) {
            struct seen_key *e = &keys[pos];
            if (!e->occupied) { *e = *k; break; }
            if (e->local == k->local && e->remote == k->remote && e->dest == k->dest && e->psn == k->psn) {
                ++repeat_posts; break;
            }
        }
        key_overflow += i == SEEN_CAP;
    }
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
    fprintf(f, "{\"rank\":%s,\"pid\":%ld,\"nonce\":\"%s\",\"header_bytes\":%zu,"
        "\"rx_data\":%llu,\"rx_ack\":%llu,\"data_drop\":%u,\"ack_drop\":%u,"
        "\"duplicate\":%u,\"duplicate_failed\":%u,\"repeat_posts\":%llu,\"key_overflow\":%llu}\n",
        rank, (long)getpid(), nonce, sizeof(uct_rm_urma_neth_t),
        (unsigned long long)rx_data, (unsigned long long)rx_ack, data_drop, ack_drop,
        duplicate, duplicate_failed, (unsigned long long)repeat_posts,
        (unsigned long long)key_overflow);
    fclose(f);
}
