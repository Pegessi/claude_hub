# 2026-11-18 Fix Antigravity false trailing API error after completed reply

## Symptom

On 2026-11-18 at 11:05:14–11:06:41 CST, an Antigravity Chat turn (`ebf25788-1524-45ca-92fd-6e04683c848c` in tab `7f02320d-538b-4024-bab5-2dcf91ce8d97`, conversation `5168cb90-af14-4d60-a821-d00fcb247a2a`, PID `1534928`) executed 4 tool steps (`read_file` at step 1247, `read_file` at step 1248, `run_command` at step 1249, `read_file` at step 1251), streamed its complete multi-paragraph answer (`"红框里的 \`b2-t256\`（\`B=2\`）和 \`b4-t3000-partial\`..."`, stream sequences 1868–1919), and then appended a red error banner at sequence 1920:

```text
API error (attempt 1): request failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF
```

followed by `turn_completed` with `status: "failed"` at sequence 1921.

Inspecting `/home/tiger/.claude_hub/storage.sqlite3` and `/home/tiger/.gemini/antigravity-cli/conversations/*.db` showed the same pattern across multiple completed turns:

- Tab `7f02320d-538b-4024-bab5-2dcf91ce8d97` (conversation `5168cb90-af14-4d60-a821-d00fcb247a2a`): all 4 turns (`turn-1794931077161-myuudxynai`, `f993c7a0-34b3-49ae-abdf-433b64e87ef0`, `ebf25788-1524-45ca-92fd-6e04683c848c`, and `12adf880-2f9c-4e1c-97b4-f992d644d48e`) delivered full post-tool or direct answers and then appended the identical `API error (attempt 1): ... streamGenerateContent?alt=sse: EOF`.
- Tab `bb06d419-ee20-4521-b431-07f940dbb572` (conversation `10eb81ca-2174-4da9-a9fa-fdc66750726b`, turn `d458efaf-9ea0-4b8f-913d-09fd54a2c3e9`) and tab `f29215f0-0524-4991-ab8b-c6519ab4265d` (conversation `00fa32b1-677b-486b-adf0-b37609a1a572`, turns `4cb5ceb8-0f9b-49b9-8849-67bcce19f4dc` and `ec757b8e-3af7-4035-b6b9-f452273ce21a`): delivered complete answers and then appended `API error (attempt 1): request failed: stream idle timeout (5m0s): context deadline exceeded`.

## Root cause

1. **Transient retry steps in `agy` conversation trajectories**:
   - In `/home/tiger/.gemini/antigravity-cli/conversations/5168cb90-af14-4d60-a821-d00fcb247a2a.db`, step `idx=17` (recorded during Turn 1 on 2026-11-17T16:05 UTC) is a `step_type=17` (`ERROR_MESSAGE`) step with `should_show_user=false` (`field 12 = 0` on `ErrorMessage` proto) and `error.short_error = "API error (attempt 1): request failed: Post \"https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse\": EOF"`.
   - `agy` immediately retried in Turn 1 (`idx=18` `PLANNER_RESPONSE`) and completed Turn 1 plus three subsequent turns (`idx=29..1254`). During the 11:05–11:06 CST turn (`ebf25788-1524-45ca-92fd-6e04683c848c`, `idx=1244..1252`), `/home/tiger/.gemini/antigravity-cli/log/cli-20261118_110442.log` confirms all 4 `streamGenerateContent?alt=sse` requests succeeded with zero HTTP errors.
