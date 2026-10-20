"""Durable ordinary-tab FIFO. No schedules, Task ownership, or automatic replay.

The application runtime lock ensures single-process ownership of the atomic JSON
store. A 202 receipt acknowledges intent persistence, not provider execution.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Awaitable, Callable, Literal, cast

from ..models.chat_message import (
    TERMINAL_STATES,
    ChatMessageCreate,
    ChatMessageEnvelope,
    ChatMessageReceipt,
    ChatMessageState,
    utc_now,
)

logger = logging.getLogger(__name__)


class InboxError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class InboxTarget:
    incarnation: str
    conversation_id: str | None = None
    waiting_reason: str | None = None


@dataclass(frozen=True)
class InboxEvidence:
    state: Literal["absent", "started", "completed", "failed", "cancelled", "live"]


class ChatMessageInbox:
    def __init__(
        self,
        path: Path,
        target: Callable[[str], InboxTarget],
        submit: Callable[[ChatMessageEnvelope, Callable[[], None]], Awaitable[None]],
        evidence: Callable[[ChatMessageEnvelope], Awaitable[InboxEvidence]],
        *,
        max_pending: int = 100,
        max_bytes: int = 1024 * 1024,
    ) -> None:
        self.path, self.target, self.submit, self.evidence = path, target, submit, evidence
        self.max_pending, self.max_bytes = max_pending, max_bytes
        self.messages: dict[str, ChatMessageEnvelope] = {}
        self.conversations: dict[str, str] = {}
        self.epochs: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._pump_locks: dict[str, asyncio.Lock] = {}
        self._wakeup = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._closing = False
        self._committed = ""
        if path.exists():
            self._restore(path.read_text(encoding="utf-8"))
        self._committed = self._dump()

    def _dump(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "messages": [m.model_dump(mode="json") for m in self.messages.values()],
                "conversations": self.conversations,
                "epochs": self.epochs,
            },
            ensure_ascii=False,
        )

    def _restore(self, payload: str) -> None:
        data = json.loads(payload)
        if data.get("version") != 1:
            raise RuntimeError("unsupported Chat inbox version")
        rows = [ChatMessageEnvelope.model_validate(m) for m in data["messages"]]
        if len({m.message_id for m in rows}) != len(rows):
            raise RuntimeError("duplicate Chat inbox identity")
        self.messages = {m.message_id: m for m in rows}
        self.conversations = dict(data["conversations"])
        self.epochs = dict(data.get("epochs", {}))

    def _save(self) -> None:
        payload = self._dump()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            # A post-replace failure may have committed. Fail closed until
            # process recovery, rather than ack/retry from divergent memory.
            self._closing = True
            self._restore(self._committed)
            raise
        self._committed = payload

    def _change(self, message_id: str, state: ChatMessageState, reason: str | None = None) -> None:
        if self._closing:
            raise InboxError(503, "Chat inbox unavailable")
        item = self.messages[message_id]
        item.state, item.updated_at = state, utc_now()
        item.waiting_reason = reason if state == "waiting" else None
        item.error = reason if state in {"failed", "uncertain"} else None
        self._save()

    def _target(self, tab_id: str) -> InboxTarget:
        target = self.target(tab_id)
        key = f"{tab_id}:{target.incarnation}"
        pinned = self.conversations.get(key)
        if pinned and (target.conversation_id or "") != pinned:
            self.epochs[key] = self.epochs.get(key, 0) + 1
            self.conversations[key] = target.conversation_id or ""
        elif not pinned and target.conversation_id:
            self.conversations[key] = target.conversation_id
        return InboxTarget(
            f"{target.incarnation}:{self.epochs.get(key, 0)}",
            target.conversation_id,
            target.waiting_reason,
        )

    async def enqueue(
        self,
        tab_id: str,
        principal: str,
        actor: str,
        payload: ChatMessageCreate,
        *,
        email: str | None = None,
    ) -> ChatMessageReceipt:
        async with self._lock:
            if self._closing:
                raise InboxError(503, "Chat inbox unavailable or shutting down")
            target = self._target(tab_id)
            fingerprint = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
            for item in self.messages.values():
                if (item.principal, item.tab_id, item.incarnation, item.call_id) == (
                    principal,
                    tab_id,
                    target.incarnation,
                    payload.call_id,
                ):
                    if item.fingerprint != fingerprint:
                        raise InboxError(409, "call_id already identifies a different message")
                    if item.expired:
                        raise InboxError(410, "Receipt expired; old call_id cannot be replayed")
                    return item.receipt()
            if target.waiting_reason == "archived":
                raise InboxError(409, "Restore this archived Chat before sending")
            pending = [
                m
                for m in self.messages.values()
                if m.tab_id == tab_id and m.state not in TERMINAL_STATES
            ]
            if (
                len(pending) >= self.max_pending
                or sum(len(m.message.encode()) for m in pending) + len(payload.message.encode())
                > self.max_bytes
            ):
                raise InboxError(429, "Chat inbox backlog limit reached")
            message_id, now = str(uuid.uuid4()), utc_now()
            item = ChatMessageEnvelope(
                message_id=message_id,
                call_id=payload.call_id,
                tab_id=tab_id,
                turn_id=f"hub-message-{message_id}",
                state="queued",
                created_at=now,
                updated_at=now,
                status_url=f"/api/workspaces/tabs/{tab_id}/messages/{message_id}",
                principal=principal,
                actor=actor,
                principal_email=email,
                incarnation=target.incarnation,
                fingerprint=fingerprint,
                message=payload.message,
                source_tab_id=payload.source_tab_id,
                sequence=max((m.sequence for m in self.messages.values()), default=0) + 1,
            )
            self.messages[message_id] = item
            self._save()
            self.wake()
            return item.receipt()

    def _owned(self, tab_id: str, message_id: str, principal: str) -> ChatMessageEnvelope:
        item = self.messages.get(message_id)
        if item is None or item.tab_id != tab_id or item.principal != principal:
            raise InboxError(404, "Chat message not found")
        if item.expired:
            raise InboxError(410, "Chat message receipt expired")
        return item

    async def get(self, tab_id: str, message_id: str, principal: str) -> ChatMessageReceipt:
        async with self._lock:
            return self._owned(tab_id, message_id, principal).receipt()

    async def list(self, tab_id: str, principal: str) -> list[ChatMessageReceipt]:
        async with self._lock:
            return [
                m.receipt()
                for m in sorted(self.messages.values(), key=lambda m: m.sequence)
                if m.tab_id == tab_id and m.principal == principal and not m.expired
            ]

    async def cancel(self, tab_id: str, message_id: str, principal: str) -> ChatMessageReceipt:
        async with self._lock:
            if self._closing:
                raise InboxError(503, "Chat inbox unavailable")
            item = self._owned(tab_id, message_id, principal)
            if item.state == "cancelled":
                return item.receipt()
            if item.state not in {"queued", "waiting"}:
                raise InboxError(409, "Only not-yet-submitted messages may be cancelled")
            self._change(message_id, "cancelled")
            self.wake()
            return item.receipt()

    def wake(self) -> None:
        self._wakeup.set()

    async def completed(self, tab_id: str, turn_id: str, status: str) -> None:
        async with self._lock:
            if self._closing:
                return
            for item in self.messages.values():
                if (
                    item.tab_id == tab_id
                    and item.turn_id == turn_id
                    and item.state not in TERMINAL_STATES
                ):
                    state: ChatMessageState = (
                        "completed"
                        if status == "completed"
                        else ("cancelled" if status in {"cancelled", "canceled"} else "failed")
                    )
                    self._change(item.message_id, state)
                    break
        self.wake()

    def _before_delivery(self, message_id: str) -> None:
        # Synchronous event-loop callback: no writer can interleave this write.
        if self._closing:
            raise InboxError(503, "Chat inbox is shutting down")
        item = self.messages[message_id]
        current = self._target(item.tab_id)
        if current.incarnation != item.incarnation or current.waiting_reason:
            raise InboxError(409, "Chat target changed before submission")
        item.submission_started = True
        self._save()

    async def reconcile(self, *, restarting: bool = False) -> None:
        for old in list(self.messages.values()):
            if old.state in {"dispatching", "accepted", "uncertain"}:
                try:
                    proof = await self.evidence(old)
                except Exception:
                    proof = InboxEvidence("started")  # unknown is never absent
                async with self._lock:
                    item = self.messages[old.message_id]
                    if item.state in TERMINAL_STATES:
                        continue
                    if proof.state in {"completed", "failed", "cancelled"}:
                        self._change(item.message_id, cast(ChatMessageState, proof.state))
                    elif (
                        item.state == "accepted"
                        and proof.state != "live"
                        and not restarting
                        and utc_now() - item.updated_at > timedelta(seconds=60)
                    ):
                        self._change(
                            item.message_id,
                            "uncertain",
                            "Accepted turn lost runtime ownership; reconcile before retry",
                        )
                    elif restarting and proof.state != "live":
                        if (
                            proof.state == "absent"
                            and not item.submission_started
                            and item.state == "dispatching"
                        ):
                            self._change(item.message_id, "queued")
                        else:
                            self._change(
                                item.message_id,
                                "uncertain",
                                "Provider delivery needs reconciliation; no automatic replay",
                            )
        async with self._lock:
            changed = False
            for item in self.messages.values():
                if (
                    item.state in TERMINAL_STATES
                    and not item.expired
                    and utc_now() - item.updated_at > timedelta(days=30)
                ):
                    # Full receipts expire, but compact arbitrary-key tombstones
                    # remain so expiry cannot make an old call replayable.
                    item.message, item.expired = "", True
                    item.source_tab_id = None
                    item.principal_email = None
                    item.actor = ""
                    changed = True
            if changed:
                self._save()

    async def pump(self, tab_id: str) -> None:
        async with self._pump_locks.setdefault(tab_id, asyncio.Lock()):
            while not self._closing:
                async with self._lock:
                    active = sorted(
                        (
                            m
                            for m in self.messages.values()
                            if m.tab_id == tab_id and m.state not in TERMINAL_STATES
                        ),
                        key=lambda m: m.sequence,
                    )
                    if not active:
                        return
                    item = active[0]
                    if item.state in {"dispatching", "accepted", "uncertain"}:
                        return
                    try:
                        target = self._target(tab_id)
                    except InboxError as exc:
                        self._change(
                            item.message_id,
                            "cancelled" if exc.status == 404 else "failed",
                            exc.detail,
                        )
                        continue
                    if target.incarnation != item.incarnation:
                        self._change(item.message_id, "failed", "Chat incarnation/backend changed")
                        continue
                    if target.waiting_reason:
                        self._change(item.message_id, "waiting", target.waiting_reason)
                        return
                    item.attempts += 1
                    self._change(item.message_id, "dispatching")
                try:
                    await self.submit(
                        item.model_copy(deep=True), lambda: self._before_delivery(item.message_id)
                    )
                except BaseException as exc:
                    async with self._lock:
                        live = self.messages[item.message_id]
                        if live.state not in TERMINAL_STATES:
                            if live.submission_started:
                                self._change(
                                    item.message_id,
                                    "uncertain",
                                    "Submission may have reached provider; no automatic replay",
                                )
                            elif isinstance(exc, InboxError) and exc.status in {403, 404}:
                                self._change(item.message_id, "failed", exc.detail)
                            elif isinstance(exc, InboxError) and exc.status == 409:
                                live.attempts = 0
                                self._change(item.message_id, "waiting", exc.detail)
                            elif live.attempts >= 12:
                                self._change(
                                    item.message_id,
                                    "failed",
                                    "Chat runtime unavailable after 12 attempts",
                                )
                            else:
                                self._change(item.message_id, "waiting", "Waiting for Chat runtime")
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    logger.debug("Chat inbox submit deferred", exc_info=True)
                    return
                async with self._lock:
                    if self.messages[item.message_id].state == "dispatching":
                        self._change(item.message_id, "accepted")
                return

    async def start(self) -> None:
        await self.reconcile(restarting=True)
        self._closing = False
        self._runner = asyncio.create_task(self._run(), name="chat-message-inbox")

    async def _run(self) -> None:
        while not self._closing:
            self._wakeup.clear()
            try:
                await self.reconcile()
                await asyncio.gather(
                    *(
                        self.pump(tab)
                        for tab in {
                            m.tab_id
                            for m in self.messages.values()
                            if m.state not in TERMINAL_STATES
                        }
                    )
                )
            except Exception:
                logger.exception("Chat inbox recovery/drain failed")
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._closing = True
        self.wake()
        if self._runner:
            await self._runner  # do not cancel provider submission
            self._runner = None
