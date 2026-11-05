# Delegated agent liveness guarantees

Design and implementation record, 2026-11-05. No operational recovery of incident sessions performed.
Task: `68944524-d953-4ed2-9d25-a03296eeab5a`.
Evidence baseline: `aa359fd17ccd804cbf296db3644552481e140667` on
`codex/agent-liveness-design`, isolated worktree
`/home/tiger/claude_hub_worktree/agent-liveness-design`.
Historical evidence references in sections 1–8 are relative to that immutable checkout.
The convergence map and implementation notes describe the consolidation branch.

## Decision

### Convergence map

Implementation task `ce373ec6-dec6-44ca-9a67-aa4d13a74efb` replaces the separate
task recovery policies below. The sole task-level owner is
`backend/claude_hub/services/workspace_manager/monitoring/liveness.py`: it evaluates
current evidence, selects recovery, owns the maximum-two-nudge budget, and decides
when an actionable notice is due. Callers supply facts or execute an already
selected operation; they do not retain a second stall timer or recovery policy.

| Existing mechanism / files under `backend/claude_hub/` | Disposition | Remaining responsibility |
| --- | --- | --- |
| Terminal auto-continue, hard reset and counters: `services/workspace_manager/monitoring/recovery.py`, `_constants.py`, session fields | Merge decisions into liveness; delete the independent loop and per-session retry budget | Terminal observations are signal inputs. Session transport performs only a selected delivery/control operation. |
| Review reminder/escalation: `services/workspace_manager/tmux/review_delivery.py` | Merge into liveness; delete the separate reminder policy and budget | Review dispatch/report intake establishes current review ownership and verdict evidence. |
| Review reaper / failed-dispatch classification: `services/workspace_manager/tmux/review_reaper.py`, `tmux/inventory.py` | Move missing-owner/missing-verdict recovery decisions into liveness; remove competing dispatch-pass recovery | Ordinary review dispatch executes a requested review; inventory supplies binding and delivery evidence. |
| Owner-loss reconciliation: `services/workspace_manager/monitoring/lifecycle.py` | Merge grace, failure and escalation decisions into liveness; delete the separate reconciler | Lifecycle projection remains derived display data; delivery adapter transports selected notices. |
| Scheduled Hub Task orphan recovery: `services/workspace_manager/scheduling/hub_tasks.py` | Merge orphan classification, retry/termination choices and limits into liveness; delete its separate sweep | Scheduler launches due work and reports actual run/session state; task-owned worker creation/cleanup remains an effect. |
| SUBAGENT death/timeout: `services/workspace_manager/monitoring/refresh.py` | Merge into liveness; remove independent failure decisions from status refresh | Refresh collects runtime evidence without deciding task recovery. |
| Dot stalled-task alarm: `services/dot/runtime/work.py`, `runtime/common.py` | Delete Dot's inactivity comparison/alarm policy; consume central verdict/notice | Dot keeps work tracking, result presentation and configured recipient delivery. Custom watch thresholds become inputs to the owner. |
| Notice eligibility, cooldown and retry: `services/workspace_manager/monitoring/lifecycle.py`, `api/task_notifications.py` | Consolidate task attention decisions with liveness; retain one delivery path | API/Dot adapter returns delivery outcomes; no new independent task-stall inference. |
| Native provider watchdogs: `services/agent_stream/runtime/watchdog.py`, `native_consumer.py` | Keep as transport/resource safety and signal producers | Terminalize a genuinely failed provider turn; never independently nudge, reassign, or decide a Task's disposition. |
| TaskMailbox, managed Chat inbox, tmux receipts | Keep as delivery mechanisms | Durable submission, deduplication and uncertainty reconciliation; no task-stall or review-gate policy. |
| Scheduled Chat delivery / normal dispatch | Keep as execution and transport lifecycle | Starts explicitly due work and reconciles its delivery; Task recovery decisions belong to liveness. |

The implementation must replace these policy paths, not call them from a wrapper.
The final working log records removed symbols and diff statistics. Pending review,
explicit pause, permissions, and ambiguous delivery are typed evidence; a generic
`needs_input` label is not permission to auto-continue. The three incident tests
include an idle worker after its launching turn ends. Durable command-exit registration
is deferred, with future wake evidence entering this same evaluator rather than a
new recovery owner.

### Consolidation implementation

`monitoring/liveness.py` is the single evaluator, persisted action-claim owner,
recovery dispatcher and notice owner. The existing monitor invokes it once per
Task with an independently bounded step. Status refresh only observes sessions;
normal dispatch, report intake, review verdict handling and transport receipt
reconciliation retain their separate execution responsibilities.

The persisted `TaskLiveness` record in `models/schema_domains/lifecycle.py` holds
one budget for `execution_epoch:review_cycle`. Two recovery actions are allowed;
replacing a session or receiving terminal output does not reset it. A Claude
context reset can be the second action after an identified API interruption; it
has no extra budget. Explicit new execution/review cycles create a fresh budget.
Legacy session/review fields remain readable solely for one-time cold migration;
no live counter writers remain. An old review envelope is imported only when its
exact dispatch identity matches. Non-idempotent claims without a completion
receipt route for inspection after restart; durable messages replay only their
stored ID and payload within a bounded window.

Worker idle grace is 60 seconds, or 20 seconds for recognized interruption or
completion output. Review idle grace uses `review_verdict_idle_timeout_seconds`
(default 180 seconds). Positive unsent terminal input gets one Enter retry and
10 seconds to clear; blocked/unknown runtime evidence settles for 180 seconds.
Missing review/scheduled owners get 60 seconds; ordinary worker loss gets five
minutes of observed loss, and initiator Chat loss retains seven days. A new
condition cannot inherit the previous condition's deadline. Concrete resumed
execution closes attention while retaining the budget and increments the notice
incident. The public SUBAGENT report-inactivity timeout remains an explicit Task
failure deadline; it does not interrupt/reset its provider turn.

