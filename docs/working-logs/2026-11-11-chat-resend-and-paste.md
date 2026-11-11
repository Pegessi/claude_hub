# Chat resend repaired, images paste where they were asked for, Ask aside removed

2026-11-11 · branch `fix/chat-composer-fixes`

## Why

Three separate complaints, one surface:

1. The composer's **Ask aside** control shipped with the 2026-11-09 UI batch and
   was never worth its cost. It forked the whole Chat into a new tab and asked
   the question there, so the aside model could not see the turn in flight,
   received only the copied *text* of earlier turns (no tool output, no images),
   cost a full extra provider session, and left a permanent tab to close. It was
   a heavier way to do "Fork from here" plus one message. Removed.
2. **Stop / Edit / Resend** — the trio of turn controls — were wrong in ways
   that were provable by reading the code (below). Resend in particular could
   never succeed on some messages and could silently delete turns.
3. Images could be pasted into the Chat composer, but not into **Resend's inline
   editor** and not into the **Goal setup dialog**, which both swallowed the
   paste.

## What changed

### Ask aside is gone

`utils/chatAside.ts`, its unit suite and the composer button, gate, styles and
fork-then-send effect are deleted. Nothing else referenced it: the backend fork
endpoint, `Fork from here`, managed-Chat refusal and the fork-seed sidecar are
untouched and still used by the per-turn fork action.

### Resend rules live in one testable place

`utils/chatEditResend.ts` now owns when the editor may open and what it may say.
The `turn_started` metadata carries two additive fields (`provider_text_sha256`,
`stored_text_incomplete`); the timeline turn surfaces them as `providerOrigin`
and `storedTextIncomplete`.

### The defects this closes

| Symptom the reader saw | Cause | Fix |
| --- | --- | --- |
| Edit button on a turn whose Resend always failed | A Goal continuation (or restart/workspace/schedule turn) sent a wrapped instruction to the provider while storing "Continue active Goal"; matching is by content, so nothing matched | Hide the editor for Hub-authored origins; the backend refuses with an explicit message |
| Long or secret-containing messages could never be resent, and the editor prefilled a truncated copy | `turn_started` is persisted **already redacted** (4000-character truncation + secret masking), and the fork compares that summary with the full provider text | Match by a hash of the provider text recorded at send time; warn in the editor when the stored copy is partial |
| A resent message could silently delete more turns than intended | The fork falls back to "largest ordinal below" then "earliest duplicate" when content repeats | Hash matching removes the class of mismatch for messages recorded after this change; a shifted match is now logged with the removed line count instead of passing silently. The heuristics themselves are unchanged on purpose — the drift they cover (failed deliveries) is real, and refusing would block legitimate edits |
| The banner's Stop looked armed and every click did nothing | Three different conditions: the composer checked `isSending \|\| isCancelling`, the banners checked only `isCancelling`, and `cancelActiveTurn` refused whenever a send was open | Both surfaces read one `canStop` gate that mirrors what the request can do, so an unavailable Stop is visibly unavailable. A send still in flight keeps blocking Stop on purpose — see below |
| A phantom "■ Stopped" row after Stop → Edit → Resend | The local Stop optimism survived a history rewrite that removed that turn id | The resend retires it with the refreshed stream |
| Enter during IME composition resent half-typed text | The inline editor had no composition guard | Guard added, matching the composer |
| A rewrite silently dropped later turns | The response said only `{ok: true}` | The backend reports `removed_turn_count`; the UI says how many turns were removed |

### Images

- **Resend editor**: pastes stage into a second `useChatAttachments` instance
  (same validation, 8 MiB/image and 40 MiB/request caps, bounded preview
  generation), render as removable chips, and travel as `attachments` on
  `POST /stream/edit-resend`. The body carries additions only — the backend
  re-sends the original turn's own images — and the new bytes are delivered past
  that reused prefix, so `send_message` stays the single writer for persisting
  previews, building their metadata and cleaning them up. edit-resend does not
  persist anything itself, which is why the merge rule cannot drift between the
  two paths.
