# R1: shared local RM Jetty candidate

Base: `6ab11cd304690d7e40605be002e92da50b36f5a1`, branch `rm-shared-jetty-r1`.

Goal: one local RM Jetty per iface, with independent logical endpoints and
completion ownership, while reusing existing inline/bcopy/mpool/arbiter code.
RC and the public URMA/UCP framework are unchanged.

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

## Validation status before hardware deployment

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
- Node ARM64 build, real MPI traffic, resource counts, RC comparison, CTP
  reliability/order/buffer-reuse guarantees: PENDING until actual evidence.

## Isolated node flow

1. Archive the user-pushed exact commit with `git archive --format=tar.gz`.
   Upload it to the existing experiment `upload/` directory.
2. On node89, run `build_isolated.sh ARCHIVE FULL_COMMIT` from that archive.
   It creates new source/build/install directories, keeps full logs and emits
   a build manifest and install tar. It does not overwrite existing builds.
3. Run `python3 SOURCE/contrib/rm_ctp/r1/accept.py CASE_DIR` on node89.
   This copies the candidate install into the same fresh path on node97 using
   the existing user account and checks the module SHA and dependencies.
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
7. The existing independent observer records new per-run files on both hosts.
   The helper checks actual process maps, per-context target-mode Jetty peaks,
   and zero live data-mode Jettys at exit. The expected single selected RM iface
   shares its Jetty. Multiple iface instances in one context require reviewing
   that expectation instead of interpreting context count as iface count.

After all planned runs, review results, unresolved CTP contracts and limitations
before declaring R1 accepted. Package one AtomGit delivery commit only after
acceptance; preserve GitHub process history and do not force-push.
