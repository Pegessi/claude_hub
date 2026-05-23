# State Machine Assessment

## System Overview

Claude Hub already has state-machine-shaped behavior, but it is implemented as enums plus imperative transition logic rather than as an explicit state machine. The current design is functional and reasonably well covered by backend tests, especially around review routing, idle auto-prompts, reviewer verdicts, and queue dispatch. The main weakness is not missing state vocabulary; it is that state ownership is spread across several entry points in `WorkspaceManager`, plus frontend display derivations and terminal-output heuristics.

The right next step is a bounded state-machine layer for Agent Workspace task/session/report policy. Do not fold low-level terminal runtime detection into the same machine. Treat terminal status as an observation source that can influence workspace state through explicit events.

## Inventory

### Shared State Vocabulary

- `backend/claude_hub/models/schemas.py` defines the core enums:
  - `AgentRuntimeStatus`: `idle`, `working`, `attention`, `offline`.
  - `WorkspaceTaskStatus`: `todo`, `queued`, `working`, `review`, `done`.
  - `ManagedSessionStatus`: `spawning`, `working`, `idle`, `needs_input`, `done`, `stopped`, `error`.
  - `AgentReportState`: worker and reviewer report states, including `ready_for_review`, `completed`, and `review_*`.
- `WorkspaceTask` carries both lifecycle status and review/human-acceptance timestamps.
- `ManagedSession` carries persisted semantic status plus sampled `runtime_status`, current assignment, and auto-continue counters.
- `AgentReport` records report-state events and review routing intent.

This vocabulary is workable, but `WorkspaceTaskStatus.REVIEW` now means several different sub-states: reviewer running, reviewer passed and awaiting human acceptance, reviewer failed, review skipped, and reviewer needs input. The frontend compensates by deriving labels/actions from report history and timestamps.

### Terminal Runtime Detection

- `backend/claude_hub/services/ttyd_manager.py` owns best-effort terminal status classification in `_classify_agent_status`.
- It samples tmux output through `get_tab_agent_status` / `list_tab_agent_statuses` and returns `TerminalAgentStatus`.
- The classifier intentionally reads only the current visible tail to avoid stale scrollback words affecting classification.
- `backend/claude_hub/api/tabs.py` exposes this through `GET /api/tabs/status`.
- `frontend/src/stores/terminalStore.ts` polls `/tabs/status` every 5 seconds and refreshes tabs when status data references unknown tab ids.

This layer is heuristic by nature. It should stay isolated from semantic task state and should publish observations such as `runtime_idle`, `runtime_working`, `runtime_attention`, and `runtime_offline`.

### Workspace Task, Session, And Report Transitions

`backend/claude_hub/services/workspace_manager.py` is the real lifecycle owner:

- Load-time normalization backfills legacy state and optional fields.
- `start_task` queues a task, selects or asks for a dispatch target, and calls `dispatch_workspace`.
- `_dispatch_task_to_session` sends the assignment prompt and moves task/session to working.
- `update_task` allows direct manual status mutation through the API and performs side effects for `done` and `working`.
- `create_report` persists an agent report, maps report state to session/task status, then calls `_after_report_recorded`.
- `_after_report_recorded`, `_should_request_task_review`, `_can_skip_task_review`, `_request_task_review`, and `_mark_task_review_skipped` implement the review routing policy.
- `_handle_review_report` applies reviewer verdicts and can return failed work to the original agent.
- `_refresh_session_statuses` maps terminal runtime observations back to managed session status, frees approved review tasks, auto-prompts idle working agents, and may request reviewer diagnosis for attention states.
- `_reconcile_task_report_statuses` repairs stale task states from report history when rendering the board.

There are good targeted guards:

- Dispatch is serialized per workspace with `_dispatch_locks`.
- Offline/stopped agents cannot accept new task dispatch.
- The original agent is held during in-flight review and released only after review approval.
- Review-failure continuation rejects reuse if the original agent is now busy with another active task.
- Review-skip requires Goal Packet evidence, acceptance checks, low/no risk, no changed files, no tracked diff, and no prior failed review.

The concern is that these guards are distributed across transition sites, not expressed as one transition table or reducer.

Current code landmarks:

