# Terminal managed Chat delivery reconciliation

## Problem and smallest repair

A resident session can retain processing receipts from initialization, an earlier
Task or a reviewer turn after the provider has finished or failed. The existing
configuration guard correctly blocks changing its model while those receipts
remain. Requiring the old delivery to be a current worker TaskMailbox followup
cannot solve this case and incorrectly mixes delivery lifetime with Task origin.

The operator action closes one proven terminal **session delivery**, without a
receiver ACK, replay, Task completion, review verdict or acceptance. Reuse the
existing workspace/pump locks, immutable envelopes, provider handoff ledger,
indexed lifecycle stream and atomic workspace document. No parallel lifecycle,
state database, reassignment or review reset is introduced.

## API and truthful provenance

`POST /api/workspaces/sessions/{session_id}/reconcile-terminal` accepts
`operation_id`, `call_id`, `reason`, `expected_created_at`, `expected_task_id`,
`expected_review_cycle` and `terminal_sequence`. The authenticated user becomes
the audit actor server-side; a client actor field is rejected.

The expected Task/cycle only guards the resident's **current binding** against
concurrent reassignment. It does not establish an older delivery's origin.
Where an authoritative TaskMailbox record exists, retain its original Task and
cycle, including historical cycles or another Task. Otherwise record
`provenance=session_delivery` and null original Task/cycle. The exact original
payload, attachments, immutable fingerprint and handoff identity remain stored;
Task-looking prompt text is never promoted to authoritative Task provenance.
`guard_task_id`/`guard_review_cycle` distinguish current binding from origin.
A conflicting other-Task source and current Task mirror is rejected.

Require a retained processing envelope, exact accepted handoff turn/digest, no
active turn, and one matching durable start/end pair in this session incarnation.
The production envelope builder and native attachment canonicalization are
shared with sending. Reject missing, uncertain, corrupt, contradictory, active,
pre-delivery-rejected or stale-incarnation evidence. Start/end epoch, identity,
provider, ordering and terminal outcome must agree; any mailbox origin must
predate the turn. Indexed lifecycle reads are bounded.

Persist actor, reason, operation/request identity, original provenance, guard
binding and completed/failed outcome as `reconciled_deliveries`, distinct from
receiver `delivered_call_ids`. Close only the selected session processing entry
and any exact current Task mirror; historical session-only messages do not gain
a current Task tombstone. The Task otherwise retains state, assignment, review,
evidence and acceptance. A separate Task tombstone prevents a mirrored/mailbox
followup retry from resuming work after reassignment. Chat retries also preserve
paused runtime. Same-operation retries return the committed audit result;
conflicts reject. Persistence errors roll back memory unless the atomic document
already committed. There is no automatic replay or model configuration change.
After all blockers are legitimately closed, the original guard can admit a
separately authorized model change.

## Goal-matched verification

Use isolated CPU data and simulate only external provider handoff. Exercise the
real API/manager/ledger/stream/configuration guard, authentication, no semantic
Task/review success, concurrency, idempotency and rollback. Keep private incident
snapshots and exact-call reproduction outside Git; public tests use sanitized
initialization, historical review and failed review payloads. Compare the old
candidate's rejection with new per-delivery closure and original guard admission.
A missing/deleted historical session is a current-state boundary, not proof that
its old receipt can still be reconciled. Record the exact remaining gap instead
of making generic green tests stand for incident recovery.

After focused verification proves the actual applicable snapshot, freeze the
candidate for one final `./scripts/verify.sh all` and independent non-Google
review. Run `check-staged` and private-pattern `check_public_history.py` before
handoff. Actual shared reconciliation, model change, deployment and provider
restart remain outside code-only authorization. No merge, push or self acceptance.
