# Task Lifecycle Refactor: one phase, one owner, one writer

Status: Layers 1–5 implemented on `codex/task-lifecycle`; remaining debt listed under Layer 5.
Continues [the 2026-05-23 state machine assessment](2026-05-23-state-machine-assessment.md),
which extracted pure policy helpers (its step 2) but never introduced the
reducer, explicit sub-phases or a single transition owner (its steps 3–6).

## Problem, with live evidence (2026-10-24)

A read-only projection of the primary Hub's `state.json` files found 18
non-terminal Tasks:

- 10 are **orphaned**:
  - 9 initiator Tasks whose source Chat is archived or deleted, still showing
    `working` (some last reported `needs_input`);
  - 1 that the initiator explicitly released.

  No reaper exists for initiator Tasks.
- 1 is a **head-of-line deadlock**. Task `cadb399d` got `review_needs_input`
  and stayed in `review`. `_can_dispatch_to` holds the worker until DONE, and
  queue migration only targets *another free* agent, so the only agent idled
  for 14+ hours while 2 queued Tasks starved. The dispatcher logged a skip
  every ~6 s throughout.
- Dot reported the queued Task as "stalled 2h (status queued)" without
  saying it was waiting on the user's answer to `cadb399d`.

### Structural causes

1. **No single owner record.** "Who executes this" is inferred from about
   7 pointers:
   - `execution_control`;
   - `session_id` / `review_session_id`;
   - the session's reverse `task_id`;
   - `execution_ref`;
   - `runtime_status`;
   - Dot `work.json`;
   - GoalRun.

   Death detection exists only for `subagent` mode and scheduled hub tasks.
2. **The coarse status hides who must act.** `review` covers AI review in
   progress, reviewer question, review failed, review passed and skipped.
   `working` cannot say "the owner is gone". Initiator `progress.state` never
   reaches `status`.
3. **No single writer.** Task `status` is assigned at about 25 sites across
   14 modules via `model_copy(update=...)`. Each site re-implements guards.
   About 15 review bookkeeping fields exist to reconcile them.
4. **Events are optional.** Monitor-detected failure, timeout and "Chat turn
   ended without report" change state without a mailbox event. Waiters and
   Dot therefore cannot react.

## Target model

```
TaskLifecycle = phase × owner × attention        (derived → later persisted)

phase:  draft → queued → running ⇄ reviewing
                   ↘        ↓  ↘
                 orphaned  needs_user   awaiting_acceptance → done
                              (any non-terminal) → failed
owner:  {kind: session | chat | none, id, state: active | idle | gone | unknown}
attention = phase ∈ {needs_user, awaiting_acceptance, orphaned}
```

| Phase | Meaning | Who must act |
|---|---|---|
| `draft` | Recorded, never asked to execute. The reason distinguishes `not_started`, `resident_proposal`, `aborted` and `handed_off_not_dispatched`. | Whoever wants it started |
| `queued` | Wants an executor. The reason distinguishes `no_agent_assigned`, `awaiting_dispatch_decision`, `waiting_for_dependencies` and `agent_held_by_other_task`, the last with `blocked_by_task_id`. | Hub |
| `running` | The owner is executing. | Owner |
| `reviewing` | The AI reviewer is judging the current round. | Reviewer |
| `needs_user` | Blocked on a human: a reviewer question, a worker prompt, or an initiator `needs_input`/`blocked`. | **User** |
| `awaiting_acceptance` | Finished, waiting for human acceptance. | **User** |
| `orphaned` | Non-terminal, but the owner is gone or released it. | **User or Hub reaper** |
| `done` / `failed` | Terminal. | — |

Rules this model makes explicit (Layers 2–3):

- **One open Task per agent; parallel Tasks get parallel agents** (decided by
  the user on 2026-10-24). An agent stays bound to its Task until the Task is
  done, so the implementation context remains consistent for revisions and
  rejection; this was already intended. New work never queues behind an agent
  that another open Task holds; it gets a dedicated agent instead. Agents are
  neither over-provisioned nor shared between open Tasks: the agent count
  follows the number of Tasks running in parallel, and an agent whose Task is
  done is reused before a new one is created.
  - Only an explicit user target ("User selected target agent") may wait
    behind a held agent.
  - Related-task and previous-assignment continuity are preferences: they
    reuse that agent only while it is free.
