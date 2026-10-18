# Feishu coalesced-batch admission

## Scope and fixed base

The follow-up review of `9478c12` reproduced a batch whose first message was
valid at native admission while a later message had crossed the future-skew
limit. Both bodies were submitted and both dedup records became completed.

This candidate starts from local main
`5e3da89b1e7f76aa859021b1c74fb8d9da4408c9`, including the restart/drain changes.
The original clock-skew patch is retained. Its application only conflicted in
the changelog; both entries were preserved. The primary checkout and original
`9478c12` worktree remain untouched. Session write restrictions require keeping
this private worktree under the session workspace instead of the usual external
worktree root.

## Repair

- Single-message and batch admission share one synchronous validation helper.
  It checks the current Bot generation, pairing/owner, message time and saved
  target while the caller holds the existing Bot gate.
- Batch admission receives the actual claimed batch, rather than only the first
  event. It acquires the shared Bot gate once, checks every member without an
  intervening await, and retains that gate through native handoff.
- No nested acquisition, new lock owner, queue, background retry or configuration
  was introduced. Follow-up history is marked only after successful handoff.
- Preserve the existing all-or-nothing rejection of an unsent batch. A local time
  rejection is the batch's failure reason, so all members keep the existing
  pre-dispatch status and their own original binding/payload retry context.
  Do not partially submit the batch or silently discard a failed member.
- A cancelled/uncertain rejection notice stays non-rerunnable. Existing native
  cancellation, model-error and reply-failure behavior is unchanged.

## Verification

The integration candidate reproduced the original failure before this repair:
**2 control cases passed, 1 invalid-follower case failed**. The unchanged external
probe now passes all **3 cases**.

The committed regression suite covers first/middle/last members of a three-message
batch, completion-subscription and Goal-lock waits, valid future timestamps,
whole-batch rejection, bounded same-ID redelivery and duplicate suppression,
lock retention through native handoff, unbinding, and cancellation during the
rejection notice. Together with existing real-intake redelivery tests, the
focused run passes **12 tests**.

An initial test-instrumentation error treated the gate context manager as a lock;
that assertion was corrected to inspect the existing gate registry's lock.
No production behavior was changed to accommodate that test error.

Strict standard validation passed all nine targets: backend **4341 passed,
10 skipped**, frontend **1029 passed**, with formatting, type checking, lint and
build checks passing. The full Feishu-focused scope passed **549 tests**.
Independent follow-up review approved tree
`f3c100450206f73b9e143c95cc6fa5b320779119` without actionable findings in the
batch-admission scope. The final validation-result update is documentation-only;
its document and staged checks are repeated before committing.

## Boundaries

All external HTTP/native sends in the regression tests are replaced with test
boundaries. No real Feishu message or historical record was replayed. No live
Mac mini, browser, terminal HMR/replay, push, merge into main, deployment or shared
service restart is part of this patch. Previously discussed Dot/UI changes and a
full durable inbox remain out of scope.
