# R1: shared local RM Jetty

## Current candidate: iface-owned control sender (RM checks passed; RC open)

Tested product: `6386751d32c66fe22a75efe596dfcecff6adf911`, developed from
`5d61752d792fa5b85a28531173425f5d19cb7868`. The target-reuse change is already
included. This record-only update preserves the tested product C/H files.
The historical `4b77878ce` results below are not substituted for current checks.

- Hard FC grants use one lazily created internal software EP per RM iface.
  It borrows the existing shared Jetty, creates no Jetty/KA import, and is never
  exposed to UCP. The existing EP constructor, arbiter, registered pools and
  completion routing are reused; data AM short/bcopy are unchanged.
- Each posted reply owns its imported target until local completion or safe
  shutdown. The legacy adapter still cancels that connection's unposted
  replies on cancel/destroy/purge; other destinations stay queued.
  Non-inline headers also remain owned until CQE. Error callbacks target only
  business EPs; a late message addressed to the internal EP is ignored.
- This changes **reply ownership**, not the wire protocol. Legacy incoming FC
  still looks up the business EP for the return address. Request IDs, remote
  instance validation, peer credits, shared RX budget, stable iface publication
  and CONNECT_TO_IFACE remain open. No control-slot reservation or network
  retry guarantee is added in this substep.
- The software owner and larger pending-pool elements have a memory cost;
  this change is not a total-memory or performance-benefit claim.

Local validation: isolated x86 make using public headers/compile-only SDK
libraries; actual RM source under ASan/UBSan, the original regressions plus
30 new inline/non-inline control cases; unchanged 10-test Python design model.
ARM64 build, both-node module/loading checks, RM four-EP target reuse, three
normal RM8B runs and RM-only FC-window 1/2/4-peer checks passed. RC remains
open: an uninstrumented old/new/old RC8B comparison passed/passed/failed.
Full regression, formal product delivery and A4 completion remain pending.
See [the current validation record](validation_a4_control_20261025.md) and
LINK_DESIGN.md §12-13; no stable performance or total-memory benefit is claimed.

After isolated node build, the combined node command is:

```bash
OMPI_ALLOW_RUN_AS_ROOT=1 OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 \
python3 CASE/source/contrib/rm_ctp/r1/accept.py CASE \
  --repeats 3 --target-reuse --fc-window
```

`--fc-window` reuses the exact recorded 16-message burst source via
`fc_window.py`, verifies the current manifest/module, and checks explicit
FC=y/W8/TX8/RX64 for RM/RC. Only the RM FC diagnostics enable debug logging;
normal 8B samples keep their normal configuration. Each RM diagnostic process
may create at most one software control owner. The total must be between the
receiver count and the whole job size: the sender can answer soft requests by
piggybacking and may never need a standalone control owner. This object count is separate
from the observer's hardware Jetty counts and from network packet counts.

## Validated substep: A4 target reuse, step 1 (not complete A4/A6)

Historical product `4b77878ce` started at
`524abdbbbdcdbf05278325de0d0f44f5532d7cc2` and included the A4 design/model
and target-management change. The following evidence applies to that product.
Do not apply its old patch to the current 5d-based control-sender candidate.

- An iface hash table owns imported RM target Jettys. Matching uses the full
  128-bit EID, uasid, Jetty ID, mode, policy, type, flags, transport and token.
- Concurrent logical EPs for the same target hold references to one import.
  Each retains its own remote EP ID, KA segment, pending, completion and flush.
  The send fast path continues to use the existing cached `ep->tjetty` pointer.
- Failed construction returns its target reference. The last reference is
  released only through the existing CQE-delayed EP cleanup. No zero-reference
  target is reused: the legacy address has no remote instance identifier.
- A failed unimport quarantines the entry until iface close, where it is
  retried after stopping the local Jetty. Repeated provider cleanup failure is
  logged as in the existing backend; recovery/leak freedom under that hardware
  failure is not claimed.
- The existing 40-byte EP address layout is preserved, with safe unaligned
  access. CONNECT_TO_EP, logical identity and current FC messages are retained.
  Stable iface publication, independent FC return and shared RX budgeting
  remain later work. There is no new software data ACK or retransmission.

Validation available before deployment: isolated x86 build; actual RM C with
ASan/UBSan mocks (shared target, allocation/import rollback, identity/rehash,
CQE retirement, quarantine and actual iface cleanup); the unchanged 10-case
Python design model; and the new two-process UCP probe over local TCP.
The local TCP probe checks the harness only, not RM/CTP/provider behavior.
The exact product at `4b77878ce65994387a1bf9e9a1c283e676c7f786` subsequently
passed ARM64 isolated build, both-node module/loading checks, the actual UCP
four-EP target-reuse diagnostic, normal MPI 8B/multi-peer regressions and the
explicit FC-window regressions on 2026-10-23. See
[the acceptance record](acceptance_a4_target_20261023.md). The recorded
`22.54 us` RC sample is retained; stable performance/non-regression and total
resource benefits remain unaccepted. Only this functional substep is closed.

