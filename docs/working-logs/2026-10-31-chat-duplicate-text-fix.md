# 2026-10-31 — Chat Duplicate Text and Sub-agent Accumulator Isolation

## Symptom

In Claude Hub Chat tabs running concurrent sub-agents (`Task` / `Agent` tool calls), main-thread assistant messages and thinking blocks intermittently rendered sentences or trailing phrases twice back-to-back (e.g., tab `a8531444-6718-492e-96f9-2cf4f8e76c68`, turn `9e6d8164-c859-4df2-b345-2f76419cdafe`, `stream_sequence` `6124` and `6127`), or emitted false-positive `assistant final text does not match streamed deltas; cannot safely reconcile` error events.

## Root Cause

1. **Shared `_TurnAccumulator` across main and sub-agent threads**:
   - `AgentStreamAdapter._turn_key` keyed `_TurnAccumulator` only by `(session_id, run_epoch)`.
   - When Claude Code runs with `--output-format stream-json --include-partial-messages`, background sub-agent records (`parent_tool_use_id: "<tool_call_id>"`) interleave on stdout with main-agent `stream_event` deltas and top-level `assistant` snapshots.
   - A sub-agent `message_start` or `assistant` record carrying its own `message.id` overwrote `state.active_provider_message_id` and called `_reset_message_state`, wiping the main agent's accumulated `state.text` and `state.thinking`.
   - When the main agent's final `assistant` snapshot arrived with the main `message.id`, `_begin_provider_message` saw `main_id != sub_id`, reset `state.text` a second time, and `_reconcile_text` / `_reconcile_thinking` re-emitted the entire snapshot verbatim.
   - If the main agent streamed deltas between a sub-agent's `thinking` block and `text` block sharing the same `sub_id`, `_reconcile_text` compared the sub-agent's text against the main agent's delta prefix and emitted a false `ERROR` event on the main turn.
2. **Coalesced tail + snapshot replay bypassed frontend `isExactMultiChunkReplay`**:
   - When the main agent streamed tail fragment `T2` after the sub-agent interruption and immediately followed with the full snapshot `T1 + T2`, `AgentStreamCoalescer` merged `T2` and `T1 + T2` into a single `text_delta` / `thinking_delta` event (`T2 + T1 + T2`).
   - Because `T2 + T1 + T2` does not align with a suffix of prior chunks, `isExactMultiChunkReplay` returned `false`.

## Fix

- **Backend (`base.py`, `claude_jsonl.py`)**:
  - Added `sub_thread_id: Optional[str] = None` to `NormalizeContext` and keyed `AgentStreamAdapter._turn_state` by `(session_id, run_epoch, sub_thread_id)`. `_clear_turn_state` clears all thread accumulators for `(session_id, run_epoch)` when the main turn completes.
  - Updated `ClaudeJsonlAdapter.normalize_line` to bind `sub_thread_id = parent_tool_use_id` on `NormalizeContext` before calling `_normalize_record`, suppress `TURN_STARTED` / `TURN_COMPLETED` on sub-agent `message_start`, `user`, and `result` records, and emit sub-agent reconciliation/result failures as `STATUS` (`provider_status="error"`, attributed to `subagent_thread`).
- **Frontend (`state.ts`, `eventValues.ts`, `reducer.ts`)**:
  - Added `trimSnapshotReplay` to detect exact multi-chunk replays, coalesced `T2 + T1 + T2` tail-plus-snapshot replays (`tailLen > 0`, i.e., `prefixLen < working.length` so legitimate single-delta repeats `>= 8` chars are preserved), and `compact=true` (`_history_chunk_count >= 2`) self-doubled `S + S` or short-prefix replays across `text_delta` and `thinking_delta`, returning only the non-duplicated text.
