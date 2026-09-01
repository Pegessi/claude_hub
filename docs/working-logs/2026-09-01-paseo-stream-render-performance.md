# Paseo Stream Rendering Performance — Long Thinking Bursts

**Date**: 2026-09-01
**Branch**: `feat/paseo-structured-ui-v2`
**Task**: Optimize Paseo stream rendering under long Thinking bursts

## Problem

Under long Claude Thinking streams (thousands of small text deltas), the
structured view would freeze the browser tab. Three root causes:

1. **Subscriber queue overflow**: The agent stream publisher used
   `asyncio.Queue(maxsize=2000)` with `put_nowait`, dropping events when the
   queue filled. Long Thinking bursts produced more events than the SSE
   subscriber could drain.
2. **Per-delta markdown re-parse**: Every `text_delta` triggered a full
   `marked.parse` + `DOMPurify.sanitize` of the entire accumulated text.
   For a growing 10KB response, this was O(n²) parse work.
3. **Per-event reactive re-render**: Each delta event committed directly to
   the Pinia store, triggering a full timeline re-render per event.

## Solution

### Backend: Event coalescer (`agent_stream/coalescer.py`)

- **Leading-edge immediate flush**: The first delta of a stream is sent
  immediately for low first-byte latency.
- **Trailing 60ms timer**: Subsequent same-stream deltas are buffered and
  merged (text concatenated) until a 60ms idle window or a barrier event.
- **Barrier events force flush**: `turn_completed`, `tool_call_completed`,
  `approval_required`, `approval_resolved`, `error`, `status` flush the
  buffer immediately.
- **Serialized drain**: An `asyncio.Lock` + `while buffer:` loop ensures
  only one coroutine drains the buffer at a time, preventing interleaved
  writes and preserving event order.
- **`_is_live` backfill semantics**: Historical catch-up events are persisted
  but not fanned out to live subscribers. Pending historical buffers are not
  flushed after `_is_live` flips.

### Frontend: Per-block markdown cache (`markdownBlocks.ts`)

- `splitBlockTokens(source)` uses `marked.lexer(source, { gfm, breaks })` to
  split the source into top-level block tokens (excluding `space` tokens).
- `MarkdownBlockCache.render(source, { complete, linkMarkdownPaths })` returns
  an array of `RenderedBlock { key, html }` descriptors instead of a single
  concatenated HTML string.
- Completed blocks (all but the last) are cached by `token.raw` plus the
  `linkMarkdownPaths` mode (`l:` / `n:` prefix). The live tail (the
  still-growing last block) is rendered fresh each call and never cached.
- When `complete=true`, the final block is also cached.
- Cache size is bounded to the number of completed blocks, not deltas.
  A 5000-delta growing tail keeps the cache at O(1).
- The cache is invalidated when `linkMarkdownPaths` changes mode, so stale
  (un)linked HTML is never served.

### Frontend: Keyed block DOM promotion (`MarkdownContent.vue`)

The original `MarkdownContent` set `v-html` on a single root element, so even
though completed blocks' HTML was cached, the entire assistant subtree was
replaced on every live-tail delta.

`MarkdownContent` now renders each block as its own `<div class="markdown-block"
:key="block.key" v-html="block.html" />`:

- **Index-stable keys**: each block's key is `block:${index}`. Completed blocks
  never change position, so their key is stable and Vue reuses their DOM node
  across renders. The live tail always sits at the last index, so its key is
  stable while it grows and Vue reuses one DOM node, updating only its
  `innerHTML`.
- **No duplicate keys**: identical paragraphs at different positions get
  different index-based keys (e.g. `block:0` and `block:1`), so Vue never
  confuses them.
- **Lists stay as one block**: marked treats a contiguous list as a single
  top-level token. We do not split list items (unsafe). A long streamed list
  is the live tail and costs exactly one parser call per delta; completed
  blocks before it are cached. The actual >50ms Long Task claim is measured
  in the Playwright E2E run, not the unit tests.
- `linkMarkdownPaths` and `markdownPathClick` behavior are preserved: path
  mentions are wrapped per block and the click handler still intercepts
  `a[data-markdown-path]`.

### Frontend: Thinking as plain text

- Thinking parts render as `<pre class="thinking-body">{{ part.text }}</pre>`.
- Zero `marked.parse` / `DOMPurify.sanitize` calls for thinking content.
- Whitespace preserved via `pre-wrap`.

