# Agent Tree + Durable Mailbox — Unified Agent-to-Agent Coordination

## Overview

This change introduces a single persistent coordination layer that converges
two previously separate mechanisms:

1. The **Resident Agent** (root supervisor that periodically scans for work).
2. **Managed-task dispatch** (workspace tasks assigned to worker sessions).

Both now live under one **Agent Tree** of `AgentRun` nodes connected by
parent/child delegation, with an append-only **event stream** (durable mailbox)
that carries all inter-agent messages and lifecycle transitions.

The design is inspired by Codex's Agent Tree semantics: a supervisor spawns
child runs, sends them messages, follows up to resume their turn, waits on
directed subtree events, and interrupts them. The Hub owns all lifecycle state;
executors (managed tasks, native subagents, external jobs) only report progress
by emitting events.

## Data Model (`models/agent_tree.py`)

### `AgentRun`

A node in the delegation tree.

| Field | Description |
| --- | --- |
| `id` | UUID. |
| `workspace_id` | Owning workspace. |
| `parent_id` | Direct parent (`None` for root). |
| `path` | Slash-separated ancestor ids (e.g. `root/parent`). Enables subtree scoping via prefix match. |
| `supervisor_id` | The run that spawned this one (usually `parent_id`). |
| `executor_kind` | `managed_task`, `native_subagent`, `external_job`, or `resident_root`. |
| `status` | `pending`, `running`, `waiting`, `blocked`, `completed`, `failed`, `interrupted`. |
| `context_ref` | Executor-specific reference (for managed tasks: the workspace task id). |
| `last_task_message` | Most recent message sent to or from this run. |
| `title`, timestamps | Metadata. |

### `AgentEvent`

An append-only entry in the per-workspace event stream.

| Field | Description |
| --- | --- |
| `sequence` | Monotonic per-workspace integer. Cursor for replay. |
| `call_id` | Client-supplied idempotency key. Duplicate calls return the existing event. |
| `correlation_id` | Optional request/response correlation. |
| `agent_run_id` | Run this event belongs to. |
| `type` | `dispatched`, `started`, `progress`, `heartbeat`, `message`, `tool_wait`, `approval_required`, `blocked`, `failed`, `completed`, `interrupted`. |
| `author`, `recipient` | Run ids. `recipient` may be `None` for broadcast/status events. |
| `payload` | Free-form dict (message text, error, context_ref, …). |
| `created_at` | Timestamp. |

`TERMINAL_EVENT_TYPES = {completed, failed, interrupted}` drive run status
transitions on ingestion.

## Actions (`services/agent_tree.py`)

`AgentTreeManager` exposes:

- **`spawn(req)`** — create a child run under `parent_id`, emit `dispatched`,
  call the executor adapter's `spawn`, then emit `started` (or `failed`).
- **`send(req)`** — append a `message` event to the recipient's mailbox
  without resuming its turn. Updates `last_task_message`.
- **`followup(req)`** — like `send` but also calls the adapter's `followup` to
  resume the executor's turn (e.g. `continue_task` for managed tasks).
- **`wait(req)`** — block until events with `sequence > since_sequence` arrive
  for `recipient_id` (or its subtree when `subtree=True`). Fast-paths if events
  already exist; otherwise waits on a per-run `asyncio.Event` with timeout.
- **`interrupt(req)`** — call the adapter's `interrupt`, set run status to
  `interrupted`, emit `interrupted`.
- **`list_runs(req)`** — return runs filtered by workspace, optional `root_id`
  subtree (prefix match on `path`), and optional `status`.

### Idempotency

Every action takes a `call_id`. `_append_event` checks `_call_index` first; if
the `call_id` was already recorded, the existing event is returned unchanged.
This makes `spawn`, `send`, `followup`, `interrupt`, and executor-emitted events
safe to retry.

### Wait / wakeup

`_append_event` calls `_wake_ancestors(run)` which sets the `asyncio.Event` on
the run and every ancestor up to the root. This means a supervisor that calls
`wait(root_id, subtree=True)` wakes up whenever any descendant emits an event.
The recipient's own event is also set so a run waiting on itself wakes up.

