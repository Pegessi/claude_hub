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
     │         send failed          │  crash / tmux session gone
     └──────────────────────────────┤
                                    ▼
                              uncertain (fail-closed)

* ``pending_call_ids``: message persisted in ``pending_messages``, not yet
  sent to tmux. Always re-deliverable on restart.
* ``processing_call_ids``: the intent to deliver was persisted (call_id
  moved out of pending) and the pump is sending / has sent the message to
  tmux, but the worker has not ACKed. "Maybe delivered" — we cannot prove
  the message reached the tmux input buffer.
* ``uncertain_call_ids``: the call_id was in-flight when the Hub crashed or
  the tmux session disappeared. Fail-closed: we do NOT auto-resend (could
  duplicate) and do NOT silently mark delivered (could lose). A
  ``delivery:uncertain`` event is emitted to the supervisor. Moves to
  delivered on worker ACK, or back to pending on explicit operator retry.
* ``delivered_call_ids``: the worker ACKed the call_id (listed it in
  ``acked_call_ids`` of a report). Only the worker's ACK moves a call_id
  here.

The ``[call_id:<id>]`` marker is embedded in the message body so the worker
can correlate its ACK. It is NOT used by the Hub as a receipt or dedup
basis — tmux pane history is not durable (markers roll out of the scroll
buffer), so relying on it for cold-recovery dedup would produce duplicate
deliveries.
"""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ..agent_tree import _request_fingerprint
from ._constants import *  # noqa: F401,F403


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
        worker's ACK). On send failure it rolls back to ``pending_call_ids``
        for retry.

        A call_id only leaves ``processing_call_ids`` when:
          * the worker ACKs it (→ ``delivered_call_ids``), or
          * the Hub crashes / tmux session is gone (→ ``uncertain_call_ids``,
            fail-closed).
        """
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        if call_id:
            # Sender-side gate: skip call_ids that are already in-flight
            # (processing), uncertain, or committed (delivered). In all
            # cases delivery is already in flight or done, so the sender
            # must NOT enqueue again.
            if call_id in session.delivered_call_ids:
                logger.debug(
                    "send_session_message call_id=%s already committed by session %s; skipping",
                    call_id,
                    session_id,
                )
                return
            if call_id in session.processing_call_ids:
                logger.debug(
                    "send_session_message call_id=%s already in-flight for session %s; skipping",
                    call_id,
                    session_id,
                )
                return
            if call_id in session.uncertain_call_ids:
                # Explicit retry: the caller is re-sending a call_id that was
                # previously marked delivery-uncertain (ambiguous tmux send
                # failure). Move it back to pending on both the session and
                # its bound task so the pump can re-deliver the original
                # payload (preserved in pending_messages). This is the
                # task-dispatch retry path; the operator-facing retry is
                # retry_uncertain_delivery which also emits a durable audit
                # event.
                logger.info(
                    "send_session_message call_id=%s was uncertain for session %s; "
                    "explicit retry requested; moving back to pending",
                    call_id,
                    session_id,
                )
                uncertain = [c for c in session.uncertain_call_ids if c != call_id]
                pending = list(session.pending_call_ids)
                if call_id not in pending:
                    pending.append(call_id)
                session_update = {
                    "uncertain_call_ids": uncertain,
                    "pending_call_ids": pending,
                }
                task = self.tasks.get(session.task_id) if session.task_id else None
                if task is not None and call_id in task.uncertain_call_ids:
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
                self.sessions[session_id] = session.model_copy(update=session_update)
                self._save_state()
                await self._pump_session_messages(session_id)
                session = self.sessions[session_id]
                if call_id in session.uncertain_call_ids:
                    raise DeliveryUncertain(
                        f"delivery of call_id={call_id} to session {session_id} "
                        "remains uncertain after retry; operator intervention required"
                    )
                return

            # Persist the message in the durable inbox and record the call_id
            # as pending. If already pending, do not duplicate.
            pending_messages = dict(session.pending_messages)
            pending_messages[call_id] = message
            pending_call_ids = list(session.pending_call_ids)
            if call_id not in pending_call_ids:
                pending_call_ids.append(call_id)
            self.sessions[session_id] = session.model_copy(
                update={
                    "pending_messages": pending_messages,
                    "pending_call_ids": pending_call_ids,
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

        4. **On failure**: roll the call_id back to ``pending_call_ids`` so
           the next pump cycle can retry.

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
                # Inbox entry missing — should not happen, but guard against
                # it. Move to delivered to avoid an infinite loop.
                logger.warning(
                    "pump: call_id=%s in pending_call_ids but missing from pending_messages; "
                    "marking delivered",
                    call_id,
                )
                self._ack_call_ids(session.task_id, session_id, [call_id])
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
                persisted = self._persist_attachments(
                    session.workspace_id,
                    f"{session.id}-message-{uuid.uuid4().hex[:8]}",
                    [],
                )
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
                await self._send_tmux_message(
                    self.sessions[session_id].tmux_session,
                    full_message,
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
        try:
            from claude_hub.models.agent_tree import AgentEventType

            root_run = self.agent_tree.get_run_by_context_ref(workspace_id, session_id)
            if root_run is None:
                # Fall back to the workspace's root run (the resident root).
                for run in self.agent_tree._runs.values():
                    if run.workspace_id == workspace_id and run.parent_id is None:
                        root_run = run
                        break
            if root_run is None:
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
        except Exception:
            logger.exception(
                "Failed to emit delivery:uncertain event for call_id=%s session=%s",
                call_id,
                session_id,
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
        through the normal delivery path. If the re-send hits another
        ambiguous failure, the call_id returns to ``uncertain_call_ids``.
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

    def _emit_delivery_retry_requested(
        self,
        workspace_id: str,
        session_id: str,
        call_id: str,
        actor: str,
        reason: str,
    ) -> None:
        """Emit a durable ``delivery:retry_requested`` event.

        Raises on any failure so the caller can compensate the state
        transition back to ``uncertain``. The event is the audit record that
        an operator explicitly requested this retry; without it the retry is
        untraceable, so we fail closed.
        """
        from claude_hub.models.agent_tree import AgentEventType

        root_run = self.agent_tree.get_run_by_context_ref(workspace_id, session_id)
        if root_run is None:
            for run in self.agent_tree._runs.values():
                if run.workspace_id == workspace_id and run.parent_id is None:
                    root_run = run
                    break
        if root_run is None:
            raise RuntimeError(
                f"no root run found for workspace {workspace_id}; "
                "cannot emit delivery:retry_requested audit event"
            )

        self.agent_tree._append_event(
            workspace_id=workspace_id,
            agent_run_id=root_run.id,
            event_type=AgentEventType.PROGRESS,
            author=root_run.id,
            recipient=root_run.id,
            call_id=f"delivery:retry:{call_id}",
            action="delivery:retry_requested",
            target=session_id,
            fingerprint=_request_fingerprint(
                "delivery:retry_requested",
                {
                    "call_id": call_id,
                    "session_id": session_id,
                    "actor": actor,
                },
            ),
            payload={
                "call_id": call_id,
                "session_id": session_id,
                "actor": actor,
                "reason": reason,
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
