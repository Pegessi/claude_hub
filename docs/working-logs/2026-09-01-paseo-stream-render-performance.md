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

### Frontend: Per-turn render revision + `v-memo` (`agentStreamTimeline.ts`, `StructuredPane.vue`)

After removing `textReveal`, the E2E run still showed 7 long tasks
(53, 55, 57, 58, 59, 53, 102 ms). The root cause was full historical VNode
reconstruction: every `authoritativeTurns` fresh-array update (which happens
on every committed batch) re-rendered **all** turns, including the completed
historical ones whose content had not changed.

`TimelineTurn` now carries a `renderRevision: number` field, initialized to
`0` in `createTurn`. `applyEventToState` tracks a `mutated` boolean per event
and increments `turn.renderRevision` only when the event visibly mutates the
turn:

| Event | Visible mutation condition |
| --- | --- |
| `turn_started` | `turn.userText !== summary` |
| `turn_completed` | `!turn.completed` |
| `text_delta` | text non-empty **and** not an exact multi-chunk replay |
| `thinking_delta` | text non-empty |
| `tool_call_started` | tool identity not already in the turn's tool map |
| `tool_call_completed` | new tool, or status changed, or result changed |
| `error` | always (pushes a new error part) |
| `status` | always (pushes a new status part) |

No-op events (empty text, exact multi-chunk replay, duplicate tool start,
unchanged tool completion) leave `renderRevision` unchanged.

`StructuredPane` puts `v-memo="[turn.renderRevision]"` on each
`.structured-turn` element. Vue's `v-memo` skips re-rendering a subtree when
its dependency array is shallow-equal to the previous render. Since completed
historical turns' `renderRevision` stops changing once they are done, Vue
reuses their existing VNodes and DOM without diffing them. Only the active
turn (whose revision advances on each incoming delta) rebuilds.

The `turns` computed spreads each turn object (`...turn`), so
`renderRevision` is included in the spread and `v-memo` receives the current
value.

Deterministic tests (`agentStreamTimelineReducer.test.mjs`):
- Active turn `renderRevision` increments on `turn_started`, `text_delta`,
  `thinking_delta`, `turn_completed`.
- Completed historical turn's `renderRevision` stays stable while a later
  turn streams.
- Empty `text_delta` and exact multi-chunk replay do not advance revision.
- Duplicate `tool_call_started` does not advance revision.
- `tool_call_completed` with unchanged status/result does not advance
  revision.

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

### Backend: bounded long-poll queue drain (no per-tick `read_since`)

`_wait_stream_events_for` (the authoritative long-poll path) previously called
`store.read_since` on **every** loop iteration — on each 1s health-poll
timeout and on every queue wake. `read_since` scans the full per-session JSONL
from byte 0, so its cost is O(rows). Measured on a session with 15,672 rows,
a single `read_since` call takes 60–76ms. Under a 30s long-poll window that
translates to ~30 full scans per connection, and with multiple tabs the
per-wake cost amplifies live-stream latency.

The waiter now follows a bounded queue-drain protocol:

1. **Initial / reconnect**: call `read_since(since)` exactly once after
   `subscribe`. If it returns events, return them.
2. **No initial events**: wait on the subscriber queue. Since `_publish`
   persists before fanout, every event that reaches the queue is already
   durable — we never need to re-scan the JSONL to find it.
3. **Queue event arrives**:
   - `seq <= since`: already delivered; skip.
   - `seq == since + 1`: contiguous with our cursor. Return this event plus
     any further contiguous events already queued (up to the 200-event limit)
     in one batch.
   - `seq > since + 1`: gap (e.g. the subscriber queue overflowed and dropped
     events). Call `read_since(since)` **once** to reconcile and return its
     page.
4. **Health-poll timeout**: only check `session_exists`, `hard_failed`, and
   the deadline. Do **not** call `read_since`.

`read_since` is therefore invoked at most twice per long-poll request: once
for the initial catch-up and once on a detected gap. The common live path
(contiguous events) incurs zero JSONL scans.

**`has_more` contract**: when the drain fills the 200-event batch and the
subscriber queue still holds events, the response sets `has_more=True`. The
long-poll client ignores `has_more` (it always re-requests), but the
hydration loop and any other consumer rely on it to know whether more events
are immediately available. `qsize()` is a stable snapshot here because no
producer can run between the last `get_nowait` and the check in a
single-threaded asyncio context.

**Duplicate / stale handling in drain**: `_fanout` puts each event once per
subscriber, so queues should not contain duplicates under normal operation.
Defensively, the drain loop skips any event with `seq <= next_seq` (stale or
duplicate of an already-collected event) instead of treating it as a gap.
Only `seq > next_seq + 1` breaks the drain with a real gap.

