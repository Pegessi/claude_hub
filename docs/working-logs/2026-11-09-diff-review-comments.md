# Diff review comments (2026-11-09)

Unsent input on a Chat tab: the reader comments on a diff line, the note stays
pinned to that line, and one click sends the whole review back to the agent as
a single message.

## Why

A tool card showed a diff but nothing could be said about it. Feedback meant
retyping the path and the code line into the composer per point, which loses
the line identity and arrives as several turns. JetBrains Air ("Add Comment" /
"Send Comments") and Conductor's line comments are the reference shape.

## What changed

- `utils/toolActivity.ts`: `ToolDiffLine` now carries `file`, `oldLine`,
  `newLine` and `fileHeader`. Unified hunks with `@@ -a,b +c,d @@` give both
  sides their numbers; `*** Update File:` envelopes and change entries name the
  file; `diff --git` / `+++ b/...` headers attribute rows without becoming
  content. A Claude `old_string`/`new_string` edit attributes the file it
  names and stays unnumbered, because nothing in the call states a line.
- `ChatToolGroup.vue`: a quiet line-number gutter, a file header row per file,
  a `+` affordance per content row (hover/focus on desktop, the row itself on
  touch), a small inline editor, and the note pinned under its row. Shift-click
  selects a row range inside one file. The pinned note and editor are
  `position: sticky; left: 0` at `100cqw`, so they stay readable while a long
  diff scrolls sideways.
- `features/chat/review/`: `reviewComments.ts` (anchor shape, location and
  message formatting — pure, unit tested), `useChatReview.ts` (per-tab list,
  sessionStorage copy, focus request, send), `ChatReviewTray.vue` (the tray).
- `ChatComposer.vue`: one additive mount, above the draft queue.
- `StructuredPane.vue` / `owners.ts`: `chatReviewKey`, scoped to the tab id.

## Delivery, deliberately not a new send path

`Send to agent` composes one message and pushes it into the composer's existing
`draftQueue`. `useChatQueue` then delivers it through `submit(..., queuedDraft)`,
which already owns busy-turn queueing, retry identity, the unconfirmed-delivery
pause and the visible queue card. Nothing bypasses the transport, and an empty
tray composes nothing.

## Verification

- Unit: `tests/diffLineAnchors.test.mjs` (numbering, file attribution, cap),
  `tests/chatReviewComments.test.mjs` (location/message text),
  `tests/chatReviewStore.test.mjs` (one-per-row upsert, edit/remove/clear,
  one queued message, focus nonces, sessionStorage round trip, blocked storage).
- Browser gate: `tests/browser_diff_review.py` on
  `tests/diff_review_harness.html` — gutter numbers, inline comment, Shift-click
  range (`:269-271`), tray jump, edit/remove, 390px geometry, and the composed
  message read from the harness queue. It was run against a defect (blank gutter)
  and failed before being trusted.
- `frontend-lint`, `frontend-types`, `frontend-tests`, `frontend-build`,
  `backend-format` all pass.

## Not covered

The tray renders only while a Chat pane is mounted; comments are local review
state, not transcript, so they are not sent with the message history and are
not visible to another browser or to Dot.
