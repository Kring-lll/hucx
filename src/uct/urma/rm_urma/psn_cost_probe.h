/* Experiment-only sampled timing. No changes to transport state. */
#ifndef HUCX_PSN_COST_PROBE_H
#define HUCX_PSN_COST_PROBE_H
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <pthread.h>

enum psn_group {
    PG_OFF, PG_PSN, PG_TX, PG_RX, PG_PROGRESS, PG_TOTAL, PG_WINDOW, PG_CONTROL
};
#define PSN_TAGS(X) \
    X(TX_HEADER_FIELDS) X(TX_ACK_REQUEST) X(TX_CLOCK) X(TX_PSN_INC) \
    X(TX_WINDOW_PUSH) X(TX_TIMER) X(RX_ACK_CHECK) X(RX_ACK_ADVANCE) \
    X(RX_ACK_REQ_GAP) X(RX_SEQ_INSERT) X(TX_PREP) X(TX_AM_HEADER_SGE) \
    X(TX_WR_PREP) X(TX_POST) X(TX_RETIRE) X(TX_RESOURCE_UPDATE) \
    X(TX_SHORT_TOTAL) X(TX_BCOPY_TOTAL) X(TX_SHORT_OK) X(TX_BCOPY_OK) \
    X(RX_MESSAGE) X(RX_CALLBACK) X(RX_DATA_CALLBACK) X(RX_CONTROL_CALLBACK) \
    X(PROGRESS_TOTAL) X(POLL_RX) X(POLL_TX) X(PROGRESS_PENDING) \
    X(TX_SKB_ALLOC) X(TX_EXTRA_COPY) X(ACK_WINDOW_RELEASE) \
    X(ACK_SEND) X(TIMER_CALLBACK) X(TIMER_SWEEP) X(RETRY_SEND)
#define PSN_ENUM(x) PT_##x,
enum psn_tag { PSN_TAGS(PSN_ENUM) PT_COUNT };
#undef PSN_ENUM
#define PSN_NAME(x) #x,
static const char *const psn_names[] = { PSN_TAGS(PSN_NAME) };
#undef PSN_NAME

struct psn_stat {
    uint64_t calls, samples, sum, min, max;
    uint32_t next, rng;
};
struct psn_thread {
    struct psn_thread *next;
    struct psn_stat stats[PT_COUNT];
};
struct psn_scope {
    struct psn_stat *stat;
    uint64_t start;
};
static __thread struct psn_thread *psn_local;
static struct psn_thread *psn_threads;
static pthread_mutex_t psn_lock = PTHREAD_MUTEX_INITIALIZER;
static enum psn_group psn_group;
static unsigned psn_stride = 64;
static uint64_t psn_hz = 1000000000ULL, psn_cal;
static const char *psn_group_name = "off";

/* ARM: physical elapsed timer ticks, not core PMU cycles. On other
 * architectures use the monotonic raw clock. Compiler and CPU ordering
 * prevent measuring operations outside the chosen interval. */
static inline uint64_t psn_now(void)
{
#if defined(__aarch64__)
    uint64_t v;
    __asm__ volatile("isb; mrs %0, cntvct_el0" : "=r"(v) :: "memory");
    return v;
#else
    struct timespec t;
    __asm__ volatile("" ::: "memory");
    if (clock_gettime(CLOCK_MONOTONIC_RAW, &t) != 0) abort();
    __asm__ volatile("" ::: "memory");
    return (uint64_t)t.tv_sec * 1000000000ULL + (uint64_t)t.tv_nsec;
#endif
}
static int psn_cmp(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return (x > y) - (x < y);
}
static void __attribute__((constructor)) psn_init(void)
{
    const char *g = getenv("HUCX_PSN_PROBE_GROUP");
    const char *n = getenv("HUCX_PSN_PROBE_SAMPLE");
    const char *const groups[] = {
        "off", "psn", "tx", "rx", "progress", "total", "window", "control"
    };
    uint64_t ticks[4096];
    if (g) {
        for (unsigned i = 0; i < sizeof(groups)/sizeof(groups[0]); ++i) {
            if (!strcmp(g, groups[i])) {
                psn_group = (enum psn_group)i;
                psn_group_name = groups[i];
                break;
            }
        }
    }
    if (psn_group == PG_OFF) return;
    if (n) {
        char *end;
        unsigned long stride = strtoul(n, &end, 10);
        if (!*end && stride >= 1 && stride <= 4096) psn_stride = (unsigned)stride;
    }
#if defined(__aarch64__)
    __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(psn_hz));
    if (!psn_hz) psn_hz = 1000000000ULL;