- **Owner loss is a transition, not a silent state.** When the owner is
  `gone` past a grace period, the reconciler moves the Task to `orphaned` and
  emits an event. Policy then redispatches (workspace control), notifies the
  source (initiator), or fails it.
- **Every transition emits exactly one TaskEvent** carrying
  `phase_from → phase_to`, reason and attention. Waiters, Dot and the Feishu
  notifier consume this one stream.

## Layers

Each layer is separately shippable and keeps legacy `status` consistent.

| Layer | Change | Behavior change | Acceptance |
|---|---|---|---|
| **1. Projection** (done) | Pure `services/task_lifecycle.py` plus `models/schema_domains/lifecycle.py`; `WorkspaceBoard.task_lifecycles`. | None (additive field). | 31 unit tests; existing workspace/board/queue/review suites pass (815); dry run on live data classifies all 18 open Tasks correctly. |
| **2. Consumers** | CLI `task list/get/status` show phase, reason and blocker plus `task list --attention/--phase` (done with Layer 1); board UI badges from `task_lifecycles` replace timestamp heuristics in `useWorkspaceTaskPresentation`; Dot watcher keys off phase transitions and pushes attention phases to Feishu deterministically (persisted `notified`, retry). | Visible and notification only. | A user sees "orphaned: Chat archived" and "queued behind cadb399d (needs your answer)" without polling. |
| **3a. Dedicated agents** (done) | `_choose_dispatch_target` provisions a dedicated agent instead of "Queued behind existing workspace agent". It inherits the launch config of the newest same-provider agent: env preset, Chat env and remote target. Free-agent reuse now requires the Task's provider. After each dispatch pass, one background job per workspace gives stranded queued Tasks their own agents; callers never wait for agent startup, and failed attempts are retried at most once per Task per 60 s. | The cadb399d deadlock cannot recur, and already-stranded Tasks are rescued. | Policy tests rewritten to the 1:1 contract; legacy stranded-queue rescue test; full suite passes. |
| **3. Single writer** | `apply_task_transition(task, event, facts) → (patch, effects, TaskEvent)` reducer. The ~25 status write sites call it; it is the only place that sets `status`, the review timestamps and the session pointers. Monitor failure, timeout and owner loss become events. | Missing events now emitted. | Transition-table tests; the cadb399d scenario no longer starves the queue; characterization suites pass. |
| **4. Owner lease and reconciler** | Persist `owner` and `phase`. A single 5 s reconciler replaces the subagent-only death check, the hub-task orphan converger, the review reaper's no-session gap and the initiator turn-only activity. Initiator owner = Chat tab (archive or delete = gone). | Orphans are resolved by policy instead of lingering. | No non-terminal Task with a gone owner survives longer than grace + 1 tick. |
| **5. Subtraction** | Load-time migration derives `phase` for old records; delete redundant fields: `dispatch_pending` → queued reason, `execution_released` → orphaned reason, `review_delivery_*`/`review_skip_*`/`human_acceptance_requested_at` → event log; `autonomous_run.phase` maps 1:1. Collapse `execution_control × task_mode` (8 combinations) once `agent run` removes the need for `subagent` mode. | Schema shrink. | Task record roughly 70 → ~35 fields; frontend derives no lifecycle. |

Parallel, independent tracks: the `claude-hub agent run --provider … --wait`
subagent primitive (bound to the calling Chat, no Task), and Dot long-task
checkpoints. Both consume Layer 2/3 events.

## Layer 1 notes

- Derivation is pure (`LifecycleFacts` in, `TaskLifecycle` out). Owner
  liveness is gathered once per board read in
  `_WorkspaceBoardMixin.task_lifecycles`.
- Latest review verdicts are computed in one pass over reports, not with the
  per-task full sort used elsewhere.
- Initiator owner liveness relies on the source Chat tab's existence and
  `archived` flag, a hard signal. A missing tmux session is *not* treated as
  gone: native Chats start their runtime lazily.