The existing `accept.py` gains optional `--target-reuse`. After its loader
check it compiles/deploys `target_reuse.c` and runs RM and RC separately.
One UCP worker creates four logical EPs to one peer worker; four distinct
64-byte tags/payloads are checked, then EPs/workers close. TCP carries only
worker addresses and completion control, as in the UCX hello-world example;
this is test bootstrap, not a product handshake or an MPI benchmark.
Each process has an external timeout; the receiver progresses UCP during close.
The diagnostic binds node97 TCP port 19943 for this case only.
For RM the debug trace must show one successful target import, at least three
reuse hits and one successful release per node/iface. These count wrapper/API
operations, not network packets or hardware TP objects. RC must pass the same
payload probe; no RC import-count or performance advantage is inferred.
The ordinary MPI star test does not necessarily exercise duplicate targets.

On the nodes, after a fixed-commit isolated build, use the existing invocation
with `--repeats 3 --target-reuse`; keep diagnostics out of performance claims.
The FC-window regression uses the exact executed helper archived under
`recorded_helpers_20261023/a4_fc_window_20261023.py`; it is pinned to the
recorded node case, module and runner. It is historical test evidence, not
product runtime or a portable tool that guesses a different installation.


Historical R1 implementation base: `6ab11cd304690d7e40605be002e92da50b36f5a1`.
The R2 FC error-path correction starts at the formal R1 delivery
`9f5f07b878e1370ca389987f79b91b44f300ea2a`, branch `rm-contract-r2`.

Product tested on ARM64 at `f0b5cd4ab6fdfceaf6ce0ed98e64db91db861842`.
Basic two-node AM and multi-peer acceptance passed on 2026-10-22; see
`acceptance_20261022.md`. Subsequent runner/documentation fixes preserve the
four tested RM product files byte-for-byte through GitHub commit
`462866e2a55dc42ef69d2ad26740ff3e07c4f59f` and the formal R1 delivery.
The R2 correction at `1f81b035a4ecbdac63201c5ce460fdaa0a4a6375` changes
the EP/iface C files. Its separate ARM64 build, normal two-node regression,
and explicit small-window FC regression passed on 2026-10-22; see
`acceptance_r2_20261022.md`. Terminal error cases remain CPU-mock coverage.
The historical R1 hardware evidence is retained for its original version.

Goal: one local RM Jetty per iface, with independent logical endpoints and
completion ownership, while reusing existing inline/bcopy/mpool/arbiter code.
RC and the public URMA/UCP framework are unchanged.

## R2 initial correction: flow-control errors

- Ignore late FC messages for a failed iface or a closing/canceled/failed EP.
- Keep a grant request pending only for `UCS_ERR_NO_RESOURCE`; release it on
  success, cancellation or a terminal error. Cancellation does not fail peers.
- Notify a queued grant's terminal error only after arbiter dispatch returns,
  so an error callback may safely purge pending requests or destroy endpoints.
- Control-request allocation failure uses the same iface error path.
- `test_shared_tx.c` adds 11 CPU cases for late FC, immediate/queued errors,
  temporary post failure and retry, cancel, cross-EP shared credit, FC-window
  exhaustion and grant wakeup, allocation failure and callback destruction/purge.
  These run with ASan/UBSan; LeakSanitizer remains disabled as described below.
- Normal hardware regression and FC-enabled window-exhaustion progress passed
  for the exact R2 commit; injected terminal errors were tested only with mocks.
  This is not CTP loss recovery or a new ACK protocol. Complete R2 contract and
  reliability acceptance remain open; see `acceptance_r2_20261022.md`.

## Product behavior

- The iface creates/deletes its shared Jetty. EPs borrow it and keep their own
  shared-target reference, EP ID, submitted/completed sequence and outstanding queue.
- R1 requests one local CQE per WR, with out-of-order completion disabled.
  Per-EP moderation is not safe to retain unchanged on a shared send queue.
  This choice must be included in latency/throughput interpretation.
- Shared-SQ waiters stay scheduled; a completion on another EP can wake them.
  RX traffic cannot indefinitely suppress bounded TX progress.
- Local flush waits for the EP's previous WRs. Flush completion sequence remains
  32 bits. Remote flush is explicitly unsupported by this AM-only candidate.