#endif
    for (unsigned i = 0; i < 4096; ++i) {
        uint64_t a = psn_now(), b = psn_now();
        ticks[i] = b - a;
    }
    qsort(ticks, 4096, sizeof(ticks[0]), psn_cmp);
    psn_cal = ticks[2048];
}
static __attribute__((noinline)) struct psn_thread *psn_register(void)
{
    struct psn_thread *t = calloc(1, sizeof(*t));
    if (!t) return NULL;
    for (unsigned i = 0; i < PT_COUNT; ++i) {
        uint32_t seed = (uint32_t)((uintptr_t)t >> 4) ^
                        (uint32_t)getpid() ^ (0x9e3779b9U * (i + 1));
        t->stats[i].rng = seed ? seed : 1;
        t->stats[i].next = 1 + seed % psn_stride;
        t->stats[i].min = UINT64_MAX;
    }
    pthread_mutex_lock(&psn_lock);
    t->next = psn_threads;
    psn_threads = t;
    pthread_mutex_unlock(&psn_lock);
    psn_local = t;
    return t;
}
/* Per-thread single-writer counters; relaxed atomic accesses also permit
 * an exit snapshot without a data race. No lock per ordinary event. */
static inline uint64_t psn_load(uint64_t *p)
{
    return __atomic_load_n(p, __ATOMIC_RELAXED);
}
static inline void psn_store(uint64_t *p, uint64_t v)
{
    __atomic_store_n(p, v, __ATOMIC_RELAXED);
}
static inline struct psn_stat *psn_stat_for(enum psn_tag tag)
{
    struct psn_thread *t = psn_local;
    if (!t) t = psn_register();
    return t ? &t->stats[tag] : NULL;
}
static inline struct psn_scope psn_start(enum psn_group group, enum psn_tag tag, int enabled)
{
    struct psn_scope s = {0};
    struct psn_stat *p;
    uint32_t r;
    if (!enabled || psn_group != group) return s;
    p = psn_stat_for(tag);
    if (!p) return s;
    psn_store(&p->calls, psn_load(&p->calls) + 1);
    if (--p->next) return s;
    /* Jitter rather than fixed-period sampling, to avoid aliasing with
     * ACK intervals and completion moderation. */
    r = p->rng;
    r ^= r << 13; r ^= r >> 17; r ^= r << 5;
    p->rng = r;
    p->next = 1 + r % (2 * psn_stride - 1);
    s.stat = p;
    s.start = psn_now();
    return s;
}
static inline void psn_end(struct psn_scope *s)
{
    uint64_t delta;
    struct psn_stat *p;
    if (!s->stat) return;
    delta = psn_now() - s->start;
    p = s->stat;
    psn_store(&p->samples, psn_load(&p->samples) + 1);
    psn_store(&p->sum, psn_load(&p->sum) + delta);
    if (delta < psn_load(&p->min)) psn_store(&p->min, delta);
    if (delta > psn_load(&p->max)) psn_store(&p->max, delta);
}
static inline void psn_count(enum psn_group group, enum psn_tag tag)
{
    struct psn_stat *p;
    if (psn_group != group) return;
    p = psn_stat_for(tag);
    if (p) psn_store(&p->calls, psn_load(&p->calls) + 1);
}
#define PSN_CAT_I(a,b) a##b
#define PSN_CAT(a,b) PSN_CAT_I(a,b)
#define PSN_SCOPE_IF(g,t,c) \
    struct psn_scope PSN_CAT(psn_scope_, __COUNTER__) \
    __attribute__((cleanup(psn_end))) = psn_start(PG_##g, PT_##t, (c))
#define PSN_SCOPE(g,t) PSN_SCOPE_IF(g,t,1)
#define PSN_COUNT(g,t) psn_count(PG_##g, PT_##t)

static void __attribute__((destructor)) psn_report(void)
{
    const char *rank;
    char host[128] = {0};
    if (psn_group == PG_OFF) return;
    rank = getenv("OMPI_COMM_WORLD_RANK");
    gethostname(host, sizeof(host)-1);
    pthread_mutex_lock(&psn_lock);
    for (unsigned i = 0; i < PT_COUNT; ++i) {
        uint64_t calls=0, samples=0, sum=0, min=UINT64_MAX, max=0;
        double raw, ns;
        for (struct psn_thread *t = psn_threads; t; t = t->next) {
            struct psn_stat *p = &t->stats[i];
            uint64_t v;
            calls += psn_load(&p->calls);
            samples += psn_load(&p->samples);
            sum += psn_load(&p->sum);
            v = psn_load(&p->min); if (v < min) min = v;
            v = psn_load(&p->max); if (v > max) max = v;
        }
        if (!calls) continue;
        raw = samples ? (double)sum / samples : 0.;
        ns = 1e9 / (double)psn_hz;
        fprintf(stderr, "PSN_PROBE host=%s rank=%s pid=%ld unit=%s group=%s tag=%s calls=%llu samples=%llu sum_ticks=%llu timer_hz=%llu cal_ticks=%llu raw_ns=%.3f net_ns=%.3f min_ticks=%llu max_ticks=%llu\n",
                host, rank ? rank : "-", (long)getpid(), PSN_PROBE_UNIT,
                psn_group_name, psn_names[i],
                (unsigned long long)calls, (unsigned long long)samples,
                (unsigned long long)sum, (unsigned long long)psn_hz,
                (unsigned long long)psn_cal, raw*ns,
                samples ? (raw-(double)psn_cal)*ns : 0.,
                (unsigned long long)(samples ? min : 0),
                (unsigned long long)max);
    }
    pthread_mutex_unlock(&psn_lock);
}
#endif