- CLI attaches the lifecycle once in `_board_tasks`, so `list`, `get`,
  `status` and the board-scanning lookups share it. JSON output adds a nested
  `lifecycle` plus flat `phase`, `phase_reason` and `blocked_by`, and older
  servers without `task_lifecycles` still work.
- `test_reviewer_question_still_holds_worker_until_policy_changes` pins
  today's occupancy, so the Layer 3 policy change is a deliberate, visible
  test edit.

## Layer 3a notes

- The `_request_dispatch_decision` branch in `_start_task_workspace` is now
  unreachable, because target selection always returns an agent. Layer 5
  deletes it together with `dispatch_pending`.
- `_next_reassignable_queued_task` remains for pinned work and for moving
  Tasks to agents that are already free. Under the 1:1 rule it should become
  dead code once no Task can be persisted behind a held agent; remove it in
  Layer 5 after one release.
- Starting a Task when the only agent is stopped now creates a new agent
  instead of returning 400. A stopped agent runs no Task, so the 1:1 rule
  applies.
- Stranded means the assigned agent is held by another open Task, or is
  archived, stopped, paused, or a terminal whose tmux is OFFLINE. Only
  "User selected target agent" waits.
- Ephemeral, scheduled and paused agents are never launch templates or
  reusable free agents. A dedicated agent keeps its Task for a 120 s
  bootstrap grace instead of losing it to another agent that frees up.
- A Task's own first start still provisions inline, as the old no-agents
  path did. Agent startup is bounded by the 25 s prompt wait, close to the
  CLI's 30 s request timeout; Layer 2/`agent run` work should move this to
  the background job as well. `WorkspaceAgentInitializationError` from start
  maps to 502, like direct agent creation.

## Layer 2 notes

- `monitoring/lifecycle.py` runs after each dispatch pass. It compares the
  derived lifecycle with the persisted `WorkspaceTask.lifecycle_mark` and
  records each change once. The change is emitted once as a `lifecycle` Task
  mailbox event, and waiters are woken after the per-workspace save, so
  `task wait` long-polls see runtime loss too, not only reports. Dot does not
  read the mailbox; it keeps its own watcher, now with lifecycle-aware stall
  text.
- Notices are deterministic and do not depend on an LLM choosing to notify:
  - attention phases (`needs_user`, `awaiting_acceptance`, `orphaned`,
    `failed`) are announced after settling, 30 s for recorded facts and
    180 s for reasons inferred only from sampled runtime output;
  - the same `phase:reason` is not announced again within 30 min, so
    flapping does not page twice;
  - a new Goal Packet is announced as a submitted plan.
- Delivery runs in one background job per workspace, never inline in the
  monitor loop. Each send is bounded at 20 s and a pass sends at most 5. It
  goes through an injected notifier (`api/task_notifications.py`), which
  routes to the Feishu chat bound to the source Chat, or to Dot when Dot
  tracks the Task.
- Not pushed:
  - states an initiator Chat reported itself (its own reply already reached
    the person);
  - children of an open parent Task, which the parent's agent supervises.
- Notification state lives in the mark itself (`notified_at`, attempts,
  backoff up to 1 h, `plan_digest`, last notice key and time), so it
  survives restarts. Conditions already older than 24 h at first
  observation (upgrades) are recorded but not pushed. A recent failure seen
  first as failed is announced.
- Next steps in notices depend on the board status: `task followup` for
  working Tasks, `continue`/`accept` for review, `continue` for failed.
- Known limits:
  - Task mailbox events still have no retention.
  - Derivation scans all reports once per workspace per tick.
  - A Feishu pool error makes a recipient look unbound, so that notice is
    dropped as unroutable.
- Dot:
  - The wake prompt no longer repeats Hub notices.
  - The brief asks for a plan first, milestone reports, and stopping to ask
    on drift.
  - The tool guide adds `task followup` for steering and
    `task list --attention`.
  - The stall alarm defaults to 1 h and names the reason, including the Task
    it waits behind.
- Product text (board, CLI) uses English `reason_text`. Feishu notices to the
  user use `NOTICE_REASON_TEXT`.
- The board card shows a lifecycle note. Attention notes are highlighted,
  and queued Tasks name their blocker. Covered by the mocked browser
  scenario `browser_workspace_detail.py` at 390, 720 and 1280 px.

