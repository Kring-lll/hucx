# R1: shared local RM Jetty

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
  target reference, EP ID, submitted/completed sequence and outstanding queue.
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
