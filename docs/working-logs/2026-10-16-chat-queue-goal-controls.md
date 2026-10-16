# Chat queue controls and image-aware Goal editing

Task: `8630a435-0b78-4a8b-9c25-cd45e06e0093` (initiator controlled).
Branch: `codex/chat-queue-goal-controls`, base `a72cf9d45c88aa05865e9619140caec38c874d8b`.

## Behavior

Queued messages expose details, saved text edits, cancellation, original image
previews and removal. Moving a message to the composer preserves an existing
composer draft in the queue. Cmd/Ctrl+Enter with an empty composer steers the
first queued message. Active Goals use their existing safe steering controller,
without a user Stop or leaving the Goal lifecycle. Managed Chat retains durable
mailbox input rather than gaining an arbitrary native steer path.

Automatic sends keep the queued item until acknowledgement and never use the
composer as scratch space. Failed queue items stay paused and cannot be retried
blindly. An authoritative matching turn consumes the uncertain item without a
provider resend; editing deliberately creates a new intent. Queued edit buffers
and activation admission live in Pinia and survive bounded KeepAlive eviction.
Draft IDs also work on remote plain HTTP, without secure-context randomUUID.

Goal objectives can be edited during an active turn. Edits apply to the next
turn; the old objective's completion signal cannot finish the new revision.
Images can accompany Goal creation or a steer, preserving provider originals
and the existing shared preview LRU. Only count/digest markers enter Goal JSON;
request-scoped image holders clear references even in inherited provider task
contexts. Interrupted image dispatch fails closed, requires reattachment and an
explicit Recover Goal action with a new intent ID, and keeps the same Goal ID.

## Verification

Final verification passed. Evidence is retained under
`/home/tiger/claude_hub_workspace/chat-queue-goal-controls/validation`.

- `./scripts/verify.sh all`: exit 0, all nine targets passed. Default backend
  scope: 3,821 passed, eight opt-in cases skipped; frontend: 882 passed.
  Exact parent exit is recorded in `all-final-exit.json`.
- `./scripts/verify.sh browser-mocks`: all 11 scenarios passed in
  `claude-hub-browser-mocks.stWr3d/run`. Coverage includes hidden editing across
  more than eight Chats, remote-HTTP UUID availability, saved queue edits,
  cancellation, original-image previews, empty-composer Goal steering without
  Stop, objective editing, image creation and same-image explicit recovery.
- Negative browser control: temporarily disabling queued shortcut steering
  failed the new acceptance assertion, with source restored byte-for-byte.
  See `negative-shortcut-pinned/probe.json` and its runner report.
- Both independent native reviewers closed all actionable findings. The review
  fixed image references inherited by provider reader tasks, queue edit/FIFO
  ownership, uncertain native retries, image-button clipping, late delivery
  edit loss, recovery identity after failed GET, and transient Goal reads.
  Current-Goal GET now shares per-tab admission locking; an ASGI concurrency
  regression verifies it cannot expose an intermediate dispatch snapshot.
- The final browser run recorded complete context/browser/driver closure,
  owned Vite exit 0 and a closed owned port. Whole descendant process-tree exit
  remains explicitly unverified.

`check-staged` is recorded with branch delivery. Terminal HMR/replay, live
provider/OAuth behavior and deployment were not run for this Chat-only change.
The first mocked browser run accepted the new Goal controls, but failed the old
composer scenario because its queue selector still described the retired
count-only UI; that scenario was updated to the new queue surface.

No shared service restart, main merge, remote publication, or runtime migration
is part of this task. Browser checks mock APIs and do not prove live provider
interruption behavior.

## Authorized native publication transplant

The user subsequently authorized merging this feature. Local main contains
unrelated Managed Chat and cleanup commits whose own task explicitly excludes
remote publication. Preserve that local main and publish a native-only feature
on remote main `47bdae723ee1984c8d50577f1ab9b4f5fa8c2561` instead of pushing the
combined local tip. The new remote baseline includes the independently published
short-pytest-ID CI fix; this task does not change that fix.

The native transplant comes from feature `19e6c0780d40e04efe25660a243a763ec3652789`.
Its manual adaptations retain the remote Goal API's existing services-module
manager access, omit Managed Chat-only guards/scenarios/tests, preserve native
approval request payloads, and carry the shared queue/recovery memo helpers into
the older native composer and test harness. The original branch's checks above
refer to the local combined feature, not this separately validated native tree.
No Managed Chat implementation or runtime-storage migration is published by
this transplant. Integration gate results are recorded after the fixed merge.


## Native retry identity review correction

The publication review caught that copying a shared retry helper onto the older
native baseline would newly retain IDs for ordinary provider sends. Native turns
have no same-ID delivery deduplication; an explicit resend must get a new intent
ID. Retention is now limited to actual active/blocked/recovery Goal-controller
inputs, plus the local variant's durable Managed Chat mailbox. A paused Goal's
ordinary manual Chat input follows the native rule. New regression cases cover
all three routes. The public composer browser's existing fresh-ID assertion also
caught the pre-correction transplant. This source repair is later than the fixed
merge records and receives its own independent review and final gate results.