Active turns, manual pause, pending human questions and ambiguous delivery never
receive generic recovery input. Exact unsent terminal input can disprove a sampled
WORKING label. Native cards use incarnation-checked runtime evidence for supported Claude,
Codex and Cursor Chat sessions. TraeX remains terminal-only under managed Chat
admission policy; its runtime classification uses the terminal signal path. Generic `needs_input` never implies a known quota or
permission cause. Delivery receipts never manufacture active-turn evidence.

Dot's `stall_hours` and watch-start configuration remain stored and supplied as
inputs. Queue/initiator watch deadlines are evaluated by the same owner; managed
idle execution receives the earlier bounded recovery ladder. Dot no longer runs
a stall comparison or rearm timer. Attention uses the existing Task notice adapter
and Dot inbox. `delivery_outcome` distinguishes sent, failed and unroutable;
`sent` means inbox acceptance, not proof that a person read it. Result cards and
explicit report questions remain Dot work tracking, distinct from stall policy.

A future durable job/timer supplies a `wake_deadline` fact to this evaluator.
This change does not register detached commands, install provider Stop hooks, or
claim exact background-job completion delivery. The third incident is recovered
from its observable idle WORKING executor.

Extend the existing Workspace monitor, task lifecycle projection, durable TaskMailbox,
and native turn events with a task-owned **liveness obligation**. Every accepted task
must have verifiable execution, a durable and bounded wake obligation, or a human
wait with delivery evidence. A recent progress report, a living CLI process, a
mailbox event with no consumer, or the text “waiting” is insufficient.

Ship reviewer-attention and idle-Chat recovery first. Add durable command completion
wakes next. Provider Stop hooks are supplementary enforcement; they cannot provide
the cross-provider guarantee. Dot remains the supervisor and explanation surface;
the recovery decision and minimum actionable notice must not depend on Dot having
an available model turn.

“Never silently stalled” means a bounded detection-and-recovery/notification
contract, not instantaneous observation or guaranteed task success. A host outage,
unreachable notification channels, unknown external side effects, and an unmanaged
background process prevent an unconditional guarantee. The system must expose those
conditions as failures of coverage, rather than silently counting them as healthy.

## 1. Evidence and root-cause taxonomy

The three incidents and their durations are supplied in the task brief. This study
does not inspect or change task `32cc3882`, its processes, or the incident sessions.
The code establishes failure mechanisms, not the exact historical trace. In
particular, permission versus quota for the Codex reviewer remains unverified.

### 1.1 Plan-review gate: delegated obligation without an assured verdict return

The reviewed-task Goal Packet gate is recognized at
`backend/claude_hub/services/workspace_manager/reports/identity.py:65`, stored as
pending review in `backend/claude_hub/services/workspace_manager/reports/intake.py:158`,
and routed at `backend/claude_hub/services/workspace_manager/reports/events.py:148`.
Worker auto-continuation intentionally stops while review is open
(`backend/claude_hub/services/workspace_manager/monitoring/recovery.py:109`).

There is already a missing-dispatch reaper, including the pending Goal Packet case
(`backend/claude_hub/services/workspace_manager/tmux/review_reaper.py:225`). Its
positive failure predicate treats a reviewer bound to this task as available even
when idle indefinitely; this avoids restarting a reviewer that is thinking
(`backend/claude_hub/services/workspace_manager/tmux/inventory.py:308`). A separate
input-box backstop excludes Chat (`backend/claude_hub/services/workspace_manager/tmux/inventory.py:364`).

The verdict reminder is narrower: both runtime and managed status must be `IDLE`
(`backend/claude_hub/services/workspace_manager/tmux/review_delivery.py:30`). After
180 seconds by default it sends one durable reminder; after another idle window it
emits `review:delivery_stalled`, leaving the round open (`backend/claude_hub/services/workspace_manager/tmux/review_delivery.py:61`,
`:123`; `backend/claude_hub/config.py:47`). That event wakes a TaskMailbox long-poll,
not inherently a new supervisor turn (`backend/claude_hub/services/task_mailbox.py:109`).

**Root cause class:** a review gate transfers execution responsibility but lacks a
universal, supervised deadline from dispatch through verdict through worker resume.
Existing recovery covers missing dispatch and a subset of idle reviewers. It does
not establish that every open review has a live producer and a delivered fallback.

### 1.2 Codex reviewer in `needs_input`: session attention hidden by task review state

While a review is open, lifecycle derivation returns `REVIEWING/ai_review_in_progress`
unless the reviewer is gone; it does not test the reviewer's attention status
(`backend/claude_hub/services/task_lifecycle.py:243`). An explicit
`REVIEW_NEEDS_INPUT` report is handled later at `:261`, but a blocked model may never
submit that report.

The terminal attention branch handles a `WORKING` task and excludes a review already
in flight (`backend/claude_hub/services/workspace_manager/monitoring/refresh.py:159`).
The reminder requires dual-IDLE, so a `NEEDS_INPUT` reviewer is excluded. The reaper
also considers that bound reviewer available. This explains a session visibly
needing input while the task remains in an apparently ongoing review.

Native Chat adds a second route to the same gap: an idle Chat owing a task handoff or
review verdict becomes managed `NEEDS_INPUT`, while its runtime remains `IDLE`
(`backend/claude_hub/services/workspace_manager/_chat.py:233`). This missing-handoff
condition is deliberately surfaced without replay, but fails the reminder predicate.
It must not be confused with an actual permission request, user pause, or quota wait.