- `backend/claude_hub/models/schemas.py:23` defines `AgentRuntimeStatus`.
- `backend/claude_hub/models/schemas.py:42` defines `WorkspaceTaskStatus`.
- `backend/claude_hub/models/schemas.py:53` defines `ManagedSessionStatus`.
- `backend/claude_hub/models/schemas.py:64` defines `AgentReportState`.
- `backend/claude_hub/services/ttyd_manager.py:1191` starts `_classify_agent_status`.
- `backend/claude_hub/services/workspace_manager.py:647` starts manual `update_task` status mutation.
- `backend/claude_hub/services/workspace_manager.py:1501` starts `_dispatch_task_to_session`.
- `backend/claude_hub/services/workspace_manager.py:2031` starts `create_report`.
- `backend/claude_hub/services/workspace_manager.py:2102` starts `_after_report_recorded`.
- `backend/claude_hub/services/workspace_manager.py:2369` starts `_handle_review_report`.
- `backend/claude_hub/services/workspace_manager.py:2680` starts `_refresh_session_statuses`.
- `backend/claude_hub/services/workspace_manager.py:3056` starts report/runtime mapping helpers.
- `frontend/src/components/AgentWorkspaceView.vue:1719` starts review display derivation helpers.
- `frontend/src/components/AgentWorkspaceView.vue:2042` starts agent runtime display derivation helpers.

### Frontend Status Derivation

- `frontend/src/stores/workspaceStore.ts` treats the backend board as source of truth and sends mutations through backend APIs.
- `frontend/src/components/AgentWorkspaceView.vue` renders columns from `WorkspaceTaskStatus`.
- Because `review` is overloaded, functions such as `reviewStatusLabel`, `activeReviewBadge`, `awaitingHumanAcceptance`, `hasBlockingReviewResult`, `canMarkDoneTask`, and `canRequestReviewTask` derive sub-state from timestamps and latest `review_*` report.
- Agent cards prefer live terminal status over persisted session status through `agentRuntimeStatus`, `agentRuntimeText`, and `agentRuntimeDetail`.

This is acceptable for display, but it means frontend action eligibility duplicates some backend policy knowledge. The backend still rejects invalid actions, but reviewers have to reason across UI helpers and backend transition logic to know the actual lifecycle.

## Stability Assessment

### What Is Stable Enough

- The enum vocabulary is explicit and serializable.
- Persistence is defensive: legacy task/session/report fields are normalized during load.
- Dispatch has a workspace-level lock, which addresses the highest-risk duplicate-dispatch race.
- The review loop has meaningful regression coverage: completed reports creating reviewers, gate states triggering review, skip-review constraints, manual review after skipped review, review pass/fail semantics, busy original-agent rejection, queue release after approval, and idle auto-continue behavior.
- Frontend status display is defensive for older payloads and refreshes both board and terminal status in workspace status flows.

### Design Debt And Risk

1. Transition ownership is too scattered.

   The same task/session fields are changed by `update_task`, `_dispatch_task_to_session`, `create_report`, `_request_goal_packet_supplement`, `_mark_task_review_skipped`, `_request_task_review`, `_handle_review_report`, `_refresh_session_statuses`, `_auto_continue_stopped_task`, `_reconcile_task_report_statuses`, `_release_task_session`, and `_release_reviewer_session`. Most individual branches are reasonable; the aggregate is hard to audit.

2. `WorkspaceTaskStatus.REVIEW` is overloaded.

   It currently covers human acceptance, AI review in progress, AI review passed, AI review failed, skipped review, and reviewer-needs-input. The frontend has to consult `review_requested_at`, `review_completed_at`, `review_skipped_at`, `human_acceptance_requested_at`, and the latest review report to choose labels/actions. This is manageable now but brittle as more review outcomes are added.

3. Report-state mapping is implicit.

   `create_report` first maps `completed`, `ready_for_review`, `blocked`, and `needs_input` to broad task/session statuses, then `_after_report_recorded` applies reviewer policy. That two-step path is correct today, but the semantics are not visible in a single place. For example, a completion report initially maps the task to `working`, then it either requests AI review or marks review skipped.

4. Runtime observations can override semantic session status.

   `_refresh_session_statuses` maps terminal observations directly into `ManagedSessionStatus`, then layers special cases on top. This is useful for liveness, but it blurs "terminal looks idle" with "task is semantically idle". The existing grace checks and tests reduce obvious regressions, yet future changes can easily reopen or release tasks accidentally.

5. Manual task status updates are broad.

   `PATCH /api/workspaces/tasks/{task_id}` can set any `WorkspaceTaskStatus` and relies on `update_task` side effects rather than a transition policy. That is useful for operations, but it bypasses some of the richer report/review semantics unless used carefully.

6. Concurrency is partially addressed.

   Dispatch is locked, but report creation, board refresh reconciliation, manual status updates, and runtime status refresh can still interleave. Python's single-process in-memory manager reduces the practical risk, but a future multi-worker deployment or persisted DB backend would need explicit optimistic concurrency or event ordering.

## Recommendation

Introduce an explicit state-machine layer for Agent Workspace lifecycle policy, but keep it small and event-driven.

