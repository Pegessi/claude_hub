# Working Log — 2026-11-11 — Native & MASO Sub-Agent Dock Hydration and Cross-Turn Continuity

## Symptom

Opening a completed or long-running MASO Chat session (such as `5514f804-0ae5-444c-a7a9-ec0dd908e4c2`, which spawned 12 sub-agents across 16 turns) showed no Agents pill/rail in the pane chrome on initial load or page reload, and expanding turns manually produced truncated colliding names (`Thread feishu_m`), duplicate cross-turn rows with stale `Active` state, and `"Waiting for sub-agent activity"` summaries.

## Root Causes

1. **Shallow conversation history + unread detail gate**:
   - `conversation_event_visible` in `backend/claude_hub/services/agent_stream/history.py` hid all `subagent_lifecycle` status events except `failed`/`cancelled`, and hid all delegation tool calls (`Task`, `Fork`, `Agent`, `spawnAgent`, `sendInput`, `SendMessage`).
   - `StructuredPane.vue` gated `nativeSubagentRows` on `!(detail && detail.status !== 'loaded')`, so historical turns contributed zero rows until the user manually clicked `Execution details · N events` on each turn.
2. **MASO delegation & cross-turn sub-agent representation**:
   - `parseSubagent` (`frontend/src/utils/subagentTool.ts`) did not recognize MASO `Task`, `Fork`, or `SendMessage`.
   - `threadName` (`frontend/src/features/chat/presentation/nativeSubagentRows.ts`) truncated any `threadId.length > 12` without `agentType` to `Thread <8chars>`, turning readable MASO IDs like `feishu_model_backend-8736` and `feishu_modes_ui-8c83` into colliding `Thread feishu_m` labels.
   - `nativeSubagentRows` emitted one row per `(turn, thread)` without deduplicating long-lived sub-agents that continued across multiple parent turns (`posthoc_reviewer-a38c`, `gemini_final_review_fix-3c37`, `feishu_reviewer-1451`), leaving earlier turns stuck at `running`.
   - `subthreadHeadline` (`chatSubagentPresentation.ts`) ignored child `SendMessage` report tool calls when a MASO child reported back without emitting `text_delta`.

## Changes

- `backend/claude_hub/services/agent_stream/history.py`: keep `subagent_lifecycle` statuses (`active`, `completed`, `failed`, `cancelled`) and sub-agent launch/directive tool events (`Agent`, `Task`, `Fork`, `spawnAgent`, `sendInput`, `SendMessage`, `collabAgentToolCall`) visible in `conversation_event_visible` and `ConversationHistoryIndex.conversation_page` (including pre-existing `visible=0` SQLite rows).
- `frontend/src/utils/subagentTool.ts` & `frontend/src/features/chat/timeline/{types,subthreads,reducer}.ts`: add `'maso'` provider support for `Task` (including multi-task fan-out), `Fork`, and `SendMessage`, and bind spawned `<subagent_name>-<hex>` thread IDs to their per-task prompts and roles.
- `frontend/src/features/chat/presentation/{chatSubagentPresentation,nativeSubagentRows}.ts`: preserve readable `<name>-<hex>` MASO thread IDs, extract child `SendMessage` reports in `subthreadHeadline`, deduplicate cross-turn sub-agents in `nativeSubagentRows`, and merge cross-turn `launchTool` and `parts` in `findNativeSubthread`.
- `frontend/src/features/chat/{StructuredPane.vue,controllers/useChatTimeline.ts,components/ChatNativeSubagentOverlay.vue}`: allow turns with real sub-agent launch/lifecycle data to project into the dock while keeping bare failed-status stubs gated, and hydrate lazy turn execution details on demand when a sub-agent window is opened (`ensureSubthreadDetailsLoaded`).
- `backend/tests/test_cleanup_background_lifetime.py`: yield `2ms` (`await asyncio.sleep(0.002)`) in `wait_until` instead of bare `sleep(0)` so coverage-traced `asyncio.to_thread` shutdown writes are not starved of the GIL during full-suite `--cov` runs.

## Validation

- `env -u FORCE_COLOR ./scripts/verify.sh all` (PASS: docs, checkout-safety, backend-format, backend-types, backend-tests [6,276 passed], frontend-lint, frontend-types, frontend-tests [1,294 passed], frontend-build)
- `CLAUDE_HUB_BROWSER_EXECUTABLE=/home/tiger/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome ./scripts/verify.sh browser-mocks` (PASS: 19/19 scenarios)
- Independent review by sub-agent `maso_subagent_dock_reviewer-d6a3`: verdict `GO`
- Cross-model adversarial review (`cross_reviewer_gpt-9d54` + `cross_reviewer_gemini-448e`):
  - Fixed cross-page `tool_call_completed` promotion and turn-scoped `(turn_id, tool_identity)` correlation + delegation argument-signature checks in `history.py`.
  - Fixed cross-turn `failed`/`cancelled` terminal status readiness in `StructuredPane.vue` (`hasEarlierReadableThread`).
  - Projected standalone `{ kind: 'subagent' }` parts (Claude `Agent` / Cursor `Task` before nested child events hydrate) into `nativeSubagentRows` and `findNativeSubthread`, and stamped `data-subagent-thread-id` on `ChatSubagentCard.vue`.
  - Fixed hex-suffix matching (`masoAgentNameMatchesThreadId`) and index-first disambiguation (`subagentViewForThread`) for duplicate or prefix-overlapping `subagent_name`s in MASO `Task` batches.
  - Updated `ensureSubthreadDetailsLoaded` to hydrate the full inclusive turn span `[firstIdx..lastIdx]` for cross-turn sub-agents and retry `'error'` detail entries on reopen.