**Root cause class:** coarse `needs_input` conflates missing handoff, explicit human
input, delivery uncertainty, pause, and provider failure; task-level review logic
does not propagate all of those conditions. The incident's specific provider cause
requires a turn/approval/error record that this study does not have.

### 1.3 Background suite finishes after worker turn: no registered completion-to-wake edge

`WorkspaceTask` stores assignment, review, delivery, progress, and lifecycle fields,
but no background-command completion obligation
(`backend/claude_hub/models/schema_domains/tasks.py:140`). TaskMailbox wakes only
registered long-poll consumers; it is not a general process-exit supervisor.

Chat status refresh exits into native observation before terminal auto-recovery
(`backend/claude_hub/services/workspace_manager/monitoring/refresh.py:93`). Native
Chat is explicitly excluded from terminal auto-continuation
(`backend/claude_hub/services/workspace_manager/monitoring/recovery.py:14`). After
the turn ends, `_chat.py:233` exposes missing handoff but does not arrange a future
turn. No durable task-owned command registration ties shell exit to continuation.

Terminal workers do have clean-idle nudges, so the supplied incident alone does not
prove its session surface or which guard suppressed recovery. The cross-surface
defect is still concrete: detached shell work is not a durable Hub wake source.
Even a provider-local background notification cannot prove delivery after its
provider process, stream consumer, or owning turn is gone.

**Root cause class:** work outlives the turn that launched it, while its completion
has no durable owner, event subscription, or bounded fallback. “Run in background
and end the turn” creates a responsibility gap.

### 1.4 Existing safety nets and their boundaries

| Existing mechanism | Coverage and gap |
| --- | --- |
| Workspace monitor | Five-second sleep between rounds, 60-second per-step timeout (`backend/claude_hub/services/workspace_manager/_constants.py:150`). Sequential work can delay a round; this is not a five-second detection SLA (`backend/claude_hub/services/workspace_manager/_workspaces.py:168`, `:291`, `:376`). |
| Terminal auto-continue | 60-second clean-idle grace; 20-second error/completion grace; up to ten soft attempts; Claude hard recovery after three soft failures, at most twice (`backend/claude_hub/services/workspace_manager/_constants.py:108`). Chat is excluded; no universal durable wake contract. |
| Review delivery | One persisted reminder, then supervisor mailbox escalation; dual-IDLE and open-round restrictions described above. Preserve its dedupe and slow-review protections. |
| Task timeout | `timeout_seconds` failure handling applies to `SUBAGENT` tasks, not all reviewed/autonomous work (`backend/claude_hub/services/workspace_manager/monitoring/refresh.py:233`). |
| Owner loss | Five-minute reconciliation for `agent_gone`/`working_without_agent` (`backend/claude_hub/services/workspace_manager/monitoring/lifecycle.py:58`). It does not solve a living but idle owner. |
| Scheduled orphan recovery | Only internal scheduled Hub Tasks, after 60 seconds with a dead owner, up to three dispatch attempts (`backend/claude_hub/services/workspace_manager/scheduling/hub_tasks.py:40`; `backend/claude_hub/services/workspace_manager/_constants.py:147`). Idle/attention owners are deliberately not dead. |
| Restart continuation | General Chat restart recovery excludes Workspace-attributed/managed Chats (`backend/claude_hub/services/restart_continuation.py:176`); managed work retains its own recovery owner. |
| Dot stalled alarm | Watcher checks every minute; default one hour; only `queued`/`working` (`backend/claude_hub/services/dot/runtime/common.py:38`). It uses latest task/progress/runtime/report time, emits once per inactivity marker, and does not mechanically recover (`backend/claude_hub/services/dot/runtime/work.py:561`, `:872`). Open `review` is outside this alarm. |

The brief's “only Dot's alarm” understates the existing mechanisms. The missing
piece is a common coverage contract and reliable transitions between those pieces.

### 1.5 A notification timestamp does not establish a delivered human wait

`TaskLifecycleMark.notified_at` explicitly includes “had no recipient”
(`backend/claude_hub/models/schema_domains/lifecycle.py:83`). Notice handling sets it
after unroutable delivery or exhausted retries as well as successful delivery
(`backend/claude_hub/services/workspace_manager/monitoring/lifecycle.py:398`).
Even `sent` only means Dot accepted an inbox entry; Dot decides whether to notify
the person (`backend/claude_hub/api/task_notifications.py:31`;
`backend/claude_hub/services/dot/runtime/work.py:103`). Child notices are suppressed
when the parent is merely `WORKING` (`backend/claude_hub/services/workspace_manager/monitoring/lifecycle.py:252`).

Consequently none of those facts alone satisfies “waiting-on-human with a delivered
notice.” Fixing only auto-nudges would leave this escape hatch.

## 2. Precise invariant and guarantee boundary

Let an obligation `o` identify `(task_id, execution_epoch, role, assignment_generation,
review_cycle, session_incarnation)`. `role` is worker or reviewer/evaluator; it is not
a new Task. A plan gate gives the worker a review wait and gives the reviewer its own
execution obligation. Evaluation, revision, queued dispatch, and final human
acceptance remain covered until resolved. Unstarted registration is excluded;
source attribution never grants Workspace execution control.

For every outstanding obligation, after a bounded transition grace:

```text
Covered(o) = Active(o) OR Wakeable(o) OR HumanWaiting(o)

Active(o): exact assigned turn is live, with fresh runtime ownership evidence;
           no blocking approval, explicit pause, or known terminal error.
Wakeable(o): durable registered producer + destination + deadline + fallback;
             the producer is live or its completion is durably queued for delivery.
HumanWaiting(o): explicit reason/action/recipient and a matching delivered notice receipt.

NOT Covered(o) -> persist violation + bounded recovery intent or notice-delivery intent.
```

