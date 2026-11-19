# MASO native sub-agent wake-up streaming

Task: `b5cd8ece-2408-4fb6-94da-4790bd7707a9`<br>
Base: `fd5eaed`<br>
Branch: `fix/maso-wakeup-b5cd8ece`<br>
Worktree: `/home/tiger/claude_hub_worktree/maso-wakeup-b5cd8ece`

## Outcome and ownership

MASO's root `agent_end` is a per-run IDLE transition, not the end of a
Task/Fork/SendMessage workflow. The HTTP session now owns one persistent SSE
subscription, tracks agent starts/ends, and confirms display quiescence against
whole-tree status. The adapter emits terminal completion only for the transport's
`hub_tree_settled` record. Root wake-ups before quiescence remain in the same Hub
turn; later root or child starts are adopted by the existing native consumer into a new
attributed turn. No alternate store, manager or history-replay pipeline was added.

The native consumer retains a subscribed MASO session even without viewers.
Verified status while waiting for descendants refreshes its existing watchdog
clock. Other providers keep their existing idle-retirement policy. Explicit Stop
still fences queued events before recursive abort and the exit/resume finalizer
barrier; normal completion, resumed active status and failed HTTP delivery no
longer imply permission to abort the remote tree.

## Protocol evidence and limits

Read-only inspection of the **loaded MASO 0.9.0 deployment**, independently
researched and spot-checked against its installed wheels:

- `maso_cowork/server/app.py:sse_stream`: the stream lives until session plugin
  closure, with 15-second keepalive comments; agent end does not close it.
- `server/plugin.py:agent_end`, `_indicator_payload`, `_running_agent_ids`:
  agent_end/agent_idle precede registry cleanup; `indicator.running` covers
  RUNNING/WAITING/PAUSED, with foreground condensation in a separate map.
- `server/session/manager.py:get_status`, `_build_agent_tree`: root status plus
  nested descendants; aggregate `is_running` alone omits PAUSED.
- `maso_core/tools/orchestration/send_message.py` and `runtime.py`: SendMessage
  enqueues RPC and wakes the target asynchronously. A scheduler gap can show
  all agents IDLE before the next root task starts. The internal agent_settled
  hook is not an HTTP tree-and-queue acknowledgment.
- History's pending user-message list **does not include native RPC messages**;
  it cannot establish a safe SendMessage completion fence.

Therefore two unchanged idle status observations (250 ms polling while a root
end is pending) are a **display quiescence heuristic**, not proof that the RPC
queue is empty. Correctness across a late wake comes from retaining the session
subscription and adopting root starts, not from pretending the heuristic is an
atomic provider acknowledgment. Children, nested activity, PAUSED, ABORTING,
root persistence finalizers and foreground condensation prevent settlement.

An SSE/status-monitor failure cannot claim lossless replay using MASO's bounded,
resettable event IDs. It reports failure and quarantines known delivery without
aborting remote work or retrying the prompt. The user must inspect/stop the MASO
session and create a new Chat. Existing explicit Stop cleanup failures still
retain their durable abort requirement. Output produced while Hub is disconnected
is not losslessly backfilled after restart. No shared-service restart or merge
is part of this change.

## Validation evidence

All commands below run from the feature worktree with fresh locked dependencies
(`uv sync --frozen --extra dev --python 3.11.16`, `pnpm install --frozen-lockfile`).

1. Baseline regression demonstrated the defect before implementation:
   `./scripts/verify.sh backend-tests -- tests/test_maso_http.py -k native_children_keep -q`
   failed because root completion arrived while two children remained active.
   Evidence: `/tmp/claude-hub-verify.05gkloub`.
2. Focused transport/adapter/tailer/store/live-subscriber suite:
   `./scripts/verify.sh backend-tests -- tests/test_maso_http.py -q` — **62 passed**.
   Evidence: `/tmp/claude-hub-verify.setzh40j`. Tests include parallel child reports,
   multiple root wakes, unique message IDs, single completion, late native starts,
   headless persistence, nested waiting/running, paused/condensing/finalizer status,
   explicit Stop during child wait, monitor/stream failure and non-destructive resume.
3. Real MASO HTTP/SSE probe, with private Hub HOME/state/tmux identity and a newly
   allocated MASO session `b9e2ff24-f72`: native Task launched one child, root ended
   in IDLE, child used SendMessage, and root woke with `ROOT_WAKE_B5CD8ECE`.
   The observed lifecycle was root_start → child_start → root_end → root_start →
   child_end → root_end → hub_tree_settled. Live subscription and durable store each
   contained **16 events, one turn_started and one turn_completed**, including the
   root wake-up answer. The probe session was explicitly exited afterwards.
   Task-local raw evidence is retained under ignored
   `tasks/maso-wakeup-b5cd8ece/live-wakeup-{result.json,output.log}`; its private Hub
   runtime was `/tmp/maso-wakeup-b5cd8ece-87g4jz66`.
   This exercises the actual provider, parser, adapter, manager consumer, store
   and live subscriber, not browser rendering or the production Hub process.
