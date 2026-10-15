"""Workspace controls for native Chat sessions without terminal side effects."""

import asyncio
from typing import Optional

from ...models import (
    AgentRuntimeStatus,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    WorkspaceAttachmentCreate,
    WorkspaceTaskStatus,
)
from .. import managed_chat
from ._constants import _now


class _ChatMixin:
    async def send_chat_session_message(
        self,
        session_id: str,
        message: str,
        attachments: list[WorkspaceAttachmentCreate] | None = None,
        call_id: str | None = None,
    ) -> None:
        """Submit explicit user input through the durable Workspace mailbox."""
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        if session.session_kind != SessionKind.CHAT:
            raise ValueError("This endpoint requires a managed Chat session")
        if not message.strip() and not attachments:
            raise ValueError("A message or attachment is required")
        if session.status == ManagedSessionStatus.STOPPED:
            raise ValueError("The managed Chat session has been stopped")
        self._assert_session_seat(session)
        # Invalid input must not undo an explicit Stop before the mailbox rejects it.
        fingerprint = self._compute_payload_fingerprint(message, attachments or [])
        if call_id and call_id in session.call_payload_fingerprints:
            if session.call_payload_fingerprints[call_id] != fingerprint:
                raise ValueError("call_id already identifies a different message")
        if session.uncertain_call_ids:
            raise RuntimeError("Chat delivery is uncertain; reconcile it before resuming")
        self._resume_chat_session(session_id)
        await self.send_session_message(session_id, message, attachments, call_id)

    def _resume_chat_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None or session.session_kind != SessionKind.CHAT or not session.chat_paused:
            return
        if session.uncertain_call_ids:
            raise RuntimeError("Chat delivery is uncertain; reconcile it before resuming")
        self.sessions[session_id] = session.model_copy(
            update={
                "chat_paused": False,
                "status": ManagedSessionStatus.IDLE,
                "runtime_status": AgentRuntimeStatus.IDLE,
                "updated_at": _now(),
            }
        )
        self._save_state()

    async def cancel_chat_session(
        self, session_id: str, *, expected_turn_id: Optional[str] = None
    ) -> bool:
        """Stop only the targeted turn and pause automatic mailbox delivery."""
        lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            if session.session_kind != SessionKind.CHAT:
                raise ValueError("This endpoint requires a managed Chat session")
            self._assert_session_seat(session)
            cancelled = await managed_chat.cancel(session, expected_turn_id=expected_turn_id)
            if cancelled:
                # Re-read after awaiting: a final report may have updated task ownership.
                session = self.sessions[session_id]
                self.sessions[session_id] = session.model_copy(
                    update={
                        "chat_paused": True,
                        "status": ManagedSessionStatus.NEEDS_INPUT,
                        "runtime_status": AgentRuntimeStatus.ATTENTION,
                        "updated_at": _now(),
                    }
                )
                self._save_state()
            return cancelled

    async def _reset_session_context(self, session: ManagedSession) -> None:
        if session.session_kind != SessionKind.CHAT:
            await self.send_session_message(session.id, "/clear")
            await asyncio.sleep(0.5)
            return
        lock = self._pump_locks.setdefault(session.id, asyncio.Lock())
        async with lock:
            session = self.sessions[session.id]
            self._assert_session_seat(session)
            if session.uncertain_call_ids:
                raise RuntimeError("Cannot reset a Chat with uncertain message delivery")
            if managed_chat.is_turn_running(session):
                await managed_chat.cancel(session)
            await managed_chat.reset_context(self.sessions[session.id])

    def _refresh_chat_session_status(self, session: ManagedSession) -> None:
        """Observe native state without terminal heuristics or automatic replay."""
        if session.status in {
            ManagedSessionStatus.STOPPED,
            ManagedSessionStatus.DONE,
            ManagedSessionStatus.ERROR,
        }:
            return
        runtime = managed_chat.runtime_status(session)
        if session.chat_paused or session.uncertain_call_ids:
            runtime = AgentRuntimeStatus.ATTENTION
        status = {
            AgentRuntimeStatus.WORKING: ManagedSessionStatus.WORKING,
            AgentRuntimeStatus.IDLE: ManagedSessionStatus.IDLE,
            AgentRuntimeStatus.ATTENTION: ManagedSessionStatus.NEEDS_INPUT,
            AgentRuntimeStatus.OFFLINE: ManagedSessionStatus.NEEDS_INPUT,
        }.get(runtime, ManagedSessionStatus.NEEDS_INPUT)
        task_id = session.current_task_id or session.task_id
        task = self.tasks.get(task_id) if task_id else None
        if (
            runtime == AgentRuntimeStatus.IDLE
            and task is not None
            and self._workspace_owns_task(task)
            and (
                task.status == WorkspaceTaskStatus.WORKING
                or (
                    task.status == WorkspaceTaskStatus.REVIEW
                    and task.review_session_id == session.id
                    and task.review_requested_at is not None
                    and task.review_completed_at is None
                )
            )
            and not session.pending_call_ids
        ):
            # A model turn ending is not a Task report. Surface the missing handoff
            # instead of declaring success or replaying potentially executed work.
            status = ManagedSessionStatus.NEEDS_INPUT
        if status != session.status or runtime != session.runtime_status:
            self.sessions[session.id] = session.model_copy(
                update={"status": status, "runtime_status": runtime, "updated_at": _now()}
            )