An intent to notify is a supervised wake obligation while delivery is pending; it
does not prematurely count as `HumanWaiting`. Similarly, an emitted command-exit
event must remain supervised through wake acceptance and a new turn start.

Use the existing lifecycle as the canonical task projection. Add reasons such as
`missing_handoff`, `reviewer_permission`, `quota_cooldown`, `wake_delivery_uncertain`,
and `notice_delivery_failed`; do not create a parallel board-status machine.
Report coverage as a separate derived fact with evidence and deadline. An idle
session may not be labelled healthy solely because legacy status is `working`.

**Two promises:** (1) supported clean turn-end transitions persist a successor
obligation before relinquishing ownership; (2) crashes, missing callbacks, stale
leases, and legacy/uninstrumented sessions are reconciled within the configured
detection bound. Arbitrary external CLI activity cannot satisfy promise (1); expose
that provider/session as sampled coverage with promise (2).

This guarantees accountability, not useful model reasoning, successful tests, or
exactly-once external side effects. An infinite but active model loop still needs a
separate execution/time budget; repeated tokens are not evidence of semantic progress.

## 3. Minimal architecture: extend current owners

Add typed liveness fields alongside `WorkspaceTask`, not a second task database:

| Record | Required facts and owner |
| --- | --- |
| Execution observation | Exact turn/run identity; source; observed sequence/time; active, blocked, idle, dead, or unknown; confidence. Runtime adapters own observations; polling cached status never refreshes evidence age. |
| Wake obligation | Stable ID, task/epoch/role/generation, kind, producer reference, consumer, creation/deadline/probe times, state, completion evidence, immutable continuation intent ID. Workspace persists it with the Task. |
| Recovery episode | Violation fingerprint, first-detected time, next action time, claimed action, fixed payload hash, nudge/resume/reassignment counts, total deadline. Workspace owns limits across restarts. |
| Human wait / notice | Blocker kind, actionable question, authorized recipient/channel, reason generation, delivery state, transport receipt ID/time; optional reply deadline. Existing notice flow owns it. |

Keep wake kinds bounded: command, review, child/dependency, timer, dispatch/delivery,
and notification delivery. A new file for the typed schema and a focused monitor
evaluator are justified; a second scheduler, message bus, or generic job platform is
not required. Use existing persistence, locks and delivery routes.

Task state and TaskMailbox already serialize into the same workspace payload
(`backend/claude_hub/services/workspace_manager/_persistence.py:125`). Persist the
obligation, recovery decision and event together under the Workspace mutation lock.
Perform delivery after commit. Stream history and Chat inbox are separate stores:
use replayable event cursors and an outbox acknowledgement, never assume a
cross-file atomic transaction. Existing post-persist observers are callbacks, not a
durable outbox (`backend/claude_hub/services/agent_stream/runtime/publication.py:201`).

### Register, complete, consume

1. **Command:** propose a scoped `claude-hub task job run` interface (not available
   today). Persist reservation before launch. Start a task-owned supervisor with a
   launch token; reconcile `reserved -> launching -> running` using a start receipt.
   Capture PID plus start identity/process group, cwd, log location, deadline, exit
   status and bounded output reference. A PID without start identity is insufficient.
   Only explicitly registered commands use this runner; never adopt arbitrary PIDs.
2. Supervisor durably records completion before publishing it. Its completion file
   survives provider turn exit and is reconciled after Hub restart. A crash between
   reservation and launch is safely retryable only with proof launch did not occur;
   unknown launch becomes an actionable incident, never duplicate execution.
   The Hub starts the supervisor outside the provider turn's process group, so
   retiring a one-shot provider cannot kill the registered producer. Its narrowly
   scoped capability can report only that job's start/exit for the current assignment;
   it cannot complete the Task or answer permissions. Cancellation records an
   explicit job policy (finish, interrupt, or require operator decision), and late
   exits after task cancellation cannot resume cancelled work.
3. Completion records enqueue one continuation using the existing session mailbox
   and native Chat delivery adapter. Wake prompt asks the owner to inspect the saved
   exit code/log and report; it does not rerun the command.
4. Keep the wake pending until a matching new turn starts; keep completion evidence
   available until the owner reports consumption or terminal outcome. A missing ACK,
   failed delivery, or turn ending without consumption reopens supervised recovery.
5. **Review:** registration is automatic and atomic with review dispatch. Completion
   requires a current-cycle verdict. An approved plan then creates a worker-resume
   delivery obligation; approval itself is not proof the worker started.
6. **Child/dependency:** register the specific target and completion/error predicate,
   destination and deadline. Subscribe before releasing the parent turn, then inspect
   current state to close the completion-before-subscribe race. Dependencies need
   not all succeed: failure must wake the parent for a decision. Detect cycles and
   reject a closed wait chain without an external producer. Supervision alone is
   not an execution dependency.
7. **Timer/quota:** store due time and reason in the obligation, using the existing
   scheduling clock/dispatcher. Do not create user schedules for internal retries.
   Bound renewal count and absolute wait duration; repeated self-renewing timers
   cannot hide the same stall indefinitely.

A stop-guard must accept only an acknowledged registration, not “job is running”
text or a recent progress report. A live background command without a completion
consumer and deadline is still uncovered.

## 4. Detection, thresholds, and false-positive controls

These are proposed defaults, not claims about current behavior. Record deadlines
in UTC, use a monotonic clock for within-process elapsed time, and revalidate after
restart/clock jumps. Use event sequence plus incarnation to reject stale observations.

