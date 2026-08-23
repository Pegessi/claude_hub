"""Session messaging and tmux output capture.

Delivery semantics: **at-least-once with fail-closed uncertainty**.

The Hub does NOT own a verifiable durable receiver for tmux stdin. Bytes
written to tmux may be consumed by the agent and then lost if the agent
process crashes, and the Hub cannot read back what the model actually
processed. Therefore we cannot guarantee exactly-once delivery.

Instead we guarantee **fail-closed**: a call_id is always in exactly one of
four states:

  pending ──persist intent──▶ processing (in-flight) ──worker ACK──▶ delivered
     │                              │
     │         send failed          │  STOPPED session / tmux gone
     └──────────────────────────────┤  (receipt unqueryable)
                                    ▼
                              uncertain (fail-closed)

  For LIVE (WORKING) sessions, ``processing`` call_ids are reconciled
  against the tmux-server receipt on cold start / monitor tick:
    * receipt present  → stay in ``processing`` (no repaste, await ACK)
    * receipt absent   → move back to ``pending`` (one safe re-delivery)
    * session gone     → move to ``uncertain`` (fail-closed)

* ``pending_call_ids``: message persisted in ``pending_messages``, not yet
  sent to tmux. Always re-deliverable on restart.
* ``processing_call_ids``: the intent to deliver was persisted (call_id
  moved out of pending) and the pump is sending / has sent the message to
  tmux, but the worker has not ACKed. "Maybe delivered" — we cannot prove
  the message reached the tmux input buffer. On cold restart, LIVE sessions
  keep the call_id here for receipt-based reconciliation; STOPPED sessions
  move it to ``uncertain``.
* ``uncertain_call_ids``: the call_id was in-flight when the Hub crashed or
  the tmux session disappeared (STOPPED/gone, receipt unqueryable).
  Fail-closed: we do NOT auto-resend (could duplicate) and do NOT silently
  mark delivered (could lose). A ``delivery:uncertain`` event is emitted to
  the supervisor. Moves to delivered on worker ACK, or back to pending on
  explicit operator retry (``retry_uncertain_delivery``) only if the tmux
  session is alive and the receipt is absent.
* ``delivered_call_ids``: the worker ACKed the call_id (listed it in
  ``acked_call_ids`` of a report). Only the worker's ACK moves a call_id
  here.

The ``[call_id:<id>]`` marker is embedded in the message body so the worker
can correlate its ACK. It is NOT used by the Hub as a receipt or dedup
basis — tmux pane history is not durable (markers roll out of the scroll
buffer), so relying on it for cold-recovery dedup would produce duplicate
deliveries.

**Duplicate delivery and platform-provided durable dedupe:**

The Hub provides durable dedupe through three mechanisms; the worker does
NOT need to keep a conversational or scratch-file processed-call list:

1. **Sender-side state machine.** A call_id is always in exactly one of
   ``pending``, ``processing``, ``delivered``, or ``uncertain``. The
   sender skips call_ids already in ``processing``, ``delivered``, or
   ``uncertain`` — they are never re-enqueued. This is durable across Hub
   restarts because the state is persisted in ``state.json``.

2. **tmux-server receipt (sender-side fence for a live session).** The
   receipt-aware delivery primitive (``_send_tmux_message_with_receipt``)
   records a tmux session user option atomically with the paste+submit. A
   later replay of the same call_id on the *same* tmux session sees the
   receipt and skips the paste — at-most-once paste per call_id per tmux
   session lifetime. The receipt is scoped to the tmux session; it does
   not survive a destroyed/recreated session.

3. **Receipt-gated re-delivery for LIVE sessions; fail-closed for gone
   sessions.** For a LIVE (WORKING) session, ``_recover_processing_via_receipt``
   queries the tmux-server receipt:
     * receipt present → stay in ``processing``, no repaste (await ACK).
     * receipt absent → move to ``pending`` for **one** safe re-delivery.
   If the tmux session is gone / unqueryable (or STOPPED) when a call_id
   is in ``processing`` or ``uncertain``, the call_id moves to (or stays
   in) ``uncertain`` and is NOT automatically re-delivered. Only the
   explicit operator-facing ``retry_uncertain_delivery`` endpoint may
   move it back to ``pending``, and only if the tmux session is alive
   and the receipt is absent. If the receipt is present (the paste
   already happened), the retry does NOT re-paste — it just ensures the
   already-pasted input is submitted and waits for the worker's ACK.

**Worker ACK is the durable commit.** A call_id only leaves
``processing_call_ids`` (or ``uncertain_call_ids`` after a receipt-present
retry) when the worker ACKs it by listing it in ``acked_call_ids`` of a
report. The ACK moves the call_id to ``delivered_call_ids``, from which it
is never re-sent. The ``[call_id:<id>]`` marker embedded in the message
body lets the worker correlate its ACK; it is NOT used by the Hub as a
receipt or dedup basis.

Because tmux stdin is not a verifiable durable receiver, an explicit
operator retry of an ``uncertain`` call_id MAY deliver the same message
to the worker twice (if the first ambiguous send actually succeeded and
the tmux session was recreated). This is an explicitly documented,
human-authorized consequence of the fail-closed uncertainty model — we
do NOT claim exactly-once effects. The Hub's guarantee is at-least-once
delivery with fail-closed uncertainty and platform-provided dedupe within
a single tmux session lifetime; exactly-once *effects* across session
recreation require the operator to verify the worker state before
retrying.
"""

from typing import TYPE_CHECKING

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ..agent_tree import _request_fingerprint
from ._constants import *  # noqa: F401,F403

if TYPE_CHECKING:
    from claude_hub.models.agent_tree import AgentRun


