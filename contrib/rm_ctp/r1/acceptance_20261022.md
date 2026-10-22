# R1 basic shared-Jetty acceptance, 2026-10-22

The basic AM shared-local-Jetty requirement passed the cases below on two
ARM64 nodes. This is a scoped functional/resource acceptance, not acceptance
of the complete RM+CTP transport or all CTP guarantees.

## Product and evidence

- Product commit: `f0b5cd4ab6fdfceaf6ce0ed98e64db91db861842`.
- Branch: `rm-shared-jetty-r1`, repository `Kring-lll/hucx`.
- Module SHA256 on both nodes:
  `0e5550b9ee63792c45b78aed2dc78c3ca2c3c454457789b433548f0386278d79`.
- Source archive SHA256:
  `efa2545c5af2f94212e382b660be409511c8a4454c2924b7c9be8b50ee0b9678`.
- Experiment root:
  `/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019`.
- Case: `r1_shared_f0b5cd4ab_MEepCG` under that root.
- OSU logs and original partial results: `CASE/accept_20261022_091204/`.
- Combined raw node results:
  `CASE/accept_20261022_091204/many_depth8_bk16vacy/results.json`.
- `acceptance_summary_20261022.json` is a transcription of user-pasted node
  output, not a copy of the raw node JSON. Original logs remain on the nodes.

The product shares one local Jetty per RM iface. Logical EP IDs, target
references and completion sequences remain per EP. Inline/bcopy and existing
UCX memory pools and pending arbitration are retained. Every WR requests a
local CQE in R1; this choice must be considered in future performance work.

## 8B validation and small repeated comparison

The saved launcher specifications were reused with candidate library paths.
OSU validation stayed enabled. A loader diagnostic was excluded from timing.
All ten timed runs exited 0 and reported the `Pass` validation column.

| Mode | Five 8B samples, microseconds | Median, microseconds |
| --- | --- | --- |
| RC | 2.27, 2.32, 2.24, 2.34, 2.33 | 2.32 |
| RM | 2.05, 1.97, 2.05, 2.06, 2.02 | 2.05 |

This is a small repeated comparison under the recorded configuration. It does
not establish a stable advantage across workloads, message sizes or scales.

## Multi-peer correctness and resources

Rank 0 was on node89; 1/2/4 remote ranks were on node97. The application used
star traffic, 2000 rounds and 16 messages per burst. Payloads were 64B for
1/2 peers and 1024B for 4 peers. Each echo was checked by peer/sequence/offset.
The application did not explicitly issue intra-node data transfers or MPI
collectives; MPI initialization/finalization may still create resources.

| Remote ranks | RM Jetty peaks per observed context | RC Jetty peaks per observed context | Outcome |
| --- | --- | --- | --- |
| 1 | 1 on rank 0 and rank 1 | 2 on both ranks | Pass |
| 2 | 1 on every rank | 3 on every rank | Pass |
| 4 | 1 on every rank | 5 on every rank | Pass |

All six cases exited 0, passed data checks, observed TX_DEPTH=8 on every rank,
found the candidate core libraries and URMA module in actual process maps,
and ended with target-mode Jetty live count 0. The observer groups peaks by
URMA context. The selected single-RM-iface setup supports the shared-Jetty
claim here; context count must not be generalized to arbitrary iface counts.
RC counts are measured workload results; the extra Jetty is not attributed
to a particular MPI or wireup action by this test.

The correct config variables are `UCX_RM_URMA_TX_DEPTH` and
`UCX_RC_URMA_TX_DEPTH`. The initial doubled `URMA_` variables were ignored,
and the assertion rejected the first resource case at depth 256. The corrected
resume reran every peer case at observed depth 8 and retained the ten 8B runs.
SSH stderr was separated from hash/JSON output, the login account was corrected
to root, and the OSU parser was corrected to require the actual `Pass` column.

## Scope and remaining work

- CPU mocks with ASan/UBSan additionally covered completion ownership,
  cross-EP pending, independent flush, sequence values beyond 65535 and 32-bit
  wrap, reentrant destruction, post-failure rollback, descriptor retirement,
  non-inline FC headers and synthetic CQEs. These are not hardware fault tests.
- Hardware traffic/finalization demonstrated the recorded data and Jetty
  resource outcomes. No complete heap or all-provider-resource leak claim is
  made; explicit mid-flight per-EP destruction/flush was not separately injected.
- R1 only advertises the accepted AM short/bcopy scope. Copied RMA/atomic/
  AM-zcopy/peer-check capabilities remain masked. No new software ACK/retry was
  introduced; any compensation requires actual CTP contract evidence.
- No hardware packet-loss recovery, per-peer failure isolation/recovery,
  large-message/RMA, bandwidth curve or two-node ppn16 collective acceptance.
- The prior day's control-queue timeouts also affected old-version runs.
  Both old and candidate communication passed on 2026-10-22; the maintenance
  action and root cause were not established by these tests.

Subsequent changes in this evidence patch only repair the test runner and
record results. The four RM product files remain byte-identical to the tested
commit. The consolidated runner has local syntax, validation-column and SSH
output checks; the hardware evidence above came from the two recorded helper
scripts, not a separately claimed run of that consolidated runner.
AtomGit delivery must preserve shared history and establish its base
before producing one requirement commit. Delivery is pending until its actual
commit and push are confirmed.
