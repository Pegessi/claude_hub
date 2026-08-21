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
| `author`, `recipient` | Run ids. `recipient` is **mandatory**: every event is directed to exactly one run. Root runs (no supervisor) self-address their events (`recipient = run.supervisor_id or run.id`). |
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

`_append_event` calls `_wake_for_run(agent_run_id, recipient)` which sets the
`asyncio.Event` **only on the named recipient**. Since `recipient` is mandatory,
root runs self-address their events (`recipient = run.supervisor_id or run.id`),
so a root run's own reports wake itself.

With recipient-directed mailbox reads, a run only sees events where
`recipient == run_id`. Therefore only the named recipient needs to be woken;
waking ancestors would cause spurious wakeups for runs that cannot see the
event. The `_wake_ancestors` method is retained as dead code for potential
future broadcast use but is no longer called.

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
    running session. **Fail-closed delivery with at-most-once paste per
    call_id per tmux session lifetime**: `send_session_message` maintains
    a four-phase outbox on the `ManagedSession`:
    `pending_call_ids → processing_call_ids → delivered_call_ids` (plus
    `uncertain_call_ids` for ambiguous failures).
    - `pending_call_ids`: persisted but not yet sent to tmux.
    - `processing_call_ids`: claimed by the receiver pump
      (`pending → processing`) **before** the tmux send
      (persist-intent-before-side-effect). The message has been sent to
      the tmux input buffer and awaits the worker's ACK.
    - `delivered_call_ids`: ACKed by the worker (listed in `acked_call_ids`
      of a report). Only the worker's ACK moves a call_id here — the ACK
      is the durable commit.
    - `uncertain_call_ids`: the tmux send failed ambiguously (we cannot
      tell whether the paste ran). Fail-closed: the Hub does NOT
      auto-resend (could duplicate) and does NOT silently mark delivered
      (could lose). An explicit operator retry via
      `retry_uncertain_delivery` is required.

    A `call_id` already in `processing_call_ids` or `delivered_call_ids`
    is skipped; otherwise it is persisted to `pending_call_ids` (with the
    message body in `pending_messages[call_id]`) before the pump delivers
    it.

    **Persist-intent-before-side-effect + tmux receipt**: the pump moves
    the call_id from `pending` to `processing` **before** sending to
    tmux. The tmux send is gated by a tmux-server-side receipt
    (`@receipt_<sha256(call_id)[:16]>` session option, set atomically
    with the paste): a second same-call_id paste to the same tmux
    session is a no-op once the receipt is set. This gives at-most-once
    paste per call_id per tmux session lifetime.

    On a **pre-side-effect** failure (tmux write not attempted) the
    call_id rolls back to `pending_call_ids` for retry. On an
    **ambiguous** failure (tmux write may have succeeded) the call_id
    moves to `uncertain_call_ids` — fail-closed.

    On cold restart, `processing_call_ids` are reconciled against the
    tmux receipt:
    - receipt present on a LIVE session → keep `processing` (the paste
      definitely ran; no repaste).
    - receipt absent on a LIVE session → move back to `pending` for one
      re-delivery (the paste definitely did not run).
    - session gone / unqueryable / STOPPED → move to `uncertain` (fail
      closed; operator retry required).

    The `[call_id:<id>]` marker embedded in the message text lets the
    worker correlate its ACK to the call_id. The Hub does NOT guarantee
    exactly-once: the tmux receipt is per-session-lifetime, and a
    destroyed-and-recreated session (same name) loses the receipt, so
    cold recovery may re-deliver once (receipt absent on the new LIVE
    session).
    **Durable ACK (call-specific, Hub-enforced)**: when the worker submits
    a report for the task, `create_report` automatically includes the
    dispatch call_id (`f"dispatch:{task_id}:{dispatch_attempt}"`) in the
    ACK set — submitting a report proves the worker processed the
    assignment. The legacy no-suffix form (`f"dispatch:{task_id}"`) is
    also accepted for backward compatibility. The worker may
    also list additional call_ids (e.g. followups it has processed) in
    `payload.acked_call_ids`. `_ack_call_ids(task_id, session_id, ack_set)`
    moves the call_ids from `pending_call_ids`/`processing_call_ids` to
    `delivered_call_ids` on both the task and the session, and removes the
    message body from `pending_messages`. Unknown or future call_ids (not
    in pending or processing) are silently ignored — this prevents a
    malicious or buggy report from poisoning the delivered set and
    suppressing a real future delivery. Once ACKed, the sender will not
    re-send the call_id.
  - `REVIEW` / `DONE`: `continue_task` to send the task back to working.
  - Task not found: re-create it with the same `agent_run_id` so the run's
    `context_ref` stays valid. **Crash safety**: `run.context_ref` is
    persisted to disk **before** `start_task` is called, so a crash during
    dispatch does not leave the run pointing at the deleted task (which
    would cause a duplicate task on retry). The `call_id` is also marked
    delivered on the new task.
  - **Fail-closed delivery + durable ACK**: for
    `TODO`/`QUEUED`/`REVIEW`/`DONE`, `call_id` is recorded in
    `task.pending_call_ids` (persisted with the task) and embedded in the
    prompt as a `[call_id:<id>]` marker. It stays in `pending_call_ids`
    until the task is dispatched to a working session, at which point
    `send_session_message` delivers it (persist-intent-before-side-effect,
    tmux-receipt-gated at-most-once paste) and claims it
    (`pending → processing`). For `WORKING`, the session-level four-phase
    outbox (above) is the durable sender record. A retry with the same
    `call_id` is a no-op on the sender side if it is already in
    `delivered_call_ids` (ACKed) or `processing_call_ids` (in-flight).
    The `[call_id:<id>]` marker lets the worker correlate its ACK to the
    call_id.
    **Durable ACK (call-specific, Hub-enforced)**:
    when the worker submits a report for the task, `create_report`
    automatically ACKs the dispatch call_id
    (`f"dispatch:{task_id}:{dispatch_attempt}"`; the legacy no-suffix
    `f"dispatch:{task_id}"` is also accepted) and any call_ids listed in
    `payload.acked_call_ids`. `_ack_call_ids` moves
    the call_ids from `pending_call_ids`/`processing_call_ids` to
    `delivered_call_ids` on the task and session. Unknown/future call_ids
    are ignored (no poisoning). This ensures an unrelated report does not
    ACK a pending followup the worker has not yet processed, and a
    malicious report cannot suppress a future delivery. Once ACKed, the
    sender will not re-send the call_id.
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
| `COMPLETED` (DIRECT task) | `completed` |
| `COMPLETED` (REVIEWED task) | `progress` (run waits for reviewer) |
| `REVIEW_STARTED` | `progress` |
| `REVIEW_PASSED` | `completed` (terminal) |
| `REVIEW_FAILED` | `progress` (task returns to WORKING; run is RUNNING) |
| `REVIEW_NEEDS_INPUT` | `blocked` |