| Condition | Evidence and proposed bound | Response |
| --- | --- | --- |
| Native turn ends with outstanding work and no successor | Persisted matching `TURN_COMPLETED`; settle 30 s for an in-flight report; reconcile by 60 s after the event | One inspect-state continuation, unless pause/input/uncertainty blocks it. |
| Terminal appears idle with no wake | Two consistent samples spanning 60 s, hook/transcript if available, no active tool/approval or explicit pause | Non-destructive inspect-state nudge; ambiguous observations never authorize kill/reassign. |
| Review has no verdict | Preserve default 180 s idle window; include Chat missing-handoff despite managed `NEEDS_INPUT`; require no active reviewer turn | Remind reviewer, not worker; supervise verdict and later resume delivery. |
| Explicit approval or structured question | Current unresolved request identity, immediately classify; notice target within 30 s | Human wait after delivery receipt; do not auto-answer. |
| Quota/rate limit/auth failure | Provider error and optional retry/reset time; classify within one reconciliation pass | Bounded retry timer when justified; otherwise actionable notice. No model/account substitution. |
| Command finishes | Durable runner exit record; enqueue within 15 s, reconcile lost event within 60 s | Inspect-result wake; nonzero exit is also a wake. |
| Registered producer loses heartbeat | Probe about every 30 s; stale after 90 s; second independent probe within 30 s | Mark suspect/unknown; resume only after ownership/termination evidence, otherwise notify. |
| Long but quiet turn/tool | Live exact transport/process + tool identity + probe lease; lack of tokens alone is insufficient | Continue observing. Default 30-minute investigation deadline, overridden by explicit job/review budget; exceeding it requests evidence or human decision, not blind termination. |
| Wake queued/accepted without a new turn | No start receipt after 120 s; queue blocked by a live turn remains supervised against that turn's budget | Reconcile delivery before retry; acceptance alone is not execution. |
| Notice undelivered | Retry after 30 s, 2 min, 5 min; use configured alternative route; notify delivery incident by 10 min | Never mark human-notified on exhaustion. Retain visible delivery failure and operator alarm. |

A strict 60-second sweep bound requires a fair, bounded local scan that does no
provider/network I/O under the scan. Reuse the existing monitor owner, but run
probes/deliveries as separately bounded work with a due queue and per-Workspace
limits. The current five-second sleep plus sequential 60-second steps cannot meet
that bound. Measure sweep lag; overdue obligations remain visible during overload.

Long foreground commands are valid active execution. Progress reports can describe
their expected duration, but may only extend a specific budget with explicit
evidence; generic “still working” must not reset an incident's absolute deadline.
Approvals and explicit user Stop/pause override normal activity: a process alive at
a prompt is blocked, and an intentional pause never triggers auto-resume.

After process death or Hub restart, validate ownership instead of trusting a saved
`active` bit. A restart grace is allowed once, not renewed by each monitor pass.
An external service-manager/host health alert must cover a dead Hub monitor; Hub
cannot guarantee its own observation while the host is unavailable.

## 5. Recovery ladder, authority, and loop limits

| Stall class | Bounded automatic actions | Escalation / stopping condition |
| --- | --- | --- |
| Idle worker / completed background command | Reconcile artifacts and delivery; nudge with original task and exact result; after 120 s without a new turn or meaningful response, one second nudge | Maximum two nudges per episode. Then one safe provider resume, then one reassignment only if old execution is proven stopped and authorization permits. Otherwise notify Dot/human. |
| Plan/final review without verdict | Same-round reviewer reminder; one repeat after 180 s if still idle and no ambiguity; inspect already persisted verdict before any retry | One reviewer replacement after fencing old assignment; same artifacts, criteria and review cycle, new assignment generation. Never approve on timeout or wake worker past the gate. |
| Permission / input request | Zero blind nudges; route exact request to its authorized recipient | Persist human wait only after receipt. Permission decision must use existing answer/control API with exact turn/request identity. |
| Quota / transient provider outage | Retry at a known reset time with bounded timer; one same-provider resume when safe | No quota estimate from silence and no assumption a new session grants quota. After two failed attempts or a configured maximum wait, ask the human. Explicit provider/model choices remain binding. |
| Dead process / orphaned session | Reconcile pending commands, changes and transport receipts; one resume using verified provider session identity | At most one replacement worker after old owner and task-owned commands are fenced. Unknown side effects or shared-session ownership goes directly to human review. |
| Ambiguous message delivery | Inspect durable native turn evidence or terminal receipt | Do not replay `uncertain` messages or manufacture another call ID to bypass the fence. Unknown receipt requires operator decision. |
| Child/review chain stalled | Recover the actual producer; notify parent through a durable wake | If parent lacks coverage, do not suppress the human notice merely because its task is WORKING. |

For an eligible idle-worker episode with responsive transports, target a decision
within 10 minutes: detection by 1 min, two 2-min response windows, one 2-min resume,
then replacement initiation or human routing. Review uses 3-min response windows,
so allow 12 minutes. Delivery outages and valid quota timers have their own explicit
deadlines; they must not disappear into this optimistic latency target.

Recovery is **resume-inspect-report**, not replay of earlier commands. Reassignment
preserves task ID, worktree and artifacts; the replacement checks dirty/untracked
state and recorded external effects before acting. Never run two writers in the
same scope. Runtime restart means a task-owned provider/session operation, never a
Hub restart, shared tmux cleanup, or deletion of a persistent session.

### Idempotency and fencing

- Persist decision before sending. Key each logical action by
  `task/epoch/role/assignment-generation/review-cycle/episode/step`; retry uses the
  same call ID and immutable payload. A later ladder step gets a new ID only after
  the previous delivery outcome is reconciled.
- Acquire Workspace mutation lock, reread assignment/control/pause/phase and
  expected revision, then claim the action. Delivery performs another identity
  check immediately before submission. A human action, report, or newer turn wins.
