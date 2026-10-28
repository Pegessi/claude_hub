"""Durable per-tab delivery core shared by direct and timer-owned intents.

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
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Awaitable, Callable, Literal, cast

from ..models.chat_message import (
    TERMINAL_STATES,
    ChatMessageCreate,
    ChatMessageEnvelope,
    ChatMessageReceipt,
    ChatMessageState,
    ChatQuestion,
    QuestionAnswerCreate,
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


async def inspect_chat_delivery(
    tab_id: str, turn_id: str, *, legacy_errors_terminal: bool = False
) -> InboxEvidence:
    """Read durable native evidence without creating a runtime (both front doors)."""
    from ..models import AgentStreamEventType
    from .agent_stream.store import AgentStreamStore

    store = AgentStreamStore("terminal-tabs", f"terminal-tab-{tab_id}")
    cursor, started = -1, False
    while True:
        page = await store.read_since(cursor, limit=5000)
        for event in page.events:
            if event.turn_id != turn_id:
                continue
            if event.type == AgentStreamEventType.TURN_STARTED:
                started = True
            elif event.type == AgentStreamEventType.TURN_COMPLETED:
                if event.payload.get("delivery_rejected"):
                    started = True  # synthetic rejection is never absence evidence
                    continue
                status = event.payload.get("status")
                return InboxEvidence(
                    "completed"
                    if status in {"completed", "success", "ok"}
                    else ("cancelled" if status in {"cancelled", "canceled"} else "failed")
                )
            elif legacy_errors_terminal and event.type == AgentStreamEventType.ERROR:
                return InboxEvidence("failed")
        if not page.has_more:
            return InboxEvidence("started" if started else "absent")
        cursor = page.next_sequence


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
        self.scheduled_prepare: Callable[[ChatMessageEnvelope], Awaitable[str | None]] | None = None
        self.scheduled_changed: Callable[[ChatMessageEnvelope], None] | None = None
        self.scheduled_liveness: Callable[[ChatMessageEnvelope], Awaitable[str]] | None = None
        self.path, self.target, self.submit, self.evidence = path, target, submit, evidence
        self.max_pending, self.max_bytes = max_pending, max_bytes
        self.messages: dict[str, ChatMessageEnvelope] = {}
        self.questions: dict[str, ChatQuestion] = {}
        self.callback_receipts: dict[str, dict[str, str]] = {}
        self.callback_nonces: dict[str, dict[str, str]] = {}
        self.answer_submit: (
            Callable[[ChatMessageEnvelope, Callable[[], None]], Awaitable[None]] | None
        ) = None
        self.question_tick: Callable[[], Awaitable[None]] | None = None
        self._answer_tasks: dict[str, asyncio.Task[None]] = {}
        self._question_tasks: dict[str, asyncio.Task[None]] = {}
        self.conversations: dict[str, str] = {}
        self.epochs: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._pump_locks: dict[str, asyncio.Lock] = {}
        self._wakeup = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        # Failed startup evidence reads must be retried with startup policy on
        # ordinary coordinator passes. A new process rebuilds this set from
        # durable in-flight states; no new delivery state or schema is needed.
        self._restart_recovery: set[str] = set()
        self._retry_handles: dict[str, asyncio.TimerHandle] = {}
        self._pump_tasks: dict[str, asyncio.Task[None]] = {}
        self.redrain_seconds = 3.0
        self._closing = False
        self._committed = ""
        if path.exists():
            original = path.read_text(encoding="utf-8")
            self._restore(original)
            if json.loads(original).get("version") == 1:
                # Never overwrite the only pre-migration evidence.
                backup = path.with_suffix(".v1-backup.json")
                if not backup.exists():
                    with backup.open("x", encoding="utf-8") as stream:
                        os.chmod(backup, 0o600)
                        stream.write(original)
                        stream.flush()
                        os.fsync(stream.fileno())
                self._committed = self._dump()
                self._save()
        self._committed = self._dump()

    def _dump(self) -> str:
        return json.dumps(
            {
                "version": 2,
                "questions": [q.model_dump(mode="json") for q in self.questions.values()],
                "callback_receipts": self.callback_receipts,
                "callback_nonces": self.callback_nonces,
                "messages": [m.model_dump(mode="json") for m in self.messages.values()],
                "conversations": self.conversations,
                "epochs": self.epochs,
            },
            ensure_ascii=False,
        )

    def _restore(self, payload: str) -> None:
        data = json.loads(payload)
        if data.get("version") not in {1, 2}:
            raise RuntimeError("unsupported Chat inbox version")
        rows = [ChatMessageEnvelope.model_validate(m) for m in data["messages"]]
        if len({m.message_id for m in rows}) != len(rows):
            raise RuntimeError("duplicate Chat inbox identity")
        questions = [ChatQuestion.model_validate(q) for q in data.get("questions", [])]
        if len({q.question_ref for q in questions}) != len(questions):
            raise RuntimeError("duplicate Chat question identity")
        by_id = {m.message_id: m for m in rows}
        for q in questions:
            if q.answer_id is not None and (
                q.answer_id not in by_id or by_id[q.answer_id].question_ref != q.question_ref
            ):
                raise RuntimeError("Question answer ownership is corrupt")
        question_refs = {q.question_ref for q in questions}
        if any(m.kind == "question_answer" and m.question_ref not in question_refs for m in rows):
            raise RuntimeError("Question answer target is corrupt")
        self.questions = {q.question_ref: q for q in questions}
        self.callback_receipts = dict(data.get("callback_receipts", {}))
        self.callback_nonces = dict(data.get("callback_nonces", {}))
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
        if item.scheduled_run_id and self.scheduled_changed:
            self.scheduled_changed(item)

    def admit_scheduled(
        self, item: ChatMessageEnvelope, *, enforce_limit: bool = True
    ) -> ChatMessageEnvelope:
        """Idempotent internal admission. Caller persists occurrence before pumping."""
        if self._closing:
            raise InboxError(503, "Chat inbox unavailable")
        existing = self.messages.get(item.message_id)
        if existing is not None:
            return existing
        item.sequence = max((m.sequence for m in self.messages.values()), default=0) + 1
        pending = [m for m in self.messages.values() if m.tab_id == item.tab_id and m.blocks_queue]
        # Existing persisted occurrences are migrated even above the new shared
        # cap; limits govern new intent acceptance, never discard old work.
        # Scheduled message size keeps its existing contract; public sends keep
        # their 64-KiB/1-MiB bounds.
        if (
            enforce_limit
            and item.state in {"queued", "waiting"}
            and len(pending) >= self.max_pending
        ):
            raise InboxError(429, "Chat inbox backlog limit reached")
        self.messages[item.message_id] = item
        self._save()
        self.wake()
        return item

    def cancel_scheduled(self, message_id: str, *, release: bool = False) -> bool:
        """Trusted schedule policy; public cancellation remains unsent-only."""
        item = self.messages.get(message_id)
        if item is None or not item.scheduled_run_id:
            return False
        if item.state in {"queued", "waiting"} or (release and item.blocks_queue):
            self._change(message_id, "cancelled")
            self.wake()
            return True
        return False

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
            pending = [m for m in self.messages.values() if m.tab_id == tab_id and m.blocks_queue]
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

    def retry_after(self, tab_id: str, seconds: float) -> None:
        """Bounded delayed wakeup for standalone embeddings; coordinator owns live retry."""
        if self._runner is not None:
            self.wake()
            return
        if tab_id in self._retry_handles or self._closing:
            return

        def retry() -> None:
            self._retry_handles.pop(tab_id, None)
            if self._closing or tab_id in self._pump_tasks:
                return
            task = asyncio.create_task(self.pump(tab_id), name=f"chat-inbox-retry-{tab_id}")
            self._pump_tasks[tab_id] = task
            task.add_done_callback(partial(self._pump_finished, tab_id))

        self._retry_handles[tab_id] = asyncio.get_running_loop().call_later(seconds, retry)

    async def completed(self, tab_id: str, turn_id: str, status: str) -> None:
        async with self._lock:
            if self._closing:
                return
            for item in self.messages.values():
                if item.tab_id == tab_id and item.turn_id == turn_id and item.blocks_queue:
                    state: ChatMessageState = (
                        "completed"
                        if status == "completed"
                        or (item.scheduled_run_id and status in {"success", "ok"})
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
        if item.state != "dispatching":
            raise InboxError(409, "Intent was cancelled before provider submission")
        if item.kind != "question_answer":
            current = self._target(item.tab_id)
            if current.incarnation != item.incarnation or current.waiting_reason:
                raise InboxError(409, "Chat target changed before submission")
        item.submission_started = True
        self._save()

    @staticmethod
    def _aware(stamp: datetime) -> datetime:
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp

    async def reconcile(self, *, restarting: bool = False, now: datetime | None = None) -> None:
        current_time = self._aware(now or utc_now())
        scheduled_inflight = {
            item.message_id
            for item in self.messages.values()
            if item.scheduled_run_id and item.state in {"dispatching", "accepted"}
        }
        self._restart_recovery.intersection_update(scheduled_inflight)
        if restarting:
            self._restart_recovery.update(scheduled_inflight)
        for old in list(self.messages.values()):
            recovering_startup = old.message_id in self._restart_recovery
            if old.kind == "question_answer":
                if restarting and old.state in {"dispatching", "accepted"}:
                    # The exact RPC may have been written; never replay a popped request.
                    if not old.submission_started:
                        self._change(old.message_id, "queued", "Recovered before answer submission")
                        continue
                    self._change(
                        old.message_id, "uncertain", "Answer delivery needs reconciliation"
                    )
                    if old.question_ref in self.questions:
                        self.questions[old.question_ref].state = "uncertain"
                        self._save()
                continue
            if old.scheduled_run_id and old.state == "uncertain":
                continue  # scheduler uncertainty is terminal history, not a direct-send fence
            if (
                old.scheduled_run_id
                and old.state in {"dispatching", "accepted"}
                and not recovering_startup
            ):
                if current_time - self._aware(old.updated_at) < timedelta(minutes=10):
                    continue
                if old.recovery_checked_at and current_time - self._aware(
                    old.recovery_checked_at
                ) < timedelta(seconds=60):
                    continue
                old.recovery_checked_at = current_time
            if old.state in {"dispatching", "accepted", "uncertain"}:
                observed_state, observed_stamp = old.state, old.updated_at
                try:
                    proof = await self.evidence(old)
                except Exception:
                    if old.scheduled_run_id:
                        # Unknown evidence cannot release a scheduled slot or
                        # become terminal uncertainty. Keep its state and retry;
                        # startup retries retain the original recovery policy.
                        logger.exception("Scheduled Chat delivery evidence read failed")
                        continue
                    proof = InboxEvidence("started")  # direct uncertainty stays blocking
                liveness = "unknown"
                if old.scheduled_run_id and proof.state not in {
                    "completed",
                    "failed",
                    "cancelled",
                    "live",
                }:
                    try:
                        liveness = (
                            "dead"
                            if recovering_startup
                            else (
                                await self.scheduled_liveness(old)
                                if self.scheduled_liveness
                                else "unknown"
                            )
                        )
                    except Exception:
                        logger.exception("Chat inbox liveness probe failed")
                async with self._lock:
                    item = self.messages[old.message_id]
                    if (item.state, item.updated_at) != (observed_state, observed_stamp):
                        continue
                    if item.state in TERMINAL_STATES:
                        continue
                    self._restart_recovery.discard(item.message_id)
                    if proof.state in {"completed", "failed", "cancelled"}:
                        self._change(item.message_id, cast(ChatMessageState, proof.state))
                    elif item.scheduled_run_id:
                        if liveness != "dead":
                            continue
                        if proof.state == "absent" and not item.submission_started:
                            item.legacy_recovery = False
                            self._change(
                                item.message_id, "queued", "recovered before provider dispatch"
                            )
                        else:
                            self._change(
                                item.message_id,
                                "uncertain",
                                "Chat turn stopped reporting while it was in flight",
                            )
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
                    (
                        item.state in TERMINAL_STATES
                        or (item.scheduled_run_id and item.state == "uncertain")
                    )
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
            for q in self.questions.values():
                answer = self.messages.get(q.answer_id or "")
                stamp = answer.updated_at if answer is not None else q.created_at
                if q.state in {"resolved", "stale"} and utc_now() - stamp > timedelta(days=30):
                    if (
                        q.prompt
                        or q.options
                        or q.reply_text
                        or (q.mirror and q.mirror.action_digests)
                    ):
                        q.prompt, q.options, q.reply_text = "", [], ""
                        if q.mirror:
                            q.mirror.action_digests.clear()
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
                            if m.tab_id == tab_id and m.blocks_queue and m.kind == "message"
                        ),
                        key=lambda m: m.sequence,
                    )
                    if not active:
                        return
                    item = active[0]
                    if any(m.state in {"dispatching", "accepted", "uncertain"} for m in active):
                        return
                    if item.scheduled_run_id and self.scheduled_prepare:
                        reason = await self.scheduled_prepare(item)
                        if not item.blocks_queue:
                            continue
                        if reason:
                            self._change(item.message_id, "waiting", reason)
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
                    if item.scheduled_run_id and not item.incarnation:
                        item.incarnation = target.incarnation
                        self._save()
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
                            elif item.scheduled_run_id and isinstance(exc, RuntimeError):
                                if "turn is already in flight" in str(exc):
                                    live.attempts = max(0, live.attempts - 1)
                                    self._change(
                                        item.message_id,
                                        "waiting",
                                        "waiting for the current Chat response",
                                    )
                                else:
                                    self._change(item.message_id, "failed", str(exc))
                            elif (
                                live.scheduled_run_id
                                and isinstance(exc, Exception)
                                and not isinstance(exc, InboxError)
                            ):
                                self._change(item.message_id, "failed", str(exc))
                            elif isinstance(exc, InboxError) and exc.status in {403, 404}:
                                self._change(item.message_id, "failed", exc.detail)
                            elif isinstance(exc, InboxError) and exc.status == 409:
                                live.attempts = (
                                    max(0, live.attempts - 1) if live.scheduled_run_id else 0
                                )
                                self._change(item.message_id, "waiting", exc.detail)
                            elif live.attempts >= (13 if live.scheduled_run_id else 12):
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

    async def claim_answer(
        self,
        question_ref: str,
        principal: str,
        actor: str,
        payload: QuestionAnswerCreate,
        *,
        callback: tuple[str, str] | None = None,
        source: str = "web",
        email: str | None = None,
    ) -> ChatMessageReceipt:
        async with self._lock:
            if self._closing:
                raise InboxError(503, "Question inbox unavailable")
            q = self.questions.get(question_ref)
            if q is None:
                raise InboxError(404, "Question not found")
            fingerprint = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
            if callback and callback[0] in self.callback_receipts:
                prior = self.callback_receipts[callback[0]]
                if prior["fingerprint"] != callback[1]:
                    raise InboxError(409, "Callback event payload changed")
                return self.messages[prior["answer_id"]].receipt()
            for existing in self.messages.values():
                if existing.kind == "question_answer" and existing.call_id == payload.call_id:
                    if (existing.principal, existing.question_ref, existing.fingerprint) != (
                        principal,
                        question_ref,
                        fingerprint,
                    ):
                        raise InboxError(409, "Answer call_id conflict")
                    if existing.expired:
                        raise InboxError(410, "Answer receipt expired")
                    return existing.receipt()
            if q.state != "open":
                raise InboxError(409, "Question already answered or stale")
            pending = [
                m
                for m in self.messages.values()
                if m.kind == "question_answer" and m.blocks_queue and m.tab_id == q.tab_id
            ]
            if (
                len(pending) >= 100
                or sum(len(m.message.encode()) for m in pending) + len(payload.selected[0].encode())
                > self.max_bytes
            ):
                raise InboxError(429, "Question answer backlog limit reached")
            now, answer_id = utc_now(), str(uuid.uuid4())
            item = ChatMessageEnvelope(
                message_id=answer_id,
                call_id=payload.call_id,
                tab_id=q.tab_id,
                turn_id=(
                    q.turn_id
                    if q.mode == "native"
                    else (
                        payload.call_id
                        if not q.session_id.startswith("terminal-tab-")
                        else f"question-answer-{answer_id}"
                    )
                ),
                kind="question_answer",
                question_ref=q.question_ref,
                state="queued",
                created_at=now,
                updated_at=now,
                status_url=f"/api/workspaces/tabs/{q.tab_id}/messages/{answer_id}",
                principal=principal,
                actor=actor,
                principal_email=email,
                turn_metadata={"answer_source": source},
                incarnation=q.incarnation,
                fingerprint=fingerprint,
                message=json.dumps(
                    {
                        "type": "ask_question_response",
                        "answers": [{"questionId": q.question_id, "selected": payload.selected}],
                    },
                    ensure_ascii=False,
                ),
                sequence=max((m.sequence for m in self.messages.values()), default=0) + 1,
            )
            if callback:
                self.callback_receipts[callback[0]] = {
                    "fingerprint": callback[1],
                    "answer_id": answer_id,
                }
            q.state, q.answer_id = "claimed", answer_id
            self.messages[answer_id] = item
            self._save()
            self.wake()
            return item.receipt()

    async def pump_answer(self, answer_id: str) -> None:
        try:
            item = self.messages[answer_id]
            if item.state not in {"queued", "waiting"} or not self.answer_submit or self._closing:
                return
            self._change(answer_id, "dispatching")
            try:
                await self.answer_submit(
                    item.model_copy(deep=True), lambda: self._before_delivery(answer_id)
                )
            except InboxError as exc:
                if (
                    not item.submission_started
                    and exc.status == 409
                    and exc.detail == "question_busy"
                ):
                    self._change(answer_id, "waiting", "question_busy")
                    return
                self._change(
                    answer_id,
                    "uncertain" if item.submission_started else "failed",
                    "Question answer could not be confirmed",
                )
            except Exception:
                self._change(
                    answer_id,
                    "uncertain" if item.submission_started else "failed",
                    "Question answer could not be confirmed",
                )
            else:
                self._change(answer_id, "completed")
            q = self.questions.get(item.question_ref or "")
            if q is not None:
                q.state = (
                    "resolved"
                    if item.state == "completed"
                    else "uncertain" if item.state == "uncertain" else "stale"
                )
                self._save()
        finally:
            self._answer_tasks.pop(answer_id, None)

    async def start(self) -> None:
        await self.reconcile(restarting=True)
        self._closing = False
        self._runner = asyncio.create_task(self._run(), name="chat-message-inbox")

    def _pump_finished(self, tab_id: str, task: asyncio.Task[None]) -> None:
        if self._pump_tasks.get(tab_id) is task:
            del self._pump_tasks[tab_id]
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning("Chat inbox pump was cancelled for tab %s", tab_id)
            return
        except Exception:
            logger.exception("Chat inbox pump failed for tab %s", tab_id)
            return
        # A fast completion can wake the coordinator while this task still
        # owns its slot. Retry the next queued head after the slot is released.
        # Waiting heads retain periodic backoff rather than spinning on busy.
        pending = sorted(
            (m for m in self.messages.values() if m.tab_id == tab_id and m.blocks_queue),
            key=lambda m: m.sequence,
        )
        if pending and pending[0].state == "queued" and not self._closing:
            self.wake()

    async def _run(self) -> None:
        while not self._closing:
            self._wakeup.clear()
            try:
                await self.reconcile()
                for item in list(self.messages.values()):
                    if item.kind == "question_answer" and item.state in {"queued", "waiting"}:
                        if item.message_id not in self._answer_tasks:
                            self._answer_tasks[item.message_id] = asyncio.create_task(
                                self.pump_answer(item.message_id), name="chat-question-answer"
                            )
                if self.question_tick:
                    await self.question_tick()
                for tab in {m.tab_id for m in self.messages.values() if m.blocks_queue}:
                    if self._closing:
                        break
                    # Submission can wait on readiness, Goal admission, or a
                    # provider acknowledgement. It never owns the coordinator:
                    # new tabs, completion wakeups and recovery keep advancing.
                    if tab not in self._pump_tasks:
                        task = asyncio.create_task(self.pump(tab), name=f"chat-inbox-pump-{tab}")
                        self._pump_tasks[tab] = task
                        task.add_done_callback(partial(self._pump_finished, tab))
            except Exception:
                logger.exception("Chat inbox recovery/drain failed")
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self.redrain_seconds)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._closing = True
        for handle in self._retry_handles.values():
            handle.cancel()
        self._retry_handles.clear()
        self.wake()
        if self._runner:
            await self._runner  # do not cancel provider submission
            self._runner = None
        # Quiescing forbids new provider submissions. Join owned pumps without
        # cancelling possibly submitted native work or losing its evidence.
        await asyncio.gather(
            *list(self._pump_tasks.values()),
            *list(self._answer_tasks.values()),
            *list(self._question_tasks.values()),
            return_exceptions=True,
        )
