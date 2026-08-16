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
| `executor_kind` | `managed_task`, `native_subagent`, or `external_job`. |
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
- `send_message` → no-op (messages live in the event stream; the next
  `followup` surfaces them).
- `followup` → `continue_task` if the task is in `review`/`done`, or
  `start_task` if still `todo`.
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
