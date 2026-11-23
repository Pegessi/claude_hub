# MASO uncertain-delivery quarantine reconciliation, Stop fencing, and local-rejection turn closure

Task: `accb041b-d0ca-41cb-b84d-f08c007ac392`<br>
Base: `443a27b3`<br>
Branch: `feat/maso-stop-turn-accb041b`<br>
Worktree: isolated feature checkout

## Outcome and ownership

1. **MASO uncertain-delivery quarantine reconciliation and delayed-enqueue fencing (`maso_http.py`, `providers/session.py`)**:
   - `.maso-delivery.json` persists `post_acknowledged` and `turn_id` alongside `uncertain` and `abort_required`.
   - When `POST /messages` has been acknowledged (`post_acknowledged=True`) and a later SSE disconnect or status-monitor failure leaves `_delivery_uncertain=True`, `_prepare_for_send()` reconciles and clears the quarantine only after verifying `GET /status` is idle (`is_running=False`, `status="idle"`, no active tree/condensation), `GET /messages/pending/{message_id}` returns `404`, and a second `GET /status` confirms the same `agent_id` is still idle with no intervening activity.
   - When `POST /messages` itself failed or timed out before HTTP acknowledgment (`post_acknowledged=False`), auto-reconciliation remains fail-closed even if `status` is idle and `pending/{id}` returns `404`, because the server handler may still be paused before broker enqueue.
   - Explicit Stop (`cancel_active_turn`) runs synchronously (`await_teardown=True, require_teardown=True`) for active turns and when `turn_in_flight=False` with `_delivery_uncertain=True` or `_needs_abort=True`. For acknowledged deliveries it deletes any pending message, aborts, exits, confirms a live idle resumed session, and clears the sidecar. For unacknowledged deliveries (`post_acknowledged=False`), after deleting pending, aborting, and exiting the old session, it allocates a fresh MASO session, writes a replayable `adopted_session_id` and text-seed journal to `.maso-delivery.json` before persisting the new conversation ID and existing fork-seed sidecar, resets `_hub_runtime_guidance_injected` and `_dot_hidden_system_pack_delivered`, and seeds prior completed Hub turns into the new session's first prompt so any delayed enqueue stays fenced on the retired session ID while preserving prior Q/A context.

2. **Cold/legacy targeted Stop, authoritative recovery UI, and `abort_required` status (`retirement.py`, `maso_events.py`, `tailer.py`, `managed_chat.py`, `terminal_runtime/manager/status.py`, `useChatDelivery.ts`)**:
   - `runtime/input.py` records the admitted `client_turn_id` on `transport._quarantined_turn_id` only after `_prepare_for_send()` succeeds, and `maso_http.py` persists `turn_id` in `.maso-delivery.json`. For legacy sidecars written without `turn_id`, `maso_http.py` recovers the quarantined turn ID only when the durable event store identifies one unambiguous unresolved turn (excluding `delivery_rejected: True` turns) so cold targeted `manager.cancel_turn(session, expected_turn_id=T)` succeeds while still rejecting stale `expected_turn_id` hints.
   - `_native_runtime_snapshot`, `managed_chat._runtime_snapshot`, `managed_chat.runtime_status`, and `get_tab_agent_status` report `AgentRuntimeStatus.ATTENTION` (`Delivery uncertain — stop or inspect session`) whenever `.maso-delivery.json` or the hydrated transport has `_delivery_uncertain=True` or `_needs_abort=True`.
   - `maso_events.py`, `runtime/input.py`, and `runtime/retirement.py` attach structured `delivery_uncertain: True` and `quarantined_turn_id` metadata to `turn_completed` events and `StreamCapabilities` (`recovery_required`, `quarantined_turn_id`). `useChatDelivery.ts` drives `stopArmed` and `stopTargetTurnId` from these authoritative fields rather than English error strings, and keeps Stop armed when provider teardown fails (`HTTP 503`) or returns a no-op (`cancelled: false`).

3. **Pre-delivery local rejection turn closure (`runtime/input.py`, `providers/session.py`)**:
   - Separated per-submission `_submission_delivery_uncertain` from persistent `transport._delivery_uncertain`. When `_prepare_for_send()` or local validation rejects a send before `POST /messages` starts, `runtime/input.py` immediately emits `ERROR` + `TURN_COMPLETED(status="failed", delivery_rejected=True)` and clears `_active_turn_id`, preventing ghost unfinished turns, false idle warnings, and `runtime_interrupted` orphan errors across user retries and scheduled tasks.

## Validation evidence

- Focused backend suite: `./scripts/verify.sh backend-tests -- tests/test_maso_http.py -q`.
- Focused frontend unit suite: `node --test tests/agentStreamStopReset.test.mjs`.
- Full verification gate (`./scripts/verify.sh all`) and mocked browser composer gate (`./scripts/verify.sh browser-mocks`).

## Recovery review closure

- Stop now waits for MASO cleanup, including active turns; other providers keep
  their existing asynchronous cancellation. Failed cleanup keeps Stop retryable.
- Adoption is replayed after identity, seed or unlink failure. The candidate is
  never discarded once its journal is durable, and a second restart after journal
  removal still loads the pending text seed. The composer explains the new-session
  boundary and loss of provider-only images/tool state before recovery.
- Recovery status takes priority over cached terminal/error snapshots. Capability
  errors retain known recovery fields. The UI clears event evidence only after a
  successful capability read begun after that event; neither HTTP 200 nor a no-op
  cancellation suppresses positive recovery state.
- Legacy matching ignores all events of locally rejected turns and refuses a
  targeted historical Stop when the quarantine cannot be correlated.
- Fault injection covers five durable adoption boundaries, cold/legacy Stop,
  failed teardown, stale targets, failed capability reads and late recovery events.
  Browser coverage includes reload, failed/no-op Stop and successful recovery.
- Public candidate history is checked with a private catalog extended to actual
  execution path variants and diagnosis identities. Private operational evidence
  remains outside the repository.

Provider HTTP calls in these checks are mocked. No claim is made about live
provider availability or cancellation of a delayed remote handler's external
side effects. No shared service or real conversation is modified.
