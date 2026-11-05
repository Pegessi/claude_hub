# Task liveness consolidation

Task `ce373ec6-dec6-44ca-9a67-aa4d13a74efb`; base
`aa359fd17ccd804cbf296db3644552481e140667`; branch
`codex/task-liveness-consolidation`. Goal Packet approved in review cycle 1;
implementation initially ran in cycle 2; the safety revision is in cycle 3. No push or main-service restart is authorized.

## Change boundary

One writer owns implementation. Two native read-only audits mapped legacy policy,
provider facts and regression coverage. An additional safety audit identified
condition-age reuse, repeated migration, skipped claims, attention resolution and
replacement races; dedicated regression cases cover those findings.

The convergence map in `docs/design/agent-liveness-guarantees.md` is the ownership
contract. Removed policy files: `monitoring/recovery.py`, `tmux/review_delivery.py`,
`scheduling/hub_tasks.py`. Lifecycle observation/notice code moved from
`monitoring/lifecycle.py` into the central `monitoring/liveness.py`; the former
`tmux/review_reaper.py` is reduced to `tmux/review_resources.py` (resource selection,
orphan tab cleanup and historical report parsing only). Refresh no longer decides
prompt retry, auto-review or SUBAGENT failure. Dispatch/scheduler no longer invoke
review/orphan recovery sweeps. Dot no longer compares inactivity timestamps.
Legacy per-session counters have no writers and are read only for cold migration.

Kept: normal Task dispatch/verdict transitions, TaskMailbox/Chat receipt delivery,
provider transport watchdogs, scheduled Chat delivery, pure lifecycle projection,
Dot result cards and explicit report-question tracking. These do not decide Task
stall recovery. The single owner receives configured watch/review timeout inputs.

## Validation and handoff

The feature incorporates main `a34cfea62b6751876df63e6f7c15e9da46f5c255`
by rebase; both changelog entries were preserved. No merge commit was created,
so the fixed-merge decision-record target does not apply. The code candidate is
`0a36e2dcad23f77a70765feb8c76fcb24daec94c`; the subsequent evidence commit
changes only this log and is checked with the documentation/staged gates.

`./scripts/verify.sh all` passed with exit **0** in the feature worktree.
The foreground command and its explicit exit receipt are retained at
`/tmp/liveness-all-round7.log`. Backend test evidence is
`/tmp/claude-hub-verify.kwt26lqq/` (pytest log, provenance and exit code).

| Check | Result |
| --- | --- |
| Documentation, checkout safety, Black/isort, backend types | Passed; 614 type-checked source files |
| Full backend tests | 6,060 passed, 9 skipped, 16 warnings; 344.50 seconds |
| Frontend lint, types, tests and production build | Passed; 1,224 tests passed |
| Explicit staged safety and whitespace checks | Passed before each code commit |
| Focused controls, cold restart, managed Chat and workspace regression groups | 230 passed after the control-lock fix |
| Structural ownership and liveness group | 55 passed after the module-level import correction |

Earlier runs exposed migrated-fixture assumptions, a delayed import prohibited by
the repository, and an obsolete concurrency-test ordering; these were corrected.
One earlier wrapper ended after a passing backend suite without its final receipt;
it was not counted as a full pass. The successful run above includes cleanup and
all standard frontend targets. The existing Vite chunk-size advisory remains.
Browser mocks/HMR/replay and manual UI checks were not run. Frontend source is
unchanged, but lifecycle attention and idle status are observable changes. Backend
state assertions do not establish browser acceptance; that remains unverified. All verification used isolated runtime homes/sockets;
no production Hub restart or live-incident experiment occurred.

## Independent audit ledger

Root was the sole writer. Both native delegates used `model_or_api=runtime-default`
and read-only scopes. Their findings were integrated; they did not claim Hub
approval, complete the Task, or mutate live sessions.

| Role / primitive / agent | Decision and evidence |
| --- | --- |
| lifecycle evidence / P-RESEARCH / `/root/lifecycle_evidence` | Accepted legacy call-path and regression inventory against original base `aa359fd`. |
| provider evidence / P-RESEARCH / `/root/provider_evidence` | Accepted provider facts and delivery boundary audit. |
| provider safety / P-JUDGE / `/root/provider_evidence` | Accepted fixes for pending-message ownership, question/startup races and watch re-arming; scoped provider admission preserves existing resume guards and one-shot inbox markers. Actual Claude/Codex preparation regressions prevent provider submission after revocation. |
| lifecycle safety / P-JUDGE / `/root/lifecycle_evidence` | Accepted fixes for condition age, one-time migration, stale claims, replacement races and explicit SUBAGENT deadlines. Final audit found no duplicate Task recovery owner; its Enter/reset contention finding was fixed and independently rechecked. |

The principal deleted policy files account for 1,784 removed lines:
`monitoring/recovery.py` (528), `tmux/review_delivery.py` (146),
`tmux/review_reaper.py` (394), `monitoring/lifecycle.py` (442) and
`scheduling/hub_tasks.py` (274). Retained review resource mechanics moved into
`review_resources.py`; notice code moved into the sole liveness owner. Refresh
lost 410 lines of mixed policy, and Dot work tracking lost 67 lines, including
its independent stall comparison. Deprecated schema fields are read-only upgrade
evidence; no old timing constants or live recovery-counter writers remain.

Mandatory Hub review of the fixed candidate is requested after this evidence
commit, explicitly including duplicate-owner detection. Local fast-forward and
human acceptance remain pending that gate at this handoff; their results belong
in the subsequent managed Task report. Origin push is forbidden.

## Limits

No durable background-job runner or Stop hook. Notice acceptance is Dot inbox
acceptance, not human receipt. Unknown quota/permission causes stay explicit
uncertainty. Direct external terminal keystrokes retain the phase-0 sampled-observation limit.
No lessons needed.


