# R2 initial FC correction: validation record (2026-10-22)

This records one correction within R2, not completion of the full R2 contract
and reliability requirement. Results below are transcribed from the operator's
terminal output and interpreted using the pinned runners' checks. Raw node
JSON/logs have not been downloaded or independently inspected by the reviewer.
Earlier R1 results retain their original version and scope.

## Identity

- Development branch: `rm-contract-r2`.
- Hardware-tested source: `1f81b035a4ecbdac63201c5ce460fdaa0a4a6375`.
- Parent: formal R1 delivery `9f5f07b878e1370ca389987f79b91b44f300ea2a`.
- Source archive: `hucx-r2-1f81b035a-source.tar.gz`.
- Archive SHA256: `86d59ca8c7f87895e326268a1af9aa489c8579d1c80d191a21fa6a928ec86e96`.
- Experiment root: `/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019`.
- CASE: experiment root + `/r1_shared_1f81b035a_xEeGOM`.
  The historical directory prefix does not identify the product version.
- Candidate module: `CASE/install/lib/ucx/libuct_ub.so.0.0.0`.
- Module SHA256: `0734cdf2ff8b6c2ea62a57cd0721f240d50ee164dbfb3f0955b561d0873e2c0f`.
- ARM64 isolated build passed on node89; existing working installs were kept.
  The same new install was deployed to node97, with module equality checked.
- Hosts: node89 / 9.88.43.89 and node97 / 9.88.43.97, authorized root account.

## Correction and CPU evidence

The product changes are confined to `rm_urma_ep.c` and `rm_urma_iface.c`:
ignore late FC for failed/closing endpoints or ifaces, retain grant requests
only for temporary resource exhaustion, return control requests on other
outcomes, and report terminal grant errors after arbiter dispatch returns.
This permits error callbacks to purge requests or destroy endpoints without
invalidating an active arbiter dispatch. Allocation failure uses the existing
iface error path. RC and public URMA/UCP code are unchanged.

The existing six R1 mock regression groups and eleven added FC error/lifetime
scenarios passed with ASan/UBSan. LeakSanitizer was disabled in that local
runtime. These tests execute actual RM source with mocked URMA calls; they
do not demonstrate real provider/hardware error recovery.

## Normal two-node regression

Command: `python3 CASE/source/contrib/rm_ctp/r1/accept.py CASE --repeats 3`,
with the authorized Open MPI root environment flags. The saved baseline
launcher configuration is reused with candidate install paths substituted.

- Results: `CASE/accept_20261022_110511/results.json`.
- Peer module equality and loader diagnostic passed; validation enabled, exit 0.
- Every 8B sample below passed validation and exited 0.

| Mode | Repetition 0 (us) | Repetition 1 (us) | Repetition 2 (us) | Median (us) |
| --- | --- | --- | --- | --- |
| RC | 2.24 | 2.36 | 2.28 | 2.28 |
| RM | 2.05 | 2.04 | 1.97 | 2.04 |

The alternating order was RC/RM, RM/RC, RC/RM. This is a small correctness
regression comparison, not evidence of a stable performance advantage.

All six 1/2/4 remote-rank data/resource cases passed at TX_DEPTH=8. The runner
checks each rank's candidate core libraries/module maps and zero target-mode
Jettys live at exit before printing PASS.

| Remote ranks | RM Jetty peaks by observed context | RC Jetty peaks by observed context |
| --- | --- | --- |
| 1 | [1] | [2] |
| 2 | [1] | [3] |
| 4 | [1] | [5] |

These counts describe this workload and the observer's context grouping.
They are not a universal RC formula or a claim of constant total resources.

## Explicit FC small-window regression

The standalone helper `r2_fc_window_20261022.py` is preserved separately from
the product tree. Its executed copy, generated C test source, command/env
records, configuration logs, app maps and observer records remain in FC_DIR.

- Helper SHA256: `e52e4f344f53e04f87647e2532e7d8de839eade71f6330d22c06133023eaf187`.
- Embedded C SHA256: `502048a73533151fccba7cde6b77c24a84ae26e7e1c6ec7b6f9a6291d44d3b59`.
- FC_DIR: `CASE/r2_fc_window8_mgob5kna`.
- Results: `FC_DIR/results.json`.
- Command: verify helper SHA256, then `python3` the uploaded helper on node89.
- Both nodes' candidate configuration checks passed for RM and RC:
  FC_ENABLE=y, FC_WND_SIZE=8, TX_DEPTH=8, RX_DEPTH=64,
  FC_SOFT_THRESH=0.5 and FC_HARD_THRESH=0.25.
- RNDV_THRESH and ZCOPY_THRESH were set to `inf` for the eager workload.
  Transport-specific settings were explicitly forwarded, and each application
  rank checked/saved its FC enable/window/TX/RX environment.
- Rank0 on node89 sent 16 messages per batch to each peer on node97. A peer
  validated every byte of all 16 before sending its application batch reply.
  Window 8 with no intermediate application reply exercises independent
  credit-grant progress, rather than relying on per-message echo piggyback.
- Each case used 2000 rounds; payload was 64B for 1/2 peers and 1024B for 4.
  The application batch reply is test traffic, not a new product ACK protocol.

| Mode | Remote ranks | Window | TX depth | Data/run | Candidate maps | Target Jetty live at exit |
| --- | --- | --- | --- | --- | --- | --- |
| RM | 1 | 8 | 8 | PASS | PASS | 0 |
| RC | 1 | 8 | 8 | PASS | PASS | 0 |
| RM | 2 | 8 | 8 | PASS | PASS | 0 |
| RC | 2 | 8 | 8 | PASS | PASS | 0 |
| RM | 4 | 8 | 8 | PASS | PASS | 0 |
| RC | 4 | 8 | 8 | PASS | PASS | 0 |

The helper checks exit 0, its application success marker, effective depth,
rank placement, candidate maps and resource retirement before each PASS.
Zero live target Jettys does not establish absence of every type of leak.
No FC-specific latency measurement or hardware fault injection was performed.

## Remaining scope

This establishes normal AM/FC progress for the tested build and workload.
Terminal FC errors and reentrant cleanup retain CPU-only evidence. Full CTP
completion, ordering, loss/retry and receive-exhaustion contracts still require
authoritative confirmation; this run does not settle them. RMA, large-message
coverage, bandwidth and ppn16 collectives remain outside this acceptance.
No formal R2 AtomGit delivery or company-main merge is recorded here.
