# Task Lifecycle Refactor: one phase, one owner, one writer

Status: Layer 1 implemented on `codex/task-lifecycle`; Layers 2–5 planned.
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

- **Occupancy ≠ affinity.** Only `running` and `reviewing` occupy a session.
  A Task in `needs_user` or `awaiting_acceptance` keeps a *preference* for
  its agent but frees it. When resumed, it re-queues with affinity; if the
  agent was reused meanwhile, it is resumed with its report history, as
  migration already does.
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
| **3. Single writer** | `apply_task_transition(task, event, facts) → (patch, effects, TaskEvent)` reducer. The ~25 status write sites call it; it is the only place that sets `status`, the review timestamps and the session pointers. Monitor failure, timeout and owner loss become events. | Occupancy rule (`needs_user` / `awaiting_acceptance` free the agent); missing events now emitted. | Transition-table tests; the cadb399d scenario no longer starves the queue; characterization suites pass. |
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
