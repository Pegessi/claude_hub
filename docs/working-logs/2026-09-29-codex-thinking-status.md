# Codex Chat Thinking/Process Visibility — 2026-09-29

## Symptom

Codex turns in the structured Chat timeline showed **zero** `thinking_delta`
events while the user saw no Thinking/process indication. Turns reported
`usage.reasoning` tokens (e.g. 390 in turn `turn-1790608993214-ahotk70hms`,
tab `ec8a33a7`), proving reasoning happened, but no reasoning content was
visible.

## Evidence: source vs normalized

A live probe against `codex app-server` (CLI 0.156.1, model `gpt-6-sol`,
`reasoningEffort: xhigh`) captured the raw JSON-RPC notifications for a
reasoning turn:

| Raw notification | Present? | Notes |
| --- | --- | --- |
| `item/started` (item.type=`reasoning`) | ✅ | `summary: []`, `content: []` |
| `item/completed` (item.type=`reasoning`) | ✅ | same empty fields |
| `item/reasoning/textDelta` | ❌ | **never emitted** |
| `thread/tokenUsage/updated` | ✅ | `reasoningOutputTokens: 27` |
| `item/agentMessage/delta` | ✅ | text deltas |

The persisted rollout (`~/.codex/sessions/…/rollout-*.jsonl`) confirms the
same: 61 `reasoning` response_items, **all** with empty `summary` and
`encrypted_content` (Fernet `gAAAAAB…`). No real `agent_reasoning` event_msg
or `item/reasoning/textDelta` method exists in any rollout file (earlier
`rg` matches were false positives — the strings appear inside command
output).

**Conclusion: the current Codex provider does not emit displayable reasoning
text.** Reasoning is performed server-side, encrypted for persistence, and
only token counts are exposed.

## Missing boundary

`CodexJsonlAdapter._normalize_tool_item` (handles `item/started` /
`item/completed`) dropped reasoning items: its `else` branch assumed
"Text/reasoning/plan items already arrive as deltas." But the current
app-server emits `item/started`/`item/completed` for reasoning items with no
deltas, so the notifications were silently discarded.

## Fix

When a reasoning item has no displayable text, emit a truthful in-flight
`STATUS`:

- `item/started` → `STATUS` "Thinking…"
- `item/completed` → `STATUS` "Done thinking"

Both carry a stable `message_id` (`reasoning:{item_id}`) + `snapshot: true`,
so the frontend replaces the indicator in place (see `status` case in
`agentStreamTimeline.ts`). No reasoning content is fabricated or exposed.

## Files

- `backend/claude_hub/services/agent_stream/codex_jsonl.py` — reasoning branch + turn/completed finalization
- `backend/tests/test_codex_subthread_attribution.py` — backend regression tests
- `frontend/src/utils/agentStreamTimeline.ts` — subthread `status` part + `applySubthreadEvent` case
- `frontend/src/components/StructuredPane.vue` — subthread status rendering
- `frontend/tests/nestedSubthreadAttribution.test.mjs` — frontend regression tests
- `CHANGELOG.md` — entry

## Review follow-up (cycle 2)

The independent reviewer rejected the first fix on two concrete defects, both
now fixed with backend + frontend regression tests:

1. **Subthread routing**: the reasoning branch returned before
   `_resolve_sub_thread`, so a child's `STATUS` got no `subagent_thread` and
   appeared on the main stream. The branch now resolves the sub-thread and
   passes `sub_thread_id`; the frontend `applySubthreadEvent` has a new
   `status` case that replaces a child status in place by `messageId`.
2. **Stale status finalization**: a cancelled/interrupted turn (or any
   terminal without `item/completed` for an in-flight reasoning item) left a
   stale "Thinking…" indicator. The adapter now tracks in-flight reasoning
   items per turn (`_inflight_reasoning`) and the `turn/completed` handler
   finalizes each in-flight status in place — "Thinking interrupted" for
   cancelled/failed, "Done thinking" otherwise.

## Remaining live-validation boundary

The fix is verified by unit tests against captured notification shapes. A
live end-to-end check (run a Codex turn in the browser and confirm the
"Thinking…" status appears and updates) is the remaining boundary — it
requires the merged branch to be served by the live backend, which is
outside this task's scope (no shared-service changes).