Do not introduce a broad "one machine for everything" abstraction. Terminal status detection, tmux process lifecycle, managed session lifecycle, task lifecycle, review lifecycle, and human acceptance are different layers with different evidence quality. The most useful design is a workspace lifecycle reducer that accepts explicit events and returns state patches plus side effects.

Recommended event boundary:

- `TASK_CREATED`
- `TASK_STARTED`
- `DISPATCH_TARGET_SELECTED`
- `TASK_DISPATCHED`
- `AGENT_REPORT_RECEIVED`
- `REVIEW_REQUESTED`
- `REVIEW_REPORT_RECEIVED`
- `HUMAN_ACCEPTED`
- `HUMAN_REQUESTED_CHANGES`
- `RUNTIME_OBSERVED`
- `AUTO_CONTINUE_LIMIT_REACHED`
- `SESSION_DELETED`
- `TASK_DELETED`

Recommended reducers:

- `task_lifecycle_transition(task, event, context) -> TaskPatch`
- `session_lifecycle_transition(session, event, context) -> SessionPatch`
- `review_lifecycle_transition(task, report, context) -> ReviewDecisionPatch`

Recommended side-effect boundary:

- Reducers should not send tmux messages, create reviewer sessions, write task records, or call git.
- They should return an action list such as `SEND_ASSIGNMENT_PROMPT`, `REQUEST_REVIEW`, `SEND_REVIEW_PROMPT`, `WRITE_TASK_RECORD`, `SEND_AUTO_CONTINUE_PROMPT`, or `RELEASE_SESSION`.
- `WorkspaceManager` remains the orchestrator that persists patches and executes side effects.

Recommended state model:

- Keep current serialized `WorkspaceTaskStatus` for compatibility.
- Add an internal derived "phase" or "substate" concept before changing persisted schema:
  - task phase: `draft`, `queued`, `executing`, `ai_review_pending`, `ai_reviewing`, `changes_requested`, `awaiting_human_acceptance`, `done`.
  - session phase: `spawning`, `available`, `assigned`, `waiting_input`, `offline`, `terminal_stopped`.
  - review phase can be derived from task timestamps and latest review report in v1 of the refactor.
- Once proven, consider persisting explicit `task_phase` or `review_phase` in a later migration.

## Rollout Plan

1. Document the current transition table.
   - Create a table of event, source conditions, target task state, target session state, side effects, and tests.
   - This can live beside the workspace manager or in docs first.

2. Extract pure mapping helpers.
   - Move `_status_from_report`, `_runtime_from_report`, `_task_status_from_report`, review-skip eligibility, and runtime-observation mapping into a small `workspace/state_policy.py`.
   - Add unit tests that call policy functions directly without FastAPI or tmux mocks.

3. Add transition-result objects.
   - Return explicit `TaskPatch`, `SessionPatch`, and `SideEffect` values from policy functions.
   - Keep `WorkspaceManager` responsible for I/O and persistence.

4. Convert report and review paths first.
   - `create_report` and `_after_report_recorded` are the highest-value conversion points because they encode most semantic workflow policy.
   - Preserve existing report-state compatibility.

5. Convert dispatch and runtime observation paths next.
   - Dispatch can stay lock-protected but should call the policy layer for assignment eligibility and queued/working patches.
   - Runtime observations should become events that policy can ignore, accept, or downgrade based on semantic task state.

6. Only then consider persisted schema changes.
   - If frontend helpers remain complex after policy extraction, add explicit backend-derived fields such as `task_phase`, `review_phase`, and `available_actions` to `WorkspaceBoard`.
   - That would reduce duplicated frontend action logic without breaking existing persisted records.

## Test Plan For Refactor

Keep `backend/tests/test_workspaces.py` as integration coverage, but add narrow tests for:

- Legal and illegal task transitions.
- Report-state to task/session transition results.
- Review skip/request routing decisions.
- Runtime-observation handling for working, idle, attention, offline.
- Queue dispatch eligibility while review is pending, passed, failed, or skipped.
- Manual `done` acceptance only when human acceptance is available or when the operator intentionally overrides.
- Replay/reload normalization of legacy records into derived phases.

## Answer To The Product Question

The current implementation is stable enough for the current feature set because it has explicit enums, defensive persistence, targeted guards, and substantial regression tests. It is not yet elegant as a lifecycle architecture. The behavior has grown into a state machine, but the machine is implicit.

Making the Agent Workspace lifecycle explicit would improve maintainability and reviewer confidence. The refactor should be incremental and should avoid over-modeling ttyd/tmux runtime detection. Start with a pure state-policy module and report/review transitions, then use that as the place where new workflow behavior is added.
