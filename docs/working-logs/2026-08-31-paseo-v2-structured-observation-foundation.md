# 2026-08-31 — Paseo v2 Backend Structured Observation Foundation (Layer B)

## System overview

Wave 1 of the Paseo v2 structured agent terminal UI adds a **provider-neutral
observation plane** that sits beside the existing raw terminal (Layer A). It
normalizes provider-specific transcripts (Claude JSONL, Codex rollout) into a
common event stream that the future structured UI can render without coupling
to any single provider's wire format.

The plane is **additive**: it never touches `/api/tabs`, `/api/terminal/*`, the
task/report/mailbox APIs, or tmux receipt order. Raw terminal behavior is
unchanged.

## Module design

```
backend/claude_hub/
├── models/
│   ├── agent_stream.py        # AgentStreamEvent, AgentStreamEventType, AgentStreamEventPage
│   └── schemas.py             # StreamCapabilities; ManagedSession gains stream_capabilities,
│                              #   agent_session_id, cursor_transport
├── services/agent_stream/
│   ├── base.py                # AgentStreamAdapter ABC, NormalizeContext, discover_source_cached
│   ├── redaction.py           # server-side redaction (env values, secrets, token literals, truncation)
│   ├── store.py               # append-only per-session JSONL store with monotonic stream_sequence
│   ├── registry.py            # adapter registry; Cursor fail-closed
│   ├── claude_jsonl.py        # Claude JSONL → normalized events
│   ├── codex_jsonl.py         # Codex rollout → normalized events
│   └── tailer.py              # per-session transcript tailer + TailerManager
└── api/
    ├── agent_stream.py        # /stream/capabilities, /events, /wait, /live (SSE), /diagnostics
    └── workspaces.py          # GET /sessions (tab → managed-session mapping)
```

### Event model

`AgentStreamEvent` carries `stream_sequence` (monotonic per session),
`session_id`, `tab_id`, `agent_type`, `type` (one of `AgentStreamEventType`),
`run_epoch`, `call_id`, `payload`, `created_at`, and `redacted`. Events are
persisted as one JSON object per line in
`STATE_ROOT/{workspace_id}/agent_streams/{session_id}.jsonl`.

### Redaction (`redaction.py`)

Runs on every event before it leaves the store to a client:

- **Env value stripping**: keys under `env`, `environment`, `env_vars`,
  `envvars`, `variables`, `env_map` have their values replaced with
  `[REDACTED]` (keys are kept so the UI can show which vars were set).
- **Sensitive key masking**: any key matching `token|secret|password|api_key|
  apikey|authorization|auth|credential|private_key|access_key` has its whole
  value masked.
- **Token literal masking**: regex patterns for `sk-`, `sk-ant-`, `ghp_`,
  `gho_`, `ghs_`, `plat_`, `xox-`, `AKIA`, `Bearer `, `ANTHROPIC_AUTH_TOKEN`
  are replaced with `[REDACTED]` inside string fields.
- **Field truncation**: any string field longer than `MAX_FIELD_CHARS = 4000`
  is truncated to 4000 chars with a `…[truncated]` suffix.

Redaction returns a **new** event and never mutates the input.

### Store (`store.py`)

`AgentStreamStore(workspace_id, session_id)`:

- `append(event)` assigns the next monotonic `stream_sequence` (recovered by
  scanning the file on construction) and appends one JSON line. IO runs in
  `asyncio.to_thread`.
- `read_since(since_sequence, limit)` returns an `AgentStreamEventPage` with
  `events`, `next_sequence`, and `has_more`.
- `count()` and `last_event_at()` support the diagnostics endpoint.
- `replace_all(events)` is reserved for snapshot-style sources (not used by
  the line-based adapters in this wave).

### Registry (`registry.py`)

`_ADAPTERS` maps `AgentType.CLAUDE → ClaudeJsonlAdapter` and
`AgentType.CODEX → CodexJsonlAdapter`. **Cursor is intentionally absent** and
`get_adapter(AgentType.CURSOR)` returns `None`, so `supports_structured` is
`False` and the capabilities endpoint reports `structured=False`.

`get_adapter_for_session(session)` additionally checks `cursor_transport`:
for Cursor sessions, only `"acp"` or `"terminal_transcript"` would resolve an
adapter (neither is wired yet), so default Cursor sessions fail closed.

### Adapters