## Executor Adapters (`services/agent_tree_adapters.py`)

The adapter interface is intentionally small:

```
spawn(run, initial_message) -> context_ref
send_message(run, message)
followup(run, message)
interrupt(run, reason)
get_status(run) -> AgentRunStatus
```

### `ManagedTaskAdapter`

Wraps the existing workspace task/session/report flow:

- `spawn` → `create_task` + `start_task` (returns task id as `context_ref`).
  Idempotent: reuses any existing task with `agent_run_id == run.id`.
- `send_message` → no-op (messages live in the event stream; the next
  `followup` surfaces them).
- `followup` → resumes the task based on its current status:
  - `TODO`: append the followup message to the prompt, then `start_task`.
  - `QUEUED`: append the followup message to the prompt (worker picks it up
    when dispatched).
  - `WORKING`: `send_session_message` to deliver the message directly to the
    running session.
  - `REVIEW` / `DONE`: `continue_task` to send the task back to working.
  - Task not found: re-create it with the same `agent_run_id` so the run's
    `context_ref` stays valid.
  - **Exactly-once delivery**: `call_id` is recorded in
    `task.delivered_call_ids` (persisted with the task). A retry with the
    same `call_id` is a no-op.
- `interrupt` → `abort_task`.
- `get_status` → maps `WorkspaceTaskStatus` to `AgentRunStatus`.

### `NativeSubagentAdapter`, `ExternalJobAdapter`

In-memory stubs that satisfy the contract so the coordination layer can be
tested end-to-end without a real subagent runtime or remote job runner.

## Integration

### Report → event bridge (`_reports.py`)

`create_report` calls `_bridge_report_to_agent_event` after persisting the
report. It looks up the run by `context_ref` (task id) and emits an event whose
type is derived from `AgentReportState`:

| Report state | Event type |
| --- | --- |
| `STARTED` | `started` |
| `WORKING` | `progress` |
| `BLOCKED` | `blocked` |
| `NEEDS_INPUT` | `approval_required` |
| `READY_FOR_REVIEW` | `progress` |
| `COMPLETED`, `REVIEW_PASSED` | `completed` |
| `REVIEW_FAILED` | `failed` |
| `REVIEW_NEEDS_INPUT` | `blocked` |

The event's `author` is the run id and `recipient` is the run's supervisor, so
the supervisor's `wait()` picks it up.

### Resident root run (`_workspaces.py`)

`_ensure_resident_root_run` creates a root run
(`executor_kind=managed_task`, `context_ref=session_id`) for the resident's
session if one doesn't exist. This lets the resident act as a supervisor and
receive directed subtree events instead of scanning global reports.

### Persistence (`_persistence.py`, `_state.py`)

`_save_state` adds `agent_runs` and `agent_events` to each workspace's
`state.json`. `_load_nested_state` calls
`agent_tree.load_from_dict(workspace_id, data)` which:

1. Reconstructs all `AgentRun` objects.
2. Sorts events by `sequence` and stores them.
3. Rebuilds `_call_index` from events.
4. Sets `_next_seq[workspace_id]` to `max(sequence) + 1`.

This preserves idempotency and monotonic sequencing across restarts.