- Reuse TaskMailbox fingerprint conflicts and durable messaging, including
  pending/processing/uncertain/ACK. Current terminal auto-continue directly sends
  tmux prompts (`backend/claude_hub/services/workspace_manager/monitoring/recovery.py:357`);
  migrate it to the same durable path instead of adding a competing recovery loop.
  Internal recovery events use the runtime actor and targeted session delivery;
  they must not impersonate a human or use generic task followup to cross a review
  gate. The public followup API accepts supervisor/human actors only.
- Existing task `execution_epoch` fences execution control; do not assume it already
  fences reviewer replacements. Add assignment generation and validate it in report,
  completion and wake intake. Stale completions are retained as audit evidence but
  cannot wake a new owner or satisfy a newer review.
- Counts survive provider restart, Hub restart, role changes, and reassignment.
  Reset an episode only after new verified progress or a new legitimate obligation;
  an ACK, token burst, or “continue” echo alone is insufficient. Proposed global
  budget: at most three recovery episodes/hour/task, then human intervention.
- Keep retries subordinate to explicit execution control. Workspace acts only on
  Workspace-owned tasks. For initiator-owned tasks, observe and notify the initiator;
  do not silently dispatch or take over. Preserve all review and human-acceptance gates.

### Human notice contract

Store `pending`, `delivered`, `failed`, `unroutable`, and `acknowledged` separately.
“Delivered” means a configured user-facing channel accepted the actionable message
with a receipt, or a connected Hub user surface acknowledged displaying its durable
notice. It does not mean the person read or agreed. Dot inbox intake and an internal
mailbox wake do not qualify. If all user routes are offline, keep notification
delivery outstanding, raise a service-health incident, and expose the failed guarantee.

The notice includes task link, exact blocker, what was attempted, preserved artifacts,
what decision is needed, and a safe next action. Dot can enrich it; a deterministic
minimum notice must not wait on Dot's model/quota. This is an intentional change to
the current Dot-only routing policy and requires product review during implementation.

## 6. Provider and session coverage

| Provider | Native Chat: verified current behavior | Terminal: current evidence and proposed coverage |
| --- | --- | --- |
| Claude | One-shot stream JSON with conversation resume (`backend/claude_hub/services/agent_stream/providers/claude.py:188`, `:233`); top-level result ends the turn (`backend/claude_hub/services/agent_stream/claude_jsonl.py:203`). Background-result filtering protects correlation; it does not register a task wake (`backend/claude_hub/services/agent_stream/providers/claude.py:62`). | tmux/frame inference and optional transcript evidence; no installed Stop-hook contract found in the bounded launcher/provider search. Add a task-scoped hook only after capability testing, with the common reconciler as fallback. |
| Codex | Persistent app-server RPC/thread resume (`backend/claude_hub/services/agent_stream/providers/spec.py:392`). User-input RPC becomes a blocking card (`backend/claude_hub/services/agent_stream/providers/codex_rpc.py:440`); other unsupported server requests receive method-not-found at `:478`. Do not assume a native permission card caused the reported incident. | Frame inference has Codex-specific busy-marker scanning (`backend/claude_hub/services/agent_status_markers.py:73`). No verified equivalent Stop hook is configured in this repository; use wrapper evidence and conservative reconciliation. |
| Cursor | One-shot stream JSON/resume (`backend/claude_hub/services/agent_stream/providers/cursor.py:16`, `:64`); answers are a later steer (`:32`), rather than a blocking RPC answer. `turn_ended` is the completion signal (`backend/claude_hub/services/agent_stream/providers/spec.py:454`). | Same sampled terminal status limits. Do not claim a blocking Stop hook or exact command-exit subscription until tested. |
| TraeX | Codex-derived persistent RPC; explicit command/file permission cards (`backend/claude_hub/services/agent_stream/providers/traex.py:157`), identity-aware permission answer at `:213`, provider turn/thread interruption at `:90`. | Common terminal frame classification; no configured equivalent Stop hook found. Normalize attention conservatively and use registered command supervision. |

All terminal providers share sampled foreground/frame classification
(`backend/claude_hub/services/terminal_runtime/manager/status.py:34`). Generic prompt
substrings produce “needs your response” at `:103`; a frozen busy frame becomes
attention after 45 seconds (`backend/claude_hub/services/terminal_runtime/defaults.py:62`).
Neither is typed proof of permission, quota, or a dead turn. Unknown evidence can
justify an actionable diagnostic notice, not automatic termination.

### Reuse active-turn watchdogs; fill the cross-turn gap

Native Chat already has 600-second stream silence detection and a configurable
7,200-second hard-silence bound, plus a 1,800-second capacity wait cap and 90-second
queue-heartbeat timeout (`backend/claude_hub/services/agent_stream/tailer.py:126`,
`:139`, `:171`). Outstanding tools suppress normal silence detection; blocking
approval cards suppress even the hard bound
(`backend/claude_hub/services/agent_stream/runtime/watchdog.py:54`, `:88`). Those
watchdogs run only with a turn in flight (`backend/claude_hub/services/agent_stream/runtime/native_consumer.py:94`).

The design must coordinate with these watchdogs, not create competing cancellations.
Registered long-command leases supply evidence to the existing hard-silence policy;
the proposed 30-minute investigation threshold is diagnostic, not a replacement
30-minute kill timeout. A blocking card transfers responsibility to the human-notice
path. Runtime restart/resume is transport recovery; only a matching new turn proves
Task continuation.

One-shot completion releases the turn guard even when a tool child remains, then
invalidates the old stdout generation
(`backend/claude_hub/services/agent_stream/providers/session.py:217`, `:237`). Tool
result normalization records an opaque result, not a durable task-owned process
(`backend/claude_hub/services/agent_stream/claude_jsonl.py:553`). This makes a
provider-neutral registered runner necessary for robust background completion.

