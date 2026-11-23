# MASO uncertain-delivery quarantine reconciliation, Stop fencing, and local-rejection turn closure

Task: `accb041b-d0ca-41cb-b84d-f08c007ac392`<br>
Base: `443a27b3`<br>
Branch: `feat/maso-stop-turn-accb041b`<br>
Worktree: `<isolated feature worktree>`

## Outcome and ownership

1. **MASO uncertain-delivery quarantine reconciliation and delayed-enqueue fencing (`maso_http.py`)**:
   - `.maso-delivery.json` now persists `post_acknowledged` and `turn_id` alongside `uncertain` and `abort_required`.
   - When `POST /messages` has been acknowledged (`post_acknowledged=True`) and a later SSE disconnect or status-monitor failure leaves `_delivery_uncertain=True`, `_prepare_for_send()` reconciles and clears the quarantine only after verifying `GET /status` is idle (`is_running=False`, `status="idle"`, no active tree/condensation), `GET /messages/pending/{message_id}` returns `404`, and a second `GET /status` confirms the same `agent_id` is still idle with no intervening activity.
   - When `POST /messages` itself failed or timed out before HTTP acknowledgment (`post_acknowledged=False`), auto-reconciliation remains fail-closed even if `status` is idle and `pending/{id}` returns `404`, because the server handler may still be paused before broker enqueue.
   - Explicit Stop (`cancel_active_turn`) now runs when `turn_in_flight=False` and `_delivery_uncertain=True`. For acknowledged deliveries it deletes any pending message, aborts, exits, confirms a live idle resumed session, and clears the sidecar. For unacknowledged deliveries (`post_acknowledged=False`), after deleting pending, aborting, and exiting the old session, it allocates and adopts a fresh MASO session so any delayed enqueue stays fenced on the retired session ID.

2. **Cold targeted Stop and quarantine runtime status (`retirement.py`, `tailer.py`, `managed_chat.py`, `terminal_runtime/manager/status.py`, `useChatDelivery.ts`)**:
   - `runtime/input.py` records the admitted `client_turn_id` on `transport._quarantined_turn_id` only after `_prepare_for_send()` succeeds, and `maso_http.py` persists `turn_id` in `.maso-delivery.json`. Cold `manager.cancel_turn(session, expected_turn_id=T)` matches the persisted `turn_id` even after `tailer.start()` has terminalized the unfinished orphan turn, while still rejecting stale `expected_turn_id` hints.
   - `_native_runtime_snapshot`, `managed_chat._runtime_snapshot`, `managed_chat.runtime_status`, and `get_tab_agent_status` report `AgentRuntimeStatus.ATTENTION` (`Delivery uncertain — stop or inspect session`) whenever `.maso-delivery.json` has `uncertain=true` or `abort_required=true`, preventing cold restarts from reporting `Ready` while sends are refused.
   - In the frontend composer (`useChatDelivery.ts`), `stopArmed` remains armed and `stopTargetTurnId` targets the quarantined turn (ignoring subsequent `delivery_rejected: true` turns) until Stop succeeds or a later turn completes normally.

3. **Pre-delivery local rejection turn closure (`runtime/input.py`, `providers/session.py`)**:
   - Separated per-submission `_submission_delivery_uncertain` from persistent `transport._delivery_uncertain`. When `_prepare_for_send()` or local validation rejects a send before `POST /messages` starts, `runtime/input.py` immediately emits `ERROR` + `TURN_COMPLETED(status="failed", delivery_rejected=True)` and clears `_active_turn_id`, preventing ghost unfinished turns, false idle warnings, and `runtime_interrupted` orphan errors across user retries and scheduled tasks.

## Validation evidence

- Focused backend suite: `./scripts/verify.sh backend-tests -- tests/test_maso_http.py -q` (73 passed).
- Focused frontend unit suite: `node --test tests/agentStreamStopReset.test.mjs` (10 passed).
- Full verification gate (`./scripts/verify.sh all`) and mocked browser composer gate (`./scripts/verify.sh browser-mocks`).