4. Initial `./scripts/verify.sh all` passed formatting, full backend types and
   frontend targets, but failed backend tests because test transport stand-ins
   lacked the new `keep_alive_when_idle` contract. The stand-ins were updated
   instead of adding a production fallback for incomplete mocks. Evidence:
   `/tmp/maso-wakeup-b5cd8ece-verify-all.log`.

Full final validation and review results are recorded below before handoff.

## Review and acceptance boundaries

Independent review covers the full candidate diff and call paths, including
transport contract fixtures outside the initial three suggested production
files. Applied lesson:
`a-closed-file-list-can-omit-behavior-required-by-acceptance-criteria-compare-the`.
The original `runtime/manager.py` factory already shares the same SessionTailer;
its native-consumer mixin is the owning location for idle-retention behavior.
No frontend code changed. Browser layout/rendering checks are not claimed; live
provider-to-store/fanout delivery and deterministic lifecycle tests are the gates
for this backend lifecycle change. Human acceptance and deployment remain pending.

### Independent review corrections

Independent review found two submission/adoption races and late child-only wake
ownership missing. All three findings were independently reproduced in four new
red regressions (`/tmp/claude-hub-verify.uakektu2`), then fixed:

- Native turn adoption and its publication now share the existing send/Stop lock,
  with generation and pending-teardown checks after acquiring it.
- Each submission advances a serial that invalidates queued settlement records;
  the monitor cannot settle while upload/POST is pending or before the exact
  broker `message_injected.injected[].id` echo consumes that UUID. Terminal
  consumption revalidates the notification after acquiring the send lock.
- A late root **or child** start opens an attributed tree-activity turn before
  interior records. Child-only work owns the provider guard and Stop, and can
  settle without another root run.

The wider focused check now passes **533 tests**:
`./scripts/verify.sh backend-tests -- tests/test_maso_http.py tests/test_maso_error_detail.py tests/test_agent_stream.py tests/test_agent_stream_native.py -q`
Evidence: `/tmp/claude-hub-verify.cq2u8f0b`. This includes **66 MASO cases**.
An earlier whole-suite pass (`/tmp/maso-wakeup-b5cd8ece-verify-all-2.log`) predates
these review fixes and is not substituted for final-candidate validation.

A second review found MASO's strict `before_input` failure can emit the retained
input echo **before** agent_start (then agent_error and agent_end). Reproduced red
at `/tmp/claude-hub-verify.64wexbh_`; exact post-baseline echo processing now precedes
the started-run gate. A new regression proves failed completion without abort.
The whole suite also caught an already-owned Codex start unnecessarily waiting
behind its own submission RPC: acknowledgment-only starts now return without
locking, while actual adoption remains serialized. The SessionState abstract
signature matches the new terminal revalidation contract. Focused MASO + native
Goal transport: **140 passed**, `/tmp/claude-hub-verify.rkcyn6d2`; complete backend
types: **658 files clean**, `/tmp/claude-hub-verify.owtxn5ew`.

A second real MASO probe on the submission-fenced implementation also passed:
owned session `281c79dd-51e`, **15 matching live/store events**, one start and one
completion, three tool starts and three tool completions. This time the root's
second end arrived **before** the child's end; completion still waited for the
child. Evidence: `tasks/maso-wakeup-b5cd8ece/live-wakeup-final-{result.json,output.log}`.
The session was explicitly exited. The final small error-path/Codex-ack fixes
are covered by their targeted regressions; the final whole-suite result follows.

### Final validation and review

- `./scripts/verify.sh all` — **PASS** on the final code/test snapshot. Backend:
  **6904 passed, 10 skipped, 17 warnings**; frontend: **1539 passed, 0 skipped**.
  Docs, checkout safety, Black/isort, all 658-file backend types, frontend lint,
  frontend types and Vite build all passed. Log:
  `/tmp/maso-wakeup-b5cd8ece-verify-final-2.log`; backend evidence:
  `/tmp/claude-hub-verify.yp8s9w47`; type evidence:
  `/tmp/claude-hub-verify.mzmif0d6`. The standard suite's skips and existing warnings
  remain visible; no browser/performance/deployment pass is implied.
- Final independent read-only review: **no remaining actionable blockers**.
  Reviewer independently ran **607 passing focused tests** (MASO, error detail,
  agent stream, native adapters and native Goal transport), evidence
  `/tmp/claude-hub-verify.ydlfmxsm`, and `git diff --check` passed. Reviewed
  production-file SHA-256 hashes were independently matched before delivery.
- `./scripts/verify.sh docs`, `git diff --check` and staged safety check run again
  after recording these final results. Only this working-log text changed after
  the whole-suite run. Branch delivery only; no main merge/push or deployment.
