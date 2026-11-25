# Backend verification sharding

The default backend suite dominates `verify.sh all`. Its tests now run in
independent runtime directories, with one coverage database per shard and a
separate covered collection pass. The runner compares each shard's collection
with the original manifest and checks final outcome counts before merging data.
Focused pytest arguments keep the existing single-process behavior. The default
Linux concurrency leaves CPU capacity for other checks and can be overridden
with `CLAUDE_HUB_VERIFY_SHARDS`; unsupported hosts retain serial execution.
Proxy bypass settings preserve caller exclusions and include loopback addresses.
An independent pre-commit review caught premature forced termination on interrupt;
the runner now sends Ctrl-C and allows bounded fixture cleanup before escalation.
A regression waits for a delayed fixture finalizer before checking process exit.

## Measurements

Baseline: clean commit `73f747c9`, Python 3.11.16, 19 available CPUs, 4 target
jobs. The clean candidate `6d96ec4e` was measured with the same command and
pool size. Both runs overlap standard targets; the type-check cache was cold in
the baseline and warm in the candidate. The task report retains the paths to
raw logs and coverage XML.

| Target | Baseline (s) | Candidate (s) |
| --- | ---: | ---: |
| docs | 2.497 | 2.480 |
| checkout-safety | 0.092 | 0.095 |
| backend-format | 5.448 | 5.390 |
| backend-types | 23.913 | 1.699 |
| backend-tests | 630.397 | 172.608 |
| frontend-lint | 8.318 | 8.321 |
| frontend-types | 7.660 | 7.495 |
| frontend-tests | 27.596 | 28.009 |
| frontend-build | 12.275 | 12.031 |
| **all wall clock** | **630.519** | **172.624** |

Baseline passed 7,755 tests with 9 skips; candidate passed 7,779 with 9
skips. Both XML reports contain the same 68,435 executable line identities.
The candidate retains every baseline hit and adds 35 hit lines. Six independent
behavior tests removed ten misses found by review: the old coverage depended on
process state left by other files, which isolated shards do not share. These
tests explicitly exercise the relevant cleanup, account resolution, model
selection, cancellation, and HTTP resource ownership contracts.

The baseline pytest process reported 623.39 s, about 7.01 s less than its
630.397 s target wall clock (setup, reporting and cleanup included). A prior
proxy failure shows a loopback
403 after 356.61 seconds and 5,491 passes. A real loopback test run with an
unusable proxy passed 18 tests.

A separate Python 3.12.3 probe ran 18 loopback tests with the C tracer and
`sysmon`: 20.289 s and 17.725 s respectively, with the same 68,435 executable
lines and identical hit sets. This focused sample does not establish full-suite
equivalence; the project default remains Python 3.11 and the runner does not
change the coverage core.

A second serial run passed 7,755 tests with 9 skips in 599.52 s; the full
Top 50 is retained in the task evidence. Its slowest cases include the
AI metrics gate (8.78 s), cold restart (7.20 s), restart continuation (6.25 s),
and CLI timeout (6.10 s). Several 4–5 s cases explicitly exercise shutdown
deadlines and should not be shortened merely to improve the benchmark. The
baseline standalone backend target took 595.182 s (7,755 passed, 9 skipped).
The clean candidate took 170.423 s (7,779 passed, 9 skipped) and retained all
baseline coverage hits.

A detached-session probe demonstrated that the initial shard helper could leave a
child alive after its worker exited; the probe child was explicitly terminated.
The helper now adopts and reaps its detached descendants. Real subprocess
regressions passed for successful and failing shards with a child that ignores
`SIGTERM`, and a signal sent through the complete `verify.sh` entrypoint
returned the expected interrupted status, removed its runtime, and reported no
owned children.

A subsequent clean candidate failed `test_frontend_build_timeout_keeps_current_backend_and_dist`
after 64.7 s because an adopted zombie kept a process group visible while its
shard was still running. The supervisor now reaps adopted zombies during
execution without releasing worker PID anchors. A real-process regression and
an isolated sharded rerun of that original test passed; the clean-candidate full run above passed after the repair. Context-traced
serial evidence identified the behavior tests needed to preserve baseline hits.