Deterministic tests (`test_agent_stream.py`):
- Initial `read_since` returns empty; repeated health ticks do not call
  `read_since` again (exactly one call total).
- A single contiguous queue event is returned directly with no second
  `read_since`.
- Stale overlap events (`seq <= since`) are skipped.
- Multiple contiguous queued events are drained in one batch.
- A non-contiguous first event (`seq > since+1`) triggers exactly one
  gap-fallback `read_since`.
- More than 200 contiguous queued events return exactly 200 with
  `has_more=True`.
- A duplicate sequence mid-drain is skipped; the contiguous prefix is
  returned intact.

### Root-cause distinction: stale probe vs. product per-wake inefficiency

The post-`v-memo` E2E run observed the backend at 40–65% CPU and attributed
it to the long-poll `read_since` loop. Subsequent investigation found the
sustained idle load was actually caused by a **stale Python E2E polling
probe** (PID 88343) left running from this worktree. The probe repeatedly
requested the legacy cursor `?after=999&limit=1000`, which the backend
ignored and answered with the first 1000 rows on every tick. After SIGTERM
of the probe, the live backend (PID 46248) dropped to 0–0.7% idle (one 7.2%
poll blip).

The product long-poll full-scan is a **separate, measured per-wake
inefficiency** (60–76ms `read_since` on 15,672 rows) that the bounded
queue-drain change above addresses. It was not the cause of the sustained
idle CPU. Neither the stale probe nor the per-wake scan is claimed as the
final UI long-task root cause until a fresh E2E run after cleanup.

### Rejected experiment: CSS `content-visibility` for history virtualization

A history A/B against the same model turn tested two CSS-only approaches to
reduce long tasks during long Thinking bursts:

- **Naive `content-visibility: auto`**: reduced long tasks 29→21 and max
  307ms→208ms, but broke the bottom gap (the scroll anchor / autoscroll
  region lost its height because off-screen turns were skipped by the
  browser).
- **`content-visibility` with hydration-height reserve**: reduced long tasks
  23→13 and max 508ms→245ms, but still broke the bottom anchor (gap 21055px).

**Verdict: rejected.** Both variants reduce long-task count/duration but
break the bottom-anchor / autoscroll contract that Paseo's history
virtualization layer relies on. A CSS-only patch is unacceptable because it
silently corrupts the scroll position the user expects during streaming.
This validates that Paseo's history virtualization / bottom-anchor layer is
a relevant performance lever, but the fix must be implemented in the
virtualization layer itself (with correct height reservation and anchor
maintenance), not as a raw CSS `content-visibility` override. Backend
optimizations (bounded queue-drain) are the priority; a proper history
virtualization fix is a separate follow-up.

### Fresh UI evidence: no product truncation

The first clean E2E run appeared to show truncated assistant/thinking output,
but this was a measurement artifact: the script queried only the first
assistant/thinking part of a turn that actually has two parts split around a
Bash tool call. The reducer output is exact: 3460 text / 3636 thinking
characters with the correct marker. Cold hydration is also exact when
summing all parts. No product truncation was present in this run.

## Changed files

- `backend/claude_hub/services/agent_stream/coalescer.py` (new)
- `backend/claude_hub/services/agent_stream/tailer.py` (modified)
- `backend/claude_hub/api/agent_stream.py` (modified — bounded long-poll
  queue drain; `read_since` no longer called on every health tick)
- `backend/tests/test_agent_stream_coalescer.py` (new)
- `backend/tests/test_agent_stream.py` (modified — added bounded queue-drain
  tests: no per-tick read_since, direct queue consumption, stale overlap
  skip, contiguous drain, gap fallback, full-batch has_more=True,
  duplicate-sequence skip)
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
- `v-memo="[turn.renderRevision]"` skips re-rendering a turn whose revision
  has not changed. If a future visible mutation is added to a turn without
  incrementing `renderRevision`, that turn will appear stale until its
  revision advances. The `mutated` flag in `applyEventToState` is the single
  source of truth for revision increments; any new event type or mutation
  path must set `mutated = true` to keep `v-memo` correct.
- The bounded long-poll queue drain relies on `_publish` persisting before
  fanout so queued events are durable. If that invariant changes (e.g. a
  future code path fans out an unpersisted event), the waiter could return
  an event that is not yet in the store, and a reconnect that calls
  `read_since` would miss it. The gap-fallback `read_since` path only
  triggers on `seq > since+1`; a silently dropped event (no gap in
  sequence) would not be detected.
