"""Operator-authorized retry of uncertain durable deliveries."""

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


class _MessageRetryMixin(_WorkspaceManagerBase):
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
                await self._retry_uncertain_delivery_locked(
                    session_id,
                    call_id,
                    reason=reason,
                    actor=actor,
                )

    async def _retry_uncertain_delivery_locked(
        self,
        session_id: str,
        call_id: str,
        *,
        reason: str,
        actor: str,
    ) -> None:
        """Retry an uncertain call while the per-session pump lock is held."""
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
        if session.session_kind == SessionKind.CHAT:
            state = self._chat_delivery_state(session, call_id)
            if state not in {"not_sent", "accepted"}:
                raise DeliveryUncertain(
                    f"cannot retry call_id={call_id} on Chat session {session_id}: "
                    "provider acceptance is unknown; automatic replay is unsafe"
                )
            receipt_present = state == "accepted"
        else:
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

            if session.session_kind == SessionKind.CHAT:
                # The provider has already accepted this turn. Only the
                # agent's report ACK may commit it; no resubmit or key press.
                return

            # The paste ran but the Hub may have died before the submit
            # verification loop could nudge additional Enter keys. Ensure the
            # already-pasted input is accepted by the TUI (no re-paste).
            message = session.pending_messages.get(call_id)
            if not message:
                # Fail closed: without the original message body we cannot
                # verify whether the input is still pending, and a blind
                # Enter could submit an unrelated line. Move back to
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
        await self._pump_session_messages_locked(session_id)

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

    def _audit_task_for_session(self, workspace_id: str, session_id: str) -> Optional[str]:
        """Return the Task id used for durable delivery audit events."""

        session = self.sessions.get(session_id)
        if session is None:
            return None
        for task_ref in (session.current_task_id, session.task_id):
            if not task_ref:
                continue
            task = self.tasks.get(task_ref)
            if task is not None and task.workspace_id == workspace_id:
                return task.id
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

        from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
        from claude_hub.services.task_graph import task_inbox_consumer_key

        task_id = self._audit_task_for_session(workspace_id, session_id)
        if task_id is None:
            raise RuntimeError(
                f"no audit task found for workspace {workspace_id} session {session_id}; "
                "cannot emit delivery:retry_requested audit event"
            )
        task = self.tasks[task_id]

        retry_prefix = f"delivery:retry:{call_id}:"
        prior_attempts = len(
            [
                event
                for event in self.task_mailbox._events.get(workspace_id, [])
                if event.call_id.startswith(retry_prefix)
            ]
        )
        attempt = prior_attempts + 1
        event_call_id = f"{retry_prefix}{attempt}"

        self.task_mailbox.append_event(
            workspace_id=workspace_id,
            task_id=task_id,
            actor_role=TaskActorRole.HUMAN,
            event_type=TaskEventType.PROGRESS,
            call_id=event_call_id,
            action="delivery:retry_requested",
            consumer_key=task_inbox_consumer_key(task),
            actor_session_id=session_id,
            target=session_id,
            payload={
                "call_id": call_id,
                "session_id": session_id,
                "actor": actor,
                "reason": reason,
                "attempt": attempt,
            },
            persist=True,
        )
