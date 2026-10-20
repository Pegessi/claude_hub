"""Authenticated receipt API and native transport adapter for ordinary tab inboxes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException

from ....auth.dependencies import get_current_user
from ....models import AgentStreamEvent, AgentStreamEventType, SessionKind, User
from ....models.chat_message import ChatMessageCreate, ChatMessageEnvelope, ChatMessageReceipt
from ....services.chat_message_inbox import (
    ChatMessageInbox,
    InboxError,
    InboxEvidence,
    InboxTarget,
    inspect_chat_delivery,
)
from ....services.runtime_isolation import resolve_runtime_home

_inbox: ChatMessageInbox | None = None


def _target(tab_id: str) -> InboxTarget:
    from ....services import ttyd_manager
    from ... import agent_stream as stream
    from .dispatch import require_tab_input_available

    try:
        session = stream._terminal_tab_session_or_404(tab_id)
        if session.session_kind != SessionKind.CHAT or not stream._is_chat_native(session):
            raise InboxError(409, "Target must be an ordinary native Chat tab")
        tab = ttyd_manager.get_tab(tab_id)
        assert tab is not None
        if tab.archived:
            waiting = "archived"
        else:
            require_tab_input_available(tab_id)
            waiting = None
    except HTTPException as exc:
        raise InboxError(exc.status_code, str(exc.detail)) from exc
    # Hash execution configuration, including secrets without exposing values.
    # Name/archive state/provider-assigned initial conversation ID are excluded.
    identity = {
        "created_at": tab.created_at.isoformat(),
        "agent_type": tab.agent_type.value,
        "session_kind": tab.session_kind.value,
        "target": tab.target.value,
        "cwd": tab.cwd,
        "env": tab.env,
        "chat_mode": tab.chat_mode.value,
        "cursor_transport": tab.cursor_transport,
    }
    incarnation = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return InboxTarget(
        incarnation, tab.agent_session_id if tab.agent_session_id_verified else None, waiting
    )


def _require_actor(item: ChatMessageEnvelope) -> None:
    from ....config import settings

    # Preserve local-network shared trust. This is not per-agent identity.
    if item.scheduled_run_id or item.principal == "local" or not settings.auth_enabled:
        return
    allowed_ids = settings.allowed_open_ids_list
    allowed_emails = [email.lower() for email in settings.allowed_emails_list]
    if allowed_ids and item.principal not in allowed_ids:
        raise InboxError(403, "Chat message sender authorization was revoked")
    if (
        not allowed_ids
        and allowed_emails
        and (item.principal_email or "").lower() not in allowed_emails
    ):
        raise InboxError(403, "Chat message sender authorization was revoked")


async def ensure_chat_runtime(tab_id: str) -> bool:
    """Ensure a direct Chat tab's native runtime is spawned and ready.

    Shared inbox cold-runtime gate. Idempotent: a warm Chat returns
    immediately; a cold or idle-reaped Chat is lazily created and its consumer
    started. Returns ``True`` only once a turn can actually be delivered, so the
    scheduler parks and retries while a first-time provider spawn (or a Codex
    app-server handshake) is still in flight instead of delivering into a
    runtime that does not exist yet.
    """
    from ....services.agent_stream.tailer import StructuredSourceUnavailable
    from ... import agent_stream as stream

    session = stream._terminal_tab_stream_session(tab_id)
    if session is None:
        return False
    manager = stream._get_tab_tailer_manager()
    try:
        tailer = await manager.ensure_started(session)
    except (ValueError, StructuredSourceUnavailable):
        stream.logger.info(
            "scheduled Chat cold runtime unavailable for tab=%s; will retry",
            tab_id,
        )
        return False
    transport = tailer.native_transport
    if transport is None or tailer.native_error is not None or manager.hard_failed(session.id):
        return False
    # Let the freshly started consumer's first tick run provider start().
    # Persistent app-servers also require a completed protocol handshake.
    if not tailer.native_started:
        await asyncio.sleep(0)
    return tailer.native_ready


async def _submit(item: ChatMessageEnvelope, before_delivery: Callable[[], None]) -> None:
    from ....services.goal_run import get_goal_admission_lock, get_goal_manager
    from ... import agent_stream as stream

    async with get_goal_admission_lock(item.tab_id):
        _require_actor(item)
        goal = get_goal_manager().current(item.tab_id)
        if goal is not None and (
            (goal.status.value in ({"active"} if item.scheduled_run_id else {"active", "blocked"}))
            or goal.dispatch_state.value != "idle"
        ):
            raise InboxError(409, "Waiting for Goal ownership to be released in Web Chat")
        target = _target(item.tab_id)
        if target.waiting_reason:
            raise InboxError(409, target.waiting_reason)
        session = stream._terminal_tab_session_or_404(item.tab_id)
        manager = stream._get_tab_tailer_manager()
        if await manager.turn_in_flight(session):
            raise InboxError(409, "Waiting for the current Chat turn")
        if not await ensure_chat_runtime(item.tab_id):
            raise InboxError(503, "Waiting for the Chat runtime to start")
        metadata = (
            item.turn_metadata
            if item.scheduled_run_id
            else {
                "origin": "hub_message",
                "hub_message": {
                    "message_id": item.message_id,
                    "actor": item.actor,
                    **({"source_tab_id": item.source_tab_id} if item.source_tab_id else {}),
                },
            }
        )

        # Check incarnation again inside the native send lock, then durably
        # mark possible submission immediately before touching the provider.
        async def check_target() -> None:
            inbox = get_inbox()
            _require_actor(item)
            current = inbox._target(item.tab_id)
            if current.incarnation != item.incarnation or current.waiting_reason:
                raise InboxError(409, "Chat target changed before submission")

        try:
            await manager.send_message(
                session,
                item.message,
                [],
                item.turn_id,
                turn_metadata=metadata,
                strip_goal_protocol=get_goal_manager().has_goal_history(item.tab_id),
                before_send=check_target,
                before_delivery=before_delivery,
                allow_question_answer=False,
            )
        except RuntimeError as exc:
            if "turn is already in flight" in str(exc):
                raise InboxError(409, "Waiting for the current Chat turn") from exc
            raise


async def _evidence(item: ChatMessageEnvelope) -> InboxEvidence:
    from ... import agent_stream as stream

    proof = await inspect_chat_delivery(
        item.tab_id,
        item.turn_id,
        legacy_errors_terminal=bool(item.scheduled_run_id and item.legacy_recovery),
    )
    if proof.state in {"completed", "failed", "cancelled"}:
        return proof
    if stream._tab_tailer_manager is not None:
        tailer = stream._tab_tailer_manager.get_tailer(f"terminal-tab-{item.tab_id}")
        if tailer and tailer.native_transport and tailer.native_transport.turn_in_flight:
            session = stream._terminal_tab_stream_session(item.tab_id)
            if session and await stream._tab_tailer_manager.active_turn_id(session) == item.turn_id:
                return InboxEvidence("live")
    return proof


def get_inbox() -> ChatMessageInbox:
    global _inbox
    if _inbox is None:
        _inbox = ChatMessageInbox(
            resolve_runtime_home() / "chat_messages.json", _target, _submit, _evidence
        )
    return _inbox


async def start_inbox() -> None:
    from ... import agent_stream as stream

    # Import persisted legacy occurrences before the coordinator can submit.
    stream.workspace_manager._admit_scheduled_intents()
    await get_inbox().start()
    stream.workspace_manager._scheduled_chat_recovery_pending = False


async def stop_inbox() -> None:
    global _inbox
    if _inbox is not None:
        await _inbox.stop()
        _inbox = None


async def observe_completion(event: AgentStreamEvent) -> None:
    if _inbox is not None and event.type == AgentStreamEventType.TURN_COMPLETED and event.turn_id:
        if not event.payload.get("delivery_rejected"):
            await _inbox.completed(event.tab_id, event.turn_id, str(event.payload.get("status")))


def register_message_routes(router: APIRouter) -> None:
    def inbox() -> ChatMessageInbox:
        try:
            return get_inbox()
        except Exception as exc:
            raise HTTPException(503, "Chat message inbox unavailable") from exc

    @router.post("/tabs/{tab_id}/messages", response_model=ChatMessageReceipt, status_code=202)
    async def enqueue(
        tab_id: str, payload: ChatMessageCreate, user: User = Depends(get_current_user)
    ) -> ChatMessageReceipt:
        try:
            return await inbox().enqueue(
                tab_id,
                user.open_id,
                "local CLI" if user.open_id == "local" else user.name,
                payload,
                email=user.email,
            )
        except InboxError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @router.get("/tabs/{tab_id}/messages")
    async def list_messages(
        tab_id: str, user: User = Depends(get_current_user)
    ) -> dict[str, list[ChatMessageReceipt]]:
        return {"messages": await inbox().list(tab_id, user.open_id)}

    @router.get("/tabs/{tab_id}/messages/{message_id}", response_model=ChatMessageReceipt)
    async def status(
        tab_id: str, message_id: str, user: User = Depends(get_current_user)
    ) -> ChatMessageReceipt:
        try:
            return await inbox().get(tab_id, message_id, user.open_id)
        except InboxError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    @router.delete("/tabs/{tab_id}/messages/{message_id}", response_model=ChatMessageReceipt)
    async def cancel(
        tab_id: str, message_id: str, user: User = Depends(get_current_user)
    ) -> ChatMessageReceipt:
        try:
            return await inbox().cancel(tab_id, message_id, user.open_id)
        except InboxError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
