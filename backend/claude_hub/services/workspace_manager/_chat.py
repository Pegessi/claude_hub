"""Workspace controls for native Chat sessions without terminal side effects."""

import asyncio
from typing import Optional

import claude_hub.services.managed_chat as managed_chat

from ...models import (
    AgentRuntimeStatus,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    WorkspaceAttachmentCreate,
    WorkspaceTaskStatus,
)
from ._base import _WorkspaceManagerBase
from ._constants import _now


class _ChatMixin(_WorkspaceManagerBase):
    async def send_chat_session_message(
        self,
        session_id: str,
        message: str,
        attachments: list[WorkspaceAttachmentCreate] | None = None,
        call_id: str | None = None,
        *,
        expected_session: ManagedSession | None = None,
    ) -> None:
        """Submit explicit user input through the durable Workspace mailbox."""
        current = self.sessions.get(session_id)
        if current is None:
            raise KeyError(session_id)
        if expected_session is not None and (
            current.workspace_id != expected_session.workspace_id
            or current.tab_id != expected_session.tab_id
            or current.created_at != expected_session.created_at
        ):
            raise KeyError(f"session {session_id} was deleted or replaced")
        if current.archived:
            raise ValueError("Unarchive the managed Chat session before sending input")
        if current.session_kind != SessionKind.CHAT:
            raise ValueError("This endpoint requires a managed Chat session")
        if not message.strip() and not attachments:
            raise ValueError("A message or attachment is required")
        if call_id:
            fingerprint = self._compute_payload_fingerprint(message, attachments or [])
            existing = current.call_payload_fingerprints.get(call_id)
            if existing is not None:
                if existing != fingerprint:
                    raise ValueError("call_id already identifies a different message")
                if call_id in current.processing_call_ids or call_id in current.delivered_call_ids:
                    return
        lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
        async with self.workspace_mutation_lock(current.workspace_id):
            async with lock:
                session = self.sessions.get(session_id)
                if (
                    session is None
                    or session.tab_id != current.tab_id
                    or session.created_at != current.created_at
                ):
                    raise KeyError(f"session {session_id} was deleted or replaced")
                await self.send_chat_session_message_locked(
                    session_id, message, attachments, call_id
                )

    async def send_chat_session_message_locked(
        self,
        session_id: str,
        message: str,
        attachments: list[WorkspaceAttachmentCreate] | None = None,
        call_id: str | None = None,
        *,
        question_key: str | None = None,
    ) -> None:
        """Submit while the caller holds the workspace and session pump locks.

        Question-card admission must retain those locks through mailbox delivery
        so another answer cannot validate the same card before it is resolved.
        """
        session = self.sessions[session_id]
        if session.archived:
            raise ValueError("Unarchive the managed Chat session before sending input")
        if session.session_kind != SessionKind.CHAT:
            raise ValueError("This endpoint requires a managed Chat session")
        if not message.strip() and not attachments:
            raise ValueError("A message or attachment is required")
        # Validate before either an idempotent acknowledgement or resuming a
        # paused session. Accepted retries never change a card or runtime state.
        fingerprint = self._compute_payload_fingerprint(message, attachments or [])
        if call_id and call_id in session.call_payload_fingerprints:
            if session.call_payload_fingerprints[call_id] != fingerprint:
                raise ValueError("call_id already identifies a different message")
            if call_id in session.processing_call_ids or call_id in session.delivered_call_ids:
                return
        if session.status == ManagedSessionStatus.STOPPED:
            raise ValueError("The managed Chat session has been stopped")
        self._assert_session_seat(session)
        if session.uncertain_call_ids:
            raise RuntimeError("Chat delivery is uncertain; reconcile it before resuming")
        self._resume_chat_session_locked(session_id)
        await self._send_session_message_locked(
            session_id, message, attachments, call_id, question_key=question_key
        )

    async def _resume_chat_session(self, session_id: str) -> None:
        """Resume a managed Chat while serialized with delivery and deletion."""
        expected = self.sessions.get(session_id)
        if expected is None:
            return
        lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
        async with self.workspace_mutation_lock(expected.workspace_id):
            async with lock:
                live = self.sessions.get(session_id)
                if (
                    live is None
                    or live.tab_id != expected.tab_id
                    or live.created_at != expected.created_at
                ):
                    return
                self._resume_chat_session_locked(session_id)

    def _resume_chat_session_locked(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None or session.session_kind != SessionKind.CHAT or not session.chat_paused:
            return
        if session.archived:
            raise RuntimeError("Unarchive the managed Chat session before resuming")
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
        expected = self.sessions.get(session_id)
        if expected is None:
            raise KeyError(session_id)
        lock = self._pump_locks.setdefault(session_id, asyncio.Lock())
        async with self.workspace_mutation_lock(expected.workspace_id):
            async with lock:
                session = self.sessions.get(session_id)
                if (
                    session is None
                    or session.tab_id != expected.tab_id
                    or session.created_at != expected.created_at
                ):
                    raise KeyError(f"session {session_id} was deleted or replaced")
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
        async with self.workspace_mutation_lock(session.workspace_id):
            async with lock:
                live = self.sessions.get(session.id)
                if (
                    live is None
                    or live.tab_id != session.tab_id
                    or live.created_at != session.created_at
                ):
                    raise KeyError(f"session {session.id} was deleted or replaced")
                self._assert_session_seat(live)
                if live.uncertain_call_ids:
                    raise RuntimeError("Cannot reset a Chat with uncertain message delivery")
                if managed_chat.is_turn_running(live):
                    await managed_chat.cancel(live)
                current = self.sessions.get(session.id)
                if (
                    current is None
                    or current.tab_id != session.tab_id
                    or current.created_at != session.created_at
                ):
                    raise KeyError(f"session {session.id} was deleted or replaced")
                await managed_chat.reset_context(current)

    def _refresh_chat_session_status(self, session: ManagedSession) -> bool:
        """Observe native state without terminal heuristics or automatic replay."""
        live = self.sessions.get(session.id)
        if live is None or live.tab_id != session.tab_id or live.created_at != session.created_at:
            return False
        # The monitor snapshots before awaiting terminal status collection.
        # Derive every update from the current incarnation so deletion, reuse,
        # mailbox, pause, and configuration changes cannot be overwritten by
        # that stale snapshot.
        session = live
        if session.status in {
            ManagedSessionStatus.STOPPED,
            ManagedSessionStatus.DONE,
            ManagedSessionStatus.ERROR,
        }:
            return False
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
            return True
        return False
