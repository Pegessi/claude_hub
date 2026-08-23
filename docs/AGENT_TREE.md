# Task Graph (agent guide)

Canonical, backend-only guide for **Workspace Task Graph / TaskMailbox**
orchestration. `/api/agent-tree/*` and `claude-hub agent-tree` remain a **compat
projection** for legacy linked run ids only — not for new orchestration.
Design history:
[2026-08-16-agent-tree-durable-mailbox.md](working-logs/2026-08-16-agent-tree-durable-mailbox.md).

Default Hub: `http://localhost:8173`. Prefix every recipe with:

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS
```

Local-network callers may omit a session cookie. Non-local callers must send
the Hub session cookie. **Prefer `claude-hub task`** (Task Graph) for all new
orchestration; use `claude-hub agent-tree` only when you already hold a compat
`AgentRun` id from pre-migration state. REST below is the source of truth.

## CLI

**Task Graph (primary).** `claude-hub task` over `/api/workspaces/.../tasks/*`.
Every `events` / `wait` / `ack` call requires an explicit `TASK_ID`
(`task:<task_id>` consumer). `--call-id` on `followup` only (omit → new UUID,
stderr `call_id=<id>`). `wait` does not ACK unless `--ack`.

```bash
uv run claude-hub --json task tree WS_ID
uv run claude-hub --json task tree WS_ID TASK_ID
uv run claude-hub --json task events WS_ID TASK_ID --since-sequence 0
uv run claude-hub --json task wait WS_ID TASK_ID --since-sequence 0 --subtree
uv run claude-hub --json task ack WS_ID TASK_ID SEQUENCE
uv run claude-hub --json task followup WS_ID TASK_ID --message "..." --call-id fu-1
uv run claude-hub --json task abort TASK_ID --reason "superseded"
```

**Agent Tree (compat projection only).** Legacy `/api/agent-tree/*` for
historically linked managed runs and cold replay of persisted AgentRun ids.
`--call-id` on `spawn` / `send` / `followup` / `interrupt`. `POST /ack` has no
`call_id`. **Do not start new orchestration here when a Task id exists.**

```bash
uv run claude-hub --json agent-tree roots WS_ID
uv run claude-hub --json agent-tree runs WS_ID
uv run claude-hub --json agent-tree events RUN_ID --since-sequence 0
uv run claude-hub --json agent-tree spawn WS_ID PARENT --message "..." --agent-type claude
uv run claude-hub --json agent-tree wait WS_ID RECIPIENT --since-sequence 0
uv run claude-hub --json agent-tree ack WS_ID RUN_ID SEQUENCE
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
  `ack`, `followup`, `abort`. Durable cursor: `consumer_ack_sequence` (per Task).
- **Session assignment.** Each Task names at most one worker (`session_id`) and
  one reviewer (`review_session_id`). Dispatch/start assigns workers with
  `target_session_id`; report intake is fail-closed to those ids. Sessions
  **execute** work; the Task Graph **owns orchestration** (events, cursors,
  parent/child structure). Worker and reviewer are ordinary Agents bound to a
  Task — not special runtime kinds.
- **TaskMailbox.** Append-only `TaskEvent` log (`sequence` monotonic). Agents
  wait/ack/followup/abort through Task APIs. Identical `call_id` + payload →
  idempotent replay (**409** on followup conflict).
- **Optional Resident agent.** An independent, long-running optional Agent.
  It is **not** part of the Task Graph: no root Task, no implicit subtree, no
  mailbox consumer, no Agent Tree supervisor role. If it participates in work,
  assign it explicitly on a Task (`POST .../tasks/{id}/start` with
  `target_session_id`) like any other session.
- **Agent Tree (compat projection only).** `/api/agent-tree/*` mirrors linked
  runs for legacy callers (`Task.agent_run_id` links). Ordinary Tasks project to
  `task.id`; runtime resolution uses canonical `Task.agent_run_id` only —
  `context_ref` is a one-shot cold-load backfill hint, not a runtime
  disambiguator. Legacy run blobs are load-only; public APIs fail closed.

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

Start on an assigned worker session (`target_session_id` is mandatory for
explicit routing; Hub does not auto-pick a worker for orchestrators):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST 'http://localhost:8173/api/workspaces/tasks/TASK_ID/start' \
  -H 'Content-Type: application/json' \
  -d '{"target_session_id":"WORKER_SESSION_ID"}'
```

Replay / wait / ack (TaskMailbox only; no AgentRun writes):

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

Create a child Task (explicit graph edge; does not spawn AgentRun):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST 'http://localhost:8173/api/workspaces/WS_ID/tasks' \
  -H 'Content-Type: application/json' \
  -d '{"title":"Child step","prompt":"Do the sub-step.","parent_task_id":"PARENT_TASK_ID"}'
```

Parent supervisors observe child progress with `subtree=true` on the **parent
Task id** — save each `TASK_ID` and its `consumer_ack_sequence`; never a
legacy run id.

## Agent Tree REST (compat projection)

Legacy `/api/agent-tree/*` run-tree APIs below remain for historically linked
managed runs and cold replay. Prefer Task Graph REST above for new
orchestration.

## Runtime boundary

| `executor_kind` | Public spawn today |
| --- | --- |
| `managed_task` | Supported. Claude / Codex / Cursor via `executor_config.agent_type`. |
| `native_subagent` | **HTTP 422** until a real runtime exists. |
| `external_job` | **HTTP 422** until a real runtime exists. |

Legacy executor kinds persisted before Task Graph unification are **load-only**;
public spawn/wait/ack against them fails closed (**HTTP 400** / **404**).

The web board exposes flat task columns and session assignment only; there is no
separate Agent Tree UI in the current product surface. Historical `native_subagent`
notes in the working log are simulator-only.

## Discover, list, replay

List every run the caller may see (compat projection; prefer Task tree APIs):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  'http://localhost:8173/api/agent-tree/runs?workspace_id=WS_ID'
```

Subtree of a known root (`status` is optional: `running`, `failed`, …):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  'http://localhost:8173/api/agent-tree/runs?workspace_id=WS_ID&root_id=ROOT_RUN_ID'
```

Cold replay (events after `since_sequence`, default subtree):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  'http://localhost:8173/api/agent-tree/runs/RUN_ID/events?since_sequence=0&subtree=true'
```

There is no dedicated `/roots` route. `GET /runs` query fields are only
`workspace_id`, `root_id`, `status`.

## Deprecated AgentRun compatibility spawn

**New work must use Task Graph APIs:** create child Tasks via
`POST /api/workspaces/WS_ID/tasks`, start with `.../tasks/TASK_ID/start`,
then `events` / `wait` / `ack` / `followup` on the Task id (see **Task Graph
REST (primary)** above). Do not treat AgentRun spawn as canonical.

The legacy compat endpoint below is for pre-migration callers that already
hold a linked `AgentRun` parent id. `POST /api/agent-tree/spawn` body is
`SpawnRequest`. Response is `AgentRun`. Save `id` (child run) and
`context_ref` (Task id for `managed_task`).

Two legacy compat spawn modes. Do not combine a hardcoded Claude `executor_config` with
an arbitrary existing `session_id`: `validate_session` strictly matches
agent/model/target/env, so a Codex or Cursor session returns **HTTP 400**.

**Config-driven spawn** — send `executor_config`, no explicit `session_id`.
Hub picks a compatible worker:

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST http://localhost:8173/api/agent-tree/spawn \
  -H 'Content-Type: application/json' \
  -d '{
    "workspace_id":"WS_ID",
    "parent_id":"ROOT_RUN_ID",
    "executor_kind":"managed_task",
    "executor_config":{"agent_type":"claude","solo_mode":true,"target":"local"},
    "title":"Investigate flaky test",
    "initial_message":"Reproduce and fix the listed failure.",
    "call_id":"spawn-investigate-flaky-1"
  }'
```

**Explicit-session routing** — `session_id` is set; omit `executor_config`
so Hub derives it (`ManagedTaskAdapter.config_from_session`), or the config
MUST exactly match that session:

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST http://localhost:8173/api/agent-tree/spawn \
  -H 'Content-Type: application/json' \
  -d '{
    "workspace_id":"WS_ID",
    "parent_id":"ROOT_RUN_ID",
    "executor_kind":"managed_task",
    "title":"Investigate flaky test",
    "initial_message":"Reproduce and fix the listed failure.",
    "call_id":"spawn-investigate-flaky-pinned-1",
    "session_id":"EXISTING_ORCHESTRATOR_SESSION_ID"
  }'
```

`session_id` is the **worker-routing** field on `SpawnRequest`, not a field
on `ManagedExecutorConfig`. If you send both, `executor_config` MUST match
the selected session (`agent_type`, `model`, `solo_mode`, `target`, `env`,
`cwd`, remote fields). `validate_session` mismatch is **HTTP 400**.

`native_subagent` / `external_job` spawn is 422. Do not copy historical
simulator payloads.

## executor_config (schema only)

`ManagedExecutorConfig` fields: `agent_type` (`claude`/`codex`/`cursor`),
`model` (Claude/Codex only), `env`, `solo_mode` (default `true`), `target`
(`local`/`remote`), `cwd`, `remote_profile_id`, `remote_cwd`,
`remote_reconnect`. There is **no** Codex-native session-picker field on this
schema. Cursor MUST omit executor_config.model because an explicit model
override is rejected (`ManagedTaskAdapter` raises `ValueError`: `Cursor
executor does not support an explicit model override`; spawn maps that to
HTTP 400).

Claude (Hub-pick default when both `executor_config` and `session_id` are
omitted: Claude, solo, inherit workspace target):

```json
{"agent_type":"claude","model":"claude-sonnet-4-6","solo_mode":true,"target":"local"}
```

Codex (model is optional; do not invent a session id here):

```json
{"agent_type":"codex","model":"gpt-5.4","solo_mode":true,"target":"local"}
```

Cursor (`solo_mode` is still a schema/session field; Cursor CLI runs YOLO and
the Hub solo-mode toggle does not apply). Cursor MUST omit executor_config.model
because an explicit model override is rejected; do not add `"model":"..."`
even if the schema accepts the field:

```json
{"agent_type":"cursor","target":"local"}
```

Remote child: set `target` to `remote` plus `remote_profile_id` /
`remote_cwd` / `remote_reconnect` as needed. `env` is a string map only.

## Wait, ACK, followup, interrupt

Directed wait (`WaitRequest`; empty list on timeout):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST http://localhost:8173/api/agent-tree/wait \
  -H 'Content-Type: application/json' \
  -d '{"workspace_id":"WS_ID","recipient_id":"ROOT_RUN_ID","since_sequence":0,"subtree":true,"timeout_seconds":30}'
```

ACK (`POST` query params; response is `AgentRun`):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST 'http://localhost:8173/api/agent-tree/ack?workspace_id=WS_ID&run_id=ROOT_RUN_ID&sequence=MAX_SEQ'
```

Followup (`FollowupRequest`; resumes the child turn):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST http://localhost:8173/api/agent-tree/followup \
  -H 'Content-Type: application/json' \
  -d '{"workspace_id":"WS_ID","recipient_id":"CHILD_RUN_ID","author_id":"ROOT_RUN_ID","message":"Fix the failing assertion.","call_id":"followup-child-1"}'
```

Interrupt (`InterruptRequest`; `reason` optional):

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST http://localhost:8173/api/agent-tree/interrupt \
  -H 'Content-Type: application/json' \
  -d '{"workspace_id":"WS_ID","run_id":"CHILD_RUN_ID","call_id":"interrupt-child-1","reason":"superseded"}'
```

One-way notify without starting a turn: `POST /api/agent-tree/send` with
`SendRequest` (`workspace_id`, `recipient_id`, `author_id`, `message`,
`call_id`, optional `correlation_id`).

## call_id contract and errors

- Identical retry: **same `call_id` + identical semantic payload** → Hub
  returns the original run/event. Do not change the body and keep the id.
- New operation, payload, or attempt: **new `call_id`**.
- Agent Tree reuse with a different action, target, or fingerprint:
  **HTTP 400** (`ValueError`). This is not 409.
- Hub **report** intake (`POST /api/workspaces/sessions/{id}/reports`) reuse
  with a different payload fingerprint: **HTTP 409**.
- Invalid `managed_task` config (`ValueError` → **HTTP 400**, not 422):
  Cursor `model` override, blank model, env/model conflict, unsupported
  `agent_type`, or pinned-session / `executor_config` mismatch.
- Unavailable executor only (`native_subagent`, `external_job`): **HTTP 422**.
- Authority / missing run: 403 / 404. Spawn may return **429** if the
  adapter signals overload (`RuntimeError`).

Delivery-uncertain: a mailbox paste may land in `uncertain_call_ids` when
tmux send is ambiguous. Do not invent a tree-level retry. Operators re-queue
with the existing session API:

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS \
  -X POST http://localhost:8173/api/workspaces/sessions/SESSION_ID/retry-uncertain \
  -H 'Content-Type: application/json' \
  -d '{"call_id":"THE_UNCERTAIN_CALL_ID","reason":"operator retry after uncertain paste"}'
```

Success is **204**. Unknown / already-delivered / cross-session ids are 400
or 404. Another ambiguous paste stays uncertain (400), not a fake 204.

## Migration / rollback

Forward load is additive. New Task Graph fields on `state.json` / workspace
index:

- **Tasks:** `parent_task_id`, `root_task_id`, `path`, `consumer_ack_sequence`,
  optional persisted `agent_run_id` (compat link to a legacy run id).
- **TaskMailbox:** `task_events`, `task_call_index`, `task_next_seq` in the
  persisted workspace blob (see working log).

Pre-migration workspace index fields (for example deprecated
`workspace:{workspace_id}:resident` consumer keys) are **load/migration-only**.
Runtime TaskMailbox accepts `task:<task_id>` consumers only; legacy keys fail
closed. Cold load runs `migrate_pre_unification_graph` once: inherit missing
`parent_task_id` from linked AgentRun parentage, backfill `agent_run_id` when
unique, lift missing per-Task ACK cursors. Runtime code **never** materializes
Tasks from unlinked AgentRuns.

**Rollback requires restoring pre-migration backups.** Before deploying or
rolling back across the Task Graph boundary, copy each workspace
`state.json`, the workspace `index.json`, and any nested workspace directory.
The first save from an older binary **drops** TaskMailbox events, Task ACK
cursors, parent/root/path fields, and legacy Agent Tree metadata; `call_id`
dedup and mailbox replay cannot be reconstructed without those files.

**UI boundary:** the web board uses flat task columns and session assignment
only. `related_task_id` is a **session/context reuse hint**, not a Task parent
(`parent_task_id`).

**Cancelled legacy plan (history only):** task `487c630c-4b63-4883-8869-0e38546366c0`
(Resident Root UI) is **[CANCELLED]** — do not treat it as an active follow-up
or product dependency.

Details: [working log](working-logs/2026-08-16-agent-tree-durable-mailbox.md)
and `CHANGELOG.md` Unreleased.
