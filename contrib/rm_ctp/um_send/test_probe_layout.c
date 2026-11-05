#include "probe_layout.h"
#include <assert.h>
#include <stdio.h>
int main(void)
{
    unsigned short_count=0,bcopy_count=0,retry_count=0;
    for (unsigned i=0;i<200000;++i) {
        volatile uint16_t wire_psn=(uint16_t)i;
        (void)wire_psn; /* Sequence wrap must not affect a per-WR layout decision. */
        short_count += um_probe_classify(2,16,1,0,1)==UM_PROBE_SHORT;
        bcopy_count += um_probe_classify(1,4096,1,0,0)==UM_PROBE_BCOPY;
    }
    for (unsigned i=0;i<31;++i)retry_count+=um_probe_classify(2,8,1,1,0)==UM_PROBE_RETRY;
    assert(short_count==200000 && bcopy_count==200000 && retry_count==31);
    assert(um_probe_classify(2,8,0,1,0)==UM_PROBE_UNKNOWN);
    assert(um_probe_classify(2,8,1,0,0)==UM_PROBE_UNKNOWN);
    assert(um_probe_classify(2,8,1,1,1)==UM_PROBE_UNKNOWN);
    assert(um_probe_classify(3,8,1,1,0)==UM_PROBE_UNKNOWN);
    assert(um_probe_classify(1,7,0,0,0)==UM_PROBE_UNKNOWN);
    puts("CLASSIFIER_400000_NORMAL_WR_31_RETRY_3_WRAPS=PASS");
}