The mapping is **reviewed-aware**: for `REVIEWED` tasks, the worker's
`COMPLETED` report does **not** terminate the run — it maps to `progress`
(the run waits for the reviewer). Only `REVIEW_PASSED` (from the reviewer)
emits the terminal `completed` event. For `DIRECT` tasks, the worker's
`COMPLETED` is the terminal event. `REVIEW_FAILED` maps to `progress` (not
`failed`): the task is sent back to `WORKING` for revisions, so the run
returns to `RUNNING`.

This same reviewed-aware mapping is applied during `recover_pending_runs`
when reconciling persisted reports into agent tree events (crash between
report persist and event bridging).

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
4. **`asyncio.Event` per run for `wait()`.** No polling. `_wake_for_run`
   wakes **only the named recipient** (not ancestors) because mailbox reads
   are recipient-directed: a run only sees events addressed to it. Ancestor
   wakeup would be spurious.
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

> **Historical note (superseded delivery semantics):** Rounds 7–31 below
> describe earlier delivery designs (send-first + pane-marker dedupe,
> "exactly-once to the tmux inbox", `STOPPED → pending` re-delivery).
> These have been **superseded** by the fail-closed + tmux-receipt design
> in **Review Round 32** (persist-intent-before-side-effect,
> `pending → processing → delivered` with `uncertain` fail-closed state,
> at-most-once paste per call_id per tmux session lifetime via
> `@receipt_<sha16(call_id)>`, cold-recovery receipt reconciliation).
> The current contract is documented in the **ManagedTaskAdapter** and
> **Integration** sections above and in Round 32 below.

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
- **Production `wait`/`ack` cycle**: `_run_resident_agent` now uses
  `agent_tree.wait(WaitRequest(workspace_id, root_run.id,
  since_sequence=root_run.ack_sequence, timeout_seconds=1.0))` to fetch
  directed events since the cursor, instead of `get_events`. After
  injecting the events into the resident's prompt, it calls
  `agent_tree.ack(workspace.id, root_run.id, max_seq)` to advance the
  cursor to the highest delivered sequence. This makes `wait`/`ack` used
  in the real Resident loop, not just tests.

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
  that `call_id` **and the event's own `payload.message`** (not the run's
  `last_task_message`). The managed-task adapter persists the call_id in
  `pending_call_ids` (or `processing_call_ids` for `WORKING` tasks)
  atomically with delivery (via `_save_state()`). On crash recovery, a
  call_id stranded in `processing_call_ids` is reconciled against the
  tmux receipt (`@receipt_<sha16(call_id)>`):
  - receipt present on a LIVE session → keep `processing` (the paste
    ran; no repaste).
  - receipt absent on a LIVE session → move back to `pending` for one
    re-delivery.
  - session gone / STOPPED / unqueryable → move to `uncertain` (fail
    closed; operator retry required).
  For `WORKING` tasks, delivery is **fail-closed with at-most-once paste
  per call_id per tmux session lifetime**: `send_session_message` embeds
  a `[call_id:<id>]` marker in the message text (for ACK correlation).
  The tmux receipt (not pane capture) is the dedupe enforcement point.
  The `processing_call_ids` set is the durable claim record; the worker
  ACK (`acked_call_ids`) is the durable commit to `delivered_call_ids`.
  This recovers ALL unmatched followups in sequence order, not just the
  latest. After replaying followups, recovery still reconciles the run's
  status via the adapter's `get_status()` (the followup replay does not
  skip reconciliation).
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
  network requests (auth disabled) skip the check. **Non-local requests
  without a session cookie fail closed with 403** (they do not fall through
  to the local-network no-auth path). **STOPPED or deleted `ManagedSession`s
  are treated as unauthenticated**: `_is_authenticated_session` returns
  `False` for any session whose `status == STOPPED`, so a stale cookie that
  resolves to a stopped session gets 403 on every action and read (spawn,
  send, followup, wait, ack, interrupt, list_runs, get_events).
- **ManagedSession read scoping**: `list_runs` and `get_run_events` scope
  reads for ManagedSessions to the session's own workspace and the runs it
  owns or supervises (its subtree). Cross-workspace reads return 403;
  same-workspace reads for runs outside the session's subtree return an
  empty list (for `list_runs`) or 403 (for `get_run_events`).
- **`ManagedTaskAdapter.followup` resume**: handles `TODO` (calls
  `start_task`), `REVIEW`/`DONE` (calls `continue_task`), and task-not-found
  (re-creates the task with the same `agent_run_id` so the run's
  `context_ref` stays valid). The task-not-found branch is **idempotent by
  `agent_run_id`**: before creating a new task it looks up an existing task
  whose `agent_run_id == run.id` (same pattern as `spawn`). If found, it
  reuses that task (updates the prompt, persists `context_ref`, starts it if
  `TODO`, and marks the `call_id` delivered) instead of creating a duplicate.
  This makes create+start crash-idempotent: a crash between `create_task`
  and the `context_ref` persist leaves a dangling task; the retry finds it
  by `agent_run_id` and reuses it, so there is exactly one task id and one
  `start_task` side effect.