### Frontend: Event micro-batching (`agentStreamBatcher.ts`)

- `AgentStreamBatcher` accumulates events and flushes on `requestAnimationFrame`
  with a 48ms `setTimeout` fallback (for backgrounded tabs).
- Barrier events flush immediately, carrying all preceding pending deltas
  in arrival order.
- `flushAndCancel()` on reset/stop/unmount ensures no events are lost.

### Frontend: Incremental timeline reducer (`agentStreamTimeline.ts`)

The original `groupEventsIntoTurns(events)` re-scanned the **entire** event
list on every batch. For a session with ~13.5k historical events, each
incoming delta re-ran the full O(n) reduction and dominated the long-task
budget (73 long tasks >50ms, p95 135ms, max 236ms in the real turn).

`IncrementalTimelineReducer` keeps the reducer state alive across calls and
only processes the unseen suffix:

- Maintains `turns`, `byTurnId`, `toolsByTurn`, `textChunksByTurn`,
  `legacyCurrent` across `reduce()` calls.
- Tracks `processedCount` and `lastAppliedKey` (session_id + tab_id +
  stream_sequence) to detect non-prefix replacements.
- On each `reduce(events)`, if `events[processedCount-1]` matches the last
  applied event, only `events.slice(processedCount)` is processed. Otherwise
  the reducer resets and reprocesses from scratch (handles session switch,
  reconnect, reset — same-length or longer non-prefix replacements).
- Returns `[...this.state.turns]` (fresh array reference) so Vue computed
  invalidation fires; turn objects are mutated in place.
- Exposes `appliedCount` (cumulative events applied since last reset) for
  deterministic bounded-work assertions.

Cost per batch is O(new events), independent of history length.

## Validation

### Backend tests

- `test_agent_stream_coalescer.py`: coalescer ordering, merge, barrier flush,
  serialized drain, `_is_live` backfill.
- `test_agent_stream.py`, `test_agent_stream_native.py`: 96 tests pass.
- Full suite: 1146 passed, 15 failed (all terminal/ttyd env failures,
  unrelated to Paseo changes).

### Frontend tests

- `markdownBlocks.test.mjs`: block splitting, `breaks:true` → `<br>`,
  output equals `marked.parse`, completed blocks cached not re-parsed,
  cache size bounded to completed blocks, 5000+5000 growing tail keeps
  cache bounded, **index-stable `block:${index}` keys are unique even for
  identical raw text**, completed block key/html stay stable while the live
  tail grows, live tail key stable while growing, previous tail keeps its
  index key when a new block appears, **cache invalidates when
  `linkMarkdownPaths` mode changes**, **long streamed list costs exactly one
  parser call per delta (the list block) regardless of item count**,
  completed blocks before a list are not re-parsed, a completed long list is
  cached as one block. Joined block HTML exactly equals `marked.parse`.
- `agentStreamBatcher.test.mjs`: rAF + 48ms timer flush, barrier types
  flush preceding deltas in order, 8577 thinking deltas → 1 commit.
- `thinkingNoMarkdown.test.mjs`: thinking block uses `<pre>{{ part.text }}`,
  no `<MarkdownContent>`, no `marked`/`DOMPurify` references; text part
  uses `<MarkdownContent>` (contrast check).
- `agentStreamTimelineReducer.test.mjs`: incremental reducer produces
  identical output to `groupEventsIntoTurns`; processes events in batches;
  resets on shrink, same-length replacement, and longer non-prefix
  replacement; does not reset on same-session append; **bounded-work test
  with 13,500 history events + 300 live deltas asserts `appliedCount`
  advances by exactly 300** (deterministic, not timing-based); preserves
  tool ordering and replay dedup across batches; returns a fresh array
  reference each call.

### Frontend: Removal of second-stage text reveal (`StructuredPane.vue`)

The original `StructuredPane` ran a character-by-character `textReveal`
animation on top of the already-batched event stream. Each committed text
batch retargeted the reveal state and paced out the new characters over a
~150ms horizon, producing 3–4 intermediate DOM/Markdown updates per batch.

