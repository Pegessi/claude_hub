# Chat question cards wait indefinitely (no auto-timeout)

Date: 2026-09-07

## Overview

Interactive question cards in the Chat UI (Claude `AskUserQuestion`, Cursor
`AskQuestion`) appeared to "time out": the agent called the question tool, the
card rendered, but within a second or two the agent ended its turn with
framing like "the user skipped/dismissed the question" — even though the user
never clicked anything. There is no Hub-side or card-side timer. The root
cause is the **one-shot `--print` transport** shared by Claude and Cursor:

- Each turn spawns `claude --print …` / `agent --print …`, writes **one** user
  message on stdin, closes stdin (EOF), and reads stream-json until exit.
- In non-interactive mode the CLI auto-declines an interactive question with a
  placeholder tool result ("Answer questions?" for Claude; a "skipped" notice
  for Cursor) instead of blocking for a real answer.
- The agent interprets that placeholder as "user declined/skipped" and ends
  the turn. The card is left dangling on a completed turn.

The fix teaches the agent a shared **interactive-question protocol**: the
placeholder means the question is *still pending in the UI*, not declined. The
agent should end the turn with one short neutral waiting line and stop, so the
card stays interactive until the user clicks it. The user's real answer then
arrives as a follow-up message and the agent continues.

Codex is unchanged: its persistent app-server already blocks the turn on
`requestUserInput` and is answered on the same stdin (see
[2026-09-06-codex-question-approval.md](2026-09-06-codex-question-approval.md)).

## Module design

All changes are in `backend/claude_hub/services/agent_stream/`.

### Shared guidance — `native.py`

- `QUESTION_PROTOCOL_GUIDANCE` — the protocol text (placeholder = still
  pending; don't say declined/skipped; don't re-ask or pick a default; end
  with a neutral waiting line; continue from the follow-up answer).
- `wrap_question_protocol_guidance(text)` / `strip_question_protocol_guidance(text)`
  — wrap the guidance in a unique sentinel block
  (`<<<HUB_QUESTION_PROTOCOL_V1>>>` … `<<<END_HUB_QUESTION_PROTOCOL_V1>>>`)
  and strip it back. `strip` uses `find` (not `startswith`) so it removes the
  block even when a wrapper (e.g. Cursor's `<timestamp>…<user_query>` envelope)
  precedes it, and leaves a malformed (open) block untouched.

### Claude — `--append-system-prompt`

`ClaudeNativeSession._build_command` adds
`--append-system-prompt QUESTION_PROTOCOL_GUIDANCE`. This is clean (not part of
the user message), is not echoed into the transcript, and persists on every
`--resume` turn. Live two-turn spike: turn 1 asks → graceful wait; turn 2
resume with the answer → agent continues, no re-ask.

### Cursor — sentinel-wrapped prompt prefix + adapter strip

The Cursor `agent` CLI has **no system-prompt flag**, so the guidance is
delivered as a sentinel-wrapped prefix on the prompt text in
`CursorCliSession._send_text` (every turn, mirroring Claude's every-turn system
prompt — guarantees the guidance is always present; the strip handles any
pollution). `CursorCliTranscriptAdapter` strips the sentinel block during
user-message normalization so it never reaches the persisted timeline or the
UI.

**Why the strip is needed only on the transcript/snapshot path.** The live
stream-json echo of a user message has *no* top-level `role`
(`{"type":"user","message":{…}}`), so the adapter's `normalize_line` dispatch
drops it before normalization. The transcript **file** user row *does* carry a
top-level `role:"user"` (keys `["role","message"]`), so it is normalized — and
that is where the strip runs.

Import direction stays acyclic: `cursor_cli_transcript.py` imports the strip
helper from `native.py` (the same direction the Codex adapter already uses).

## Key issues / pitfalls

- **Do not mistake this for a Hub timeout.** No timer exists; the "timeout" is
  the CLI's immediate auto-decline on stdin EOF in `--print` mode.
- **Every-turn injection (Cursor) is deliberate.** A first-turn-only prefix
  would be lost on `--resume`; every-turn injection matches Claude's
  `--append-system-prompt` semantics and the strip keeps the transcript clean.
  Token cost is ~897 chars/turn — acceptable.
- **Strip must tolerate the transcript envelope.** Cursor persists user text
  as `<timestamp>…</timestamp>\n<user_query>\n{prompt}\n</user_query>`. A
  `startswith`-based strip would miss the sentinel; `find`-based strip removes
  the block while preserving the envelope and closing tag.
- **Live-validated, not just unit-tested.** A live Cursor spike confirmed the
  agent calls `AskQuestion`, gets auto-declined, and ends with
  "我已经把选择卡片放在上面了——等待你的选择。" (graceful wait, no declined/skipped
  framing); a two-turn resume spike confirmed the agent continues when the
  answer arrives ("你选择了蓝色。确认完成，我们搞定了。") without re-asking.

## Validation

- Backend: 1292 passed, 1 skipped; the single failure
  (`test_recovery_real_ttyd.py::test_real_cold_restart_7tab_bijection`) is a
  pre-existing environmental real-ttyd/tmux failure — it fails identically on
  the clean base with these changes stashed, and it has no `agent_stream`
  dependency.
- `agent_stream` suite: 160 passed (153 existing + 7 new).
- Frontend: 276 unit tests pass; eslint clean; `vue-tsc && vite build` clean.
- black / isort / mypy clean on the changed backend files.
