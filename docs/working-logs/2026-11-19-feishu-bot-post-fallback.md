# Feishu Bot formatted delivery rejection fallback

Formatted Bot replies previously returned false after a rejected post without
trying text. Proactive notices could fall back from a card to a post, but not
from a rejected post to text. The card catch also covered generic Bot errors,
including uncertain-delivery errors that must not be retried.

Reply delivery now catches only FeishuBotRemoteError from a non-text payload.
It logs the safe exception description, remote_message and log_id, checks current
delivery ownership and the caller's still_valid predicate again inside the Bot
gate, and makes one reply_text attempt with the original thread flag and token.
The fallback receipt goes through the existing sent-message/thread bookkeeping.

Proactive delivery retains card-to-post fallback only after explicit remote
rejection, adds a final send_text attempt after a rejected post, and rechecks
current ownership and binding before every attempt. Generic, timeout and
uncertain-delivery exceptions stop the chain and log only their class names.
A failed text attempt remains observable as false; fallback cannot guarantee
delivery when Feishu also rejects text or the transport fails.

## Incident analysis boundary

The two historical replies generated well-formed local post JSON payloads of
2476 and 2643 UTF-8 bytes, with no local control/fence/size validation failures.
The later rejected sample did not contain the earlier angle-bracket email
structure. The incident logs retained only HTTP 400 and the exception class,
not the remote response code/message. Thus an angle-bracket root cause is not
established, and no speculative formatting transformation was added.
A synthetic nested-list/inline-code/email example checks that local formatting
remains representable and bounded. This does not claim server acceptance.
No historical reply text is committed; no live Feishu sends were performed.

The root changelog exceeds 1500 lines already and is left unchanged to honor
this task's strict limit on every modified file. This focused log records the
behavioral change and investigation instead.

## Validation and handoff

Focused regressions cover HTTP 400 post fallback with both thread flag shapes,
receipt ID and landed-thread tracking, current/binding invalidation inside the
gate, proactive card/post/text chains, non-retryable generic/timeout/uncertain
errors, already-text rejection, and failed plain-text fallback.
Full verification, DLP and independent review results are recorded in the Task
report. Integration and live service operations remain controller-owned.