- **Dispatch crash-idempotency (send → WORKING persist)**:
  `_dispatch_task_to_session` persists `session.task_id = task.id` (and
  saves) **before** sending the assignment prompt. The prompt is sent with
  `call_id = f"dispatch:{task.id}:{task.dispatch_attempt}"`. After the
  send, `task.status` is set
  to `WORKING` and saved. A crash between the send and the WORKING persist
  leaves the session holding a QUEUED task. `_can_dispatch_to` returns
  False for such sessions (they own a non-DONE task), so the normal
  dispatch loop will not re-assign the task to another session. On the
  next `dispatch_workspace` pass, `_recover_queued_task_ownership` detects
  the session holding a QUEUED task and re-sends the assignment prompt
  with the same dispatch call_id. Sender-side dedup applies: if the
  call_id is already in `session.processing_call_ids` (the send completed
  and the pump claimed it before the crash),
  `send_session_message` skips the re-send — the dispatch is
  crash-idempotent, no duplicate prompt. If the call_id is still in
  `pending_call_ids` (crash before the pump claimed it), the
  pump sends it to the tmux inbox exactly once, and the eventual report
  submission ACKs the call_id into `delivered_call_ids`.
- **Durable ACK via report submission (call-specific, Hub-enforced)**:
  `create_report` automatically ACKs the dispatch call_id
  (`f"dispatch:{task_id}:{dispatch_attempt}"`; legacy no-suffix
  `f"dispatch:{task_id}"` also accepted) — submitting a report proves the
  worker
  processed the assignment. The worker may also list additional call_ids
  in `payload.acked_call_ids`. `_ack_call_ids(task_id, session_id, ack_set)`
  moves the call_ids from `pending_call_ids`/`processing_call_ids` to
  `delivered_call_ids` on the task and session, and removes the message
  body from `pending_messages`. Unknown or future call_ids
  (not in pending or processing) are silently ignored — this prevents future-ID
  poisoning, where a malicious report could suppress a real future
  delivery by pre-ACKing a not-yet-sent call_id. A pending followup that
  the worker has not yet processed stays in `pending_call_ids` (or
  `processing_call_ids` for a live session) and is delivered by the pump
  with at-most-once paste per call_id per tmux session lifetime (tmux
  receipt-gated). Once ACKed, the sender will not re-send those call_ids.
- **Terminal guard relaxation**: `FAILED` is truly terminal; `INTERRUPTED`
  and `COMPLETED` may transition back to `RUNNING` via `followup` (resume).

### 3. Adversarial tests + migration guide + E2E + UI follow-up

- 17 new tests added: crash injection (late persist failure keeps in-memory
  state), duplicate delivery (adapter not re-triggered), lost delivery
  (recovery retries adapter), `ResidentRootAdapter` behaviour, followup
  resume (`INTERRUPTED`→`RUNNING`, `COMPLETED`→`RUNNING`, `FAILED` cannot
  resume), API authority (non-owner 403, owner 200), and
  `ManagedTaskAdapter.followup` (starts TODO task, recreates deleted task).
- Round 17 hard-exit + stale-session adversarial tests:
  - `test_working_followup_session_outbox_survives_hard_exit_and_reload`:
    simulates a crash after `send_session_message` sends but before the
    session-level `delivered_call_ids` persist; reloads the manager; verifies
    the `call_id` is in `processing_call_ids`. On reload, the message is
    **not** re-delivered to a LIVE session — the Hub enforces at-most-once
    paste per call_id per tmux session lifetime via the tmux receipt
    (`@receipt_<sha16(call_id)>`), and cold recovery keeps `processing`
    call_ids whose receipt is present. A third call with the same call_id
    is also skipped (sender-side dedup via `processing_call_ids`). This
    proves at-most-once delivery across hard-exit + cold reload for a
    live tmux session: the tmux receipt proves the paste ran, so the Hub
    does not duplicate it. (If the session were gone, the call_id would
    move to `uncertain` — fail-closed.)
  - `test_deleted_task_recreation_persists_context_ref_before_start_task`:
    mocks `dispatch_workspace` to raise after `start_task` persists the
    task's `QUEUED` status; verifies `run.context_ref` was already updated
    and persisted; reloads and confirms no duplicate task is created on
    retry AND `start_task` is not re-invoked (the task is already `QUEUED`,
    so `followup` skips `start_task`). Exactly one task id and one total
    start side effect.
  - `test_stopped_session_rejected_for_all_actions`: sets a session's status
    to `STOPPED` and verifies every action (spawn, send, followup, wait, ack,
    interrupt, list_runs, get_events) returns 403.

## Review Round 27 Fixes (2026-08-19)

Review attempt 27 failed (3/10 AC passing). Three required fixes were
implemented:

### 1. Reviewed-aware report mapping during `recover_pending_runs`

`recover_pending_runs` reconciles persisted reports into agent tree events
(crash between report persist and event bridging). The report-state →
event-type mapping now mirrors `_bridge_report_to_agent_event`:

- For **REVIEWED** tasks, the worker's `COMPLETED` report maps to `PROGRESS`
  (the run waits for the reviewer), **not** the terminal `COMPLETED` event.
- Only `REVIEW_PASSED` emits the terminal `COMPLETED` event.
- `REVIEW_FAILED` maps to `PROGRESS` (the task returns to `WORKING`; the run
  is `RUNNING`).

The mapping is computed per-run by checking
`task.task_mode == WorkspaceTaskMode.REVIEWED`.

**Test**: `test_recover_pending_runs_reviewed_completed_maps_to_progress` —
persists a `COMPLETED` report for a REVIEWED task without bridging the event,
runs `recover_pending_runs`, and asserts the emitted event is `PROGRESS` (not
`COMPLETED`) and the run status is `WAITING`.

### 2. Wake only the named mailbox recipient

`_wake_for_run(agent_run_id, recipient)` now wakes **only** the named
recipient. It no longer calls `_wake_ancestors`. With recipient-directed
mailbox reads, a run only sees events where `recipient == run_id`, so waking
ancestors would cause spurious wakeups for runs that cannot see the event.

`_wake_ancestors` is retained as dead code for potential future broadcast
use.

**Test**: `test_wake_for_run_only_wakes_named_recipient` — creates a
root→child tree, sets `asyncio.Event`s on both, calls `_wake_for_run(child,
child)`, and asserts only the child's event is set (root's is not). Also
verifies self-addressed (`recipient=root.id`) wakes the author.

### 3. Durable receiver pump on cold recovery + cumulative side effects

`recover_pending_runs` moves stranded `processing_call_ids` back to
`pending_call_ids` and re-pumps them. The pane-marker check in the pump
prevents duplicate tmux sends:

1. For every session in the workspace, move `processing_call_ids` back to
   `pending_call_ids` (the claim may have been persisted before or after
   the tmux send — we can't tell without checking the pane).
2. Pump `pending_call_ids` (`_pump_session_messages(session.id)`). For each
   call_id, the pump captures the tmux pane and checks for the
   `[call_id:<id>]` marker:
   - If present → the message was already sent before the crash; move to
     `processing` WITHOUT re-sending.
   - If absent → the message was never sent (crash before send); send it
     to tmux, then move to `processing`.

This covers both crash windows:
- **Pre-send crash** (claim persisted, send not done): marker absent →
  send now. No loss.
- **Post-send/pre-persist crash** (send done, claim not persisted): the
  call_id is in `pending` (not `processing`), marker present → skip send.
  No duplicate.

The only exception is a session whose tmux inbox is gone
(`ManagedSessionStatus.STOPPED`): `_expire_processing_leases` moves the
stranded `processing_call_ids` back to `pending_call_ids` for re-delivery,
because the input buffer was destroyed with the tmux session.

The receiver-verifiable receipt (`delivered_call_ids`) ensures a call_id
the worker already ACKed is **not** re-delivered: only the worker's ACK
moves a call_id to `delivered`, and the pump skips call_ids in
`delivered_call_ids`.

**Test**: `test_cumulative_side_effects_before_ack_and_cold_recovery_pump` —
sends 3 messages (each with a counted, persisted side effect), ACKs only the
first, adds a 4th message as pending (never sent), then cold-restarts.
Asserts the pane-marker check prevents duplicate delivery of the 2 unACKed
`processing` messages (they're already in the tmux inbox), the 4th pending
message is delivered once, and the ACKed one is not.

## Review Round 31 Fixes (2026-08-19)

Review attempt 31 failed (3/10 AC passing). The previous fix (73aa665)
removed duplicate cold replay by converting the pre-send crash window into
permanent loss. Three required fixes were implemented:

### 1. Receiver-verifiable durable inbox/receipt (no loss, no duplicate)

**Problem**: the pump claimed (pending → processing, persist) *before*
sending to tmux. A crash between claim and tmux send left the call_id in
`processing_call_ids` but never sent to tmux. Cold recovery skipped
`processing` call_ids → permanent message loss.

**Fix**: send-first + pane-marker dedupe. The pump now:
1. Captures the tmux pane and checks for the `[call_id:<id>]` marker.
2. If marker present → message was already sent (post-send/pre-persist
   crash); move to `processing` WITHOUT re-sending.
3. If marker absent → send to tmux first; on failure leave in `pending`
   (no claim made, no rollback needed).
4. After successful send (or marker confirmed), move call_id from
   `pending` to `processing` and persist.

A per-session `asyncio.Lock` (`self._pump_locks[session_id]`) serializes
`_pump_session_messages` to prevent concurrent sends of the same call_id.

This covers both crash windows:
- **Pre-send crash** (claim persisted, send not done): impossible — claim
  only happens after send succeeds.
- **Post-send/pre-persist crash** (send done, claim not persisted): call_id
  stays in `pending`, marker present on recovery → skip send. No duplicate.

### 2. Real Resident cycle using wait/ack

**Problem**: `wait`/`ack` were only called from test code. The production
Resident loop used `get_events` directly and never advanced the cursor.

**Fix**: `_run_resident_agent` now uses
`agent_tree.wait(WaitRequest(workspace_id, root_run.id,
since_sequence=root_run.ack_sequence, timeout_seconds=1.0))` to fetch
directed events since the cursor. After injecting events into the
resident's prompt, it calls
`agent_tree.ack(workspace.id, root_run.id, max_seq)` to advance the cursor
to the highest delivered sequence.

### 3. ACK-correlated followup result

**Problem**: the `followup:outcome` event was published with
`delivered: false` but nothing flipped it to `delivered: true`.

**Fix**: `_ack_call_ids` now calls `_emit_followup_delivered_if_followup`
for each ACKed call_id. This method looks up the call_id in
`agent_tree._call_record`; if `action == "followup"`, it emits a
`followup:delivered` event with `payload={"delivered": true,
"followup_call_id": call_id}`. This correlates the followup delivery
proof with the worker's ACK.

## Review Round 32 Fixes (2026-08-21)

### Fail-closed delivery semantics (persist-intent-first + uncertain state)

**Problem**: the Round 31 "send-first + pane-marker dedup" design relied on
tmux pane history to detect already-sent messages. But tmux pane history is
bounded and the `[call_id:<id>]` marker can roll out of the scroll buffer,
so cold recovery would not see it and would re-send the message → duplicate
delivery. Additionally, the design could not distinguish between a
pre-side-effect failure (tmux write not attempted, safe to retry) and an
ambiguous failure (tmux write may have succeeded, retrying could duplicate).

**Fix**: **persist-intent-before-side-effect** with a fail-closed
`uncertain` state. The delivery state machine is now:

```
pending ──persist intent──▶ processing (in-flight) ──worker ACK──▶ delivered
   │                              │
   │         pre-side-effect      │  crash / tmux session gone
   │         failure (rollback)   │  (ambiguous)
   └──────────────────────────────┤
                                  ▼
                            uncertain (fail-closed)
```

- **Persist intent before side effect**: the pump moves the call_id from
  `pending_call_ids` to `processing_call_ids` and persists *before* writing
  to tmux. This is the durable record of our intent to deliver.
- **Pre-side-effect failure** (exception before `_send_tmux_message`, e.g.
  `_ensure_session_ready_for_send` fails): the tmux write was NOT attempted,
  so it is safe to roll the call_id back to `pending_call_ids` for retry.
- **Ambiguous failure** (exception inside `_send_tmux_message`): the tmux
  write MAY have succeeded before the wrapper raised. We cannot prove the
  message was not delivered, so we fail closed: move the call_id to
  `uncertain_call_ids`. We do NOT auto-resend (could duplicate) and do NOT
  silently mark delivered (could lose). A `delivery:uncertain` event is
  emitted to the supervisor.
- **Success**: the call_id stays in `processing_call_ids` (in-flight) until
  the worker ACKs it. Only the worker's ACK moves it to
  `delivered_call_ids`.
- **Cold recovery**: any call_id in `processing_call_ids` at startup is
  moved to `uncertain_call_ids` (fail-closed). We cannot prove the message
  reached the tmux input buffer, so we do NOT auto-resend.

**`DeliveryUncertain` exception**: `send_session_message` raises
`DeliveryUncertain(RuntimeError)` when the call_id is in
`uncertain_call_ids` — fail-closed, no silent auto-resume. This propagates
to the API layer, which catches `RuntimeError` and returns HTTP 400. The
call_id and payload remain persisted in `uncertain_call_ids` /
`pending_messages`.

**Explicit retry (operator-only)**: `send_session_message` does NOT move
an uncertain call_id back to pending. The only path out of `uncertain` is
the explicit `retry_uncertain_delivery` method (exposed as
`POST /sessions/{id}/retry-uncertain`), which queries the tmux receipt:

- **receipt present** → the paste already happened; move the call_id back
  to `processing_call_ids` (no repaste) and nudge Enter via
  `_ensure_submitted_without_repaste` so the TUI accepts the pending input.
- **receipt absent, session alive** → the paste did not run; move the
  call_id back to `pending_call_ids` so the pump re-delivers it once.
- **session gone / unqueryable** → stays `uncertain` (fail-closed).

The retry also emits a durable `delivery:retry_requested` audit event.

**Legacy contract preserved**: the feedback-summary dispatch failure test
(`test_feedback_summary_dispatch_failure_is_visible_retryable_and_delete_safe`)
expects HTTP 400 with "remains visible in Todo" so the task stays retryable.
`DeliveryUncertain` (a `RuntimeError`) propagates from `send_session_message`
→ `_dispatch_task_to_session` → `_start_feedback_summary_task`, which catches
`Exception`, calls `_mark_feedback_summary_retryable` (task → TODO), and
raises `RuntimeError("... remains visible in Todo ...")`. The API returns
400. On retry, the operator calls `retry_uncertain_delivery`, which (receipt
absent) moves the call_id back to pending, the pump re-delivers (succeeds),
and the task moves to WORKING (HTTP 201).

**Why no pane-marker dedup**: tmux pane history is not durable. Relying on
the `[call_id:<id>]` marker for cold-recovery dedup would produce duplicate
deliveries when the marker rolls out of the scroll buffer. The marker is now
only used by the *worker* to correlate its ACK (`acked_call_ids`), not by
the Hub as a receipt or dedup basis.

### Receipt-based at-most-once paste per (call_id, tmux session)

**Problem**: the fail-closed `uncertain` state prevents silent loss and
silent duplication, but it pushes every ambiguous failure to an operator
retry. For the common case — the Hub crashed *after* the tmux paste
succeeded but *before* the Hub recorded success — we can do better: the
tmux server itself can witness the paste and leave a durable (for the
session's lifetime) receipt that cold recovery can query. This lets us
distinguish "paste definitely happened" from "paste definitely did not
happen" from "cannot tell", and only fail closed in the last case.

**Fix**: a tmux-server-side **receipt** — a session user option
`@receipt_<sha256(call_id)[:16]>` — set atomically with the paste. The
send primitive `_send_tmux_message_with_receipt` does:

1. `load-buffer` the message into a named buffer
   `buf_<sha256(call_id + \x00 + tmux_session)[:16]>`.
2. An atomic `if-shell -F` check-and-paste: if the receipt option is
   already set, do nothing; otherwise `paste-buffer` + `send-keys C-m`
   (submit) + `set-option @receipt_<hash> 1`. This is a single tmux
   command, so the receipt is set iff the paste ran.
3. `_ensure_submitted_without_repaste` verifies the input was submitted
   (captures the pane, checks the input box is empty) and nudges `C-m`
   only if needed — never re-pastes the message body.
4. `delete-buffer` the named buffer.

`_query_tmux_receipt(session, call_id)` runs
`show-options -qv @receipt_<hash>`: returns `True` if set, `False` if
absent (rc=0, empty stdout), raises `RuntimeError` if the session is gone.

**Cold recovery with receipts** (`_recover_processing_via_receipt`, called
by the monitor tick and on demand):

- **receipt present** → the paste happened. Keep the call_id in
  `processing` (await worker ACK). Nudge `C-m` via
  `_ensure_submitted_without_repaste` in case the Hub died between the
  atomic paste and the submit verification. Do NOT re-paste.
- **receipt absent, session alive** → the paste did NOT run (Hub died
  before the tmux command, or the command failed before setting the
  receipt). Safe to move the call_id back to `pending` for one
  re-delivery.
- **session gone / unqueryable** → cannot prove either way. Fail closed:
  move to `uncertain`.

`_recover_uncertain_deliveries` (cold start) now only moves
`processing_call_ids` → `uncertain` for **STOPPED** sessions (tmux inbox
gone, receipt unqueryable). **LIVE** (`WORKING`) sessions keep their
processing call_ids so the monitor's receipt reconciliation can decide.

**Real boundary (honesty)**: this guarantees **at-most-once paste per
call_id per live tmux session lifetime**, plus Hub-side durable
envelope/ACK (`pending_messages` + `call_payload_fingerprints` +
`delivered_call_ids`). It does NOT guarantee exactly-once across a
destroyed-and-recreated receiver tmux session: if the session is killed
and a new one starts with the same name, the receipt is gone. If the new
same-name LIVE session is up before the monitor reconciles,
`_recover_processing_via_receipt` sees **receipt absent, session alive**
and moves the call_id back to `pending` for **one safe re-delivery** —
so the message may be pasted twice across the two session lifetimes.
Only when the session is **gone / unqueryable / STOPPED** (tmux inbox
gone, receipt unqueryable) does cold recovery fail-closed to `uncertain`
(operator must retry explicitly via `retry_uncertain_delivery`). The
worker's ACK (`acked_call_ids` → `delivered_call_ids`) is the only
proof the model actually processed the message; the tmux receipt only
proves the bytes reached the tmux input buffer.

**Immutable call_id payload**: `call_payload_fingerprints[call_id]` is a
sha256 over the message text + per-attachment (filename, normalized mime,
sha256(bytes)). Computed on first send and kept forever. A re-send with
the same payload is idempotent at any state; a re-send with a different
payload raises `ValueError` (call_id identifies a single durable
delivery). Legacy state (call_id in a state list without a stored
fingerprint) has its fingerprint backfilled from the persisted envelope;
inconsistent state (non-delivered, no envelope, no fingerprint) raises
`RuntimeError` (fail closed).

**Tests** (`tests/test_tmux_receipt_integration.py`, real tmux server,
UUID-unique session, `cat >> effect_file` records pasted bytes):

- `test_sequential_duplicate_same_call_id_pastes_once`: two sequential
  sends, effect count = 1, receipt = true.
- `test_ten_concurrent_same_call_id_pastes_once`: 10 concurrent sends
  via `asyncio.gather`, effect count = 1, receipt = true.
- `test_pre_send_failure_sets_no_receipt_and_no_paste`: Hub raises before
  the tmux command; receipt = false, effect count = 0.
- `test_post_send_failure_receipt_present_no_repaste_on_recovery`: Hub
  raises after the real send (paste + receipt set). A LIVE
  `ManagedSession` is persisted with the call_id in `processing_call_ids`
  + stored envelope + fingerprint. A fresh `WorkspaceManager` reloads
  state; `_recover_processing_via_receipt` (production receipt query, no
  mock) keeps the call_id in `processing` (not pending, not uncertain)
  and effect count stays 1. `resume_existing_call` and `_pump_session_messages`
  also do not repaste.

## Review Round 33 Fixes (2026-08-21)

### Delivery state-machine correctness

Five correctness fixes so ambiguous/partial failures never leave the
lifecycle terminal or silently lost.

#### 1. `followup` on `DeliveryUncertain` stays non-terminal

`followup` persists the `MESSAGE` intent (run event) **before** the tmux
send. If the send raises `DeliveryUncertain`, the run must NOT be marked
`FAILED` — the intent is durable and the operator retries via
`retry_uncertain_delivery`. The `except DeliveryUncertain` block now logs
a warning and re-raises without `_update_run_status(FAILED)` or a
`FAILED` event.

#### 2. `emit_event` duplicate branch: persist-fail re-raises, wake after commit

The duplicate-`call_id` branch of `emit_event` previously returned
success even if `_persist` failed. Now it calls `self._persist()`
directly and only `_wake_for_run(...)` after the durable commit succeeds;
any persist exception propagates.

#### 3. ACK-vs-retry race: reconcile before delivered event, no swallow, no downgrade

`_emit_followup_delivered_if_followup` now calls
`reconcile_followup_outcome` (which appends `followup:outcome`) **before**
appending the `followup:delivered` event, and does not swallow
persistence exceptions. After the pump, a `call_id` already in
`delivered_call_ids` is never downgraded to `uncertain` (the
receipt-present compensation path checks `cur_session.delivered_call_ids`
first).

#### 4. `_ack_call_ids` transaction ordering: reconcile before delivered mutation

Because `agent_tree._persist` → `_wm._save_state()` saves the **full**
workspace state, the lifecycle reconciliation (outcome event + delivered
event + resident ack) must run **before** the session/task `delivered`
mutation. That way a persist failure leaves disk at `processing` with
the payload intact. The session/task `delivered` mutation is in-memory
only; `create_report`'s final `_save_state` commits it.

#### 5. `reconcile_followup_outcome` delegates terminal semantics to `_update_run_status`

Removed the `run.status not in _TERMINAL_STATUSES` guard. Reconcile now
always calls `_update_run_status(RUNNING, persist=False)`. The existing
transition validator allows `COMPLETED`/`INTERRUPTED` → `RUNNING`
(resume) and refuses `FAILED` → `RUNNING` (truly terminal). A
`DeliveryUncertain` on a completed/interrupted run is therefore
recoverable by operator retry.

### Regression tests (10, `tests/test_agent_tree.py`)

- `test_followup_delivery_uncertain_keeps_run_non_terminal`
- `test_reconcile_resumes_non_failed_terminal_runs` (parametrized:
  WAITING/COMPLETED/INTERRUPTED→RUNNING, FAILED stays FAILED)
- `test_reconcile_persist_fail_in_memory_retained_then_durable`
- `test_retry_uncertain_reconcile_fail_compensates_then_succeeds`
- `test_emit_event_duplicate_persist_fail_reraises_no_wake`
- `test_ack_delivered_event_persist_fail_reload_keeps_processing_and_payload`
- `test_ack_vs_retry_race_delivered_not_downgraded`
- plus three supporting cases.

### Validation (unmasked exit codes)

| Suite | Result | Exit |
| --- | --- | --- |
| `tests/test_agent_tree.py` | 156 passed | 0 |
| `tests/test_workspaces.py` | 133 passed | 0 |
| `tests/test_tmux_receipt_integration.py` | 4 passed | 0 |
| `tests/test_workspace_resident_agent.py` | 60 passed | 0 |

mypy clean on `claude_hub/`; black/isort clean on touched files;
`git diff --check` clean.

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

## Validation Results (2026-08-21 freeze)

### Tests (unmasked exit codes)

| Suite | Result | Exit |
| --- | --- | --- |
| `tests/test_workspaces.py` | 133 passed | 0 |
| `tests/test_agent_tree.py` | 156 passed | 0 |
| `tests/test_tmux_receipt_integration.py` | 4 passed | 0 |
| `tests/test_workspace_resident_agent.py` | 60 passed | 0 |

### mypy (clean cache, `uv run mypy .`)

| Scope | Errors |
| --- | --- |
| Total | 106 (baseline a562ae9: 159) |
| `claude_hub/` (production) | 0 |
| `tests/test_agent_tree.py` | 0 (new this branch) |
| `tests/test_workspaces.py` | 39 (historical baseline) |

All 106 remaining errors are pre-existing in other test files
(`test_recovery_integration_v4v6.py` 47, `test_env_presets.py` 7,
`test_feedback_lessons.py` 7, `test_tabs.py` 2, `test_recovery_real_ttyd.py` 2,
`test_orphan_tab_reconcile.py` 1, `test_ttyd_manager.py` 1). No new errors
introduced by this branch.

### Contract checks (rg)

- `retry_uncertain_delivery`: only defined in `_messaging.py`, exposed via
  `api/workspaces.py`, and exercised in `tests/test_agent_tree.py`. **No
  internal/resident/loop auto-invocation** — fail-closed uncertain state
  requires an explicit operator retry.
- Dispatch producer call_id: `dispatch:{task.id}:{dispatch_attempt}`
  (attempt-scoped) in `_dispatch.py`.
- Dispatch consumer ACK (`_reports.py`): accepts both
  `dispatch:{task_id}:{dispatch_attempt}` (new) and `dispatch:{task_id}`
  (legacy, no attempt suffix) for backward compatibility.
- `send_session_message` fingerprint invariant: always checks
  `call_payload_fingerprints` first; different payload → `ValueError`.
  Recovery paths use the separate `resume_existing_call` (no fingerprint
  comparison, operates on persisted envelope).
- Legacy backfill: call_id in a state list without a fingerprint → derive
  fingerprint from persisted envelope (`list[WorkspaceAttachment]`, read
  from `att.path`); delivered-without-payload → no-op; inconsistent
  (non-delivered state, no envelope) → `RuntimeError`. Fingerprint present
  but call_id absent from all state lists → `RuntimeError` (fail closed).

### Formatting / whitespace

- `black`: 12 files left unchanged.
- `isort`: clean.
- `git diff --check`: clean (no whitespace errors).

## Report intake idempotency (call_id + fingerprint) — 2026-08-21

### Problem

`create_report` persists the report (and the ACKed call_id's
processing→delivered transition) via `_save_state` **before** running
post-commit side effects:

1. `_save_state()` — durable commit (report, task transition, call_id ACK)
2. `_after_report_recorded(...)` — review dispatch (can fail)
3. `_bridge_report_to_agent_event(report, session)` — agent-tree event (can fail)

If step 2 or 3 raises **after** step 1 succeeds, the client receives an
error and retries. Without an idempotency key the retry creates a
**second** report, a **second** task transition, and a **second** bridged
agent-tree event.

### Fix

Mirror the existing message-delivery idempotency pattern
(`call_payload_fingerprints`):

- `AgentReportCreate.call_id` / `AgentReport.call_id`: optional
  client-supplied idempotency key.
- `ManagedSession.report_call_ids: Dict[str, str]`: maps `call_id → report_id`,
  persisted in the same `_save_state` as the report.
- `_compute_report_fingerprint`: sha256 over canonical JSON (sorted keys)
  of the report's **content** fields (state, message, changed_files,
  validation, risks, acked_call_ids, …). Excludes bookkeeping fields
  (`id`, `workspace_id`, `session_id`, `created_at`, `review_cycle`,
  `call_id`).

`create_report` flow with `call_id`:

1. Look up `session.report_call_ids[call_id]`.
2. If found:
   - fingerprint matches → re-run `_after_report_recorded` and
     `_bridge_report_to_agent_event` against the **existing** report,
     then return it. Both side effects are individually idempotent:
     `_after_report_recorded` skips when a review is already in flight
     or a verdict is already recorded for this round;
     `_bridge_report_to_agent_event` uses `call_id=f"report:{report.id}"`
     so `emit_event` deduplicates.
   - fingerprint differs → `ValueError` (a call_id identifies a single
     durable report; reusing it with different content is rejected).
3. If not found: create the report, store `report_call_ids[call_id] = report.id`
   in `session_update`, persist via `_save_state`, then run side effects.

### Invariant

Same `call_id` + same fingerprint → existing report (no duplicate).
Same `call_id` + different fingerprint → `ValueError`.

### Regression test

`test_report_intake_idempotent_after_error_then_retry`:

1. Set up workspace, task, managed session, agent run (context_ref = task_id).
2. Put the dispatch call_id in `processing_call_ids`.
3. Submit a `READY_FOR_REVIEW` report with `call_id` that ACKs the dispatch
   call_id. Monkeypatch `_after_report_recorded` to run the real logic
   then raise `RuntimeError` (simulating error-after-commit).
4. Assert: report persisted (1 report), dispatch call_id moved to
   `delivered_call_ids`, **0** bridged events (bridge never ran because
   `_after_report_recorded` raised first).
5. Retry the **same** report with the **same** `call_id`.
6. Assert: still exactly 1 report, retry returns the existing report's id,
   exactly 1 bridged event (emitted by the idempotent retry path),
   dispatch call_id stays in `delivered_call_ids` (not re-processed, not
   duplicated), `review_attempts` did not double.

`test_report_call_id_reuse_with_different_payload_raises`:

- Submit a report with `call_id`, then submit a second report with the
  **same** `call_id` but a different `message`. Assert `ValueError` and
  no duplicate report.

### Validation (2026-08-21)

| Suite | Result | Exit |
| --- | --- | --- |
| `tests/test_agent_tree.py` | 158 passed | 0 |
| `tests/test_workspaces.py` | 133 passed | 0 |
| `tests/test_tmux_receipt_integration.py` + `test_workspace_resident_agent.py` | 64 passed | 0 |

- `black`: reformatted `_reports.py` (1 file).
- `isort`: clean.
- `mypy claude_hub/models/schemas.py claude_hub/services/workspace_manager/_reports.py`: Success, no issues.

### Migration / rollback

- `report_call_ids` defaults to `{}` on existing sessions; no migration
  needed.
- `call_id` on `AgentReportCreate`/`AgentReport` defaults to `None`;
  existing callers are unaffected.
- Rollback is safe: the new fields are ignored by older code.

### Residual risks

- The fingerprint covers the content fields listed in
  `_compute_report_fingerprint`. If a future field is added to
  `AgentReportCreate`/`AgentReport` and not included in the fingerprint,
  two reports that differ only in that field would be treated as
  identical. Mitigation: keep the fingerprint field list in sync with the
  model (the test `test_report_call_id_reuse_with_different_payload_raises`
  exercises the message field; new fields should get similar coverage).
- The idempotency key is client-supplied. A buggy client that reuses a
  `call_id` across genuinely different reports gets a `ValueError` rather
  than silent corruption — fail-closed.

## Report intake idempotency hardening (2026-08-21)

Reviewer feedback on the initial call_id + fingerprint implementation
identified three gaps: (1) production prompts emitted no `call_id`, so
intake was non-idempotent in practice; (2) `goal_packet` was omitted from
the fingerprint, so a GP revision with the same call_id was silently
dropped; (3) concurrent same-call_id requests could both pass the
existence check and create two reports. This round closes all three.

### Changes

1. **Required `call_id` + legacy compatibility adapter.**
   `create_report` now requires a non-empty `call_id`. If the client
   omits it (legacy), the backend generates a deterministic
   `legacy:<fingerprint[:32]>` call_id from the payload content. Same
   content → same adapter call_id → same report, so legacy paths still
   get idempotency without tracking a call_id.

2. **`goal_packet` in the canonical fingerprint.**
   `_compute_report_fingerprint`'s `content_fields` tuple now includes
   `goal_packet`. Two reports that differ only in their Goal Packet no
   longer collide on fingerprint; reusing a call_id with a revised GP
   correctly raises `ReportCallIdConflict` (HTTP 409).

3. **Atomic claim via per-`(session_id, call_id)` lock.**
   Added `_report_call_locks: dict[str, asyncio.Lock]` to
   `WorkspaceManager.__init__`. `create_report` acquires the lock for
   `f"{session_id}:{call_id}"` before the existence check + report
   creation. Two concurrent same-call_id requests are serialized: the
   first creates the report; the second sees it and either returns it
   (fingerprint match) or raises 409 (mismatch).

4. **HTTP 409 Conflict on call_id reuse.**
   Added `ReportCallIdConflict(ValueError)` exception. The API layer
   catches it and returns HTTP 409 (previously `ValueError` bubbled up
   as HTTP 500). Inheriting from `ValueError` keeps existing
   `pytest.raises(ValueError)` tests passing.

5. **`call_id` in every production report prompt.**
   All report curl examples (worker assignment, reviewer bootstrap,
   reviewer verdict, resident agent, `_report_endpoint_curl` used by
   continue/recovery prompts) now include a `call_id` field and instruct
   the agent to reuse the same call_id when resubmitting the same report
   after a failure or context reload.

### New regression tests

- `test_report_concurrent_same_call_id_creates_one_report`: two
  concurrent `create_report` calls with the same call_id return the
  same report id; exactly one report row exists.
- `test_report_goal_packet_included_in_fingerprint`: two reports
  differing only in `goal_packet` have different fingerprints.
- `test_report_legacy_call_id_adapter_is_deterministic`: a report
  without `call_id` gets a `legacy:` call_id; resubmitting the same
  content returns the existing report.
- `test_production_prompts_include_call_id_in_report_examples`: every
  report curl example in `_prompts.py` contains a `call_id` field.

### Validation (2026-08-21 hardening)

| Suite | Result | Exit |
| --- | --- | --- |
| `tests/test_agent_tree.py` | 162 passed | 0 |
| `tests/test_workspaces.py` | 133 passed | 0 |

- `black`: clean (5 files).
- `isort`: clean.
- `mypy` on modified files: Success, no issues.

## Round 3: durable fingerprint persistence + save-failure rollback + per-cycle call_ids (2026-08-21)

Reviewer round-3 feedback identified three remaining gaps: (1) the canonical
fingerprint was recomputed from the persisted report on each retry rather than
stored at creation time, so a model-serialization drift could silently change
the fingerprint and break idempotency; (2) a `_save_state` failure after
in-memory mutation left the report/session/task in a half-committed state
(in-memory report not on disk → phantom conflict on retry or data loss on
restart); (3) production call_id formats used `{task_id}-{state}`, which
collides across repeated progress/review rounds.

### Changes

1. **Persist the canonical fingerprint at creation time.**
   Added `ManagedSession.report_call_fingerprints: Dict[str, str]` mapping
   `call_id → sha256_hex_digest`. On first report creation, the fingerprint
   computed from the incoming `AgentReportCreate` payload is stored on the
   session. On retry, the persisted fingerprint is the source of truth for
   comparison — we do NOT recompute from the persisted `AgentReport`, because
   the report's serialization could drift from the original payload. This
   guarantees that a retry with the exact same content (including the exact
   Goal Packet) always matches, regardless of model changes.

2. **Pre-commit rollback on `_save_state` failure.**
   `_create_report_under_lock` now snapshots the original session and task
   objects BEFORE any mutation (including `_rename_session_for_task`). If
   `_save_state` raises, the handler:
   - removes the report from `self.reports` if it was newly added,
   - restores `self.sessions[session.id]` to the original session object,
   - restores `self.tasks[task_id]` to the original task object,
   - re-raises the exception.
   Because all mutations replace dict entries with new objects (`model_copy`),
   the original references are untouched and can be restored directly. The
   client then retries with the same call_id; since nothing was persisted, the
   retry creates the report fresh.

3. **Per-cycle call_id identities in all production prompts.**
   Replaced `{task_id}-{state}` call_id formats with `{task_id}-{state}-{n}`
   where `n` is a monotonically increasing integer per logical report. This
   prevents collisions when a worker posts multiple "working" reports across
   review rounds, or when a reviewer runs multiple review rounds. Updated
   examples: worker working/started, reviewer review_started/review_passed,
   Goal Packet report, Goal Packet supplement, and resident heartbeat
   (`resident-heartbeat-<cycle_count>`).

4. **`call_id` in Resident heartbeat and Goal Packet supplement prompts.**
   The resident master-mode heartbeat curl example and the Goal Packet
   supplement curl example (sent to workers when GP evidence is missing) now
   include a `call_id` field.

### New regression tests

- `test_report_call_id_fingerprint_includes_goal_packet`: same call_id +
  identical Goal Packet returns the existing report; same call_id + changed
  Goal Packet raises `ReportCallIdConflict`.
- `test_report_save_failure_rolls_back_in_memory_state`: with `_save_state`
  failing on the first call, the in-memory report/session/task are rolled
  back to their pre-mutation state; a retry succeeds and creates the report.
- `test_report_call_id_idempotent_across_cold_reload`: after persisting a
  report with call_id C, a fresh `WorkspaceManager` loaded from the same
  state root recognizes a retry with the same call_id + same content and
  returns the existing report (proving the fingerprint is on disk, not just
  in memory).

### Validation (round 3)

| Suite | Result | Exit |
| --- | --- | --- |
| `tests/test_agent_tree.py` | 165 passed | 0 |
| `tests/test_workspaces.py` | 133 passed | 0 |

- `black`: clean (4 files).
- `isort`: clean.
- `mypy` on modified files: Success, no issues.