class _MessagingMixin:
    async def send_session_message(
        self,
        session_id: str,
        message: str,
        attachments: list[WorkspaceAttachmentCreate] | None = None,
        call_id: str | None = None,
    ) -> None:
        """Enqueue a message for a managed session's terminal.

        Sender-side semantics: **at-least-once, fail-closed**.

        The sender does NOT write to tmux directly. Instead it:
          1. Skips call_ids already in ``delivered_call_ids`` (worker ACKed)
             or ``processing_call_ids`` / ``uncertain_call_ids`` (already
             in-flight or uncertain — re-enqueuing would risk a duplicate).
          2. Persists the message body in ``session.pending_messages[call_id]``
             and appends ``call_id`` to ``pending_call_ids``.
          3. Triggers the receiver pump (``_pump_session_messages``).

        The pump performs the actual delivery: it persists the intent
        (pending → processing) BEFORE sending to tmux. On send success the
        call_id stays in ``processing_call_ids`` (in-flight, awaiting the
        worker's ACK). On a **pre-side-effect** failure (tmux write not
        attempted) the call_id rolls back to ``pending_call_ids`` for retry.
        On an **ambiguous** failure (tmux write may have succeeded) the
        call_id moves to ``uncertain_call_ids`` (fail-closed: no auto-retry,
        no silent delivered); only ``retry_uncertain_delivery`` may move it
        back to ``pending``.

        A call_id only leaves ``processing_call_ids`` when:
          * the worker ACKs it (→ ``delivered_call_ids``), or
          * the session is STOPPED / tmux gone at cold start
            (→ ``uncertain_call_ids``, fail-closed), or
          * receipt-based reconciliation on a LIVE session finds the receipt
            absent (→ ``pending_call_ids`` for one safe re-delivery).
          (Receipt present → stays in ``processing``, no repaste.)
        """
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        if call_id:
            # Compute the canonical payload fingerprint (message text +
            # per-attachment filename, normalized mime, and sha256 of the
            # decoded bytes). This is done BEFORE any filesystem or state
            # mutation so a conflicting-payload send is rejected without
            # touching disk.
            new_fp = self._compute_payload_fingerprint(message, attachments or [])

            # Fingerprint-first immutable-payload check. A call_id identifies
            # a single durable delivery; reusing it with a different payload
            # is rejected at EVERY state (pending / processing / delivered /
            # uncertain). Only the identical payload is idempotent.
            #
            # Recovery paths that rebuild the prompt (which may drift) must
            # NOT go through send_session_message — they use
            # resume_existing_call, which operates on the persisted envelope
            # without comparing fingerprints.
            existing_fp = session.call_payload_fingerprints.get(call_id)

            # ------------------------------------------------------------------
            # Legacy backfill: a call_id may be present in a state list
            # (pending / processing / delivered / uncertain) but have no
            # entry in call_payload_fingerprints if it was persisted before
            # the fingerprint field was introduced.
            #
            # We must NOT silently treat such a call_id as "absent" and
            # overwrite its envelope. Instead:
            #   * pending / processing / uncertain with a stored message +
            #     attachments: derive the fingerprint from the STORED
            #     envelope, backfill it, then run the normal
            #     fingerprint-vs-new-payload comparison.
            #   * delivered with no retained payload: the worker already
            #     ACKed it; fail closed by treating it as delivered (no-op)
            #     — we never re-send a delivered call_id.
            #   * inconsistent (call_id in a non-delivered state list but no
            #     stored envelope): fail closed — raise, never treat as new.
            # ------------------------------------------------------------------
            in_any_state = (
                call_id in session.pending_call_ids
                or call_id in session.processing_call_ids
                or call_id in session.delivered_call_ids
                or call_id in session.uncertain_call_ids
            )
            if existing_fp is None and in_any_state:
                stored_msg = session.pending_messages.get(call_id)
                stored_atts = session.pending_attachments.get(call_id)
                if stored_msg is not None:
                    # We have the stored envelope — derive its fingerprint
                    # from the persisted attachment files (not data_url) and
                    # backfill it so the immutable-payload invariant applies
                    # from now on. Fail closed if any persisted file is
                    # missing/corrupt.
                    existing_fp = self._compute_persisted_payload_fingerprint(
                        stored_msg, stored_atts or []
                    )
                    fps = dict(session.call_payload_fingerprints)
                    fps[call_id] = existing_fp
                    self.sessions[session_id] = session.model_copy(
                        update={"call_payload_fingerprints": fps}
                    )
                    self._save_state()
                    session = self.sessions[session_id]
                elif call_id in session.delivered_call_ids:
                    # Delivered but no retained payload (legacy). The worker
                    # already ACKed it; fail closed as delivered no-op. We
                    # cannot verify the payload matches, so we never re-send.
                    logger.debug(
                        "send_session_message call_id=%s delivered (legacy, "
                        "no fingerprint/payload); no-op",
                        call_id,
                    )
                    return
                else:
                    # Inconsistent: call_id in a non-delivered state list but
                    # no recoverable envelope. Fail closed — never treat as
                    # a new delivery.
                    raise RuntimeError(
                        f"inconsistent state: call_id={call_id} present in "
                        f"session {session_id} state lists but has no stored "
                        "envelope and no fingerprint"
                    )

            if existing_fp is not None:
                if new_fp != existing_fp:
                    raise ValueError(
                        f"call_id {call_id} already used with a different "
                        "payload; cannot mutate a committed delivery. Use a "
                        "new call_id for a different message."
                    )
                # Identical payload: idempotent. State decides the action.
                if call_id in session.uncertain_call_ids:
                    raise DeliveryUncertain(
                        f"delivery of call_id={call_id} to session {session_id} is "
                        "uncertain (ambiguous tmux send failure); explicit operator "
                        "retry required via retry_uncertain_delivery"
                    )
                if call_id in session.pending_call_ids:
                    # Same payload, still pending: pump the stored envelope.
                    await self._pump_session_messages(session_id)
                    session = self.sessions[session_id]
                    if call_id in session.uncertain_call_ids:
                        raise DeliveryUncertain(
                            f"delivery of call_id={call_id} to session {session_id} is "
                            "uncertain (ambiguous tmux send failure); operator retry "
                            "required"
                        )
                    return
                if call_id in session.processing_call_ids:
                    logger.debug(
                        "send_session_message call_id=%s already in-flight for "
                        "session %s; skipping",
                        call_id,
                        session_id,
                    )
                    return
                if call_id in session.delivered_call_ids:
                    # Committed delivery: identical payload is idempotent no-op.
                    logger.debug(
                        "send_session_message call_id=%s already committed by "
                        "session %s; skipping",
                        call_id,
                        session_id,
                    )
                    return
                # Inconsistent: a fingerprint exists for this call_id but it is
                # not in any state list (pending / processing / delivered /
                # uncertain). Fail closed rather than silently discarding the
                # request or treating it as a new delivery.
                raise RuntimeError(
                    f"inconsistent state: call_id={call_id} has a stored "
                    f"fingerprint in session {session_id} but is absent from "
                    "all delivery state lists (pending/processing/delivered/"
                    "uncertain)"
                )

            # call_id has no stored fingerprint — first send to this session.
            # Persist attachments using a safe owner id (sha256 of the
            # call_id) rather than the raw call_id, which may contain "/" or
            # ".." and escape the attachments directory.
            safe_owner = self._safe_attachment_owner_id(session.id, call_id)
            persisted = self._persist_attachments(
                session.workspace_id,
                safe_owner,
                attachments or [],
            )
            pending_messages = dict(session.pending_messages)
            pending_messages[call_id] = message
            pending_attachments = dict(session.pending_attachments)
            pending_attachments[call_id] = persisted
            pending_call_ids = list(session.pending_call_ids)
            if call_id not in pending_call_ids:
                pending_call_ids.append(call_id)
            call_payload_fingerprints = dict(session.call_payload_fingerprints)
            call_payload_fingerprints[call_id] = new_fp
            self.sessions[session_id] = session.model_copy(
                update={
                    "pending_messages": pending_messages,
                    "pending_attachments": pending_attachments,
                    "pending_call_ids": pending_call_ids,
                    "call_payload_fingerprints": call_payload_fingerprints,
                }
            )
            self._save_state()
            session = self.sessions[session_id]

            # Kick the receiver pump to deliver the message.
            await self._pump_session_messages(session_id)
            # After the pump, check whether the call_id landed in
            # uncertain_call_ids (ambiguous tmux send failure). If so, raise
            # DeliveryUncertain so the caller knows the dispatch failed and
            # can surface it / retry explicitly. The call_id and payload
            # remain persisted in uncertain_call_ids / pending_messages.
            session = self.sessions[session_id]
            if call_id in session.uncertain_call_ids:
                raise DeliveryUncertain(
                    f"delivery of call_id={call_id} to session {session_id} is "
                    "uncertain (ambiguous tmux send failure); operator retry "
                    "required"
                )
            return

        # No call_id: fire-and-forget (no delivery guarantee). Used for
        # one-shot prompts that don't need at-least-once semantics.
        persisted = self._persist_attachments(
            session.workspace_id,
            f"{session.id}-message-{uuid.uuid4().hex[:8]}",
            attachments or [],
        )
        await self._ensure_session_ready_for_send(session)
        await self._send_tmux_message(
            session.tmux_session,
            self._append_attachment_block(message, persisted),
        )

    async def resume_existing_call(
        self,
        session_id: str,
        call_id: str,
    ) -> bool:
        """Resume a previously-persisted call_id using its stored envelope.

        Recovery-only path. Unlike :meth:`send_session_message`, this does
        NOT accept a new payload and does NOT compare fingerprints. It
        operates solely on the persisted envelope so that a rebuilt prompt
        (which may drift due to lesson context, timestamps, etc.) never
        overwrites the original delivery.

        State semantics:

        * ``pending``    — pump the stored payload to tmux.
        * ``processing`` — no-op (already in flight).
        * ``delivered``  — no-op (already ACKed by the worker).
        * ``uncertain``  — raise :class:`DeliveryUncertain` (operator retry
          only; no silent auto-resend).
        * ``absent``     — return ``False`` so the caller knows a fresh send
          via :meth:`send_session_message` is required.

        Returns ``True`` if the call_id was found and handled (pumped or
        no-op), ``False`` if it is absent.
        """
        if session_id not in self.sessions:
            raise KeyError(f"session {session_id} not found")
        session = self.sessions[session_id]

        if call_id in session.uncertain_call_ids:
            raise DeliveryUncertain(
                f"delivery of call_id={call_id} to session {session_id} is "
                "uncertain (ambiguous tmux send failure); operator retry "
                "required"
            )

        if call_id in session.pending_call_ids:
            logger.debug(
                "resume_existing_call call_id=%s pending; pumping stored " "envelope",
                call_id,
            )
            await self._pump_session_messages(session_id)
            session = self.sessions[session_id]
            if call_id in session.uncertain_call_ids:
                raise DeliveryUncertain(
                    f"delivery of call_id={call_id} to session {session_id} is "
                    "uncertain (ambiguous tmux send failure); operator retry "
                    "required"
                )
            return True

        if call_id in session.processing_call_ids:
            logger.debug(
                "resume_existing_call call_id=%s already in-flight for " "session %s; no-op",
                call_id,
                session_id,
            )
            return True

        if call_id in session.delivered_call_ids:
            logger.debug(
                "resume_existing_call call_id=%s already committed by " "session %s; no-op",
                call_id,
                session_id,
            )
            return True

        # call_id absent from all state lists.
        return False

    async def _pump_session_messages(self, session_id: str) -> None:
        """Receiver pump: deliver pending call_ids to the tmux inbox.

        Lifecycle of a call_id through the pump:

          pending_call_ids  ──persist intent──▶  processing_call_ids  ──worker ACK──▶  delivered_call_ids
                │                                       │
                │            send failed                │  crash / tmux gone
                └───────────────────────────────────────┤
                                                        ▼
                                                  uncertain_call_ids

        **Persist intent before side effect:**

        For each pending call_id the pump:

        1. **Persist intent**: move the call_id from ``pending_call_ids`` to
           ``processing_call_ids`` and persist. This is the durable record
           that we intend to deliver this call_id. If we crash here, the
           call_id is in ``processing_call_ids`` and will be moved to
           ``uncertain_call_ids`` on cold recovery (fail-closed).

        2. **Send**: send the message to tmux. The send is synchronous.

        3. **On success**: the call_id stays in ``processing_call_ids``
           (in-flight, awaiting the worker's ACK). We do NOT move it to
           delivered — only the worker's ACK does that.

        4. **On failure**:
           - **Pre-side-effect** (exception before the tmux write, e.g.
             ``_ensure_session_ready_for_send`` fails): roll the call_id back
             to ``pending_call_ids`` so the next pump cycle can retry.
           - **Ambiguous** (exception inside ``_send_tmux_message``): the tmux
             write may have succeeded. Fail closed: move the call_id to
             ``uncertain_call_ids`` (no auto-retry, no silent delivered).
             Only ``retry_uncertain_delivery`` may move it back to
             ``pending``.

        **Why no pane-marker dedup:**

        We used to capture the tmux pane and check for the ``[call_id:<id>]``
        marker to decide whether a message was already sent. This is
        unreliable: tmux pane history is bounded and the marker can roll out
        of the scroll buffer, so cold recovery would not see it and would
        re-send the message (duplicate delivery). We no longer use pane
        contents as a receipt or dedup basis.

        **Worker ACK** (``_ack_call_ids``): the worker confirms it processed
        the message by including the call_id in ``acked_call_ids``. Only the
        worker's ACK moves the call_id to ``delivered_call_ids`` and removes
        the message from ``pending_messages``.
        """
        session = self.sessions.get(session_id)
        if not session:
            return

        # Serialize pump cycles per session so two concurrent calls cannot
        # both send the same pending call_id to tmux.
        lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            await self._pump_session_messages_locked(session_id)

    async def _pump_session_messages_locked(self, session_id: str) -> None:
        """Inner pump logic — caller must hold ``self._pump_locks[session_id]``."""
        session = self.sessions.get(session_id)
        if not session:
            return

        # Snapshot the pending call_ids to deliver. We iterate over a copy
        # because we mutate the session during the loop.
        pending = list(session.pending_call_ids)
        for call_id in pending:
            # Re-fetch the session each iteration: a previous iteration may
            # have mutated it.
            session = self.sessions.get(session_id)
            if not session:
                return
            if call_id not in session.pending_call_ids:
                # Already claimed by a concurrent pump cycle.
                continue

            message = session.pending_messages.get(call_id)
            if message is None:
                # Inbox entry missing — data integrity error. We cannot
                # deliver a message whose body we no longer have, and we
                # MUST NOT silently mark it delivered (the worker never
                # received it). Fail closed: move the call_id from pending
                # to uncertain_call_ids and emit a delivery:uncertain event
                # so an operator can investigate. The call_id is NOT
                # re-deliverable (no body) but stays visible as uncertain.
                logger.error(
                    "pump: call_id=%s in pending_call_ids but missing from "
                    "pending_messages; moving to uncertain (fail-closed, "
                    "no silent delivered)",
                    call_id,
                )
                pending_call_ids = [c for c in session.pending_call_ids if c != call_id]
                uncertain = list(session.uncertain_call_ids)
                if call_id not in uncertain:
                    uncertain.append(call_id)
                self.sessions[session_id] = session.model_copy(
                    update={
                        "pending_call_ids": pending_call_ids,
                        "uncertain_call_ids": uncertain,
                    }
                )
                # Mirror on the task if bound.
                task = self.tasks.get(session.task_id) if session.task_id else None
                if task is not None and call_id in task.pending_call_ids:
                    task_pending = [c for c in task.pending_call_ids if c != call_id]
                    task_uncertain = list(task.uncertain_call_ids)
                    if call_id not in task_uncertain:
                        task_uncertain.append(call_id)
                    self.tasks[task.id] = task.model_copy(
                        update={
                            "pending_call_ids": task_pending,
                            "uncertain_call_ids": task_uncertain,
                        }
                    )
                self._save_state()
                self._emit_delivery_uncertain(session.workspace_id, session_id, call_id)
                continue

            # ---- PERSIST INTENT BEFORE SIDE EFFECT ----
            # Move the call_id from pending to processing (in-flight) and
            # persist BEFORE sending to tmux. This is the durable record of
            # our intent to deliver. If we crash after this point but before
            # the worker ACKs, the call_id is in processing_call_ids and
            # cold recovery moves it to uncertain_call_ids (fail-closed).
            now = datetime.now(timezone.utc)
            pending_call_ids = [c for c in session.pending_call_ids if c != call_id]
            processing_call_ids = list(session.processing_call_ids)
            if call_id not in processing_call_ids:
                processing_call_ids.append(call_id)
            processing_call_ids_at = dict(session.processing_call_ids_at)
            processing_call_ids_at[call_id] = now
            session_update = {
                "pending_call_ids": pending_call_ids,
                "processing_call_ids": processing_call_ids,
                "processing_call_ids_at": processing_call_ids_at,
            }
            task_update: dict[str, Any] = {}
            task = self.tasks.get(session.task_id) if session.task_id else None
            if task is not None and call_id in task.pending_call_ids:
                task_pending = [c for c in task.pending_call_ids if c != call_id]
                task_processing = list(task.processing_call_ids)
                if call_id not in task_processing:
                    task_processing.append(call_id)
                task_update = {
                    "pending_call_ids": task_pending,
                    "processing_call_ids": task_processing,
                }
            self.sessions[session_id] = session.model_copy(update=session_update)
            if task is not None and task_update:
                self.tasks[task.id] = task.model_copy(update=task_update)
            self._save_state()

            # ---- SEND to tmux ----
            # The [call_id:<id>] marker lets the worker correlate its ACK.
            # It is NOT used by the Hub as a receipt or dedup basis.
            marker = f"[call_id:{call_id}]"
            try:
                # Attachments were already persisted by send_session_message
                # into session.pending_attachments[call_id]. Retrieve them
                # here so the delivered message includes the attachment
                # block. We do NOT re-persist (that would duplicate files).
                persisted = session.pending_attachments.get(call_id, [])
                await self._ensure_session_ready_for_send(self.sessions[session_id])
                full_message = f"{marker}\n{message}"
                full_message = self._append_attachment_block(full_message, persisted)
            except Exception:
                # Pre-side-effect failure: the tmux write was NOT attempted.
                # Safe to roll the call_id back to pending so the next pump
                # cycle can retry. The message body stays in pending_messages.
                logger.exception(
                    "pump: pre-send failure for call_id=%s session %s; "
                    "rolling back to pending for retry",
                    call_id,
                    session_id,
                )
                self._rollback_processing_to_pending(session.task_id, session_id, [call_id])
                continue

            try:
                # Use the receipt-aware delivery primitive. The tmux server
                # records a receipt (session user option) atomically with the
                # paste+submit, so a later replay of the same call_id sees the
                # receipt and skips the paste (at-most-once paste per call_id
                # per tmux session lifetime).
                await self._send_tmux_message_with_receipt(
                    self.sessions[session_id].tmux_session,
                    full_message,
                    call_id,
                )
            except Exception:
                # Ambiguous failure: the tmux write MAY have succeeded before
                # the wrapper raised. We cannot prove the message was not
                # delivered, so we fail closed: move the call_id to
                # uncertain_call_ids (do NOT auto-resend, do NOT silently
                # mark delivered) and emit a delivery:uncertain event.
                logger.exception(
                    "pump: ambiguous send failure for call_id=%s session %s; "
                    "moving to uncertain (fail-closed, no auto-retry)",
                    call_id,
                    session_id,
                )
                self._mark_processing_as_uncertain(session_id, [call_id])
                continue

            # ---- SUCCESS: stay in-flight ----
            # The message was sent to tmux. The call_id stays in
            # processing_call_ids (in-flight) until the worker ACKs it.
            # We do NOT move it to delivered — only the worker's ACK does
            # that. If we crash here, cold recovery moves it to
            # uncertain_call_ids (fail-closed).

    def _expire_processing_leases(self, session_id: str, max_age_seconds: float = 300.0) -> int:
        """Move stale ``processing_call_ids`` to ``uncertain_call_ids``.

        A call_id in ``processing_call_ids`` means the pump persisted the
        intent to deliver and sent (or was about to send) the message to
        tmux. The Hub CANNOT prove the message reached the tmux input
        buffer, so we cannot safely re-deliver it (that could duplicate the
        model turn).

        Therefore, for **live** sessions we NEVER move processing call_ids
        back to pending. They stay in processing until the worker ACKs them
        (→ delivered) or the session is stopped (→ uncertain).

        For sessions whose tmux session is gone
        (``ManagedSessionStatus.STOPPED``), the input buffer may have been
        destroyed, but we still cannot prove the message was not delivered
        before the session died. So we move them to ``uncertain_call_ids``
        (fail-closed) rather than back to pending. A ``delivery:uncertain``
        event is emitted to the supervisor.

        Returns the number of call_ids moved to uncertain.
        """
        session = self.sessions.get(session_id)
        if not session:
            return 0
        if not session.processing_call_ids:
            return 0

        # Only expire for sessions whose tmux inbox is gone. For live
        # sessions the message may already be in the tmux input buffer;
        # re-delivering it would risk a duplicate turn.
        if session.status != ManagedSessionStatus.STOPPED:
            return 0

        now = datetime.now(timezone.utc)
        expired: list[str] = []
        for call_id in session.processing_call_ids:
            claimed_at = session.processing_call_ids_at.get(call_id)
            if claimed_at is None:
                # No timestamp — treat as expired (conservative).
                expired.append(call_id)
                continue
            age = (now - claimed_at).total_seconds()
            if age >= max_age_seconds:
                expired.append(call_id)

        if not expired:
            return 0

        self._mark_processing_as_uncertain(session_id, expired)
        return len(expired)

    async def _recover_processing_via_receipt(self, session_id: str) -> int:
        """Reconcile ``processing_call_ids`` against the tmux-server receipt.

        After a Hub restart (or on each monitor tick), a call_id in
        ``processing_call_ids`` may or may not have actually been pasted into
        the tmux input buffer. The tmux-server receipt (a session user option
        set atomically with the paste) lets us distinguish:

        * **receipt present** — the paste happened. Keep the call_id in
          ``processing`` and wait for the worker's ACK. Do NOT re-deliver.
        * **receipt absent, tmux session alive** — the paste did NOT happen
          (the Hub died before the tmux command ran, or the command failed
          before setting the receipt). Safe to move the call_id back to
          ``pending`` so the pump re-delivers it.
        * **tmux session gone / unqueryable** — we cannot prove either way.
          Fail closed: move to ``uncertain`` (no automatic resend; operator
          retry required).

        Returns the number of call_ids whose state changed.
        """
        session = self.sessions.get(session_id)
        if not session or not session.processing_call_ids:
            return 0

        to_pending: list[str] = []
        to_uncertain: list[str] = []

        for call_id in list(session.processing_call_ids):
            try:
                receipt_set = await self._query_tmux_receipt(session.tmux_session, call_id)
            except RuntimeError:
                # Session gone or tmux query failed — fail closed.
                to_uncertain.append(call_id)
                continue
            if receipt_set:
                # Paste happened. The message may still be sitting in the
                # input box if the Hub died between the atomic paste+first
                # C-m and the submit verification loop. Nudge Enter (no
                # re-paste) so the TUI accepts it, then keep the call_id
                # in processing to await the worker's ACK.
                message = session.pending_messages.get(call_id)
                if not message:
                    # Fail closed: without the original message body we
                    # cannot verify whether the input is still pending,
                    # and a blind C-m could submit an unrelated line.
                    # Move to uncertain so an operator can decide.
                    to_uncertain.append(call_id)
                    continue
                try:
                    await self._ensure_submitted_without_repaste(session.tmux_session, message)
                except Exception:
                    # Cannot verify submit state (capture failed or input
                    # still pending after retries). Fail closed: move to
                    # uncertain so an operator can decide. The receipt
                    # remains set so no future replay re-pastes.
                    logger.exception(
                        "pump: submit-nudge failed for receipt-present call_id=%s "
                        "session %s; moving to uncertain",
                        call_id,
                        session_id,
                    )
                    to_uncertain.append(call_id)
                continue
            # Receipt absent on a live session: the paste never ran. Safe to
            # return to pending for re-delivery.
            to_pending.append(call_id)

        changed = 0
        if to_pending:
            self._rollback_processing_to_pending(session.task_id, session_id, to_pending)
            changed += len(to_pending)
        if to_uncertain:
            self._mark_processing_as_uncertain(session_id, to_uncertain)
            changed += len(to_uncertain)
        return changed

    def _mark_processing_as_uncertain(self, session_id: str, call_ids: list[str]) -> None:
        """Move call_ids from ``processing_call_ids`` to ``uncertain_call_ids``.

        Fail-closed: we cannot prove the message was not delivered, so we do
        NOT re-send it and do NOT silently mark it delivered. Emit a
        ``delivery:uncertain`` event to the supervisor so the condition is
        visible and an operator can decide whether to retry.
        """
        if not call_ids:
            return
        uncertain_set = set(call_ids)

        session = self.sessions.get(session_id)
        if session is None:
            return

        processing = [c for c in session.processing_call_ids if c not in uncertain_set]
        uncertain = list(session.uncertain_call_ids)
        for cid in call_ids:
            if cid not in uncertain:
                uncertain.append(cid)
        processing_call_ids_at = {
            cid: ts
            for cid, ts in session.processing_call_ids_at.items()
            if cid not in uncertain_set
        }
        self.sessions[session_id] = session.model_copy(
            update={
                "processing_call_ids": processing,
                "uncertain_call_ids": uncertain,
                "processing_call_ids_at": processing_call_ids_at,
            }
        )

        # Mirror on the task.
        task = self.tasks.get(session.task_id) if session.task_id else None
        if task is not None:
            task_processing = [c for c in task.processing_call_ids if c not in uncertain_set]
            task_uncertain = list(task.uncertain_call_ids)
            for cid in call_ids:
                if cid not in task_uncertain:
                    task_uncertain.append(cid)
            self.tasks[task.id] = task.model_copy(
                update={
                    "processing_call_ids": task_processing,
                    "uncertain_call_ids": task_uncertain,
                }
            )

        self._save_state()

        # Emit a delivery:uncertain event for each call_id so the supervisor
        # (resident root run) can observe the condition.
        for call_id in call_ids:
            self._emit_delivery_uncertain(session.workspace_id, session_id, call_id)

        logger.info(
            "pump: moved %d processing call_ids to uncertain for session %s (fail-closed)",
            len(call_ids),
            session_id,
        )

    def _emit_delivery_uncertain(self, workspace_id: str, session_id: str, call_id: str) -> None:
        """Emit a ``delivery:uncertain`` event to the workspace's resident root run.

        This makes the fail-closed condition visible to the supervisor so an
        operator can decide whether to retry the delivery.
        """
        from claude_hub.models.agent_tree import AgentEventType

        root_run = self.agent_tree.get_run_by_context_ref(workspace_id, session_id)
        if root_run is None:
            # Fall back to the workspace's root run (the resident root).
            for run in self.agent_tree._runs.values():
                if run.workspace_id == workspace_id and run.parent_id is None:
                    root_run = run
                    break
        if root_run is None:
            # Legacy/non-Agent-Tree sessions have no supervisor mailbox.
            # Their fail-closed state remains durable on the session/task,
            # but there is no applicable event recipient.
            return

        self.agent_tree._append_event(
            workspace_id=workspace_id,
            agent_run_id=root_run.id,
            event_type=AgentEventType.PROGRESS,
            author=root_run.id,
            recipient=root_run.id,
            call_id=f"delivery:uncertain:{call_id}",
            action="delivery:uncertain",
            target=session_id,
            fingerprint=_request_fingerprint(
                "delivery:uncertain",
                {"call_id": call_id, "session_id": session_id},
            ),
            payload={
                "call_id": call_id,
                "session_id": session_id,
                "reason": "in-flight call_id lost on session stop / crash; "
                "cannot prove delivery. Operator retry required.",
            },
            rollback_on_error=False,
        )

    async def retry_uncertain_delivery(
        self,
        session_id: str,
        call_id: str,
        *,
        reason: str,
        actor: str,
    ) -> None:
        """Operator-initiated retry of an uncertain delivery.

        Moves ``call_id`` from ``uncertain_call_ids`` back to
        ``pending_call_ids`` so the normal pump path can re-deliver it. The
        original payload (stored in ``pending_messages``) is preserved.

        Fail-closed gates (all raise ``ValueError`` except session-not-found
        which raises ``KeyError`` → HTTP 404):

          * The session MUST exist.
          * The call_id MUST be in the session's ``uncertain_call_ids``.
            Unknown, delivered, processing, and pending call_ids are rejected.
          * The call_id MUST have a stored payload in
            ``session.pending_messages`` (otherwise we cannot re-deliver the
            original message).
          * If the session is bound to a task, the task MUST exist,
            ``task.session_id == session_id`` (no session/task divergence),
            and the call_id MUST also be in ``task.uncertain_call_ids``.

        On success the state transition (uncertain → pending on both session
        and task) is persisted, then a durable ``delivery:retry_requested``
        audit event is emitted. If the event emission fails, the state is
        compensated back to ``uncertain`` and the method raises — no tmux
        send occurs. Only after the audit event is durable does the pump run
        through the normal delivery path.

        After the pump, the session state is re-checked. If the call_id
        landed back in ``uncertain_call_ids`` (the re-send hit another
        ambiguous tmux failure), ``DeliveryUncertain`` is raised so the
        caller surfaces a visible failure (HTTP 400) instead of a false
        HTTP 204 success. The uncertain state and original payload are
        preserved for another explicit operator retry.
        """
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"session {session_id} not found")

        if call_id not in session.uncertain_call_ids:
            # Reject unknown / delivered / processing / cross-session calls.
            if call_id in session.delivered_call_ids:
                raise ValueError(f"call_id {call_id} is already delivered; cannot retry")
            if call_id in session.processing_call_ids:
                raise ValueError(
                    f"call_id {call_id} is in-flight (processing); "
                    "cannot retry until it settles to uncertain"
                )
            if call_id in session.pending_call_ids:
                raise ValueError(f"call_id {call_id} is already pending; no retry needed")
            raise ValueError(
                f"call_id {call_id} not found in session {session_id} " "uncertain_call_ids"
            )

        # The original message body must still be present so we can re-deliver
        # the exact same payload. If it is missing, fail closed rather than
        # sending an empty/unknown message.
        if call_id not in session.pending_messages:
            raise ValueError(
                f"call_id {call_id} has no stored payload in "
                f"session {session_id} pending_messages; cannot retry "
                "without the original message body"
            )

        # If the session is bound to a task, the task must agree on the
        # call_id's uncertain state and on the session binding. No
        # session/task divergence allowed.
        task = None
        if session.task_id:
            task = self.tasks.get(session.task_id)
            if task is None:
                raise ValueError(
                    f"session {session_id} is bound to task "
                    f"{session.task_id} which does not exist"
                )
            if task.session_id != session_id:
                raise ValueError(
                    f"session {session_id} is bound to task {task.id} but "
                    f"task.session_id={task.session_id} (divergence); "
                    "cannot retry"
                )
            if call_id not in task.uncertain_call_ids:
                raise ValueError(
                    f"call_id {call_id} is uncertain on session "
                    f"{session_id} but not on bound task {task.id}; "
                    "cannot retry due to session/task state divergence"
                )

        # Ensure the fail-closed state is durably observable before an
        # operator retry can remove it from the uncertain queue.
        self._emit_delivery_uncertain(session.workspace_id, session_id, call_id)

        # ---- Receipt query: decide whether a second paste is safe ----
        # The tmux-server receipt tells us whether the original paste
        # actually happened. This is the single source of truth for
        # "did the message reach the tmux input buffer":
        #
        #   * receipt present  -> the paste ran. Do NOT paste again.
        #                        Move call_id to processing and wait for
        #                        the worker's ACK.
        #   * receipt absent   -> the paste never ran (Hub died before
        #                        the tmux command, or the command failed
        #                        before setting the receipt). Safe to
        #                        re-deliver: move to pending and pump.
        #   * session gone /   -> cannot prove either way. Fail closed:
        #     unqueryable         stay uncertain, raise DeliveryUncertain.
        receipt_present: Optional[bool] = None
        try:
            receipt_present = await self._query_tmux_receipt(session.tmux_session, call_id)
        except RuntimeError:
            # tmux session gone or query failed — fail closed.
            raise DeliveryUncertain(
                f"cannot retry call_id={call_id} on session {session_id}: "
                "tmux session is gone or unqueryable; delivery remains "
                "uncertain. Operator must verify the worker state before "
                "deciding whether to re-dispatch."
            )

        if receipt_present:
            # The original paste already happened. A second paste would
            # duplicate the model turn, so we MUST NOT re-deliver. Move
            # the call_id from uncertain to processing and wait for the
            # worker's ACK. Emit the retry audit event for traceability.
            uncertain = [c for c in session.uncertain_call_ids if c != call_id]
            processing = list(session.processing_call_ids)
            if call_id not in processing:
                processing.append(call_id)
            updated_session = session.model_copy(
                update={
                    "uncertain_call_ids": uncertain,
                    "processing_call_ids": processing,
                }
            )
            self.sessions[session_id] = updated_session
            updated_task = None
            if task is not None:
                task_uncertain = [c for c in task.uncertain_call_ids if c != call_id]
                task_processing = list(task.processing_call_ids)
                if call_id not in task_processing:
                    task_processing.append(call_id)
                updated_task = task.model_copy(
                    update={
                        "uncertain_call_ids": task_uncertain,
                        "processing_call_ids": task_processing,
                    }
                )
                self.tasks[task.id] = updated_task
            self._save_state()

            # Audit event is MANDATORY: no state transition may be
            # untraceable. If emission fails, compensate the state back
            # to uncertain and re-raise so the operator sees the failure.
            try:
                self._emit_delivery_retry_requested(
                    workspace_id=session.workspace_id,
                    session_id=session_id,
                    call_id=call_id,
                    actor=actor,
                    reason=reason,
                )
            except Exception:
                # Compensate: move call_id back to uncertain on both
                # session and task. No tmux side effect has happened
                # (we only moved state), so rollback is clean.
                comp_session = updated_session.model_copy(
                    update={
                        "uncertain_call_ids": uncertain + [call_id],
                        "processing_call_ids": [c for c in processing if c != call_id],
                    }
                )
                self.sessions[session_id] = comp_session
                if updated_task is not None:
                    comp_task = updated_task.model_copy(
                        update={
                            "uncertain_call_ids": task_uncertain + [call_id],
                            "processing_call_ids": [c for c in task_processing if c != call_id],
                        }
                    )
                    self.tasks[updated_task.id] = comp_task
                self._save_state()
                logger.exception(
                    "retry_uncertain_delivery: failed to emit retry audit event "
                    "for call_id=%s (receipt present, no re-paste); compensated "
                    "state back to uncertain",
                    call_id,
                )
                raise

            # The paste ran but the Hub may have died before the submit
            # verification loop could nudge additional C-m. Ensure the
            # already-pasted input is accepted by the TUI (no re-paste).
            message = session.pending_messages.get(call_id)
            if not message:
                # Fail closed: without the original message body we cannot
                # verify whether the input is still pending, and a blind
                # C-m could submit an unrelated line. Move back to
                # uncertain and surface to the operator.
                comp_session = updated_session.model_copy(
                    update={
                        "uncertain_call_ids": uncertain + [call_id],
                        "processing_call_ids": [c for c in processing if c != call_id],
                    }
                )
                self.sessions[session_id] = comp_session
                if updated_task is not None:
                    comp_task = updated_task.model_copy(
                        update={
                            "uncertain_call_ids": task_uncertain + [call_id],
                            "processing_call_ids": [c for c in task_processing if c != call_id],
                        }
                    )
                    self.tasks[updated_task.id] = comp_task
                self._save_state()
                raise DeliveryUncertain(
                    f"Cannot retry call_id={call_id}: message body missing; "
                    f"moved back to uncertain"
                )
            try:
                await self._ensure_submitted_without_repaste(session.tmux_session, message)
            except Exception:
                # Cannot verify submit state (capture failed or input still
                # pending after retries). Fail closed: move back to uncertain
                # and surface to the operator. The receipt remains set so no
                # future replay re-pastes.
                comp_session = updated_session.model_copy(
                    update={
                        "uncertain_call_ids": uncertain + [call_id],
                        "processing_call_ids": [c for c in processing if c != call_id],
                    }
                )
                self.sessions[session_id] = comp_session
                if updated_task is not None:
                    comp_task = updated_task.model_copy(
                        update={
                            "uncertain_call_ids": task_uncertain + [call_id],
                            "processing_call_ids": [c for c in task_processing if c != call_id],
                        }
                    )
                    self.tasks[updated_task.id] = comp_task
                self._save_state()
                logger.exception(
                    "retry_uncertain_delivery: submit-nudge failed for "
                    "receipt-present call_id=%s session %s; moved back to "
                    "uncertain",
                    call_id,
                    session_id,
                )
                raise DeliveryUncertain(
                    f"Cannot verify submit for call_id={call_id}; " f"moved back to uncertain"
                )

            logger.info(
                "retry_uncertain_delivery: call_id=%s receipt present on session %s; "
                "no re-paste, moved to processing awaiting worker ACK",
                call_id,
                session_id,
            )
            # Reconcile the agent-tree followup lifecycle: the dispatch has
            # now settled (the paste ran; the call_id is in processing awaiting
            # the worker ACK). Emit the followup:outcome event and move the
            # run to RUNNING so the tree reflects the actual delivery state.
            # Idempotent — no-op if the outcome event already exists.
            try:
                self.agent_tree.reconcile_followup_outcome(
                    workspace_id=session.workspace_id, call_id=call_id
                )
            except Exception:
                # The worker may have ACKed between the processing move and
                # this reconcile (moving call_id to delivered). In that case
                # the ACK path already reconciled the lifecycle successfully
                # (else delivered would've been rolled back to processing).
                # We must NOT downgrade a delivered call_id to uncertain —
                # pending_messages was cleared on ACK, so the next
                # receipt-present retry would raise "message body missing".
                cur_session = self.sessions.get(session_id)
                if cur_session is not None and call_id in cur_session.delivered_call_ids:
                    logger.warning(
                        "retry_uncertain_delivery: reconcile_followup_outcome failed "
                        "for receipt-present but already-delivered call_id=%s "
                        "session=%s; ACK path already reconciled lifecycle, "
                        "returning success without downgrade",
                        call_id,
                        session_id,
                    )
                    return
                # call_id is still in processing. Compensate: move it back to
                # uncertain so the next operator retry takes the receipt-present
                # path (no re-paste, since the tmux receipt already exists)
                # and re-attempts reconciliation.
                comp_session = updated_session.model_copy(
                    update={
                        "uncertain_call_ids": uncertain + [call_id],
                        "processing_call_ids": [c for c in processing if c != call_id],
                    }
                )
                self.sessions[session_id] = comp_session
                if updated_task is not None:
                    comp_task = updated_task.model_copy(
                        update={
                            "uncertain_call_ids": task_uncertain + [call_id],
                            "processing_call_ids": [c for c in task_processing if c != call_id],
                        }
                    )
                    self.tasks[updated_task.id] = comp_task
                self._save_state()
                logger.exception(
                    "retry_uncertain_delivery: reconcile_followup_outcome failed "
                    "for receipt-present call_id=%s session=%s; compensated state "
                    "back to uncertain",
                    call_id,
                    session_id,
                )
                raise DeliveryUncertain(
                    f"reconcile_followup_outcome failed for call_id={call_id}; "
                    f"moved back to uncertain for operator retry"
                )
            return

        # Receipt absent: the paste never ran. Proceed with the normal
        # uncertain -> pending -> pump re-delivery path.

        # Move uncertain -> pending on the session.
        uncertain = [c for c in session.uncertain_call_ids if c != call_id]
        pending = list(session.pending_call_ids)
        if call_id not in pending:
            pending.append(call_id)
        updated_session = session.model_copy(
            update={
                "uncertain_call_ids": uncertain,
                "pending_call_ids": pending,
            }
        )
        self.sessions[session_id] = updated_session

        # Mirror on the task (we already verified task is consistent above).
        if task is not None:
            task_uncertain = [c for c in task.uncertain_call_ids if c != call_id]
            task_pending = list(task.pending_call_ids)
            if call_id not in task_pending:
                task_pending.append(call_id)
            self.tasks[task.id] = task.model_copy(
                update={
                    "uncertain_call_ids": task_uncertain,
                    "pending_call_ids": task_pending,
                }
            )

        self._save_state()

        # Emit a durable delivery:retry_requested audit event. This MUST
        # succeed before we pump (side effect). If it fails, compensate the
        # state back to uncertain so we never have an untraceable retry.
        try:
            self._emit_delivery_retry_requested(
                workspace_id=session.workspace_id,
                session_id=session_id,
                call_id=call_id,
                actor=actor,
                reason=reason,
            )
        except Exception:
            # Compensate: move call_id back to uncertain on both session and
            # task, and re-persist. No tmux send has happened yet.
            comp_session = updated_session.model_copy(
                update={
                    "uncertain_call_ids": uncertain + [call_id],
                    "pending_call_ids": [c for c in pending if c != call_id],
                }
            )
            self.sessions[session_id] = comp_session
            if task is not None:
                comp_task = self.tasks[task.id].model_copy(
                    update={
                        "uncertain_call_ids": task_uncertain + [call_id],
                        "pending_call_ids": [c for c in task_pending if c != call_id],
                    }
                )
                self.tasks[task.id] = comp_task
            self._save_state()
            logger.exception(
                "retry_uncertain_delivery: failed to emit durable "
                "delivery:retry_requested event for call_id=%s session=%s; "
                "compensated state back to uncertain",
                call_id,
                session_id,
            )
            raise

        logger.info(
            "retry_uncertain_delivery: moved call_id=%s from uncertain to "
            "pending for session=%s (actor=%s, reason=%s)",
            call_id,
            session_id,
            actor,
            reason,
        )

        # Pump through the normal delivery path. If the re-send hits another
        # ambiguous failure, the call_id returns to uncertain_call_ids.
        await self._pump_session_messages(session_id)

        # Fail-closed: re-fetch the session and verify the call_id did NOT
        # land back in uncertain_call_ids. If it did, the retry hit another
        # ambiguous tmux failure and the delivery is still uncertain. We
        # must NOT return success (which the API would map to HTTP 204) —
        # raise DeliveryUncertain so the caller surfaces a visible failure
        # (HTTP 400) and the operator can decide whether to retry again.
        # The uncertain state and the original payload are preserved.
        post_session = self.sessions.get(session_id)
        if post_session is not None and call_id in post_session.uncertain_call_ids:
            raise DeliveryUncertain(
                f"retry of call_id={call_id} to session {session_id} hit "
                "another ambiguous tmux send failure; delivery remains "
                "uncertain. Another explicit operator retry is required "
                "via retry_uncertain_delivery."
            )

        # The re-delivery succeeded: the call_id is now in processing (or
        # delivered) and no longer uncertain. Reconcile the agent-tree
        # followup lifecycle so the run/event/outcome match the delivery
        # state. Idempotent — no-op if the outcome event already exists.
        post_session = self.sessions.get(session_id)
        post_task = self.tasks.get(task.id) if task is not None else None
        call_id_delivered = post_session is not None and call_id in post_session.delivered_call_ids

        try:
            self.agent_tree.reconcile_followup_outcome(
                workspace_id=session.workspace_id, call_id=call_id
            )
        except Exception:
            if call_id_delivered:
                # The worker already ACKed this call_id. The ACK path
                # (_emit_followup_delivered_if_followup) runs
                # reconcile_followup_outcome BEFORE the delivered mutation
                # is committed; if reconcile had failed there,
                # _ack_call_ids's outer except would have rolled the
                # call_id back to processing. Therefore a delivered
                # call_id implies the lifecycle was already reconciled
                # successfully — the outcome event exists and the run is
                # RUNNING. This post-pump reconcile is a redundant
                # idempotent no-op; a transient persist failure here must
                # NOT downgrade a delivered call_id to uncertain (that
                # would strand it: pending_messages was cleared on ACK,
                # so the next receipt-present retry would raise
                # "message body missing"). Log and return success.
                logger.warning(
                    "retry_uncertain_delivery: reconcile_followup_outcome failed "
                    "for already-delivered call_id=%s session=%s; ACK path already "
                    "reconciled lifecycle, returning success without downgrade",
                    call_id,
                    session_id,
                )
                return
            # call_id is in processing (worker has not ACKed yet, or ACK's
            # reconcile failed and rolled back to processing). Downgrade to
            # uncertain so the operator can retry. The tmux receipt was set
            # by the pump, so the next retry takes the receipt-present path
            # (no re-paste) and re-attempts reconciliation. pending_messages
            # still holds the payload.
            if post_session is not None:
                comp_session = post_session.model_copy(
                    update={
                        "uncertain_call_ids": list(post_session.uncertain_call_ids) + [call_id],
                        "processing_call_ids": [
                            c for c in post_session.processing_call_ids if c != call_id
                        ],
                    }
                )
                self.sessions[session_id] = comp_session
            if post_task is not None:
                comp_task = post_task.model_copy(
                    update={
                        "uncertain_call_ids": list(post_task.uncertain_call_ids) + [call_id],
                        "processing_call_ids": [
                            c for c in post_task.processing_call_ids if c != call_id
                        ],
                    }
                )
                self.tasks[post_task.id] = comp_task
            self._save_state()
            logger.exception(
                "retry_uncertain_delivery: reconcile_followup_outcome failed "
                "for receipt-absent call_id=%s session=%s after successful "
                "pump; compensated state back to uncertain",
                call_id,
                session_id,
            )
            raise DeliveryUncertain(
                f"reconcile_followup_outcome failed for call_id={call_id}; "
                f"moved back to uncertain for operator retry"
            )

    def _audit_run_for_session(self, workspace_id: str, session_id: str) -> Optional["AgentRun"]:
        """Return a compat AgentRun for agent-tree audit events (never creates resident_root)."""

        from claude_hub.models.agent_tree import ExecutorKind

        session = self.sessions.get(session_id)
        if session is not None and session.current_task_id:
            task = self.tasks.get(session.current_task_id)
            if task is not None and task.agent_run_id:
                run = self.agent_tree._runs.get(task.agent_run_id)
                if run is not None and run.workspace_id == workspace_id:
                    return run
        run = self.agent_tree.get_run_by_context_ref(workspace_id, session_id)
        if run is not None:
            return run
        for candidate in self.agent_tree._runs.values():
            if candidate.workspace_id != workspace_id:
                continue
            if candidate.executor_kind == ExecutorKind.MANAGED_TASK:
                return candidate
        return None

    def _emit_delivery_retry_requested(
        self,
        workspace_id: str,
        session_id: str,
        call_id: str,
        actor: str,
        reason: str,
    ) -> None:
        """Emit a durable ``delivery:retry_requested`` event.

        Each retry attempt gets a *unique, monotonic* event call_id of the
        form ``delivery:retry:{call_id}:{attempt}`` so repeated retries of
        the same uncertain call_id are individually traceable (they do NOT
        dedupe to the first event). The attempt number is computed by
        counting existing ``delivery:retry:{call_id}:*`` events for this
        workspace.

        The event fingerprint includes ``call_id``, ``session_id``,
        ``actor``, ``reason``, and ``attempt`` so two retries with different
        actors/reasons/attempts never collide.

        Raises on any failure so the caller can compensate the state
        transition back to ``uncertain``. The event is the audit record that
        an operator explicitly requested this retry; without it the retry is
        untraceable, so we fail closed.
        """
        from claude_hub.models.agent_tree import AgentEventType

        root_run = self._audit_run_for_session(workspace_id, session_id)
        if root_run is None:
            raise RuntimeError(
                f"no audit run found for workspace {workspace_id} session {session_id}; "
                "cannot emit delivery:retry_requested audit event"
            )

        # Count prior retry attempts for this call_id to compute the next
        # attempt number. We scan the workspace event stream for events
        # whose call_id starts with the retry prefix for this call_id.
        retry_prefix = f"delivery:retry:{call_id}:"
        prior_attempts = len(
            [
                e
                for e in self.agent_tree._events.get(workspace_id, [])
                if e.call_id.startswith(retry_prefix)
            ]
        )
        attempt = prior_attempts + 1
        event_call_id = f"{retry_prefix}{attempt}"

        self.agent_tree._append_event(
            workspace_id=workspace_id,
            agent_run_id=root_run.id,
            event_type=AgentEventType.PROGRESS,
            author=root_run.id,
            recipient=root_run.id,
            call_id=event_call_id,
            action="delivery:retry_requested",
            target=session_id,
            fingerprint=_request_fingerprint(
                "delivery:retry_requested",
                {
                    "call_id": call_id,
                    "session_id": session_id,
                    "actor": actor,
                    "reason": reason,
                    "attempt": attempt,
                },
            ),
            payload={
                "call_id": call_id,
                "session_id": session_id,
                "actor": actor,
                "reason": reason,
                "attempt": attempt,
            },
            rollback_on_error=False,
        )

    async def _ensure_session_ready_for_send(self, session: ManagedSession) -> None:
        created = await ttyd_manager.ensure_tab_tmux_session(session.tab_id)
        if not created:
            return

        deadline = asyncio.get_running_loop().time() + 12
        last_output = ""
        while asyncio.get_running_loop().time() < deadline:
            try:
                last_output = await self._capture_tmux_output(session.tmux_session)
            except RuntimeError:
                await asyncio.sleep(0.3)
                continue
            if self._agent_input_ready(last_output):
                return
            await asyncio.sleep(0.5)

        logger.warning(
            "Timed out waiting for workspace agent input prompt before sending to %s. "
            "Sending anyway. Last output tail: %s",
            session.id,
            last_output[-200:],
        )

    async def _capture_tmux_output(self, tmux_session: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "capture-pane",
            "-p",
            "-S",
            "-120",
            "-t",
            tmux_session,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux capture-pane failed with code {proc.returncode}")
        return stdout.decode("utf-8", errors="ignore")

    def _agent_input_ready(self, output: str) -> bool:
        lower = output.lower()
        return any(
            marker in lower
            for marker in (
                "implement {feature}",
                "? for shortcuts",
                "/ for commands",
                "openai codex",
                "claude code",
                "permissions:",
                "cursor agent",
                "/auto-run",
            )
        )

    def _compute_payload_fingerprint(
        self,
        message: str,
        attachments: list[WorkspaceAttachmentCreate],
    ) -> str:
        """Compute a durable canonical fingerprint of a (message, attachments) payload.

        The fingerprint is sha256 over a canonical encoding of:
          * the message text
          * for each attachment: filename, normalized (stripped/lowercased)
            mime_type, and sha256 of the base64-decoded bytes.

        Same message + same attachment content always yields the same
        fingerprint regardless of base64 padding or whitespace. This is the
        source of truth for the immutable-call_id invariant: a call_id's
        fingerprint is stored on first send and kept forever (even after
        ACK), so a re-send with the same payload is idempotent and a re-send
        with a different payload is rejected.
        """
        h = hashlib.sha256()
        h.update(message.encode("utf-8"))
        for att in attachments:
            h.update(b"\x00")
            h.update(att.filename.encode("utf-8"))
            h.update(b"\x00")
            h.update(att.mime_type.strip().lower().encode("utf-8"))
            h.update(b"\x00")
            # Decode the attachment bytes and hash their content. Same-size
            # different bytes produce different fingerprints.
            try:
                raw = base64.b64decode(att.data_url.split(",", 1)[1], validate=True)
            except (binascii.Error, ValueError, IndexError):
                raw = b""
            h.update(hashlib.sha256(raw).digest())
        return h.hexdigest()

    def _compute_persisted_payload_fingerprint(
        self,
        message: str,
        attachments: list[WorkspaceAttachment],
    ) -> str:
        """Compute the canonical fingerprint of a PERSISTED (message, attachments)
        envelope.

        Unlike :meth:`_compute_payload_fingerprint`, which operates on
        :class:`WorkspaceAttachmentCreate` (base64 ``data_url``), this reads
        the persisted file bytes from each :class:`WorkspaceAttachment`'s
        ``path`` and hashes them. Used for legacy backfill: a call_id present
        in a state list but without a stored fingerprint can have its
        fingerprint derived from the persisted envelope so the
        immutable-payload invariant applies from then on.

        Fail-closed: if any attachment file is missing or unreadable, raise
        rather than silently producing a wrong fingerprint (which could let a
        conflicting payload slip through).
        """
        h = hashlib.sha256()
        h.update(message.encode("utf-8"))
        for att in attachments:
            h.update(b"\x00")
            h.update(att.filename.encode("utf-8"))
            h.update(b"\x00")
            h.update(att.mime_type.strip().lower().encode("utf-8"))
            h.update(b"\x00")
            try:
                raw = Path(att.path).read_bytes()
            except (OSError, FileNotFoundError) as exc:
                raise RuntimeError(
                    f"cannot fingerprint persisted attachment {att.filename} "
                    f"(id={att.id}): file at {att.path} is missing/unreadable"
                ) from exc
            h.update(hashlib.sha256(raw).digest())
        return h.hexdigest()

    def _safe_attachment_owner_id(self, session_id: str, call_id: str) -> str:
        """Return a filesystem-safe owner id for a call_id's attachments.

        The raw call_id is external input and may contain "/" or "..", which
        would escape the workspace attachments directory. We derive a safe
        owner id from sha256(session_id + call_id) so it is unique per
        (session, call_id) and contains only hex characters.
        """
        h = hashlib.sha256()
        h.update(session_id.encode("utf-8"))
        h.update(b"\x00")
        h.update(call_id.encode("utf-8"))
        return h.hexdigest()
