# Feishu message admission and clock skew

Follow-up review found that coalesced batches only rechecked their first member.
The batch repair and updated integration evidence are recorded in
[batch admission](2026-10-18-feishu-batch-admission.md); the results below describe
the earlier single-message candidate.

## Problem

The previous upper time bound required the Feishu message timestamp to be no
later than the Hub host's wall clock. Small positive offsets such as 91, 143
and 152 ms could therefore reject an otherwise current message. The rejection
returned without a reason visible to the user, and the durable dedup record
was finished as a generic failure. A later delivery with the same message ID
was then refused regardless of whether a Chat turn had ever started.

The reported timestamps came from another machine. This work reproduces the
boundary with controlled inputs; it does not claim to have inspected or replayed
those original messages.

## Repair contract

- Allow a small configured future-clock offset while preserving the exact
  activation lower bound, malformed timestamp checks and inverted-clock rejection.
- Give qualified, still-bound users a bounded clock-failure explanation and retain
  a stable diagnostic reason without logging message bodies or credentials.
  Old-before-binding and malformed messages retain their previous silent rejection;
  revoked owners receive no new outbound notice.
- Keep atomic duplicate suppression. A failed record does not by itself prove
  that nothing executed.
- Restrict any same-ID recovery to an explicit pre-dispatch reason, a bounded
  attempt/time window and the same original binding and message fingerprint.
- Do not re-execute dispatched, uncertain, completed, reply-failed or legacy
  generic-failed messages. Rebinding must never move a previously rejected message
  into a different Chat.

## Configuration and recovery limits

`CLAUDE_HUB_FEISHU_MESSAGE_FUTURE_SKEW_SECONDS` configures the production
`feishu_message_future_skew_seconds` setting. It defaults to `5.0` and must be
finite and nonnegative; zero requests the former strict upper bound. It does not
expand the activation lower bound or permit an activation time after the host's
current time.

A retryable record must explicitly identify a pre-dispatch
`message_future_over_skew` rejection. It allows at most two total attempts within
ten minutes and requires the same original Bot, pairing, tab/workspace, chat,
sender and message identity, plus a SHA-256 fingerprint of the normalized message.
A changed payload or binding, expired window or exhausted attempt budget remains
blocked. A failed rejection notice is also non-rerunnable because its send result
may be uncertain.

This patch is separate from Dot's final-answer delivery policy and the proposed
frontend receipt UI. It does not add a durable inbox or alter the SDK's delivery
acknowledgement contract. Accepting a safe redelivery is not a promise that
Feishu will redeliver an event already acknowledged by the SDK.

## Workspace boundary

The source baseline is `3f935616eee4c41bf95395875a7d310d42cd8edc`.
The repair uses a task-local Git copy and feature worktree because the session's
write boundary excludes changes to the primary repository. The primary checkout,
its local commits, bot bindings and running services are preserved.

## Cross-validation corrections

- Use exact rational clock bounds and the configured decimal allowance rather
  than an epsilon or floating-point addition; zero allowance remains strict.
- Recheck the captured binding/message context after awaited profile refresh,
  including a previously unbound message. A claim check alone is not sufficient.
- Preserve legacy claim vetoes and reject retries when elapsed wall time is negative
  or the retry context is incomplete.
- Preserve the old no-response behavior for messages from before binding. Current
  authorized users still receive an explanation for host-clock failures.
- A dedicated local admission exception proves that a time rejection happened
  before native submission. Preserve this reason through batch outcomes instead
  of treating invocation of the Chat bridge as proof that the provider executed.
  A generic provider error with matching HTTP headers does not provide this proof.
- Real intake/Chat-admission tests move the clock while completion subscription
  is pending and then redeliver the same event, asserting one eventual native send.

## Validation

- Baseline focused gate: `test_feishu_bot_admission_guard.py` and
  `test_feishu_message_dedup_migration.py`: **200 passed**.
- Repair-focused gate (Bot pool, admission, dedup migration, timestamp precision
  and full intake redelivery): **325 passed**.
- All Feishu test modules: **538 passed**, including the updated bookkeeping
  failure injection that must not send a second reply.
- Backend type check: **535 source files, no issues**.
- Strict standard gate: **all nine targets passed**; backend **4330 passed,
  10 skipped**, frontend **1029 passed**, with format/type/lint/build and checkout
  safety checks passing.
- Independent review found a late admission-time classification gap; it was fixed
  and re-reviewed. Final reviewed runtime/tests tree:
  `08a0354c0db87d0b256f02d387e3f79a22133392`, with no remaining findings.
- The final working-log result update is documentation-only. No live Feishu or
  Mac mini replay, browser suite or terminal HMR/replay suite was run for this
  backend-only patch.

## Delivery boundary

No original user message is replayed. No service is restarted, and no branch is
merged or pushed as part of this repair request.
