# Claude AskUserQuestion approval cards

Date: 2026-09-05

## Overview

Claude structured Chat (`session_kind=chat`) gains interactive approval cards
for the `AskUserQuestion` tool, closing the gap with Cursor's `AskQuestion`
cards (shipped 2026-09-02). When a Claude agent calls `AskUserQuestion`, the
structured pane renders an interactive question card (options as toggle
buttons); the user's selection is routed back to the agent as a follow-up
message.

Previously the Claude adapter never emitted `approval_required`, so an
`AskUserQuestion` call surfaced only as a raw tool row. The frontend already
half-anticipated Claude — the timeline reducer maps a dismissed
`AskUserQuestion` to `cancelled` — but no card ever appeared because the
backend emitted no approval event.

## Backend

`backend/claude_hub/services/agent_stream/claude_jsonl.py`:

- `supports_approval_ui` flips `False → True` so the session is advertised as
  approval-capable.
- New module helper `_normalize_ask_user_question_args(args)` maps Claude's
  tool input to the shared approval-card shape:
  - `id` → `str(index)` (Claude has no question ids)
  - `prompt` → `question` (falls back to `header`)
  - `options` → `[{id: label, label: label}]` (Claude has no option ids; the
    label doubles as the id so the submitted answer is self-describing)
  - `allow_multiple` → `bool(multiSelect)`
  - Returns `None` when no question renders, so the caller leaves the ordinary
    tool row as the only surface.
- New method `_emit_ask_user_question_approval(events, tool_call_id, args, ctx)`
  appends an `APPROVAL_REQUIRED` event (`kind: "ask_question"`, `title` from the
  first question's `header`) — the same payload shape Cursor emits.
- Called from both emission sites, immediately after `TOOL_CALL_STARTED`:
  - `_handle_tool_use_stop` (streaming `content_block_stop` path)
  - `_normalize_assistant` (final-snapshot path)

  Both sites are already deduplicated by `emitted_tool_call_ids`, so exactly
  one approval card is emitted per tool call regardless of path ordering.

## Frontend

`frontend/src/utils/agentStreamTimeline.ts`:

- The tool-group exclusion (`toolName !== 'AskQuestion'`) now also skips
  `AskUserQuestion`, so the raw tool row is hidden and only the card renders.
  This is the only frontend change: the `approval_required` reducer, the
  `parseStructuredQuestions` parser, the `formatAskQuestionResponse` submit
  flow, and the `StructuredPane.vue` card UI are all provider-agnostic and
  reused unchanged.

## Answer routing

Claude (like Cursor) runs one-shot `--print` subprocesses per turn; the
conversation id is persisted and resumed. The answer therefore cannot be a
blocking tool result on the same process's stdin. `StructuredPane.vue` submits
the answer as a steer (when a turn is in flight) or a normal follow-up message
via `formatAskQuestionResponse` →
`{type:'ask_question_response', answers:[{questionId, selected:[labels]}]}`.
Because option ids are labels, the next turn maps the selected labels back to
its own `AskUserQuestion` options.

## Out of scope

- `ExitPlanMode` / plan-approval cards are NOT mapped. Per the plan-mode
  contract (`docs/working-logs/2026-09-02-paseo-activity-and-plan-mode-contract.md`,
  recovery rule 7), provider-native plan approval may be mapped only when the
  adapter emits a verifiable plan boundary. `AskUserQuestion` is a question
  tool, not a plan boundary.

## Key issues / pitfalls

- **Two emission paths, one card.** Claude emits tool_use both as streaming
  `content_block_stop` deltas and as a final top-level `assistant` snapshot.
  Emitting the approval from both paths would double the card. The existing
  `emitted_tool_call_ids` turn-scoped dedup (already used for
  `TOOL_CALL_STARTED`) guards both sites, so the approval fires exactly once.
- **Malformed-input fallback.** If `AskUserQuestion` carries no renderable
  question, `_normalize_ask_user_question_args` returns `None` and no card is
  emitted. The raw tool row is hidden unconditionally (matching Cursor), so a
  malformed call shows neither card nor row — an accepted, schema-enforced rare
  case; the turn continues and the user can re-ask.
- **Streaming partial-JSON guard.** When the streamed `input_json_delta`
  fragments fail to parse, `_handle_tool_use_stop` returns early (no
  `TOOL_CALL_STARTED`, no approval); the final snapshot path then emits both.
  The approval rides the same fallback as the tool row.

## Validation

- Backend: `uv run pytest tests/test_agent_stream.py
  tests/test_cursor_cli_transcript_agent_stream.py` — 110 passed (3 new Claude
  approval tests: final-snapshot, streaming-once, malformed-no-approval).
- Frontend: `node --test tests/*.test.mjs` — 269 passed (1 new reducer test:
  AskUserQuestion hidden from tool_group + approval card rendered).
- `vue-tsc --noEmit`, `eslint`, `black`, `isort`, `mypy` all clean.
