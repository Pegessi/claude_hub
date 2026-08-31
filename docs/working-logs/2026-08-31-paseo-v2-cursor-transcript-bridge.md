# 2026-08-31 — Paseo v2 Cursor same-pane transcript bridge

## System overview

Paseo v2 has exactly two views of one agent session: the existing Raw terminal
and a Structured observation timeline. Cursor must use the same interactive
`agent` process for both views; a second provider process would make the two
views describe different conversations.

The verified local Cursor CLI (`2026.08.25-3e8eec8`) writes its same-pane
JSONL under:

```text
<CURSOR_DATA_DIR>/projects/<canonical-cwd-key>/agent-transcripts/<sid>/<sid>.jsonl
```

The bridge is intentionally version-pinned. An unrecognized executable,
schema, data root, cwd, SID, or transcript path leaves Structured unavailable
and keeps Raw usable.

## Module design

- `ttyd_manager.py` derives and validates launch provenance, canonicalizes the
  data directory, and injects the pinned `CURSOR_DATA_DIR` into the child
  process. Custom `HOME` therefore cannot split Cursor's writer from the
  Structured reader.
- `_sessions.py` chooses `terminal_transcript` only for the known CLI version,
  generates one Cursor SID, and persists the exact source binding on both the
  terminal tab and managed session. Unsupported versions remain plain Cursor
  terminal tabs.
- `cursor_cli_transcript.py` validates the exact binding, reads complete JSONL
  snapshots, and normalizes user text, assistant text/tool calls, and
  `turn_ended` states to the provider-neutral stream schema.
- `tailer.py` preserves the append-only store/SSE contract. It emits only a
  newly observed snapshot suffix, keeps stream sequences monotonic, persists
  snapshot identity/kind state across restart, and permits only Cursor's
  observed final-row transition: a trailing `turn_ended` can be replaced by
  the next user row. Other previously published-history rewrites fail closed
  to Raw rather than silently replacing a live timeline.
- `workspace_manager.delete_session` calls `discard_session_stream` before a
  friendly managed-session ID can be reused. This stops any in-memory tailer
  and deletes both its JSONL events and cursor checkpoint.
- `useAgentStream` starts at sequence `-1`, because sequences are zero-based
  and the API cursor is exclusive; otherwise the first turn was invisible.

## Images and validation

Attachments are persisted under the isolated workspace state. The prompt block
states that images are input, not metadata, and requires native image
inspection when the answer depends on them.

An isolated real Cursor run used a separate runtime home, `tmux -L
ch-paseo-e2e`, local backend port `8309`, isolated ttyd base port, and a custom
Cursor data directory. It verified:

1. Raw Cursor received `PASEO_E2E`; its actual transcript became Structured
   events 0/1.
2. SSE delivered new monotonic records and a second real user turn reached
   `run_epoch=2` with `SECOND_CURSOR_E2E`.
3. A screenshot attachment was persisted, Cursor invoked `Read` on the image,
   and answered the visible title `Agent Workspace`. The structured timeline
   contained the user image input, tool call, and response.
4. Session deletion cleared the stream/cursor before the friendly session ID
   was reused.

The preview terminal, isolated tmux server, backend process, and temporary
runtime directory were stopped/trashed after the check; the live 5173/8173
service and default tmux server were not touched.