## Layer 3 notes (single writer)

- `services/task_transitions.py` defines `apply_task_update(task, update,
  cause=...)` and the `(from, to) -> causes` table. All 26 status writers in
  `workspace_manager` now call it. This covers literal updates and the
  dynamic dicts from report intake, reviewer verdicts, start, initiator
  progress/handoff and manual PATCH (`cause="manual"`, unrestricted like load
  repair).
- Unexpected edges log in production and raise under
  `CLAUDE_HUB_STRICT_TASK_TRANSITIONS=1`, which `tests/conftest.py` sets.
  The full suite passes in strict mode. The edges this surfaced are existing
  behavior and are commented in the table:
  - reports on never-started records;
  - redispatch of a scheduled Task whose worker died;
  - verdicts landing while the Task is marked working;
  - Feedback Reaper abort closing as done;
  - handoff reopening done.
- `tests/test_task_transitions.py` fails if any service writes
  `model_copy(update={"status": WorkspaceTaskStatus...})` directly, so the
  single-writer property cannot erode silently.
- The table makes the remaining complexity visible: about 15 causes write
  status. Layer 4 adds the reconciler as one more named cause instead of
  another ad-hoc writer.

## Layer 4 notes (owner loss is resolved, not left lingering)

- The owner lease is the persisted `lifecycle_mark`: its `changed_at` is when
  the Hub first saw the current owner state. Liveness comes from the facts
  the Hub already observes, so agents do not have to send heartbeats:
  - session status/archive, and runtime OFFLINE for terminals;
  - Chat tab existence and archive.
- `_reconcile_orphaned_tasks` runs after each observation and resolves
  orphaned Tasks per `OWNER_LOSS_POLICY`, with cause `reconcile` in the
  transition table:
  - `agent_gone` / `working_without_agent` (workspace control, working)
    fail after 5 min with a reason that names `task continue`. Continuing a
    failed Task whose agent is gone already reassigns it to a fresh,
    dedicated agent.
  - `initiator_chat_gone` fails after 7 days. A failed initiator record
    rejects further progress, so only `task handoff` revives it; the user is
    notified immediately.
  - Not handled here: subagent Tasks (existing death/timeout failure),
    scheduled hub Tasks (existing redispatch converger), and queued Tasks on
    a gone agent (dedicated-agent rescue).
- Quickly resolved owner loss is not pushed on its own; the person hears
  about the resolved failure once.

## Layer 5 notes (subtraction) and remaining debt

Removed, because the earlier layers made it unreachable or it was already
broken:
- The dispatcher-decision request in `start_task`: target selection always
  returns an agent now. This removed `_request_dispatch_decision`,
  `_build_dispatch_decision_prompt` and their abstract contracts. The
  `dispatch-decision` endpoint stays for compatibility with Tasks persisted
  as `dispatch_pending`.
- The `claude-hub work` CLI group. Every command called
  `/api/tabs/{tab}/work`, removed with ChatWork in 782d295, so it could only
  404.
- In total, about 240 lines of dead code.

Documented in `docs/TASK_GRAPH.md` ("Task lifecycle") and the
`ARCHITECTURE.md` ownership table.

Deliberately not done yet. Each needs its own migration and review:
- **Persisted fields.** The Task record still carries `dispatch_pending`,
  `execution_released`, `review_delivery_*`, `review_skip_*` and the review
  timestamps. Dropping them needs a load-time migration and a downgrade
  story, because `extra=forbid` rejects unknown fields. The lifecycle
  projection already gives a single answer, so the remaining value is
  smaller records, not behavior.
- **Frontend review badges.** `useWorkspaceTaskPresentation` still derives
  review badges from timestamps. They could read `task_lifecycles` instead,
  but that needs a browser pass over every review sub-state.
- **`subagent` task mode.** Once `codex/agent-run` (`claude-hub agent run`)
  is merged, the Task-board `subagent` mode can be deprecated, which shrinks
  the execution_control × task_mode matrix.
- **`_next_reassignable_queued_task`.** Under the 1:1 rule, only pinned or
  legacy queued work reaches it. Remove it after one release.
- **Mailbox retention.** Task mailbox events have no retention policy.