## API (`api/agent_tree.py`)

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/agent-tree/spawn` | Create a child run. |
| `POST` | `/api/agent-tree/send` | Append a mailbox message. |
| `POST` | `/api/agent-tree/followup` | Message + resume turn. |
| `POST` | `/api/agent-tree/wait` | Block on directed events (cursor). |
| `POST` | `/api/agent-tree/interrupt` | Interrupt a run. |
| `GET` | `/api/agent-tree/runs` | List runs (subtree/status filter). |
| `GET` | `/api/agent-tree/runs/{run_id}/events` | Replay event stream. |

### Authentication and authority

Agent-tree endpoints do **not** use the global `get_current_user` dependency
(which only accepts human `LoginSession` cookies). Instead they extract the
session id from the request cookie via `_get_session_id` and resolve the
principal in two ways:

- **Human user**: the session id resolves to a valid `LoginSession`
  (`auth.session.get_session`). Human users own every run in the workspace.
- **Agent session**: the session id is a key in `workspace_manager.sessions`
  (a `ManagedSession`). Agent sessions may only act on runs they execute
  (`run.context_ref == session_id`, or for `managed_task` runs, the task's
  `session_id`).

Read endpoints (`list_runs`, `get_run_events`, `wait`) allow any
authenticated principal (human or agent). Mutating endpoints (`spawn`,
`send`, `followup`, `ack`, `interrupt`) require the caller to own the
`author_id` run (or be a human user). Local-network requests skip authority
enforcement.

## Key Design Decisions

1. **Hub owns state, adapters own execution.** Runs and events are the source
   of truth. Executors never mutate `AgentRun.status` directly; they emit
   events and the Hub applies the status transition.
2. **`path` for subtree scoping.** A run's `path` encodes all ancestors, so
   "all descendants of root" is a simple prefix match. No tree traversal
   needed for filtering.
3. **`call_id` for idempotency.** Every action and every executor-emitted
   event carries a `call_id`. Retries are safe because duplicates return the
   existing event.
4. **`asyncio.Event` per run for `wait()`.** No polling. `_wake_ancestors`
   fans out to the supervisor chain so a single event can wake multiple
   waiters at different levels.
5. **`context_ref` decouples runs from executors.** A run doesn't care whether
   it's backed by a managed task, a native subagent, or an external job. The
   adapter translates. This lets the same tree mix executor kinds.

## Tests (`tests/test_agent_tree.py`)

16 tests cover:

- Root run creation.
- Spawn creates child run with correct path/supervisor and emits
  `dispatched` + `started`.
- `call_id` idempotency on spawn.
- `send` appends a `message` event and updates `last_task_message`.
- `followup` resumes the executor's turn.
- `wait` returns existing events immediately.
- `wait` blocks until an event arrives.
- `wait` times out with an empty list.
- `interrupt` sets status and emits `interrupted`.
- `list_runs` scopes to a subtree.
- `emit_event` updates run status from terminal event types.
- `emit_event` is idempotent on `call_id`.
- Report → agent event bridge (managed task report surfaces as a `completed`
  event addressed to the supervisor).
- Save/load round-trip: runs, events, sequence continuity, and call_id
  idempotency survive a restart.
- Concurrent spawns produce distinct run ids.
- Concurrent waits all wake on a single event.

## Review Round 7 Fixes (2026-08-18)

Review attempt 7 failed (1/7 AC passing). Three required fixes were implemented:

### 1. Atomic / recoverable persistence

- **Outbox for interrupt**: the `INTERRUPTED` intent event is persisted
  *before* the adapter's `interrupt` call. If the process crashes between
  persisting the event and the adapter returning, `recover_pending_runs`
  retries `adapter.interrupt` for any run that has an `INTERRUPTED` event
  but is not yet in `INTERRUPTED` status.
- **`rollback_on_error` parameter**: `_append_event` and `_update_run_status`
  accept `rollback_on_error: bool = True`. In the *intent phase* (before the
  adapter side-effect), a persist failure rolls back the in-memory mutation.
  In the *outcome phase* (after the adapter side-effect succeeded),
  `rollback_on_error=False` logs the late save failure but keeps the
  in-memory state that matches the executor's actual state; the durable
  state is reconciled by the next successful persist.
- **Interrupt errors no longer swallowed**: the adapter call's exception is
  re-raised so the caller knows the interrupt did not complete. The
  `INTERRUPTED` event is already persisted, so recovery can retry.

### 2. Subtree boundaries + correct transitions

- **`_validate_messaging_boundary(author, recipient)`**: `send` and
  `followup` enforce that the recipient is the author's supervisor, a run
  in the author's subtree, or the author itself. Cross-subtree (sibling)
  messaging is rejected with `400`.
- **Outbound mixing fix**: `_events_for(subtree=True)` excludes events
  authored by the run itself (unless addressed to itself), so a supervisor
  does not re-read its own sent messages from its mailbox.
- **Terminal status guard**: `COMPLETED`, `FAILED`, `INTERRUPTED` are
  terminal — `_update_run_status` refuses transitions out. `interrupt` is
  a no-op on terminal runs.
- **ACK cursor bounds**: `ack_sequence` only moves forward and may not
  exceed the workspace's current max sequence.

### 3. Resident production readiness

- **Root run before bootstrap**: `_ensure_resident_root_run(workspace.id,
  session_id=None)` is called *before* `ensure_workspace_agent`, so the
  root run exists when the bootstrap prompt is built. After the session is
  created, `_ensure_resident_root_run(workspace.id, session_id=session.id)`
  links the root run's `context_ref` to the resident session id.
- **`ack_sequence` consumption**: the resident bootstrap prompt includes
  the root run's persisted `ack_sequence` as the starting `since_sequence`
  for `wait`. Event injection into the prompt uses
  `since_sequence=root_run.ack_sequence` so only unprocessed events are
  surfaced.

### Production E2E (port 8174, worktree backend)

- Terminal-resident workspace creates root run before session bootstrap.
- Spawn child via `native_subagent` succeeds; `dispatched` + `started`
  events persisted.
- root→child and child→root messages succeed; sibling→sibling `send`
  returns `400` (boundary).
- Child ACK advances `ack_sequence` 0→3; `wait since_sequence=3` returns
  empty. ACK backwards (3→1) returns `400`.
- `interrupt` persists `interrupted` event and sets status `interrupted`.
- `followup` on interrupted run leaves status `interrupted` (terminal
  guard).
- Root subtree mailbox contains 0 root-authored outbound events.

## Review Round 8 Fixes (2026-08-18)

Review attempt 8 failed (0/7 AC passing, 3 partial). Three required fixes were
implemented:

### 1. Intent / Delivery / Outcome protocol (durable per-action)

Split saves (multiple `_persist()` calls per action) are replaced with a
batched in-memory-then-single-persist pattern. Every mutating action
(`spawn`, `followup`, `interrupt`, `send`, `emit_event`) follows three phases:

1. **Intent phase**: mutate in-memory state (run fields, append event) with
   `persist=False`. No side-effects yet.
2. **Delivery phase**: call the executor adapter (`spawn`, `followup`,
   `interrupt`). On adapter failure, mark the run `FAILED` and persist that
   single outcome.
3. **Outcome phase**: apply the result of the adapter call to in-memory state
   (`context_ref`, status `RUNNING`, `STARTED` event) and call `_persist()`
   **once**. If this persist fails, the in-memory state already matches the
   executor's actual state; the error propagates and the next successful
   persist (or restart reconciliation) will catch up.

`_append_event`, `_update_run_status`, and `_set_last_message` accept a
`persist: bool = True` parameter. Callers pass `persist=False` during the
intent phase and call `_persist()` once at the end of the outcome phase.

**Reconciliation on restart** (`recover_pending_runs`): recovery scans the
full event stream for each run and replays every unmatched intent in
sequence order:

- Latest event is `INTERRUPTED` → retry `adapter.interrupt`, set status
  `INTERRUPTED`.
- Every followup `MESSAGE` event (payload `followup=True`) that does NOT
  have a matching `call_id:outcome` event → retry `adapter.followup` with
  that `call_id` and append the outcome event. The adapter is idempotent on
  `call_id` (via `delivered_call_ids` on the task), so a partially delivered
  followup is a no-op. This recovers ALL unmatched followups, not just the
  latest — a crash can leave several followup intents persisted without
  their outcomes.
- Run is `PENDING` with no `context_ref` → retry `adapter.spawn`.
- Run is `PENDING` with a `context_ref` → set status `RUNNING` (spawn
  succeeded but the outcome persist was lost).

### 2. Resident root adapter + API authority + followup resume

- **`ResidentRootAdapter`**: `spawn`, `send_message`, and `followup` are
  no-ops (the resident session is created outside the agent tree and picks up
  mailbox messages on its next periodic cycle). `interrupt` aborts the
  resident session. `get_status` returns `RUNNING` while the session exists.
- **`ExecutorKind.RESIDENT_ROOT`**: new executor kind for the resident root
  run, created by `_ensure_resident_root_run`.
- **API actor authority** (`_assert_authority`): every mutating API action
  (`spawn`, `send`, `followup`, `interrupt`) verifies that the caller's
  session owns the `author_id` run (`run.context_ref == session_id`). Local
  network requests (auth disabled) skip the check.
- **`ManagedTaskAdapter.followup` resume**: handles `TODO` (calls
  `start_task`), `REVIEW`/`DONE` (calls `continue_task`), and task-not-found
  (re-creates the task with the same `agent_run_id` so the run's
  `context_ref` stays valid).
- **Terminal guard relaxation**: `FAILED` is truly terminal; `INTERRUPTED`
  and `COMPLETED` may transition back to `RUNNING` via `followup` (resume).

### 3. Adversarial tests + migration guide + E2E + UI follow-up

- 17 new tests added: crash injection (late persist failure keeps in-memory
  state), duplicate delivery (adapter not re-triggered), lost delivery
  (recovery retries adapter), `ResidentRootAdapter` behaviour, followup
  resume (`INTERRUPTED`→`RUNNING`, `COMPLETED`→`RUNNING`, `FAILED` cannot
  resume), API authority (non-owner 403, owner 200), and
  `ManagedTaskAdapter.followup` (starts TODO task, recreates deleted task).

## Migration / Rollback Guide

### What changes on disk

`state.json` for each workspace gains two new top-level keys:

```json
{
  "agent_runs": [ ... ],
  "agent_events": [ ... ]
}
```

No existing keys are removed or renamed. The change is purely additive.

### Migration (forward)

No manual migration is required. On the first backend restart after deploying
this change:

1. `_load_nested_state` calls `agent_tree.load_from_dict(workspace_id, data)`.
2. If `agent_runs` / `agent_events` are absent (old state), `load_from_dict`
   initializes empty collections.
3. **Historical managed_task root → resident_root migration**: any root run
   (`parent_id is None`) persisted as `executor_kind=managed_task` is
   converted to `resident_root`. This covers runs created before the
   `resident_root` executor kind existed. The migration also links the
   resident root run to the workspace's `resident_agent_session_id` if its
   `context_ref` is null. See `agent_tree.py:load_from_dict` (lines
   1386–1426).
4. The resident root run is created lazily by the workspace manager /
   resident agent on the next workspace access if it does not yet exist.

Existing workspace tasks, sessions, and reports are unaffected. The agent
tree is a new layer on top of the existing task flow.

### Rollback (backward)

Rolling back to a version without the agent tree is safe:

1. Stop the backend.
2. Deploy the previous version.
3. The previous version's `_load_nested_state` ignores the unknown
   `agent_runs` / `agent_events` keys (they are simply not read).
4. No data loss: workspace tasks, sessions, and reports are intact.

The only functional regression is that the resident agent reverts to its
pre-tree behaviour (scanning global reports instead of using the directed
mailbox). The `agent_runs` / `agent_events` keys remain in `state.json` but
are harmless.

### Data safety notes

- `agent_runs` and `agent_events` are append-only / immutable after creation
  (except for status and `last_task_message` updates). A rollback does not
  corrupt them.
- If you want to fully purge agent-tree state after rollback, delete the
  `agent_runs` and `agent_events` keys from each workspace's `state.json`.
  This is optional and not required for correctness.

## Reproducible Resident-Managed-Task E2E

This E2E exercises the full path: resident root run → spawn a managed-task
child → the child task runs and reports → the resident receives the event.

### Prerequisites

- Backend running on `http://localhost:8173` (or your dev port).
- A workspace with the resident agent enabled.

