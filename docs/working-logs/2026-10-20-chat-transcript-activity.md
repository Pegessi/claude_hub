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
- `ChatTurnWorking` adds a live stopwatch (`formatStopwatch`) to the turn
  that is live: incomplete, latest, and with a turn in flight. It owns its
  interval so the memoized turn does not re-render each second; `ChatTimeline`'s
  `v-memo` includes that live flag. An incomplete turn whose completion never
  arrived is treated as history: no footer, actions visible.
- While a turn is live, fork/plan actions and the time are hidden; Copy stays
  so a partial answer can still be taken.
- Mobile and touch: chevrons do not depend on hover, touch rows are 40px,
  phones get denser tool blocks and 15px prose, and the Goal bar stays on one
  row with sideways-scrolling actions.
- Diff parsing treats `--- `/`+++ ` as headers only as a pair, skips
  `diff --git`/`index` lines, and keeps dash-prefixed content in patches
  without `@@`. Very large arguments that are still streaming (incomplete JSON
  over 64 KiB) are re-parsed per 32 KiB chunk instead of per delta.

## Validation

- `node --test tests/*.test.mjs`: 1080 pass, 0 fail (new `toolActivity.test.mjs`
  covers parsing, caching, truncation, streaming throttle and the stopwatch).
- `vue-tsc --noEmit`, `eslint src tests`, `vite build`: clean.
- `tests/browser_chat_composer.py` against an owned Vite server (5291): PASS.
- Mocked showcase screenshots in dark/light, desktop/mobile: no page errors.
- Two independent read-only reviews. The first confirmed child cards re-render
  because `visibleTurns` copies each turn and found parsing/a11y/remount issues;
  the second (approve with nits) found the multi-file diff header bug, the
  stale-incomplete-turn footer, streaming re-parse cost, weak source-regex
  tests and this log's drift. All were fixed.

## Known limits

- Rendering freshness of child tool cards relies on `visibleTurns` copying
  turns (`useChatTimeline.ts`); if that copy is removed, pass a revision prop.
- Nested sticky summaries inside an open sub-agent thread can share `top: 0`.
- The stopwatch trusts the server `startedAt`; a server clock behind the
  client inflates it (a server clock ahead is clamped to mount time).
- Several layout guarantees are still asserted by source-pattern tests;
  behavior is covered by `browser_chat_composer.py` and showcase screenshots.