2. **`agy` `steps.BuildJSONOutput` poisons `result` with any historical/pre-retry `ERROR_MESSAGE` step**:
   - In `agy` 1.5.5 (`google3/third_party/jetski/cli/printmode`), `(*session).runTurn` slices the current turn's steps (`turnSteps = allSteps[initialCount:]`) and verifies that `postResponseFailure(turnSteps)` is `nil` (because `LastResponseBearingStepIndex(turnSteps)` is after `idx=17`, or `idx=17` belongs to an earlier turn). `runTurn` returns `nil`, and `runStreamTurn` calls `streamJSONEmitter.EmitFinal(ctx, convID, s.Snapshot(), startTime)` with `status = "SUCCESS"`.
   - However, `EmitFinal` passes `s.Snapshot()` (the entire conversation's steps across all turns) to `printmode.buildResultOutput`, which calls `steps.BuildJSONOutput(convID, steps, startTime, response, nil)`.
   - Inside `steps.BuildJSONOutput`, it unconditionally calls `steps.LastStepError(steps)` -> `steps.LastRunErrorDetails(steps)`, which scans backward over the entire `steps` slice for any step with `errorMessage` (`step_type=17` with `should_show_user == false`) or `status == ERROR`, without scoping to the current turn or checking whether a subsequent `agent_response` succeeded.
   - Finding `idx=17` from Turn 1, `BuildJSONOutput` overwrites `output.Status = "ERROR"` and `output.Error = "API error (attempt 1): ..."`. Back in `buildResultOutput`, the guard `if status == "SUCCESS" && output.Error == ""` sees `output.Error != ""` and leaves `result.status = "ERROR"` and `result.finished_reason = "error"` even though `EmitFinal` was called with `"SUCCESS"` and `result.response` holds the complete reply.
3. **Unconditional `ERROR` handling in `AntigravityEventAdapter._normalize_result`**:
   - During `PollPrintmode`, `agy` emits `step_update` records (`step_type: "error_message"`, `state: "ERROR"`) whenever a new `step_type=17` step occurs in the active turn, but does not re-emit historical steps from earlier turns.
   - `AntigravityEventAdapter._normalize_step` ignored `step_type == "error_message"`, and `_normalize_result` trusted `status_value == "ERROR"` unconditionally—emitting `AgentStreamEventType.ERROR` and `TURN_COMPLETED(status="failed")` even when the turn had delivered a substantive final reply after all tool calls and no `error_message` step occurred after the final `agent_response`.

## Fix

1. **Track post-tool `agent_response` vs. `error_message` step ordering in `AntigravityEventAdapter` (`backend/claude_hub/services/agent_stream/antigravity_events.py`)**:
   - Extend `_UsageState` (per-session turn state reset on `(run_epoch, turn_id)` and `init`) with `post_tool_text` and `saw_error_after_response`.
   - In `_normalize_step`:
     - On `step_type == "agent_response"` with non-empty `text_delta`, append to `post_tool_text` and clear `saw_error_after_response = False` (a new response chunk after an in-turn retry supersedes the earlier retry error).
     - On `step_type == "tool"` (`state in {"ACTIVE", "DONE"}`), reset `post_tool_text = ""` and `saw_error_after_response = False` so pre-tool preamble text never masks a post-tool failure.
     - On `step_type == "error_message"`, set `saw_error_after_response = True`.
2. **Suppress false trailing `result` errors on completed replies while preserving genuine failures**:
   - Add `_is_recovered_turn_result_error(error_message, has_substantive_final_reply=..., saw_error_after_response=...)`:
     - Require `has_substantive_final_reply` (`is_substantive_answer` on the post-tool assistant reply, or on `result.response` when no `step_update` steps were streamed).
     - Never suppress when ` error_message.endswith("(response may be truncated)")` (`agy`'s `postResponseFailure` marker when an error step occurred after partial output) or when `saw_error_after_response` is `True` for a non-retry terminal error.
     - Suppress when `error_message` is a transient `API error (attempt N): ...` retry diagnostic or a stale historical trajectory error (`not saw_error_after_response`) on a turn that produced a substantive final answer.
   - When suppressed, `_normalize_result` omits `AgentStreamEventType.ERROR` and emits `TURN_COMPLETED` with `status="completed"`.
   - Genuine failures—turns with no substantive post-tool reply, turns that fail after a tool call with only pre-tool text, partial responses with `(response may be truncated)`, or terminal errors after the last response—continue to emit `ERROR` and `TURN_COMPLETED(status="failed")`.