### Steps

1. **Find the resident root run**:
   ```bash
   curl -s "http://localhost:8173/api/agent-tree/runs?workspace_id=<ws_id>" \
     | jq '.[] | select(.executor_kind=="resident_root")'
   ```
   Note the root run `id` (call it `$ROOT`).

2. **Spawn a managed-task child**:
   ```bash
   curl -s -X POST "http://localhost:8173/api/agent-tree/spawn" \
     -H "Content-Type: application/json" \
     -d '{
       "workspace_id": "<ws_id>",
       "parent_id": "'"$ROOT"'",
       "executor_kind": "managed_task",
       "initial_message": "echo hello from managed task",
       "call_id": "e2e-spawn-1"
     }' | jq '{id, status, context_ref}'
   ```
   The response contains the child run `id` and `context_ref` (the workspace
   task id). Status should be `running`.

3. **Wait for the child to complete** (from the resident's perspective):
   ```bash
   curl -s -X POST "http://localhost:8173/api/agent-tree/wait" \
     -H "Content-Type: application/json" \
     -d '{
       "workspace_id": "<ws_id>",
       "recipient_id": "'"$ROOT"'",
       "since_sequence": 0,
       "subtree": true,
       "timeout_seconds": 60
     }' | jq '.[] | {sequence, type, author, payload}'
   ```
   You should see a `completed` event authored by the child run.

4. **Interrupt and resume**:
   ```bash
   # Interrupt the child
   curl -s -X POST "http://localhost:8173/api/agent-tree/interrupt" \
     -H "Content-Type: application/json" \
     -d '{
       "workspace_id": "<ws_id>",
       "run_id": "<child_id>",
       "call_id": "e2e-interrupt-1",
       "reason": "pause"
     }' | jq '.status'
   # -> "interrupted"

   # Resume via followup
   curl -s -X POST "http://localhost:8173/api/agent-tree/followup" \
     -H "Content-Type: application/json" \
     -d '{
       "workspace_id": "<ws_id>",
       "author_id": "'"$ROOT"'",
       "recipient_id": "<child_id>",
       "message": "continue",
       "call_id": "e2e-followup-1"
     }' | jq '.type'
   # -> "message"
   ```
   The child run's status returns to `running`.

### Expected outcomes

- `spawn` creates a workspace task (visible in the workspace's task list)
  and an `AgentRun` with `executor_kind=managed_task`.
- The task's report (when it reaches `COMPLETED` or `REVIEW_PASSED`) is
  bridged to a `completed` event in the agent tree.
- `interrupt` aborts the workspace task; `followup` re-creates or continues
  it.
- The resident root run's `ack_sequence` can be advanced via `POST
  /api/agent-tree/ack`.

## UI Follow-up Task (real, scoped)

**Task ID**: `487c630c-4b63-4883-8869-0e38546366c0`

The backend API is complete. The following UI task is the concrete next
increment:

**Task**: Add an Agent Tree panel to the workspace view.

**Scope**:

1. **Tree list** (`AgentTreePanel.vue`): fetch `GET /api/agent-tree/runs`
   for the workspace, render runs as an indented tree (using `path` for
   hierarchy). Each row shows status badge, executor kind, title, and
   `last_task_message`.
2. **Run detail drawer**: on row click, fetch `GET
   /api/agent-tree/runs/{run_id}/events` and render the event stream
   (sequence, type, author→recipient, payload).
3. **Actions**: for the selected run, expose buttons:
   - **Spawn child** (form: executor_kind, initial_message) → `POST /spawn`.
   - **Send message** → `POST /send`.
   - **Follow up** → `POST /followup`.
   - **Interrupt** → `POST /interrupt`.
4. **Mailbox ACK**: for the resident root run, show `ack_sequence` and a
   button to advance it to the latest event sequence (`POST /ack`).

**Non-goals** (deferred):

- Real-time WebSocket push for new events (poll every 5s for now).
- Native subagent / external job executor kinds in the UI (only
  `managed_task` is selectable).
- Editing run metadata.

**Acceptance criteria**:

- The tree renders all runs for the workspace with correct indentation.
- Clicking a run shows its event stream.
- Spawn creates a child run visible in the tree without a full page reload.
- Interrupt changes the run's status badge to `interrupted`.
- The resident root run's ACK button advances `ack_sequence`.
