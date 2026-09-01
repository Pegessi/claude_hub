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
- `MarkdownBlockCache.render(source, complete)` renders each block token
  individually. Completed blocks (all but the last) are cached by `token.raw`.
  The live tail (last token) is always rendered uncached to avoid caching
  every intermediate delta.
- When `complete=true` (stream ended), the final block is also cached.
- Cache size is bounded to the number of completed blocks, not deltas.
  A 5000-delta growing tail keeps the cache at O(1).

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
  cache bounded.
- `agentStreamBatcher.test.mjs`: rAF + 48ms timer flush, barrier types
  flush preceding deltas in order, 8577 thinking deltas → 1 commit.
- `thinkingNoMarkdown.test.mjs`: thinking block uses `<pre>{{ part.text }}`,
  no `<MarkdownContent>`, no `marked`/`DOMPurify` references; text part
  uses `<MarkdownContent>` (contrast check).

### E2E (Playwright, isolated backend 18173 / frontend 5275)

- 1493-char Thinking stream: max long-task **277ms** (P95 237ms),
  58 long tasks total.
- **Zero subscriber queue drops** after coalescer restart.
- Tool call path verified (WebSearch executed, result rendered).
- Thinking confirmed rendered as `<pre>` (no markdown parse).
- Interaction probe (style recalc): < 1ms after stream completion.

## Changed files

- `backend/claude_hub/services/agent_stream/coalescer.py` (new)
- `backend/claude_hub/services/agent_stream/tailer.py` (modified)
- `backend/tests/test_agent_stream_coalescer.py` (new)
- `backend/tests/test_agent_stream.py` (modified)
- `frontend/src/utils/markdownBlocks.ts` (new)
- `frontend/src/utils/agentStreamBatcher.ts` (new)
- `frontend/src/components/MarkdownContent.vue` (modified)
- `frontend/src/components/StructuredPane.vue` (modified)
- `frontend/src/composables/useAgentStream.ts` (modified)
- `frontend/tests/markdownBlocks.test.mjs` (new)
- `frontend/tests/agentStreamBatcher.test.mjs` (new)
- `frontend/tests/thinkingNoMarkdown.test.mjs` (new)

## Residual risks

- The 60ms coalescer window adds up to 60ms latency to non-barrier deltas.
  This is acceptable for text/thinking but could be tuned per stream type.
- The per-block cache keys on `token.raw`; if two blocks have identical raw
  text but different context (e.g., different list nesting), the cached HTML
  is reused. This is correct because `marked.parser([token])` is stateless
  per token.
- `breaks:true` is passed to both `lexer` and `parser`; if marked changes
  its option handling, the single-newline → `<br>` behavior could break.