Use the existing persisted activity observer for turn/tool/approval/error events
(`backend/claude_hub/services/agent_stream/runtime/publication.py:370`). Persist a
genuine turn completion truthfully, then reconcile or atomically arrange its next
obligation in Task state. Never suppress completion to pretend an idle turn is live.
Persist an event cursor so a failed observer is replayed after restart.

### Assessment of the four candidate ideas

- **Periodic auto-continue sweeper: accept with constraints.** Extend the existing
  monitor, typed cause checks and durable delivery. Two failed nudges is a sensible
  default cap, not permission to replay uncertain work or bypass a gate.
- **Turn-end guard: accept as a supplementary fast path.** A Hub native boundary can
  check ownership for all supported Chat adapters. A Claude Code Stop hook requires
  a capability experiment against the installed CLI; the repository does not prove
  veto semantics. If supported, block a graceful stop at most once per turn to ask
  for a registered successor/report, with a short API timeout and recursion guard.
  Explicit user cancellation must always win. Hook failure falls back to durable
  reconciliation, not an infinite refusal to stop. A report alone is insufficient
  unless it establishes the next owner or a terminal/human state.
- **Background completion wake: accept and prioritize.** Register before launch,
  durably record exit, enqueue once, and supervise until continuation starts. Do not
  parse arbitrary shell output into executable recovery commands.
- **Detect and route `needs_input`: accept with typed causes.** Distinguish permission,
  user question, auth, quota/capacity, missing handoff, explicit pause and delivery
  uncertainty. Structured evidence outranks terminal text; unknown stays unknown.

No provider-equivalent Stop-hook capability is assumed. This local-only study does
not establish whether external CLI versions offer additional hooks. Provider-specific
runtime experiments belong to phase 3 in isolated sessions, not the live incidents.

## 7. Ranked implementation plan

Effort estimates are engineering days for one developer familiar with this code,
including focused tests and review, excluding deployment and provider availability.
Phases are deliberately separable; partial rollout must advertise partial coverage.

| Rank / phase | Work and likely files | Acceptance / effort |
| --- | --- | --- |
| 0: current blind spots | `services/task_lifecycle.py`, `workspace_manager/_chat.py`, `tmux/review_delivery.py`, `monitoring/refresh.py`; propagate reviewer attention and distinguish Chat missing handoff from actual input/pause/uncertainty | Idle Chat reviewer can be reminded; blocked reviewer gets an actionable route; active reviewer is untouched. 1–2 days. |
| 1: common bounded recovery | Add `models/schema_domains/liveness.py` and `workspace_manager/monitoring/liveness.py`; extend `tasks.py`, schema exports, `_persistence.py`, `_workspaces.py`, `monitoring/recovery.py`, `messaging/delivery.py`, `reports/intake.py` | Durable coverage/episodes, fair sweep, two-nudge cap, exact assignment fencing and restart reconciliation; legacy policy loops are deleted and replaced by one evaluator. 3–5 days. |
| 1b: genuine human wait | `models/schema_domains/lifecycle.py`, `monitoring/lifecycle.py`, `api/task_notifications.py`, Dot notice bridge and existing Workspace UI notification projection | Receipt-backed delivery, no false notified marker, deterministic fallback when configured, parent-stall escalation. 2–3 days; required before claiming the human-wait guarantee. |
| 2: durable producer wakes | Scoped job runner and CLI/API in existing Task command/routes; task wake fields; `reports/events.py`, review dispatch/resume, `task_mailbox.py`, `agent_stream/runtime/publication.py`, scheduling dispatcher | Launch/exit/consume protocol, lost-event reconciliation, review verdict-to-worker start, child completion races, bounded timers. 4–6 days. |
| 3: adapter enforcement | Provider adapters/capability declarations, terminal status/hooks, native turn-end observer, provider error classification | Claude/Codex/Cursor/traex matrix exercised; graceful Stop check where supported; explicit sampled coverage elsewhere. 3–5 days. |
| 4: operational guarantee | Existing monitor health/metrics and deployment health checks; operator controls, controlled rollout | Crash/restart/overload and channel-loss tests meet bounds, observe-only rollout then scoped enforcement. 2–3 days. |

Sequence: phase 0 provides immediate value; phases 1 and 1b make recovery and
notification trustworthy; phase 2 closes the background-command gap; phase 3 raises
coverage at provider boundaries. Total roughly 15–24 days, with phase 0 useful alone.
Do not wait for every provider's hook before fixing the review and native Chat gaps.

### Test strategy

Use injected clocks and fake transports for transition/deadline tests, plus isolated
process integration for command supervision. Do not test these by disturbing active
Workspace sessions. Add assertions against observable outcomes, not just internal
counter increments.

| Scenario | Evidence required and nearest existing tests |
| --- | --- |
| Three reported stalls | Gate remains closed until verdict; reviewer attention is exposed and notified; command exits after turn-end and exactly one continuation starts. Extend `backend/tests/test_review_delivery.py`, `test_managed_chat_orchestration.py`, `test_task_lifecycle.py`. |
| False positives | Long silent live tool, intermittent output, busy reviewer, explicit Stop/pause, legitimate quota timer: no kill, reassignment, or gate bypass. Extend `test_hard_recovery.py`, `test_workspaces.py` and adapter tests. |
| Crash boundaries | Crash before/after obligation save, launch, exit record, wake enqueue, provider acceptance, report commit. Restart produces one logical wake or explicit uncertainty, never duplicate command execution. Extend `test_task_mailbox.py`, `test_task_mailbox_report_intake.py`, `test_managed_chat_mailbox.py`, `test_chat_message_inbox.py`. |
| Concurrency | Duplicate/out-of-order events; report races sweep; late old-owner verdict; manual abort/reassignment during probe; PID reuse; two monitor passes. One owner and current-generation verdict always win. |
| Supervision | Child completes before registration; child fails; parent is also stalled; circular waits. No stranded parent or permanent WORKING-based suppression. Extend `test_task_lifecycle_notices.py`. |
| Notice delivery | Unroutable, Dot quota failure, transport timeout/exhaustion and duplicate receipts cannot count as delivered. Successful current-generation receipt creates human wait exactly once. |
| Monitoring fairness | One slow session/network operation does not delay all obligations beyond deadline; monitor death produces an external health alarm. Extend `test_workspace_monitor_isolation.py`. |
| Session/provider matrix | Chat and terminal × worker/reviewer × active/idle/input/paused/unknown × pending/accepted/uncertain delivery; each unsupported capability has an explicit degraded path. |

