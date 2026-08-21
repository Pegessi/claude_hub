# Agent Tree (agent guide)

Canonical, backend-only guide for using the already-reviewed Agent Tree /
durable mailbox. Design history lives in
[2026-08-16-agent-tree-durable-mailbox.md](working-logs/2026-08-16-agent-tree-durable-mailbox.md).
UI is a separate follow-up: task `487c630c-4b63-4883-8869-0e38546366c0`.

Default Hub: `http://localhost:8173`. Prefix every recipe with:

```bash
curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS
```

Local-network callers may omit a session cookie. Non-local callers must send
the Hub session cookie. Do not invent a CLI; there is no `claude-hub agent-tree`
command.

## Mental model

- **Resident / root.** A workspace Resident (when enabled) owns a
  `resident_root` `AgentRun`. That run is the supervisor mailbox.
- **Child run.** `POST /api/agent-tree/spawn` creates an `AgentRun` under
  `parent_id`. For `managed_task`, Hub also creates a workspace Task and
  dispatches it to a worker session. `context_ref` is the Task id.
- **Events / cursor / ACK.** Actions append to a per-workspace event stream
  (`sequence` is monotonic). `wait` returns events with `sequence > since_sequence`
  for `recipient_id` (or its subtree when `subtree=true`). `ack` persists
  `ack_sequence` so a restart resumes there.
- **Hub Task / session vs Agent Tree.** Tasks and sessions are how
  `managed_task` executes. Agent Tree itself is the run tree + event stream;
  you can list/wait/ack runs without creating a new Hub Task by hand. Do not
  treat Task status as a substitute for run events.

Hub owns lifecycle (`pending` / `running` / `waiting` / `blocked` /
`completed` / `failed` / `interrupted`). Agents report via events and
`POST /api/workspaces/sessions/{id}/reports`, not by forging run status.

## Runtime boundary

| `executor_kind` | Public spawn today |
| --- | --- |
| `managed_task` | Supported. Claude / Codex / Cursor via `executor_config.agent_type`. |
| `resident_root` | Created by Hub for the Resident; do not spawn this. |
| `native_subagent` | **HTTP 422** until a real runtime exists. |
| `external_job` | **HTTP 422** until a real runtime exists. |

No Agent Tree UI yet (`487c630c`). Historical `native_subagent` notes in the
working log are simulator-only.

## Discover, list, replay

List every run the caller may see (filter `resident_root` locally for roots):

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

## Spawn

`POST /api/agent-tree/spawn` body is `SpawnRequest`. Response is `AgentRun`.
Save `id` (child run) and `context_ref` (Task id for `managed_task`).

Two spawn modes. Do not combine a hardcoded Claude `executor_config` with
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
MUST exactly match that session. Resident master is explicit-session: `session_id`
is mandatory, so omit hardcoded `executor_config`:

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

Forward load is additive (`AgentRun` / events / fingerprints on
`state.json`). Before rolling back a binary that does not know those fields,
back up each workspace `state.json` and drain writers. The first old-version
save **drops** Agent Tree and report-fingerprint metadata; replay and
`call_id` dedup cannot be recovered without that backup.

Details: [working log](working-logs/2026-08-16-agent-tree-durable-mailbox.md)
and `CHANGELOG.md` Unreleased.