`AgentStreamAdapter` (ABC in `base.py`) defines `adapter_id`, `schema_version`,
`supports_approval_ui`, `supports_tool_timeline`, `capabilities(session)`,
`discover_source(session)`, `normalize_line(raw, ctx)`, and optional
snapshot hooks.

- **Claude JSONL**: maps `user` message → `TURN_STARTED`, assistant `text` →
  `TEXT_DELTA`, `thinking` → `THINKING_DELTA`, `tool_use` →
  `TOOL_CALL_STARTED` (with `call_id`), `tool_result` →
  `TOOL_CALL_COMPLETED`. Sidechain (`isSidechain`) and `meta` lines are
  skipped.
- **Codex rollout**: maps `user_message` → `TURN_STARTED`, assistant
  `message` → `TEXT_DELTA`, `agent_reasoning` → `THINKING_DELTA`,
  `function_call`/`custom_tool_call` → `TOOL_CALL_STARTED`,
  `function_call_output`/`custom_tool_call_output` → `TOOL_CALL_COMPLETED`,
  `error`/`stream_error` → `ERROR`, `task_complete` → `TURN_COMPLETED`.
  `capabilities()` returns `structured=False` until the rollout file exists.

`discover_source()` prefers the exact `agent_session_id` from the live ttyd
process, falling back to a time-anchored heuristic (`_pick_backfill_session`)
that scans the agent's session directory for the most recent transcript.

### Tailer (`tailer.py`)

`SessionTailer` owns one transcript file per session:

- Polls every `POLL_INTERVAL_S = 1.0s`, opening and closing the file each
  poll (no long-held fd — avoids the fd-leak class of outage).
- Persists a cursor (`{session}.cursor.json`) with `path`, `inode`, `offset`,
  `run_epoch`, `snapshot_source_ids`, `snapshot_digest` so restarts resume
  without re-emitting.
- Handles file rotation (inode change) by resetting offset to 0.
- Emits normalized, redacted events to subscriber queues
  (`SUBSCRIBER_QUEUE_MAX = 2000`).
- `_HARD_FAILED_SESSION_IDS` provides process-local fail-closed: once a
  session's source is unrecoverable, capabilities report `structured=False`.

`TailerManager` is a get-or-create factory with `subscribe(session)`,
`ensure_started(session)`, `get_store(...)`, `hard_failed(session_id)`, and
`stop_all()`. It is lazily constructed in `api/agent_stream.py` with a
`session_getter` that reads from `workspace_manager.sessions`.

### API (`api/agent_stream.py`)

All routes are under `/api/workspaces`:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/sessions/{id}/stream/capabilities` | `StreamCapabilities` (fail-closed `structured=False`) |
| GET | `/sessions/{id}/stream/events` | history replay (`since_sequence`, `limit`) |
| POST | `/sessions/{id}/stream/wait` | long-poll for new events |
| GET | `/sessions/{id}/stream/live` | SSE live stream (hello → events → ping) |
| GET | `/sessions/{id}/stream/diagnostics` | adapter id, tail path, event count, last error |

`GET /api/workspaces/sessions` returns the list of `ManagedSessionPublic` for
tab-to-managed-session mapping (read-only, env values redacted).

### Lifespan

`main.py`'s lifespan `finally` block calls
`_get_tailer_manager().stop_all()` to shut down all tailer tasks cleanly.

## Key issues / pitfalls

- **`workspace_manager` shadowing**: `services/__init__.py` does
  `from .workspace_manager import workspace_manager`, so
  `import claude_hub.services.workspace_manager as wm` binds `wm` to the
  singleton instance, not the package. The store and tests use
  `importlib.import_module("claude_hub.services.workspace_manager")` to reach
  the package and its `STATE_ROOT`.
- **No fd leaks**: the tailer opens/closes the transcript file on every poll
  rather than holding a file handle. This avoids the `Too many open files`
  outage class that previously crashed the shared backend.
- **Cursor fail-closed by construction**: no Cursor adapter is registered,
  and `get_adapter_for_session` returns `None` for default
  `cursor_transport="terminal"`. The capabilities endpoint therefore reports
  `structured=False` and the raw terminal remains the only surface for
  Cursor sessions until an ACP/transcript bridge lands.
- **Redaction is non-mutating**: `redact_event` returns a new event so the
  in-store copy stays intact (the store persists raw normalized events;
  redaction applies at read time).
- **Monotonic sequence recovery**: on store construction, `_recover_next_seq`
  scans the JSONL for the max `stream_sequence` so a restarted tailer
  continues from the right number.
