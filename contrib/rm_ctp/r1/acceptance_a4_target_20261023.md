# A4 target reuse substep: functional acceptance, 2026-10-23

This closes the functional regression for one target-management substep. It
does not complete A4/A5/A6, establish CTP reliability semantics, or demonstrate
latency/total-memory benefits. Results are transcribed from the operator's
terminal summaries and interpreted using the pinned runners. Raw node logs
and JSON have not been downloaded or independently inspected by the reviewer.

## Version and installation

- Hardware-tested source: `4b77878ce65994387a1bf9e9a1c283e676c7f786`.
- Parent: `524abdbbbdcdbf05278325de0d0f44f5532d7cc2`.
- Source archive: `hucx-a4-4b77878ce-source.tar.gz`.
- Archive SHA256:
  `c0790fb3507e8a5cfa407530bc294dc65d62c7883ef3155ec2693e608f0fa1a1`.
- Experiment root:
  `/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019`.
- CASE: experiment root + `/r1_shared_4b77878ce_DbsqE3`.
- ARM64 isolated build passed on node89. The same installation was deployed
  into a new matching case on node97; existing working versions were retained.
- Candidate module: `CASE/install/lib/ucx/libuct_ub.so.0.0.0`.
- Both-node module SHA256:
  `ed196bd68a22353067c73fc5f1a7a2e177123f9139beebc532ce767aebc4591c`.
- Hosts/accounts: node89/9.88.43.89 and node97/9.88.43.97, authorized root.
- The later record-only commit preserves the hardware-tested product source.
  Its different Git SHA does not identify another compiled module.

## Product change and local evidence

Only the four RM EP/iface C/H files change product behavior. An iface-owned
khash holds reference-counted imported target Jettys, keyed by all 128 EID
bits, uasid, Jetty ID, mode, policy, type, flags, transport and token. An EP
borrows its target but retains its own remote EP ID, KA segment, pending,
completion and flush ownership. Existing sends keep using `ep->tjetty`.

Failed construction returns its target reference. The last reference is
released through the existing CQE-delayed EP cleanup. The legacy address has
no remote instance identifier, so zero-reference imports are not retained for
reuse. Failed unimports are quarantined until iface-close retry. Persistent
provider cleanup failure remains a logged failure, not proven recovery.
The existing 40-byte EP wire layout is preserved with safe unaligned access.
RC, UM and common UCP/URMA product source are unchanged.

Local ASan/UBSan mocks cover the actual RM connect/get-address/cleanup paths,
shared-target ownership, allocation/import/segment rollback, complete keys,
128-target rehash, reference overflow, delayed release, quarantine and close
ordering. Existing 70000-completion/pending/flush and 11 FC-error cases remain
covered. LSan was disabled; fixture target/segment live counters end at zero.
The separate 10-test Python model does not implement or validate the new C FC
protocol. The new UCP probe was first checked with two local TCP processes.

## Two-node UCP target-reuse diagnostic

Command, with both Open MPI root-allow environment flags:

```text
python3 CASE/source/contrib/rm_ctp/r1/accept.py CASE --repeats 3 --target-reuse
```

Results: `CASE/accept_20261023_172443/results.json`.
Peer module equality and the loader diagnostic passed, validation enabled,
exit 0. The diagnostic then ran `target_reuse.c` in RM and RC separately.

This is a UCP test, not an MPI collective or multi-rank workload. One sender
worker creates four concurrent logical EPs to one receiver worker. Each sends
a distinct 64-byte tagged payload. TCP carries only worker addresses and a
completion marker; the payloads use the selected UCX transports. Receiver
progress continues while sender EPs close.

| Mode | Payload/exit validation | Per-node RM target-wrapper operations |
| --- | --- | --- |
| RM | Both ends pass, four EPs, exit 0 | Import 1, reuse at least 3, release 1 |
| RC | Both ends pass, same payload scenario, exit 0 | RC target counts were not measured |

