"""Operator closure of accepted terminal Chat deliveries, without receiver ACK."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal, cast

from claude_hub.models import SessionKind
from claude_hub.models.schema_domains.requests import ReconcileTerminalDeliveryRequest
from claude_hub.models.schema_domains.sessions import TerminalDeliveryReconciliation
from claude_hub.services import managed_chat
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.local_storage import read_document
from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase


class _MessageReconciliationMixin(_WorkspaceManagerBase):
    async def reconcile_terminal_delivery(
        self,
        session_id: str,
        payload: ReconcileTerminalDeliveryRequest,
        *,
        actor: str,
    ) -> TerminalDeliveryReconciliation:
        """Close one proven terminal envelope, retaining its payload and failed outcome.

        The persisted disposition is its own operator audit record and dedupe
        tombstone. It never enters delivered_call_ids, emits a worker report,
        advances a mailbox cursor, resumes execution, or changes review gates.
        """
        expected = self.sessions.get(session_id)
        if expected is None:
            raise KeyError(session_id)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"request": payload.model_dump(mode="json"), "actor": actor},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        async with self.workspace_mutation_lock(expected.workspace_id):
            lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = self.sessions.get(session_id)
                if (
                    session is None
                    or session.tab_id != expected.tab_id
                    or session.created_at != expected.created_at
                    or session.created_at != payload.expected_created_at
                ):
                    raise ValueError("Session incarnation changed")
                if session.session_kind != SessionKind.CHAT or not actor.strip():
                    raise ValueError("An authenticated operator and managed Chat are required")
                for prior in session.reconciled_deliveries.values():
                    if prior.operation_id == payload.operation_id:
                        if prior.request_fingerprint != fingerprint:
                            raise ValueError("Reconciliation operation_id conflicts")
                        return prior
                if payload.call_id in session.reconciled_deliveries:
                    raise ValueError("Delivery already reconciled under a different operation_id")
                task = self.tasks.get(payload.expected_task_id)
                if (
                    task is None
                    or task.workspace_id != session.workspace_id
                    or task.review_cycle != payload.expected_review_cycle
                    or session.id not in {task.session_id, task.review_session_id}
                    or payload.expected_task_id not in {session.task_id, session.current_task_id}
                ):
                    raise ValueError("Task assignment or review cycle changed")
                self._require_workspace_execution(task)
                if session.archived or managed_chat.is_turn_running(session):
                    raise ValueError("Wait for the active Chat turn to finish")
                call_id = payload.call_id
                if (
                    call_id not in session.processing_call_ids
                    or call_id in session.pending_call_ids
                    or call_id in session.uncertain_call_ids
                    or call_id in session.delivered_call_ids
                    or call_id not in session.pending_messages
                    or call_id not in session.call_payload_fingerprints
                ):
                    raise ValueError("Only a retained processing envelope can be reconciled")
                if self._chat_delivery_state(session, call_id) != "accepted":
                    raise ValueError("Durable provider acceptance is required")
                # Task binding guards concurrent reassignment; it does not
                # establish the origin of older messages in this resident.
                mailbox_call = self.task_mailbox.lookup_call(session.workspace_id, call_id)
                origin = mailbox_call.get("event") if mailbox_call else None
                if mailbox_call is not None and (
                    origin is None
                    or origin.task_id != mailbox_call.get("target")
                    or origin.call_id != call_id
                ):
                    raise ValueError("Delivery mailbox provenance is inconsistent")
                if (
                    origin is not None
                    and origin.task_id != task.id
                    and call_id in task.processing_call_ids
                ):
                    raise ValueError("Another Task origin conflicts with the current Task mirror")
                envelope_fingerprint = self._compute_persisted_payload_fingerprint(
                    session.pending_messages[call_id], session.pending_attachments.get(call_id, [])
                )
                if envelope_fingerprint != session.call_payload_fingerprints[call_id]:
                    raise ValueError("Retained delivery envelope is inconsistent")
                full_message, native_attachments = self._delivery_envelope(
                    session, session.pending_messages[call_id], call_id
                )
                await asyncio.to_thread(
                    managed_chat.validate_accepted_delivery,
                    session,
                    full_message,
                    native_attachments,
                    call_id,
                )
                store = AgentStreamStore(session.workspace_id, session.id)
                # Corrupt/incomplete histories must not be interpreted as an
                # authoritative terminal result, even if one good row remains.
                with store._read_database() as (db, name):
                    if db.execute(
                        "SELECT 1 FROM stream_errors WHERE name=? LIMIT 1", (name,)
                    ).fetchone():
                        raise ValueError("Stream history contains unreadable events")
                starts = list(store.iter_events(turn_id=call_id, kind="turn_started", limit=2))
                ends = list(store.iter_events(turn_id=call_id, kind="turn_completed", limit=2))
                if len(starts) != 1 or len(ends) != 1:
                    raise ValueError("Exactly one durable start and terminal event are required")
                start, end = starts[0], ends[0]
                # TaskMailbox events are stamped with utcnow (naive UTC),
                # unlike legacy ManagedSession creation times (local naive).
                origin_time = None
                if origin is not None:
                    origin_time = (
                        origin.created_at.replace(tzinfo=timezone.utc)
                        if origin.created_at.tzinfo is None
                        else origin.created_at.astimezone(timezone.utc)
                    )
                outcome = end.payload.get("status")
                if (
                    end.stream_sequence != payload.terminal_sequence
                    or end.stream_sequence <= start.stream_sequence
                    or end.run_epoch is None
                    or start.run_epoch != end.run_epoch
                    or start.created_at.tzinfo is None
                    or end.created_at.tzinfo is None
                    or (origin_time is not None and origin_time > start.created_at)
                    or start.created_at.astimezone(timezone.utc)
                    < session.created_at.astimezone(timezone.utc)
                    or end.created_at < start.created_at
                    or start.session_id != session.id
                    or end.session_id != session.id
                    or start.tab_id != session.tab_id
                    or end.tab_id != session.tab_id
                    or start.agent_type != session.agent_type
                    or end.agent_type != session.agent_type
                    or outcome not in {"completed", "failed"}
                    or end.payload.get("delivery_rejected")
                ):
                    raise ValueError("Terminal evidence does not match this accepted delivery")
                record = TerminalDeliveryReconciliation(
                    operation_id=payload.operation_id,
                    request_fingerprint=fingerprint,
                    actor=actor,
                    reason=payload.reason,
                    call_id=call_id,
                    task_id=origin.task_id if origin else None,
                    review_cycle=origin.review_cycle if origin else None,
                    provenance="task_mailbox" if origin else "session_delivery",
                    guard_task_id=task.id,
                    guard_review_cycle=task.review_cycle,
                    session_created_at=session.created_at,
                    terminal_sequence=end.stream_sequence,
                    run_epoch=cast(int, end.run_epoch),
                    outcome=cast(Literal["completed", "failed"], outcome),
                    created_at=datetime.now(timezone.utc),
                )
                updated = session.model_copy(
                    update={
                        "processing_call_ids": [
                            x for x in session.processing_call_ids if x != call_id
                        ],
                        "processing_call_ids_at": {
                            k: v for k, v in session.processing_call_ids_at.items() if k != call_id
                        },
                        "reconciled_deliveries": {**session.reconciled_deliveries, call_id: record},
                    }
                )
                task_update = task
                if call_id in task.processing_call_ids or (
                    origin is not None and origin.task_id == task.id
                ):
                    task_update = task.model_copy(
                        update={
                            "processing_call_ids": [
                                x for x in task.processing_call_ids if x != call_id
                            ],
                            "reconciled_call_ids": list(
                                dict.fromkeys(task.reconciled_call_ids + [call_id])
                            ),
                        }
                    )
                # Persist the disposition, audit identity and mirrored outbox
                # atomically in the existing workspace document transaction.
                self.sessions[session.id] = updated
                self.tasks[task.id] = task_update
                try:
                    self._save_workspace_state(session.workspace_id)
                except Exception:
                    # A response failure after SQLite commit must not undo
                    # memory or make an idempotent retry look like a new action.
                    persisted: dict[str, Any]
                    try:
                        persisted = read_document(self._workspace_state_file(session.workspace_id))
                    except Exception:
                        persisted = {}
                    committed = None
                    for item in persisted.get("sessions", []):
                        if item.get("id") == session.id:
                            committed = item.get("reconciled_deliveries", {}).get(call_id)
                            break
                    if committed == record.model_dump(mode="json"):
                        return record
                    self.sessions[session.id] = session
                    self.tasks[task.id] = task
                    raise
                return record
