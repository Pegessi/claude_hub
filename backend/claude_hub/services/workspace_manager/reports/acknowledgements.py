"""Report processing split from the compatibility mixin."""

from collections.abc import Mapping

from ...task_graph import task_supervisor_consumer_key
from .._constants import *  # noqa: F401,F403
from .identity import _ReportIdentityMixin


class _ReportAcknowledgementsMixin(_ReportIdentityMixin):
    def _ack_call_ids(
        self, task_id: Optional[str], session_id: str, call_ids: list[str]
    ) -> set[tuple[str, str]]:
        """Receiver-side commit: move call_ids from pending/processing/uncertain to delivered.

        This is the *commit* step of the durable delivery gate. The delivery
        (pending → processing) happens in the receiver pump
        (``_pump_session_messages``) which persists the intent BEFORE sending
        to tmux. This commit runs when the worker submits a report that
        includes the call_id in ``acked_call_ids`` — proving the worker
        processed the message.

        **Target verification (anti-forgery):**

        TaskMailbox followups are verified Task-first: the call_id must sit
        on the Task outbox, the mailbox call record target must be exactly
        this Task, and the reporting session must be the Task's current
        worker assignment. Session outbox membership is not required for
        that path; Session delivered is updated only when the same call_id
        is also present on the Session.

        Legacy dispatch/internal call_ids (no TaskMailbox record) still
        require Session outbox membership plus mailbox call-record target
        binding. Cross-task and cross-session ACKs fail closed.

        Call_ids in ``pending_call_ids``, ``processing_call_ids``, or
        ``uncertain_call_ids`` are all eligible for ACK (the worker may have
        processed the message even if the Hub's state machine hasn't caught
        up, e.g. a crash between send and persist). Unknown call_ids (not in
        any of the three) are ignored to prevent future-ID poisoning.

        After committing, the call_id's message body is removed from
        ``session.pending_messages`` (the durable inbox) since it is no
        longer needed for re-delivery.
        """
        wake_targets: set[tuple[str, str]] = set()
        if not call_ids:
            return wake_targets
        acked = set(call_ids)

        task = self.tasks.get(task_id) if task_id else None
        session = self.sessions.get(session_id)

        session_call_ids: set[str] = set()
        if session is not None:
            session_call_ids.update(session.pending_call_ids)
            session_call_ids.update(session.processing_call_ids)
            session_call_ids.update(session.uncertain_call_ids)

        verified_acked: list[str] = []
        for call_id in acked:
            mailbox_record = (
                self.task_mailbox.lookup_call(task.workspace_id, call_id)
                if task is not None
                else None
            )
            mailbox_target = mailbox_record.get("target") if mailbox_record else None
            if mailbox_record is not None and task is not None and mailbox_target == task.id:
                if not self._can_ack_task_mailbox_call(call_id, task, session, mailbox_record):
                    logger.warning(
                        "Rejecting TaskMailbox ACK: call_id=%s task_id=%s session_id=%s",
                        call_id,
                        task_id,
                        session_id,
                    )
                    continue
                verified_acked.append(call_id)
                continue
            if mailbox_target in self.tasks:
                logger.warning(
                    "Rejecting cross-task TaskMailbox ACK: call_id=%s target=%s task_id=%s",
                    call_id,
                    mailbox_target,
                    task_id,
                )
                continue
            if call_id not in session_call_ids:
                continue
            if not self._verify_call_target(call_id, task_id, session_id):
                logger.warning(
                    "Rejecting forged ACK: call_id=%s does not target task_id=%s session_id=%s",
                    call_id,
                    task_id,
                    session_id,
                )
                continue
            verified_acked.append(call_id)

        if not verified_acked:
            return wake_targets

        verified_set = set(verified_acked)
        workspace_id = (
            session.workspace_id
            if session is not None
            else task.workspace_id if task is not None else None
        )

        # ACK reconciliation stays in memory. The caller owns the workspace
        # commit and wakes consumers only after that commit returns.
        for call_id in verified_acked:
            # followup: reconcile outcome + emit delivered event.
            wake_target = self._emit_followup_delivered_if_followup(workspace_id, call_id)
            if wake_target is not None:
                wake_targets.add(wake_target)

        # Stage delivery bookkeeping in the same workspace transaction.
        # create_report persists it with the report and mailbox.
        if task is not None:
            to_ack_pending = [c for c in task.pending_call_ids if c in verified_set]
            to_ack_processing = [c for c in task.processing_call_ids if c in verified_set]
            to_ack_uncertain = [c for c in task.uncertain_call_ids if c in verified_set]
            to_ack = to_ack_pending + to_ack_processing + to_ack_uncertain
            if to_ack:
                pending = [c for c in task.pending_call_ids if c not in verified_set]
                processing = [c for c in task.processing_call_ids if c not in verified_set]
                uncertain = [c for c in task.uncertain_call_ids if c not in verified_set]
                delivered = list(task.delivered_call_ids)
                for cid in to_ack:
                    if cid not in delivered:
                        delivered.append(cid)
                self.tasks[task.id] = task.model_copy(
                    update={
                        "pending_call_ids": pending,
                        "processing_call_ids": processing,
                        "uncertain_call_ids": uncertain,
                        "delivered_call_ids": delivered,
                    }
                )

        if session is not None:
            to_ack_pending = [c for c in session.pending_call_ids if c in verified_set]
            to_ack_processing = [c for c in session.processing_call_ids if c in verified_set]
            to_ack_uncertain = [c for c in session.uncertain_call_ids if c in verified_set]
            to_ack = to_ack_pending + to_ack_processing + to_ack_uncertain
            if to_ack:
                pending = [c for c in session.pending_call_ids if c not in verified_set]
                processing = [c for c in session.processing_call_ids if c not in verified_set]
                uncertain = [c for c in session.uncertain_call_ids if c not in verified_set]
                delivered = list(session.delivered_call_ids)
                for cid in to_ack:
                    if cid not in delivered:
                        delivered.append(cid)
                # Remove committed messages from the durable inbox and clear
                # their claim timestamps.
                pending_messages = {
                    cid: msg
                    for cid, msg in session.pending_messages.items()
                    if cid not in verified_set
                }
                pending_attachments = {
                    cid: atts
                    for cid, atts in session.pending_attachments.items()
                    if cid not in verified_set
                }
                processing_call_ids_at = {
                    cid: ts
                    for cid, ts in session.processing_call_ids_at.items()
                    if cid not in verified_set
                }
                self.sessions[session_id] = session.model_copy(
                    update={
                        "pending_call_ids": pending,
                        "processing_call_ids": processing,
                        "uncertain_call_ids": uncertain,
                        "delivered_call_ids": delivered,
                        "pending_messages": pending_messages,
                        "pending_attachments": pending_attachments,
                        "processing_call_ids_at": processing_call_ids_at,
                    }
                )
        return wake_targets

    def _can_ack_task_mailbox_call(
        self,
        call_id: str,
        task: Optional[WorkspaceTask],
        session: Optional[ManagedSession],
        record: Mapping[str, Any],
    ) -> bool:
        """Allow ACK for a TaskMailbox-tracked call_id on the assigned worker."""
        if task is None or session is None:
            return False
        if call_id not in (
            set(task.pending_call_ids)
            | set(task.processing_call_ids)
            | set(task.uncertain_call_ids)
        ):
            return False
        if record.get("target") != task.id:
            return False
        if task.session_id != session.id or session.workspace_id != task.workspace_id:
            return False
        assigned = {session.task_id, session.current_task_id}
        if task.id not in assigned:
            return False
        return True

    def _verify_call_target(self, call_id: str, task_id: Optional[str], session_id: str) -> bool:
        """Verify that ``call_id``'s call record targets ``task_id``/``session_id``.

        Strict target binding: a tracked call record's target run must resolve
        to the exact task and session that is reporting the ACK. Cross-task
        or cross-session ACKs are rejected.

        Returns True only for:
          * Untracked call_ids (no call record) — legacy dispatch/internal
            call_ids that are verified by being in the session's outbox
            (the ``session_call_ids`` membership check performed by the
            caller before invoking this method).

        Returns False (fail closed) for:
          * A tracked call record whose target run is missing or unresolvable.
          * A tracked call record whose target task differs from ``task_id``.
          * A tracked call record whose target task's session differs from
            ``session_id``.
        """
        workspace_id = None
        session = self.sessions.get(session_id)
        if session is not None:
            workspace_id = session.workspace_id
        else:
            task = self.tasks.get(task_id) if task_id else None
            if task is not None:
                workspace_id = task.workspace_id

        if workspace_id is None:
            return False

        record = self.task_mailbox.lookup_call(workspace_id, call_id)
        if record is None:
            # No call record — legacy untracked call (dispatch/internal).
            return True

        target_task_id = record.get("target")
        if not target_task_id or target_task_id not in self.tasks:
            return False

        if task_id and target_task_id != task_id:
            return False
        target_task = self.tasks.get(target_task_id)
        if (
            target_task is not None
            and target_task.session_id
            and target_task.session_id != session_id
        ):
            return False
        return True

    def _emit_followup_delivered_if_followup(
        self, workspace_id: Optional[str], call_id: str
    ) -> tuple[str, str] | None:
        """TaskMailbox followups do not emit separate delivered events."""

        return None

    def _rollback_processing_to_pending(
        self, task_id: Optional[str], session_id: str, call_ids: list[str]
    ) -> None:
        """Roll back call_ids from ``processing`` back to ``pending``.

        This is the inverse of the pump's intent-persist step. It is used
        when a tmux send fails: the call_id was moved to processing
        (in-flight) before the send, and on failure we move it back to
        pending so the next pump cycle can retry.

        The message body stays in ``pending_messages`` (it was never
        removed) so re-delivery has the payload.
        """
        if not call_ids:
            return
        rollback = set(call_ids)

        task = self.tasks.get(task_id) if task_id else None
        if task is not None:
            to_rollback = [c for c in task.processing_call_ids if c in rollback]
            if to_rollback:
                processing = [c for c in task.processing_call_ids if c not in rollback]
                pending = list(task.pending_call_ids)
                for cid in to_rollback:
                    if cid not in pending:
                        pending.append(cid)
                self.tasks[task.id] = task.model_copy(
                    update={
                        "pending_call_ids": pending,
                        "processing_call_ids": processing,
                    }
                )

        session = self.sessions.get(session_id)
        if session is not None:
            to_rollback = [c for c in session.processing_call_ids if c in rollback]
            if to_rollback:
                processing = [c for c in session.processing_call_ids if c not in rollback]
                pending = list(session.pending_call_ids)
                for cid in to_rollback:
                    if cid not in pending:
                        pending.append(cid)
                processing_call_ids_at = {
                    cid: ts
                    for cid, ts in session.processing_call_ids_at.items()
                    if cid not in rollback
                }
                self.sessions[session_id] = session.model_copy(
                    update={
                        "pending_call_ids": pending,
                        "processing_call_ids": processing,
                        "processing_call_ids_at": processing_call_ids_at,
                    }
                )
