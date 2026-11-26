# Preserve review-time Goal steering

## Contract and ownership

`GoalRunController` remains the lifecycle owner. While strict review runs,
steering now appends bounded user input under the existing Goal lock, without
cancelling the reviewer or starting another worker. The completed review round
is retained for PASS, BLOCK and reviewer errors (errors fail closed as BLOCK).
A pending user message takes precedence over automatic completion or parking:
one continuation receives both the review outcome and the ordered user input.
The agent decides how to respond; emitting complete still invokes strict review.
A steer at an exhausted round budget starts a fresh cycle after recording the
finished round in the old cycle.

Ordinary worker steering still interrupts. Explicit Pause/Stop, Clear/Cancel
and manual Complete still cancel strict review. Objective-revision invalidation
continues to reject stale review outcomes.

## Delivery and recovery

Text is bounded to the existing 32,000-character limit including separators.
Queued originals are controller-owned, in-memory copies; combined images obey
the existing 10-image / 40 MiB original-plus-preview limits. Nothing writes image
data URLs into Goal persistence. Delayed dispatch scopes these originals through
the existing Goal image transport instead of relying on the expired HTTP request
context. Acceptance or failed dispatch releases them; explicit stopping also
releases them. Durable image markers require reattachment after interruption or
restart, as before. Recovery retains every accepted instruction and appends new
amendments without duplicating an unchanged amendment on failed-delivery retries.
The delayed dispatcher detaches only its own image batch so an earlier RPC's
return cannot erase a later review queue.

A bounded list of review-steer receipts reconciles accepted HTTP requests and
optimistic frontend messages without inventing provider turns. HTTP receipts
remain bound to the original Goal after Clear/replacement, and acknowledge those
historical retries without stranding another optimistic bubble. Goal details show
queued input/images; healthy review-time images no longer imply interrupted
delivery. The eventual combined turn supplies the ordinary Chat transcript.

## Verification boundary

Event-driven tests hold a reviewer open through single/multiple steers and
exercise PASS, BLOCK, error, duplicate request IDs, aggregate limits, image-only
input, request-context exit, explicit cancellation and uncertain dispatch.
The original controller failed the new test by clearing review-in-progress.
A frontend negative control using the original delivery controller retained two
optimistic turns; the new receipt test clears both without a worker event.
The existing rendered Goal browser scenario now checks queued text plus images.

All tests use private temporary storage or browser-mocked APIs. No live provider,
database or shared-service mutation is part of this change. Full commands,
results, review findings and the exact commit are recorded in the managed Task
report. Live-provider acceptance remains distinct from these regressions.
