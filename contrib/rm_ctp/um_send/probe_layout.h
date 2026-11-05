/* Pinned a4d SEND layouts; no packet history and no PSN arithmetic. */
#ifndef RM_UM_PROBE_LAYOUT_H
#define RM_UM_PROBE_LAYOUT_H
#include <stdint.h>
enum um_probe_layout { UM_PROBE_UNKNOWN, UM_PROBE_SHORT, UM_PROBE_BCOPY, UM_PROBE_RETRY };
static inline enum um_probe_layout um_probe_classify(uint32_t sges, uint32_t first_len,
        unsigned complete, unsigned solicited, unsigned inlined)
{
    /* am_short: neth(8)+short_hdr(8), then application bytes, INLINE. */
    if (sges == 2 && first_len == 16 && inlined && !solicited) return UM_PROBE_SHORT;
    /* am_bcopy: one contiguous neth+packed payload, not inline. */
    if (sges == 1 && first_len >= 8 && !inlined && !solicited) return UM_PROBE_BCOPY;
    /* ep_resend -> send_ctl: neth(8), saved body; SIGNALED|SOLICITED. */
    if (sges == 2 && first_len == 8 && complete && solicited && !inlined) return UM_PROBE_RETRY;
    return UM_PROBE_UNKNOWN;
}
#endif