## Review cycle 3: two blocking safety races

Hub review `02b410cc-e5b0-457e-9fe5-83e0de2e65a5` rejected candidate `96912a1`.
The revision preserves the passing consolidation and fixes only the two blocking
contracts, including sibling failure/cleanup paths with the same cause:

- Reuse the evaluator's review-obligation predicate at final native submission.
  A same-cycle review or plan gate revokes a worker wake after native preparation;
  the real pending-mailbox/explicit-review route is covered in addition to actual
  Claude/Codex preparation tests. Proven-unsent input is retired without an ACK.
- Revalidate provisioning outcomes under the workspace lock before FAILED/rebind.
  Resumed, replaced, paused or superseded owners retain their work. Dispatch
  failures with possible submission retain their receipts and unfinished claim.
  Session deletion calls the owner's fence after both workspace and pump locks;
  the blanket FAILED scheduled-task cleanup retry was deleted.

The focused six-file regression group passed **272 tests** in 3.57 seconds,
including 30 new cases. Evidence: `/tmp/liveness-focused-round8.log` and
`/tmp/claude-hub-verify.nhfle6to/`. Black/isort passed. Initial type checking caught
an omitted scheduling host signature for the optional deletion callback; the
contract was corrected before full validation. Full verification and independent
Hub re-review are recorded with the next fixed-candidate handoff.

Native read-only `revision_failure_audit` / P-RESEARCH /
`/root/lifecycle_evidence` (`model_or_api=runtime-default`) identified the sibling
dispatch exception and repeated FAILED cleanup paths; accepted and included in
the same fix. It has no Task reporting or completion authority. Nonblocking dead
helper cleanup was left outside this revision, as requested. No lessons needed.


At fixed code commit `77f1bfc`, the native read-only safety re-audit found no
remaining blocker or duplicate owner and independently replayed **27 tests**
(`/tmp/claude-hub-verify.38mlge53/`). The first full revision run stopped its backend
suite on an old dependency-test mock assertion that required cleanup without the
new callback (4,816 passed, 3 skipped before failure); frontend targets passed.
`/tmp/liveness-all-round8.log` records exit 1, not a full pass. The assertion now
checks the cleanup target and its authority callback; the 251-test dependency,
workspace and liveness group passed (`/tmp/claude-hub-verify.hda7suzy/`). Runtime
code did not change after `77f1bfc`. A fresh full run follows this fixture update.


### Fixed-candidate verification (2026-11-06)

The fresh foreground `./scripts/verify.sh all` **passed, exit 0**, at
`c145623dc1bc6f71bf1be06672b78aaaa3f5af82`. All docs, checkout safety,
Black/isort, backend types, backend tests, frontend lint/types/tests/build passed.
Backend: **6,090 passed, 9 skipped, 16 warnings**, 342.70 seconds. Frontend:
**1,224 passed, 0 failed**. The existing Vite chunk-size advisory remains.

Evidence: `/tmp/liveness-all-round9.log`, including `LIVENESS_VERIFY_EXIT=0`;
`/tmp/claude-hub-verify.egkr1wwr/{pytest.log,provenance.json,exit-code}` confirms the
feature-worktree interpreter and successful isolated cleanup. This subsequent
commit records evidence only; docs, staged safety and whitespace are rechecked.
No runtime source changed after the independently audited `77f1bfc`.

The cycle-3 native P-JUDGE audit was accepted: 27 adversarial cases passed with no
remaining scoped blocker or second recovery policy. It supplements the required
Hub reviewer and does not replace its verdict. The fixed candidate is submitted
for mandatory Hub review with explicit attention to both reproduced blockers and
the absence of duplicate mechanisms. Local main remains `a34cfea`; fast-forward
is pending independent approval. Human acceptance remains required; no push.
Browser/HMR/replay and live-provider checks remain unrun and are not claimed.


## Review cycle 4: cancelled provisioning cleanup

Hub report `718419e6-b66d-4a19-9e62-2768518c4399` confirms both P1 fixes and
single-owner consolidation pass, but rejects `8fa1642` for leaking the unused
replacement when `ensure_workspace_agent` returns its initial SPAWNING object.
The prior fixture returned an IDLE snapshot and missed the production contract.

The correction records the unused replacement's immutable incarnation in existing
TaskLiveness state before claim cancellation. The existing per-Task pass resolves
the current resource, retires it only when unused/idle with no unresolved receipts,
and retains the resource obligation while bootstrap is active. Restart and new
review/execution generations preserve that obligation. Adoption/recycling removes
old cleanup authority; the final callback repeats canonical-reference checks after
pump contention. Existing P1 fences remain intact; no second recovery mechanism.

Real `ensure_workspace_agent` tests now cover terminal/Chat SPAWNING returns with
idle, active, still-spawning and pending/processing/uncertain live states, restart,
generation changes, adoption and final-lock races. An independent read-only audit
`unused_replacement_audit` / P-JUDGE / owner=root /
agent=`/root/lifecycle_evidence`, `model_or_api=runtime-default`, was accepted:
**17 tests passed**, no remaining scoped blocker or duplicate policy;
evidence `/tmp/claude-hub-verify.dm1el7xb/`. Owner's initial four-file regression
run passed **261 tests** (`/tmp/claude-hub-verify.mldrljo1/`); final focused/full
validation follows the added final-adoption guard. No lessons needed.


Final focused group including dependency, scheduled, managed Chat and structural
checks: **267 passed**, 4.26 seconds (`/tmp/claude-hub-verify.a77ohy4v/`). Complete
backend types passed, 614 files (`/tmp/claude-hub-verify.wgu7hrr8/`). The full run
below validates the final recorded-claim check and simplified state updates too.
