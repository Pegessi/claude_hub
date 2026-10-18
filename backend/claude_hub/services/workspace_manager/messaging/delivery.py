"""Durable message enqueue, resume, and one-at-a-time delivery pumps."""

import claude_hub.services.managed_chat as managed_chat
from claude_hub.services.session_seat import SessionSeatMismatch
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _MessageDeliveryMixin(_WorkspaceManagerBase):
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
        # A duplicate caller must not wait behind a provider side effect that
        # already owns this call id. This lock-free fast path is read-only; all
        # mutations and every first delivery still serialize below. The locked
        # implementation repeats the fingerprint/state validation.
        current = self.sessions.get(session_id)
        if current is None:
            raise KeyError(session_id)
        expected_tab_id = current.tab_id
        expected_created_at = current.created_at
        if call_id:
            existing_fp = current.call_payload_fingerprints.get(call_id)
            if existing_fp is not None:
                new_fp = self._compute_payload_fingerprint(message, attachments or [])
                if new_fp != existing_fp:
                    raise ValueError(
                        f"call_id {call_id} already used with a different payload; "
                        "cannot mutate a committed delivery. Use a new call_id for a "
                        "different message."
                    )
                if call_id in current.processing_call_ids or call_id in current.delivered_call_ids:
                    return
        lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
        async with self.workspace_mutation_lock(current.workspace_id):
            async with lock:
                live = self.sessions.get(session_id)
                if (
                    live is None
                    or live.tab_id != expected_tab_id
                    or live.created_at != expected_created_at
                ):
                    raise KeyError(f"session {session_id} was deleted or replaced")
                await self._send_session_message_locked(
                    session_id,
                    message,
                    attachments,
                    call_id,
                )

    async def _send_session_message_locked(
        self,
        session_id: str,
        message: str,
        attachments: list[WorkspaceAttachmentCreate] | None = None,
        call_id: str | None = None,
        *,
        question_key: str | None = None,
    ) -> None:
        """Persist and deliver input while the per-session pump lock is held."""
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)
        self._assert_session_seat(session)
        if session.session_kind == SessionKind.CHAT:
            # Native transports cannot queue a second model turn themselves.
            # Even unkeyed bootstrap/follow-up input uses the durable inbox.
            call_id = call_id or f"chat:{uuid.uuid4().hex}"

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
                    await self._pump_session_messages_locked(session_id)
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
            question_answer_calls = dict(session.chat_question_answer_calls)
            if question_key is not None:
                question_answer_calls[question_key] = call_id
            self.sessions[session_id] = session.model_copy(
                update={
                    "pending_messages": pending_messages,
                    "pending_attachments": pending_attachments,
                    "pending_call_ids": pending_call_ids,
                    "call_payload_fingerprints": call_payload_fingerprints,
                    # One state write claims both the answer and its envelope.
                    # Pump or card-resolution failure must not reopen admission.
                    "chat_question_answer_calls": question_answer_calls,
                }
            )
            self._save_state()
            session = self.sessions[session_id]

            # Kick the receiver pump to deliver the message while retaining the
            # same lock that protected the durable enqueue.
            await self._pump_session_messages_locked(session_id)
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
        expected = self.sessions.get(session_id)
        if expected is None:
            raise KeyError(f"session {session_id} not found")
        lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
        async with self.workspace_mutation_lock(expected.workspace_id):
            async with lock:
                live = self.sessions.get(session_id)
                if (
                    live is None
                    or live.tab_id != expected.tab_id
                    or live.created_at != expected.created_at
                ):
                    raise KeyError(f"session {session_id} was deleted or replaced")
                return await self._resume_existing_call_locked(session_id, call_id)

    async def _resume_existing_call_locked(self, session_id: str, call_id: str) -> bool:
        """Resume a persisted call while the per-session pump lock is held."""
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
            await self._pump_session_messages_locked(session_id)
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

        # Every producer uses workspace -> pump ordering. Native Chat publish
        # observers re-enter the workspace lock inline while the pump owns the
        # provider send, so deletion can never hold workspace while waiting on
        # this pump.
        lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
        async with self.workspace_mutation_lock(session.workspace_id):
            async with lock:
                live = self.sessions.get(session_id)
                if (
                    live is None
                    or live.tab_id != session.tab_id
                    or live.created_at != session.created_at
                ):
                    return
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
            if session.session_kind == SessionKind.CHAT and (
                session.chat_paused
                or session.uncertain_call_ids
                or managed_chat.is_turn_running(session)
            ):
                # Leave the envelope pending. A busy turn is not an ambiguous
                # delivery, and later input must not bypass an uncertain turn.
                return

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
            try:
                self._save_state()
            except BaseException:
                if session.session_kind == SessionKind.CHAT:
                    # No provider side effect has started. Restore only this
                    # intent transition, retaining its admitted envelope/claim,
                    # so an immediate retry cannot mistake it for an accepted
                    # in-flight send. Even a partially committed save is safe:
                    # the next pump must persist intent again before delivery.
                    self.sessions[session_id] = session
                    if task is not None and task_update:
                        self.tasks[task.id] = task
                raise

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
                self._assert_session_seat(self.sessions[session_id])
                await self._ensure_session_ready_for_send(self.sessions[session_id])
                full_message = f"{marker}\n{message}"
                native_attachments: list[WorkspaceAttachment] = []
                if session.session_kind == SessionKind.CHAT:
                    native_attachments = [
                        item for item in persisted if item.mime_type.startswith("image/")
                    ]
                    persisted = [
                        item for item in persisted if not item.mime_type.startswith("image/")
                    ]
                full_message = self._append_attachment_block(full_message, persisted)
            except SessionSeatMismatch:
                logger.exception(
                    "pump: session seat mismatch for call_id=%s session %s; "
                    "moving to uncertain (fail-closed, no auto-retry)",
                    call_id,
                    session_id,
                )
                self._mark_processing_as_uncertain(session_id, [call_id])
                continue
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
                if session.session_kind == SessionKind.CHAT:
                    self._rollback_chat_processing_to_pending(session_id, [call_id])
                    raise
                self._rollback_processing_to_pending(session.task_id, session_id, [call_id])
                continue

            if session.session_kind == SessionKind.CHAT:
                try:
                    await managed_chat.send_message(
                        self.sessions[session_id], full_message, native_attachments, call_id
                    )
                except (Exception, asyncio.CancelledError) as exc:
                    state = self._chat_delivery_state(self.sessions[session_id], call_id)
                    if state == "not_sent":
                        self._rollback_chat_processing_to_pending(session_id, [call_id])
                        raise
                    if (
                        state != "accepted"
                        and call_id in self.sessions[session_id].processing_call_ids
                    ):
                        self._mark_processing_as_uncertain(session_id, [call_id])
                    # Cancellation must propagate after the mailbox records
                    # uncertainty; callers must not mistake it for acceptance.
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    logger.exception(
                        "Chat delivery failed for call_id=%s session=%s state=%s",
                        call_id,
                        session_id,
                        state,
                    )
                # A model turn can outlive send_message. The monitor pumps
                # the next envelope only after this native turn has ended.
                return

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