Authoritative E2E (turn `c01df02c-0a46-4cca-b9af-e4a58e0953b4`, real day1
Claude, 5275/18173) measured **98 `text_delta` events** expanding into
**375 distinct assistant text DOM states** — a ~3.8× multiplier from the
reveal layer. Each reveal frame re-ran the live-tail markdown parse and
`v-html` update, driving long tasks.

The `textReveal` module and all reveal lifecycle code in `StructuredPane`
have been removed. Assistant text now streams directly from the batched
event stream (backend 60ms coalescer + frontend rAF/48ms batcher): each
committed batch updates the visible text exactly once. On `turn_completed`,
`MarkdownContent`'s `complete` prop flips to `true`, caching the final
block and exposing the exact final text synchronously.

Deterministic test (`agentStreamTimelineReducer.test.mjs`): 4 text batches
produce exactly 4 distinct assistant text values, each equal to the full
accumulated `assistantText` (no partial-reveal prefix).

### E2E (Playwright, isolated backend 18173 / frontend 5275)

**Authoritative run (commander), turn `c01df02c-0a46-4cca-b9af-e4a58e0953b4`:**

- Duration: 91.68s
- 519 `thinking_delta` / 98 `text_delta`; thinking 4572 chars, final text
  1474 chars; one Bash start+completed.
- Waiting placeholder seen; final marker visible; joined block HTML exactly
  equals `MarkdownBlockCache` complete render.
- Scroll held at top, Latest visible, click gap=0; tool completed.
- **375 distinct assistant text DOM states** and 511 Thinking states.
- **Long Tasks after hydration reset: 6 durations — 58, 60, 50, 76, 64,
  90 ms.** Gate still fails (>50ms).

Root signal: the `textReveal` second-stage interpolation magnified 98 text
events into 375 DOM/Markdown live-tail updates. Removed (see above). The
6 long tasks above are from the pre-removal build; the post-removal E2E
is pending re-run.

### Correctness invariants preserved

- Replay/reconnect: non-prefix replacement detection resets the reducer.
- Dedup: `isExactMultiChunkReplay` still suppresses multi-chunk replays.
- Tool ordering: parts are appended in emission order; `turn_completed`
  flips still-running tools to `completed`.
- Exact final: incremental output is byte-identical to `groupEventsIntoTurns`
  for the same event list (asserted in tests).

## Changed files

- `backend/claude_hub/services/agent_stream/coalescer.py` (new)
- `backend/claude_hub/services/agent_stream/tailer.py` (modified)
- `backend/tests/test_agent_stream_coalescer.py` (new)
- `backend/tests/test_agent_stream.py` (modified)
- `frontend/src/utils/agentStreamTimeline.ts` (modified — added
  `IncrementalTimelineReducer`, refactored shared event-application logic)
- `frontend/src/utils/markdownBlocks.ts` (new)
- `frontend/src/utils/agentStreamBatcher.ts` (new)
- `frontend/src/components/MarkdownContent.vue` (modified)
- `frontend/src/components/StructuredPane.vue` (modified — uses
  `IncrementalTimelineReducer` instead of `groupEventsIntoTurns`; removed
  second-stage `textReveal` interpolation)
- `frontend/src/utils/textReveal.ts` (deleted — unused after reveal removal)
- `frontend/src/composables/useAgentStream.ts` (modified)
- `frontend/tests/markdownBlocks.test.mjs` (new)
- `frontend/tests/agentStreamBatcher.test.mjs` (new)
- `frontend/tests/thinkingNoMarkdown.test.mjs` (new)
- `frontend/tests/agentStreamTimelineReducer.test.mjs` (new — includes
  one-update-per-batch deterministic test)
- `frontend/tests/textReveal.test.mjs` (deleted — module removed)

## Residual risks

- The 60ms coalescer window adds up to 60ms latency to non-barrier deltas.
  This is acceptable for text/thinking but could be tuned per stream type.
- The per-block cache keys on `token.raw`; if two blocks have identical raw
  text but different context (e.g., different list nesting), the cached HTML
  is reused. This is correct because `marked.parser([token])` is stateless
  per token.
- `breaks:true` is passed to both `lexer` and `parser`; if marked changes
  its option handling, the single-newline → `<br>` behavior could break.
- The incremental reducer mutates turn objects in place and returns a fresh
  outer array. Downstream computed properties (`turns`) create new turn
  objects via spread, so Vue reactivity is preserved. If a future consumer
  relies on turn object identity across batches, it must handle mutation.
