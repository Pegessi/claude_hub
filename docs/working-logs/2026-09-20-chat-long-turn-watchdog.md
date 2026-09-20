# Chat long-turn watchdog (2026-09-20)

## System overview

`SessionTailer._run_native` owns the native provider push loop for structured
Chat. It publishes normalized provider records, tracks the active turn guard,
and reaps turns that can no longer make progress.

The watchdog had two layers:

1. `STREAM_INACTIVITY_TIMEOUT_S = 600s`: no accepted provider record for ten
   minutes.
2. `MAX_TURN_DURATION_S = 3600s`: an absolute one-hour cap from the first tick
   where `turn_in_flight` was observed.

The absolute cap assumed that any turn longer than one hour was hung. That is
not valid for agent-driven reviews, test suites, builds, and subagent
coordination. A concrete TraeX turn ran from 09:16:10 to 10:16:10 and received
text/thinking/tool records in its final second; the one-hour cap cancelled it
even though the largest event gap in the turn was ~64 seconds.

## Real incident evidence

TraeX tab `b5f3a382…`: the cancelled turn ran exactly 1h and received
text/thinking/tool records in its final second; the largest event gap was ~64s.
This proved the one-hour cap was a false positive on active work.

Cursor Goal tab `f24c55c3…` (`Continue active Goal` turns, Goal
`cba7a5e3…`): four consecutive Goal turns ended the same way — the model
finished its answer, the CLI never sent a terminal record (its stdout stayed
open because the turn had spawned the isolated preview servers / long-lived
shell children), and the silence watchdog cancelled the turn after exactly ten
minutes. All four were recorded as `cancelled`; the Goal controller therefore
paused on every turn and never parsed a checkpoint. The raw Cursor transcript
(`~/.cursor/projects/.../agent-transcripts/<id>.jsonl`) confirms the model's
final assistant block carried a complete trailing
`<goal-status state="complete">…</goal-status>` envelope — the model-side
turn was finished; only the provider's terminal record was missing.

## Design

The fix removes total wall-clock duration as a hang signal. A turn is only
reaped when the model itself is expected to stream and no provider record is
accepted for ten minutes.

Three legitimate external waits suppress the inactivity check:

- Outstanding `tool_call_started` without `tool_call_completed` (shell
  commands, tests, builds, subagents).
- A blocking `approval_required` card before `approval_resolved`.
- TraeX/Codex capacity queueing. A raw `queue/status` record with
  `state` `queued`/`waiting` sets the wait (read in `_note_raw_provider_wait`,
  so it does not leak a field into the persisted STATUS payload); a model
  delta or `state=ready` clears it.

These waits are tracked by stable provider call ids and reset at authoritative
turn start, provider-started turn start, terminal completion, cancellation, and
failure.

### Goal-terminal synthesis

For a **one-shot CLI** (Claude/Cursor, `eof_is_fatal=False`) whose stdout is
held open by a lingering child, the provider may never emit a result record
even after the model output is complete. When the turn is a Goal continuation
and the raw assistant text ends with a complete `goal-status` block (same
trailing contract the Goal controller requires), the tailer:

1. Suppresses the silence watchdog (`_goal_terminal_at`).
2. After `GOAL_TERMINAL_GRACE_S` (60 s) with no provider completion, or on a
   clean one-shot EOF before the grace elapses, publishes a synthesized
   `turn_completed(status=completed)` carrying the sanitized visible text and,
   on the observer copy only, the raw `_goal_protocol_text`.
3. Calls `transport.stop()` to retire the stale one-shot stream **and terminate
   the lingering process/children** (the next send spawns a fresh process),
   resets per-turn state, and lets the Goal observer parse the checkpoint and
   route the Goal (continue/blocked/needs_input/complete).

Synthesis intentionally requires a controller-valid trailing envelope. If the
model never produced one, the controller would mark the Goal failed regardless
of completion path; in that case the existing silence reap (Goal → paused,
resumable) remains the correct fallback rather than pretending success.

### Reviewer corrections (post-implementation)

An independent review found and these were fixed:

- **Persistent transports excluded.** Codex/TraeX are persistent app-servers
  (`eof_is_fatal=True`) that emit their own terminal `turn/completed`. Early
  synthesis there released the guard while the turn still ran, causing
  cross-turn contamination, a duplicate terminal event, and an overlapping turn
  start on auto-continue. Synthesis is gated to one-shot transports only.
- **Grace does not arm during external waits and re-validates the tail.** The
  envelope latch is recomputed on every text delta (a model that keeps writing
  after the block un-latches), and the grace is not considered expired while a
  tool or blocking question is still outstanding.
- **EOF before grace.** A clean process exit with an envelope but no result now
  synthesizes completion instead of marking the Goal failed; a nonzero exit
  still fails.
- **Scan-window parity.** `_GOAL_TAIL_SCAN_CHARS` is pinned to exactly the
  controller's protocol window (`64 KiB + 4096`) so the tailer can never accept
  an envelope the controller cannot parse.
- **Synthetic question tool wait.** Codex/TraeX blocking questions are a
  `TOOL_CALL_STARTED` with no matching completed event; resolving the approval
  now also drops that id from the active-tool set, so the silence watchdog
  re-arms after the answer.
- EOF/failure terminal path clears the per-turn wait sets like the other
  terminal paths.

## Pitfalls

- Do not infer tool activity from UI parts alone. The watchdog must consume
  normalized events before persistence so it works with zero subscribers.
- Tool output often arrives only at process exit. Resetting the inactivity
  clock on `tool_call_started` is insufficient; the check must remain
  suppressed for the full outstanding tool lifetime.
- Durable approval cards can outlive Claude/Cursor turns for late answer
  routing, so blocking-card state is separate from `_pending_approvals`.
- Never synthesize a Goal completion on a persistent app-server; its own
  `turn/completed` is authoritative.

## Validation

- `uv run pytest tests/test_agent_stream.py tests/test_goal_run.py \
  tests/test_goal_protocol.py tests/test_provider_question_protocol.py`
- Full suite (browser-only e2e excluded): 1646 passed, 7 skipped.
- `uv run black --check claude_hub/services/agent_stream/tailer.py \
  claude_hub/services/agent_stream/codex_jsonl.py tests/test_agent_stream.py`
- `uv run isort --check-only ...`
- `uv run mypy claude_hub/services/agent_stream/tailer.py \
  claude_hub/services/agent_stream/codex_jsonl.py`