Roll out observation-only first, record false-positive reasons and sweep lag, then
enable non-destructive wakes for task-owned sessions. Gate destructive recovery on
termination proof and ownership. Track uncovered obligation count/age, wake-to-turn
latency, undelivered notices, recovery budget exhaustion, and repeated stalls.

## 8. What Dot can do now, without backend changes

These are available operator actions, not actions performed by this research task.
They mitigate stalls but cannot establish the proposed guarantee.

1. Require foreground long commands or a live foreground waiter in delegated briefs;
   explicitly prohibit ending a turn after an unregistered detached test command.
   Require report/verdict API submission and its response before yielding.
2. Set an explicit short watcher threshold when appropriate, for example
   `claude-hub dot watch TASK_ID --stall-hours 0.1`. The current service default is
   one hour; CLI help still says two, so explicit configuration avoids ambiguity.
   Watcher alarms do not cover all review states and do not automatically recover.
3. Inspect `claude-hub task status TASK_ID --workspace-id WORKSPACE_ID`,
   `claude-hub task review TASK_ID --workspace-id WORKSPACE_ID`, and agent status.
   Distinguish worker/reviewer, native pause, unresolved question, quota, and uncertain
   delivery before intervening. Session `needs_input` is not itself a diagnosis.
4. For a confirmed idle **WORKING** worker with no pending Goal Packet gate,
   review in flight, explicit pause/input, or delivery uncertainty, send a bounded
   inspection prompt through
   `claude-hub task followup WORKSPACE_ID TASK_ID --message 'Inspect current task and saved command results; resume only unfinished work and report the outcome.' --call-id 'liveness-TASK_ID-EPISODE-1'`.
   Reuse the exact payload/ID on transport retry. Verify a new turn/report afterward.
   Stop after two unsuccessful interventions and surface the blocker.
5. Do not use generic task followup on an open review to nudge its reviewer:
   `tasking/followup.py:47` routes REVIEW to worker continuation. Instead use the
   existing reviewer-specific Session message/review control after identifying its
   current binding; request resubmission of the same verdict/call ID. Never turn a
   missing verdict into approval. Confirm the review report was persisted.
6. `claude-hub task continue TASK_ID --message '...'` is for REVIEW/FAILED, not a
   generic WORKING-task heartbeat (`dispatching/continuation.py:57`). Use it only
   when continuation is authorized and the review/input condition is resolved.
   Preserve the same task and configured review policy; do not accept on the user's
   behalf or change provider/model to evade quota.
7. For an ordinary Chat message, `tab message-status TAB_ID MESSAGE_ID` distinguishes
   accepted from completed. Task followup ACKs likewise prove message processing,
   not completion of the task. A background process's exit must be inspected, never
   inferred from a quiet terminal.
8. Send an actionable human notice for permission/quota/uncertainty with the task
   link and exact next decision. Do not blindly answer prompts, operate tmux, create
   schedules without a request, or restart shared sessions. Dot's own turn ending
   does not create a future check; use its existing watch events and be explicit
   about the remaining lack of automatic recovery.

## 9. Research validation and handoff

Research used local code and selected CLI help only; no web research or provider
documentation claims. Main was clean, fetched, and 20 commits ahead of origin/main;
the local commits were preserved. Work was isolated on the baseline above. No
implementation, incident-task action, session restart, commit, push, or additional
changelog/working-log file is included. This section records the research provenance
within the sole requested document.

The document passed an independent read-only design critique; its one operational
clarification was incorporated: even a WORKING task must not receive a generic
worker nudge while a Goal Packet review is pending. Both lifecycle and provider
evidence handoffs were checked against source by the owner. Explicit source line
references and cited test paths were mechanically checked for existence/range;
the check does not claim to prove each semantic conclusion.

The required full verification was attempted in the isolated checkout: documentation
and committed-checkout safety passed; three backend targets could not run without
the worktree's `.venv`, and four frontend targets could not run without installed
dependencies. Those seven targets are not passes. No dependency installation or
runtime test was needed to substantiate this code-grounded design.

Independent evidence and critique roles used the configured runtime default model/API,
with read-only scope and no direct task reporting. Final Hub evaluator review and
human acceptance remain required. Historical incident
causes and proposed timing bounds have not been reproduced. No lessons needed.

### Admission and incident re-arming

The central owner fences reserved `liveness:` and migrated `review-delivery:`
envelopes under the workspace/mailbox locks at admission, each pending retry and
after readiness waits. Its final native callback runs immediately before provider
submission. A changed generation, assignment, session incarnation, active turn,
question or pause prevents automatic input. Cancellation removes only proven-unsent
pending payload/fingerprint entries; accepted, processing and uncertain receipts
are preserved. A cancelled durable claim retains its ID and spent attempt so a
stale caller cannot resurrect it. Enqueue alone does not finish a recovery action.
Generic user messages and exact question answers retain their existing transport.

A watched queued or initiator Task re-arms on a newer recorded activity marker,
even without a managed active session. This closes the prior notice incident,
keeps its recovery budget and permits one subsequent alert after the configured
watch interval expires again. Unchanged monitor observations do not write state.
