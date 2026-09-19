# 2026-09-19 — Reliable Chat drag and compact history hydration

## System overview

The Chat sidebar now implements reordering with Pointer Events rather than the
browser's native HTML drag-and-drop subsystem. A primary pointer press on any
non-menu part of a row becomes a drag after a five-pixel movement threshold.
The sidebar hit-tests the pointer against visible rows, displays a before/after
marker, and commits the ID-based order through the existing tab-order endpoint.
Clicking without crossing the threshold continues to activate the session.

Cold structured-Chat hydration requests a compact representation of persisted
history. The event store reads the same append-only JSONL rows and preserves the
raw page cursor, but adjacent compatible `text_delta` and `thinking_delta` rows
are returned as a single presentation event. Live SSE and long-poll delivery are
unchanged and continue to use strict contiguous sequence reconciliation.

## Module design

- `ChatSidebar.vue` owns a document-level pointer gesture so the drag remains
  active outside the source row. It records the last visible drop candidate;
  `pointerup` commits that candidate because browsers may retarget the release
  event to the element pressed initially. The pin target is absolutely
  positioned so appearing at drag start never changes row geometry.
- `AgentStreamStore.read_since(..., compact=True)` compacts only the response,
  never the durable log. Merge identity matches the live coalescer: event type,
  turn, message, run epoch, and plan metadata must agree, and snapshots are hard
  boundaries. `next_sequence` remains the last raw sequence read.
- Each compacted event carries `_history_chunk_count`. The timeline reducer uses
  this count when recognizing provider snapshots that replay several prior
  deltas, preventing duplicate assistant text after compaction.
- `useAgentStream` applies compacted hydration pages directly and advances its
  buffer to the raw page cursor. After hydration, normal contiguous buffering is
  restored for live events.

## Evidence and performance

Read-only samples from three existing long histories showed raw-to-compacted
event-count reductions of 11,105 to 3,677 (66.9%), 15,734 to 1,433 (90.9%),
and 5,344 to 536 (90.0%). Their serialized response sizes fell from 9.65 MB to
6.97 MB, 8.10 MB to 2.32 MB, and 3.02 MB to 1.09 MB respectively. This removes
thousands of browser JSON objects and reducer iterations without truncating any
conversation content.

The pointer path is covered by an opt-in real Vue + Chromium regression test
that creates isolated Chat tabs, performs actual mouse movement, verifies the
rendered order, and inspects the complete order request. Unit tests cover raw
cursor preservation, merge boundaries, and snapshot replay semantics.

## Key issues and pitfalls

- A drag-only helper inserted in normal flow changes every row's coordinates at
  the exact moment dragging begins. Drop affordances must overlay the list or
  reserve their space permanently.
- Do not recompute the final target from a retargeted `pointerup`; commit the
  insertion marker produced by the last `pointermove`.
- Compacted event sequences are intentionally sparse. They must not enter the
  contiguous live buffer one by one; advance that buffer using the raw page
  cursor after committing the compacted page.
- Never compact across snapshot, message, turn, run-epoch, event-type, or plan
  boundaries. Page boundaries remain boundaries as well, trading a small amount
  of compression for straightforward pagination correctness.
