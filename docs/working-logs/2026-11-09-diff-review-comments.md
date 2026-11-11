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

## Stale anchors (review finding F-14)

A comment is anchored on `${part.key}:${tool.key}:${row}`, and a warm-cache
history reload re-derives those keys, so a saved note can outlive its row.
`reviewAnchorRowCount` / `reviewAnchorIsLive` (pure, unit tested) resolve an
anchor against the live `timeline.turns`; `ChatReviewTray` asserts the verdict
when an entry is clicked and the owning `ChatToolGroup` asserts it again when it
cannot find the row it was asked to reveal. An unreachable row marks the entry
`Line no longer in this turn` (the note keeps its file and line and still goes
out with the review) instead of silently doing nothing, and the mark clears as
soon as the row resolves. The flag is in-memory only: it describes the current
timeline, so it is stripped before persisting and a stored comment without a row
identity is dropped on load.

## Range and tap corrections (review finding M-4)

The row range logic now lives in `reviewComments.ts` as pure, unit-tested
helpers: `reviewRangeRows` (one file, hunk rows skipped, either direction),
`reviewRangeLines` (the span reported low-to-high, because a removal carries
only an original-file number and an addition only a new-file number, so an
offset hunk could make a range read backwards), `reviewCommentInRows` (the note
sitting anywhere inside the range is the note being edited) and
`reviewRowTapOpensEditor` (touch only, never inside a nested control).
`ChatToolGroup`'s gutter `+` also stops its click from bubbling to the row's tap
handler, so one tap opens one editor, and an edited note is re-anchored onto its
new range (`useChatReview.reanchorComment`) instead of being duplicated.

## Not covered

The tray renders only while a Chat pane is mounted; comments are local review
state, not transcript, so they are not sent with the message history and are
not visible to another browser or to Dot.