Counts are successful C-wrapper import/reference operations, not network
packet or hardware-TP counts. A local Jetty is a different resource from an
imported target. This case proves reuse when several logical EPs address the
same target; a usual one-EP-per-peer MPI workload may have no such cache hits.
It does not prove reduced UCP wireup or earlier iface-address publication.

## Normal MPI correctness regressions

All six 8-byte samples passed data validation and exited 0. Alternating order:
RC/RM, RM/RC, RC/RM. Values are OSU latency readings in microseconds.

| Mode | Repetition 0 | Repetition 1 | Repetition 2 | Median |
| --- | ---: | ---: | ---: | ---: |
| RC | 22.54 | 2.29 | 2.40 | 2.40 |
| RM | 2.14 | 2.10 | 2.07 | 2.10 |

The RC 22.54 sample is retained. Its cause is unknown. Three short validated
runs, including this high sample, do not establish stable performance,
non-regression, or an RM advantage. No sample is silently discarded.

All six MPI star cases passed. Each uses one rank on node89 and 1/2/4 remote
ranks on node97. The runner checks rank placement, candidate maps, actual
target-mode TX depth 8, data and zero target-mode Jettys live at exit.

| Remote ranks | RM local-Jetty peaks by observed context | RC peaks |
| --- | --- | --- |
| 1 | [1] | [2] |
| 2 | [1] | [3] |
| 4 | [1] | [5] |

This star workload is not the four-EP/same-target UCP diagnostic. Jetty counts
do not measure target-import savings, queue memory or a general RC formula.

## Explicit small-window FC regression

Executed helper: `CASE/a4_fc_window_20261023.py`.
An exact copy is retained at
`recorded_helpers_20261023/a4_fc_window_20261023.py`.

- Helper SHA256:
  `3d63cfac452e0f1c5e767a2197eaa8a4888ee172b01d48bdf252fe65d3fa7499`.
- Its embedded C test is unchanged from the previous R2 regression, SHA256:
  `502048a73533151fccba7cde6b77c24a84ae26e7e1c6ec7b6f9a6291d44d3b59`.
- Only pinned product/runner identities, case/results paths, output prefix
  and error label changed. The tested product source was not edited.
- FC_DIR: `CASE/a4_fc_window8_7s8b80yt`.
- Results: `FC_DIR/results.json`; generated C, executed-helper copy,
  commands/env, configuration logs, maps and observer files remain there.
- Both nodes parsed RM and RC settings successfully: FC enabled, window 8,
  TX depth 8, RX depth 64; soft/hard thresholds 0.5/0.25.
- Rendezvous and zcopy thresholds were set to `inf` for this eager test.
  Actual rank environment and candidate libraries/modules were checked.
- Rank0 sends 16 messages per batch to each peer. Peers validate all 16 before
  returning the application batch reply. This requires normal independent FC
  progress rather than relying on intermediate application replies.

| Mode | Remote peers | Configuration/data/maps/depth | Jettys live at exit |
| --- | --- | --- | --- |
| RM | 1, 2, 4 | All pass, 2000 batches each | 0 |
| RC | 1, 2, 4 | All pass, same workload | 0 |

Payloads are 64 bytes for 1/2 peers and 1024 bytes for 4 peers. This validates
normal FC progress in those specific cases, not arbitrary sender counts or
the future shared-RX credit ledger. Hardware fault injection was not run.

## Acceptance boundary and next work

The target-reuse substep's functional gates passed: offline checks, ARM64
build, module/loading checks, actual same-target/multiple-EP reuse, normal
MPI regressions and the explicit FC-window regression. It is ready for one
scoped delivery commit after this record is synchronized.

A4/A5/A6 remain incomplete. Next work includes stable iface-address publication,
independent FC reply identity/lifetime, peer credits and shared-RX budgeting,
the known-iface connection path, then its correctness and scale acceptance.
Current CONNECT_TO_EP and FC messages remain in use. There is no new data ACK
or retransmission. Necessary CTP guarantees remain C1 work.

Initialization/unwarmed-first-message costs, total queues/memory, performance
thresholds, two-node PPN20 comparison and larger RM scales remain unaccepted.