- EP destruction defers physical cleanup until its WRs complete. IDs, target
  references and registered descriptors remain alive. Callbacks cannot free the
  EP while completion processing still accesses its queue.
- Invalid/synthetic completion contexts are not used as an EP array index.
  Shared-Jetty errors stop new submissions and conservatively affect all EPs;
  no per-peer fault isolation or recovery is claimed.
- Non-inline FC grants use separate registered descriptors until their CQEs.
- R1 advertises AM short/bcopy and relevant connection/pending facilities.
  Copied RMA/atomic/AM-zcopy/peer-check capabilities are temporarily masked.
  RMA and its rkey/visibility guarantees belong to later dedicated acceptance.

## Validation

- Full local x86 HUCX build passed against public SDK headers at
  `50288b2a4a62fb76e3584acd8d8ed1c1071a485d` and compile-only placeholder libs.
  These libs are not a URMA implementation and are never included in the patch.
- `run_mock.sh BUILD_DIR URMA_INCLUDE` executes the actual RM sources with
  mocked URMA calls and real UCX queues/mpools. ASan and UBSan are enabled.
- Covered: shared credit/routing, cross-EP pending, independent flush, sequence
  beyond 65535 and 32-bit wrap, 70000 completions, reentrant destruction from a
  completion callback, bcopy post rollback, delayed descriptor/EP retirement,
  distinct non-inline FC headers, synthetic error completion handling.
- LeakSanitizer is disabled by default because this Work runtime restricts its
  `/proc` task enumeration. It can be enabled separately with ASAN_OPTIONS.
- ARM64 build, two-node 8B validation, five alternating RC/RM 8B repetitions,
  and 1/2/4 remote-rank star traffic passed. RM target-mode Jetty peaks were
  one per observed context; RC peaks in this workload were 2/3/5. Each rank
  passed candidate-library maps checks and target-mode Jetty live-at-exit=0.
- Hardware loss/retry, complete CTP semantic guarantees, RMA, large messages,
  and ppn16 collectives remain outside this acceptance.

## Isolated node flow

1. Archive the user-pushed exact commit with `git archive --format=tar.gz`.
   Upload it to the existing experiment `upload/` directory.
2. On node89, run `build_isolated.sh ARCHIVE FULL_COMMIT` from that archive.
   It creates new source/build/install directories, keeps full logs and emits
   a build manifest and install tar. It does not overwrite existing builds.
3. Run `python3 SOURCE/contrib/rm_ctp/r1/accept.py CASE_DIR` on node89.
   This copies the candidate install into the same fresh path on node97 using
   `root@9.88.43.97` and checks the module SHA and dependencies. Run under the
   authorized root account with `OMPI_ALLOW_RUN_AS_ROOT=1` and
   `OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1`. The remote upload directory is created
   first. For the already-deployed case, pass `--no-deploy`; an existing peer
   install is not overwritten by the default deploy path.
4. The runner reads `osu_minimal8_ff_14ns8/{rm,rc}_run.json`, substitutes the
   explicitly known old install prefix and preserves the saved launcher setup.
   Unsupported input shapes fail with a short diagnostic rather than guessing.
5. One loader diagnostic correctness run is excluded from timing samples.
   Five alternating RC/RM 8B runs retain OSU `-c` validation and saved iterations.
   The reported medians are a small repeated comparison, not proof of a stable
   advantage across workloads.
6. The MPI star app validates 1/2/4 remote peers, 16-message bursts, with 64B
   and 1024B payloads. Multi-peer cases use TX_DEPTH=8 for both modes; observer
   records must confirm that the setting took effect. These are correctness
   and resource tests, not multi-peer performance measurements.
   The effective settings are `UCX_RM_URMA_TX_DEPTH` and
   `UCX_RC_URMA_TX_DEPTH`, explicitly forwarded to remote ranks with `-x`.
7. The existing independent observer records new per-run files on both hosts.
   The helper checks actual process maps, per-context target-mode Jetty peaks,
   and zero live data-mode Jettys at exit. The expected single selected RM iface
   shares its Jetty. Multiple iface instances in one context require reviewing
   that expectation instead of interpreting context count as iface count.

`recorded_helpers_20261022/` preserves the exact scripts used in the reported
node run. The first helper completed the five 8B repetitions but used an
ineffective depth variable; its resource assertion stopped the first peer
case. The second helper resumed all peer cases with the correct variable and
passed. These case-specific historical scripts are evidence, not the entry
point for a new run. Use the corrected `accept.py` above for new runs.

Package one AtomGit delivery commit for the accepted R1 scope, preserve shared
history and verify that the four product files match the tested commit.
Formal AtomGit delivery is a separate workflow checkpoint.
