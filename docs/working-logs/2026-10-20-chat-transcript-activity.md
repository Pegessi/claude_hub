# Chat transcript and tool activity redesign (2026-10-20)

Branch `codex/chat-interaction-polish`, based on `main` c960fdc.

## Goal

Make agent conversations read like Codex/ChatGPT transcripts instead of
stacked bubbles: prose is the document, tool calls are a quiet activity log,
and only what needs attention (failures, approvals) is loud.

## Changes

- Assistant text is unboxed and full-width; part avatars were removed so
  every part shares one reading column. User bubbles are unchanged.
- `src/utils/toolActivity.ts` classifies Claude/Codex/Cursor/MCP tool names
  into kinds and derives `verb`, `target`, a full `command` and a line `diff`.
  Results are cached per tool row (validated by `name`/`argsText`) and group
  headlines are cached per array length, because the reducer mutates rows in
  place while streaming.
- `ChatToolGroup` renders an activity line; expanded commands show a terminal
  block, edits a diff, and raw JSON stays available behind "Raw input".
  Status is a glyph with screen-reader text; failures stay a labelled pill,
  including a failed count inside a still-running group.
- `ChatSubthread` delegates to `ChatToolGroup` and `ChatThinkingCard`.
- `ChatTurnWorking` adds a live stopwatch to the latest incomplete turn. It
  owns its interval so the memoized turn does not re-render each second;
  `ChatTimeline`'s `v-memo` includes whether a turn is the latest.

## Validation

- `node --test tests/*.test.mjs`: 1074 pass, 0 fail (new `toolActivity.test.mjs`).
- `vue-tsc --noEmit`, `eslint src tests`: clean.
- `tests/browser_chat_composer.py` against an owned Vite server (5291): PASS.
- Mocked showcase screenshots in dark/light, desktop/mobile: no page errors.
- Independent read-only review: confirmed child cards re-render because
  `visibleTurns` copies each turn; parsing, a11y and remount findings fixed.

## Known limits

- Rendering freshness of child tool cards relies on `visibleTurns` copying
  turns (`useChatTimeline.ts`); if that copy is removed, pass a revision prop.
- Nested sticky summaries inside an open sub-agent thread can share `top: 0`.
