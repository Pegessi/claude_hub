# Task Graph (agent guide)

Canonical, backend-only guide for **Workspace Task Graph / TaskMailbox**
orchestration. The product surface is **`claude-hub task`** and
`/api/workspaces/{id}/tasks/*` only. For removed legacy orchestration and
one-time migration, see [Migration / removed history](#migration--removed-history).

Default Hub: `http://localhost:8173`. Prefix every recipe with:

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS
```

Local-network callers may omit a session cookie. Non-local callers must send
the Hub session cookie. **Use `claude-hub task`** for shell-driven automation;
REST below is the source of truth.

## CLI

**Task Graph.** `claude-hub task` over `/api/workspaces/.../tasks/*`.
Every `events` / `wait` / `ack` call requires an explicit `TASK_ID`
(`task:<task_id>` consumer). `--call-id` on `followup` only (omit → new UUID,
stderr `call_id=<id>`). `wait` does not ACK unless `--ack`.

```bash
uv run claude-hub --json task create WS_ID --title "Child step" --prompt "Do the sub-step." --parent-task-id PARENT_TASK_ID
uv run claude-hub --json task tree WS_ID
uv run claude-hub --json task tree WS_ID TASK_ID
uv run claude-hub --json task events WS_ID TASK_ID --since-sequence 0
uv run claude-hub --json task wait WS_ID TASK_ID --since-sequence 0 --subtree
uv run claude-hub --json task ack WS_ID TASK_ID SEQUENCE
uv run claude-hub --json task followup WS_ID TASK_ID --message "..." --call-id fu-1
uv run claude-hub --json task abort TASK_ID --reason "superseded"
uv run claude-hub --json task start TASK_ID --target-session-id WORKER_SESSION_ID
```

## Mental model

**Workspace → Task Graph → Session assignment.** One workspace owns one Task
Graph, ordinary managed sessions (worker / reviewer roles on Tasks), reports,
and TaskMailbox events keyed by `task:<task_id>`.

- **Workspace.** Container for tasks, managed sessions, optional resident
  settings (independent periodic agent), and durable state under
  `~/.claude_hub/workspaces/`.
- **Task Graph (canonical).** `WorkspaceTask` nodes linked by `parent_task_id`
  (materialized `root_task_id` / `path`). Supervisors coordinate via TaskMailbox
  consumers (`task:<task_id>` only). Primary commands: `tree`, `events`, `wait`,
  `ack`, `followup`, `abort`, `start`. Durable cursor: `consumer_ack_sequence`
  (per Task).
- **Session assignment.** Each Task names at most one worker (`session_id`) and
  one reviewer (`review_session_id`). Dispatch/start assigns workers with
  `target_session_id`; report intake is fail-closed to those ids. Sessions
  **execute** work; the Task Graph **owns orchestration** (events, cursors,
  parent/child structure). Worker and reviewer are ordinary managed sessions
  bound to a Task — not special runtime kinds.
- **TaskMailbox.** Append-only `TaskEvent` log (`sequence` monotonic). Agents
  wait/ack/followup/abort through Task APIs. Identical `call_id` + payload →
  idempotent replay (**409** on followup conflict).
- **Optional Resident agent.** An independent, long-running optional agent for
  periodic or free-form workspace work. It is **not** part of the Task Graph: no
  root Task, no implicit subtree, no mailbox consumer, and **must not** be bound
  to Tasks via `POST .../tasks/{id}/start` (`target_session_id`). Task
  orchestration uses ordinary worker/reviewer sessions only.
- **`related_task_id`.** Optional session/context reuse hint when starting a
  Task; it is **not** a graph parent (`parent_task_id` owns tree structure).

Lifecycle columns (`todo` / `working` / …) are human-facing board state.
Agents driving automation should use TaskMailbox `wait`/`ack`, not poll board
status as a control plane.

Report intake remains fail-closed: workers match `task.session_id`, reviewers
match `task.review_session_id`; unassigned Tasks (`session_id is None`) cannot
be claimed by the first report — assign via dispatch/start or set `session_id`
on the Task record first.

## Task Graph REST (primary)

Top-level tasks:

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  'http://localhost:8173/api/workspaces/WS_ID/tasks/tree'
```

Subtree (includes root):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  'http://localhost:8173/api/workspaces/WS_ID/tasks/TASK_ID/tree'
```

Create a child Task (explicit graph edge):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST 'http://localhost:8173/api/workspaces/WS_ID/tasks' \
  -H 'Content-Type: application/json' \
  -d '{"title":"Child step","prompt":"Do the sub-step.","parent_task_id":"PARENT_TASK_ID"}'
```

Start on an assigned worker session (`target_session_id` is mandatory for
explicit routing; Hub does not auto-pick a worker for orchestrators):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST 'http://localhost:8173/api/workspaces/tasks/TASK_ID/start' \
  -H 'Content-Type: application/json' \
  -d '{"target_session_id":"WORKER_SESSION_ID"}'
```

Replay / wait / ack (TaskMailbox only):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  'http://localhost:8173/api/workspaces/WS_ID/tasks/TASK_ID/events?since_sequence=0&subtree=true'

curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST 'http://localhost:8173/api/workspaces/WS_ID/tasks/TASK_ID/wait?subtree=true&timeout_seconds=30'

curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST 'http://localhost:8173/api/workspaces/WS_ID/tasks/TASK_ID/ack' \
  -H 'Content-Type: application/json' \
  -d '{"sequence":MAX_SEQ}'
```

Followup (TaskMailbox `FOLLOWUP`; **409** on call_id reuse with different body):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST 'http://localhost:8173/api/workspaces/WS_ID/tasks/TASK_ID/followup' \
  -H 'Content-Type: application/json' \
  -d '{"message":"Continue with the plan.","call_id":"followup-task-1"}'
```

Abort (returns Task to manual control; emits TaskMailbox `ABORT`):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST 'http://localhost:8173/api/workspaces/tasks/TASK_ID/abort' \
  -H 'Content-Type: application/json' \
  -d '{"reason":"operator stop"}'
```

Parent supervisors observe child progress with `subtree=true` on the **parent
Task id** — save each `TASK_ID` and its `consumer_ack_sequence`.

## Session reports

Workers and reviewers POST lifecycle reports to the session surface:

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST 'http://localhost:8173/api/workspaces/sessions/SESSION_ID/reports' \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"TASK_ID","state":"working","message":"Progress update","call_id":"report-progress-1"}'
```

Report intake is idempotent on `call_id` + payload fingerprint (**409** when the
same `call_id` is reused with a different body).

## call_id contract and errors

- Identical retry: **same `call_id` + identical semantic payload** → Hub
  returns the original event/report. Do not change the body and keep the id.
- New operation, payload, or attempt: **new `call_id`**.
- Task followup reuse with a different action, target, or fingerprint:
  **HTTP 409** (`TaskCallIdConflict`).
- Hub **report** intake reuse with a different payload fingerprint: **HTTP 409**.
- Missing Task / session authority: **404** / **403** as appropriate.

Delivery-uncertain: a mailbox paste may land in `uncertain_call_ids` when
tmux send is ambiguous. Operators re-queue with the session API:

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST 'http://localhost:8173/api/workspaces/sessions/SESSION_ID/retry-uncertain' \
  -H 'Content-Type: application/json' \
  -d '{"call_id":"THE_UNCERTAIN_CALL_ID","reason":"operator retry after uncertain paste"}'
```

Success is **204**. Unknown / already-delivered / cross-session ids are 400
or 404.

## Migration / removed history

Legacy Agent Tree orchestration (**removed from runtime**):

- `/api/agent-tree/*` routes and `claude-hub agent-tree` CLI
- Runtime `AgentRun` trees, `resident_root` supervisors, and `agent_run_id`
  on Tasks

**Load-only migration:** the first cold load after upgrade runs
`legacy_state_migration` once per workspace: inherits `parent_task_id` from
linked legacy blobs, projects historical events into `task_events`, lifts ACK
cursors, strips legacy keys, and writes `state.json.pre-migration-backup`.
Runtime code never reads `agent_runs` / `agent_events` after a successful
migrate+save.

**Rollback requires restoring pre-migration backups.** Copy each workspace
`state.json`, workspace `index.json`, and nested workspace directory before
crossing the Task Graph boundary.

**UI boundary:** the web board uses flat task columns and session assignment
only. `related_task_id` is a **session/context reuse hint**, not a Task parent
(`parent_task_id`).

**Cancelled legacy plan (history only):** task `487c630c-4b63-4883-8869-0e38546366c0`
(Resident Root UI) is **[CANCELLED]** — do not treat it as an active follow-up
or product dependency.

Design history (includes removed Agent Tree semantics — **not** current API):
[2026-08-16-agent-tree-durable-mailbox.md](working-logs/2026-08-16-agent-tree-durable-mailbox.md).
See also `CHANGELOG.md` Unreleased.
