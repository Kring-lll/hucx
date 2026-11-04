/* Temporary diagnostic source only; never included by the product build. */
#ifndef HUCX_SEND_COST_DIAG_H
#define HUCX_SEND_COST_DIAG_H
#include <ucs/profile/profile_defs.h>
#include <errno.h>
extern int hucx_profile_active __attribute__((weak));
extern void hucx_cost_add(unsigned id, unsigned long long amount) __attribute__((weak));
#define COST_ACTIVE() (&hucx_profile_active && __atomic_load_n(&hucx_profile_active, __ATOMIC_ACQUIRE))
#define COST_CALL(name, func, ...) ({ \
    ucs_typeof(func(__VA_ARGS__)) cost_result; \
    if (COST_ACTIVE()) { \
        int cost_before_errno = errno, cost_after_errno; \
        UCS_PROFILE_CODE_ALWAYS(name, { errno = cost_before_errno; \
            cost_result = func(__VA_ARGS__); cost_after_errno = errno; }); \
        errno = cost_after_errno; \
    } else { cost_result = func(__VA_ARGS__); } \
    cost_result; })
#define COST_VOID(name, func, ...) do { \
    if (COST_ACTIVE()) { int cost_before_errno = errno, cost_after_errno; \
        UCS_PROFILE_CODE_ALWAYS(name, { errno = cost_before_errno; \
            func(__VA_ARGS__); cost_after_errno = errno; }); errno = cost_after_errno; } \
    else { func(__VA_ARGS__); } \
} while (0)
#define COST_ADD(id, amount) do { \
    if (COST_ACTIVE() && hucx_cost_add) hucx_cost_add(id, amount); \
} while (0)
static inline int hucx_cost_empty(void) { return 0; }
#endif
