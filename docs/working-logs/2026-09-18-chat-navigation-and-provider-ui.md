# 2026-09-18 — Chat navigation and provider UI

## System overview

The Chat sidebar now treats the backend tab list as the durable ordering source.
The browser stores only presentation preferences: pinned tab IDs, group folding,
and the bounded preview expansion state. Dragging reorders IDs through the
existing tab-order endpoint, while pinning changes only the local presentation.
The desktop sidebar width is another browser-local presentation preference,
bounded to 200–480px. Its edge separator supports pointer and keyboard resizing;
the entire session row is the navigation target while its action menu and rename
input remain independent controls.

Structured Chat keeps the complete event history in the existing stream cache,
but mounts only the latest 40 turns initially. Loading an earlier page records
the first visible row and restores that row's viewport position after the DOM
grows. Every activation resets the timeline gate and pins the final rendered
layout to the tail. Hydration, cache reconciliation, initial tail placement,
and older-message expansion all expose visible progress.

## Provider prompts and plans

Cursor 2026.09.15 emits AskQuestion as a top-level tool_call protobuf JSON
record rather than an assistant content block. The adapter accepts both forms
and deduplicates their call IDs. Codex and TraeX permit questions whose options
are null or empty; these render as free-text cards, with secret questions using
a password input. Option descriptions survive normalization.

The Codex-family adapter publishes final plan items as authoritative snapshots,
because protocol deltas are not guaranteed to concatenate to the completed
plan. Turn-start events persist the selected mode. A completed successful Plan
turn can therefore show an explicit Implement plan control, which switches to
Agent mode and starts a new user-approved implementation turn.
TraeX-only queue lifecycle and recovery notifications are translated to the
provider-neutral `status` event already rendered by the timeline. This includes
queued/waiting/ready, reroute/fallback, warnings, and loop-recovery attempts.

## Key issues and pitfalls

- A failed question response must not mark its approval card resolved; the send
  function returns success explicitly so the card remains actionable on errors.
- Progress and proposal plan snapshots need stable, distinct message IDs and
  must bypass text coalescing.
- A bounded render window must retain older unresolved approvals and active edit
  rows so optimization never makes required interaction inaccessible.
- Drag ordering uses tab IDs rather than visible indices because pinned and
  collapsed rows are presentation projections of the server order.