- **Goal setup**: the objective field forwards pastes to the same composer
  attachment pipeline `startGoal` already serializes, so a pasted image rides
  the first Goal turn like an added one. While editing an existing Goal the
  paste is inert: that dialog has no image field, and a staged draft would be
  invisible yet still attached to the next send.

## Deliberately not done

- Removing an *original* attachment while resending: needs a second contract
  field (`removed_attachment_ids`) and a UI affordance the reader did not ask
  for. The originals stay read-only thumbnails.
- Pausing the draft queue for the duration of a rewrite: a queued draft can
  still leave during the edit round-trip and be rejected as busy.
- Hash matching cannot help turns recorded before this change: a legacy turn
  carries no digest and no `stored_text_incomplete` flag, so its editor still
  opens on the redacted summary and the resend fails with the honest 409
  ("no matching provider user message") rather than silently sending a mangled
  message. Detecting the truncation marker client-side would guess at a
  redaction format; the flag is the contract instead.
- A hung send is still not interruptible. Enabling Stop mid-send was tried and
  reverted: a direct-tab send holds the tab's `goal_admission_lock` across
  provider delivery and `/stream/cancel` acquires the same lock, so the cancel
  would only queue behind the hung send. Fixing this needs a bounded send POST,
  which is a separate change.
- Removing an *original* attachment while resending, and pausing the draft queue
  for the duration of a rewrite, remain open.

## Verification

See the merge entry in `CHANGELOG.md` for the exact check results. The mocked
browser scenarios (23 runners, none skipped) were run against this build once
the other worktree's harness released the shared ports; they cover the composer
paste surfaces, the goal image flow, the edit-resend request body and the inbox
and work-log surfaces touched here.

## One behavior change worth naming

An original image whose cached preview has been evicted is now dropped from the
resent turn instead of keeping a "Preview expired" placeholder. Its bytes cannot
be re-sent, and keeping its id while omitting its bytes breaks the 1:1
image-to-metadata alignment the reuse rule relies on — with an added image in the
same resend, that misalignment would attach the wrong thumbnail. The resent turn
therefore renders fewer images; the provider never received that image either
way.

## Cross-model review

An independent reviewer (a different model, given the diff and the repo rules
rather than the author's conclusions) found one blocking defect the author
introduced, one high, two medium and three low items. Everything actionable was
fixed in the same commit:

- **Blocking (author's regression):** previews persisted for an edited turn's
  added images were never deleted when the provider rejected the delivery — the
  ordinary `send_message` contract deliberately keeps them (a failed ordinary
  turn stays in the store and must render), but edit-resend rolls the store
  back, erasing the `turn_started` that referenced them. `send_message` now
  takes `retain_previews_on_delivery_failure`, and edit-resend passes `False`
  when it added images. The regression test drives a real `send_message` against
  a failing transport, which is what the previous mock-based test could not see.
- **High:** the resend body copied the attachment draft verbatim instead of
  using `toStreamSendAttachment`, so a preview equal to the original uploaded
  the same base64 twice and counted it twice against the 40 MiB request cap —
  a set the editor accepted could be rejected with HTTP 400.
- **Medium:** the editor's attachment controller was gated on the composer's
  send flag (not the edit's), Resend stayed clickable while a pasted image was
  still being prepared (leaving a stale error), and `startEdit` cleared the
  draft without aborting a pending preparation batch. Also added behavioural
  tests for the whole resend flow, which only the gate had covered.
- **Medium, rejected on evidence:** enabling Stop while a send POST is open was
  reverted — the send holds the admission lock the cancel needs, so it would
  not have helped and could have queued a cancel behind a hung send.
- **Low:** question answers dispatched through the runtime path now record
  `origin: question_answer` (they were editable); a paste while a Goal is being
  created is ignored (its image snapshot is already taken); an IME cancelled
  with Escape no longer latches the composition flag; the working log's claim
  about legacy truncated turns was corrected.
