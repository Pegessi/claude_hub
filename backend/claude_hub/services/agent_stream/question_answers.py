"""Exact question admission shared by browser, CLI and authenticated Feishu.

The inbox owns claims and submission fences. Providers own live requests. This
module never turns a stale answer into an ordinary prompt or starts a runtime.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Callable

from fastapi import HTTPException

from ...models import AgentStreamEvent, AgentStreamEventType, ManagedSession, User
from ...models.chat_message import (
    ChatMessageEnvelope,
    ChatMessageReceipt,
    ChatQuestion,
    QuestionAnswerCreate,
    utc_now,
)
from ..chat_message_inbox import ChatMessageInbox, InboxError
from .providers.questions import parse_ask_question_response


class QuestionAnswers:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._mirror_cursor = 0

    def lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    @staticmethod
    def context(session_id: str) -> tuple[Any, Any, Any]:
        from ...api import agent_stream as stream

        session: Any
        if session_id.startswith("terminal-tab-"):
            session = stream._terminal_tab_session_or_404(session_id.removeprefix("terminal-tab-"))
            manager = stream._get_tab_tailer_manager()
        else:
            session = stream.workspace_manager.sessions.get(session_id)
            manager = stream._get_tailer_manager()
        if session is None:
            raise InboxError(409, "Question session no longer exists")
        tailer = manager.get_tailer(session.id)
        if tailer is None or not tailer.matches_session(session):
            raise InboxError(409, "Question runtime no longer exists")
        return session, manager, tailer

    @staticmethod
    def inbox() -> ChatMessageInbox:
        from ...api.routes.agent_stream.messages import get_inbox

        inbox = get_inbox()
        inbox.answer_submit = answers.submit
        inbox.question_tick = answers.tick
        return inbox

    async def capture(self, session_id: str, turn_id: str, call_id: str) -> ChatQuestion:
        session, _, tailer = self.context(session_id)
        snapshot = tailer.question_snapshot(turn_id, call_id)
        if snapshot is None:
            raise InboxError(409, "Question no longer pending")
        raw = snapshot["questions"]
        if (
            snapshot["kind"] != "ask_question"
            or call_id.startswith("permission:")
            or len(raw) != 1
            or not isinstance(raw[0], dict)
            or raw[0].get("is_secret")
            or raw[0].get("allow_multiple")
        ):
            raise InboxError(409, "Question requires Web Chat")
        question = raw[0]
        if not isinstance(question.get("id"), str) or not isinstance(question.get("prompt"), str):
            raise InboxError(409, "Question requires Web Chat")
        inbox = self.inbox()
        async with inbox._lock:
            if inbox._closing:
                raise InboxError(503, "Question inbox unavailable")
            for q in inbox.questions.values():
                if (q.session_id, q.incarnation, q.turn_id, q.call_id) == (
                    session_id,
                    snapshot["incarnation"],
                    turn_id,
                    call_id,
                ):
                    return q
            if (
                sum(
                    q.tab_id == session.tab_id and q.state in {"open", "claimed", "uncertain"}
                    for q in inbox.questions.values()
                )
                >= 100
            ):
                raise InboxError(429, "Open question limit reached")
            now = utc_now()
            q = ChatQuestion(
                question_ref=secrets.token_hex(16),
                tab_id=session.tab_id,
                session_id=session_id,
                incarnation=snapshot["incarnation"],
                owner_created_at=str(getattr(session, "created_at", "")),
                owner_workspace_id=str(getattr(session, "workspace_id", "")),
                turn_id=turn_id,
                call_id=call_id,
                run_epoch=snapshot["run_epoch"],
                question_id=question["id"],
                prompt=question["prompt"],
                options=[
                    o["id"]
                    for o in question.get("options", [])
                    if isinstance(o, dict) and isinstance(o.get("id"), str)
                ],
                mode=snapshot["mode"],
                created_at=now,
                expires_at=now + timedelta(hours=24),
            )
            inbox.questions[q.question_ref] = q
            inbox._save()
            inbox.wake()
            return q

    def live(self, q: ChatQuestion) -> tuple[Any, Any, Any]:
        session, manager, tailer = self.context(q.session_id)
        snapshot = tailer.question_snapshot(q.turn_id, q.call_id)
        if snapshot is None or (snapshot["incarnation"], snapshot["run_epoch"]) != (
            q.incarnation,
            q.run_epoch,
        ):
            raise InboxError(409, "Question no longer pending")
        return session, manager, tailer

    async def admit(
        self,
        q: ChatQuestion,
        user: User,
        payload: QuestionAnswerCreate,
        *,
        callback: tuple[str, str] | None = None,
        source: str = "web",
        mirror_send_uuid: str | None = None,
        action_digest: str | None = None,
    ) -> ChatMessageReceipt:
        from ...api import agent_stream as stream
        from ..goal_run import get_goal_admission_lock

        async with get_goal_admission_lock(q.tab_id):
            async with AsyncExitStack() as owner:
                if not q.session_id.startswith("terminal-tab-"):
                    session, _, _ = self.context(q.session_id)
                    await owner.enter_async_context(
                        stream._agent_stream_bindings.managed_stream_interaction(
                            session, require_chat=True
                        )
                    )
                async with self.lock(q.session_id):
                    inbox = self.inbox()
                    # Recheck trusted external binding at the winner's admission boundary.
                    if source == "feishu":
                        from ...api.routes.feishu_bot.card_actions import current_binding

                        current_binding(q)
                        if (
                            q.mirror is None
                            or q.mirror.send_uuid != mirror_send_uuid
                            or (
                                action_digest is not None
                                and action_digest not in q.mirror.action_digests
                            )
                        ):
                            raise InboxError(409, "Question mirror was replaced")
                        if q.expires_at <= utc_now():
                            raise InboxError(409, "Question mirror expired")
                    # Retry lookup must precede live checks: a resolved request is gone.
                    if q.state == "open":
                        session, _, _ = self.live(q)
                        if not q.session_id.startswith("terminal-tab-") and q.answer_id is None:
                            owned = (
                                set(getattr(session, "call_payload_fingerprints", {}))
                                | set(getattr(session, "pending_call_ids", []))
                                | set(getattr(session, "processing_call_ids", []))
                                | set(getattr(session, "delivered_call_ids", []))
                                | set(getattr(session, "uncertain_call_ids", []))
                            )
                            if payload.call_id in owned:
                                raise InboxError(409, "Answer call_id already has a mailbox owner")
                    return await inbox.claim_answer(
                        q.question_ref,
                        user.open_id,
                        user.name,
                        payload,
                        callback=callback,
                        source=source,
                        email=user.email,
                    )

    async def runtime_web(self, session_id: str, payload: Any) -> dict[str, Any]:
        """Web-only exact path when persistence is unavailable or shape unsupported.

        This preserves multi/secret web questions without exporting them, and
        keeps the browser functional when the optional durable inbox is corrupt.
        """
        from ...api import agent_stream as stream
        from ...api.routes.agent_stream.dispatch import dispatch_tab_stream_input
        from ..goal_run import get_goal_admission_lock

        session, manager, _ = self.context(session_id)
        async with get_goal_admission_lock(session.tab_id):
            async with AsyncExitStack() as ownership:
                if not session_id.startswith("terminal-tab-"):
                    session = await ownership.enter_async_context(
                        stream._agent_stream_bindings.managed_stream_interaction(
                            session, require_chat=True
                        )
                    )
                async with self.lock(session_id):
                    _, _, tailer = self.context(session_id)
                    snap = tailer.question_snapshot(
                        payload.question_turn_id, payload.question_call_id
                    )
                    parsed = parse_ask_question_response(payload.text)
                    if snap is None or not parsed:
                        raise HTTPException(409, "Question no longer pending")
                    expected = {q["id"] for q in snap["questions"]}
                    if (
                        len(parsed) != len(expected)
                        or {a.get("questionId") for a in parsed} != expected
                        or any(
                            not isinstance(a.get("selected"), list)
                            or not a["selected"]
                            or any(not isinstance(v, str) or not v.strip() for v in a["selected"])
                            for a in parsed
                        )
                    ):
                        raise HTTPException(400, "Incomplete question answer")
                    mapped = {a["questionId"]: a["selected"] for a in parsed}
                    if sum(
                        len(value.encode()) for a in parsed for value in a["selected"]
                    ) > 65536 or any(
                        not q.get("allow_multiple") and len(mapped[q["id"]]) != 1
                        for q in snap["questions"]
                    ):
                        raise HTTPException(400, "Invalid question answer bounds")
                    if snap["mode"] == "native":
                        if not await manager.answer_pending_question(
                            session,
                            payload.text,
                            payload.question_turn_id,
                            expected_call_id=payload.question_call_id,
                        ):
                            raise HTTPException(409, "Question no longer pending")
                    elif session_id.startswith("terminal-tab-"):
                        await dispatch_tab_stream_input(
                            session.tab_id,
                            payload,
                            stream._agent_stream_bindings,
                            goal_locked=True,
                        )
                        await manager.resolve_answered_questions(
                            session,
                            payload.text,
                            expected_turn_id=payload.question_turn_id,
                            expected_call_id=payload.question_call_id,
                        )
                    else:
                        if await manager.turn_in_flight(session):
                            raise HTTPException(409, "Wait for current provider turn")
                        await stream.workspace_manager.send_chat_session_message_locked(
                            session.id,
                            payload.text,
                            call_id=payload.client_turn_id,
                            question_key=json.dumps(
                                [payload.question_turn_id, payload.question_call_id],
                                separators=(",", ":"),
                            ),
                        )
                        await manager.resolve_answered_questions(
                            session,
                            payload.text,
                            expected_turn_id=payload.question_turn_id,
                            expected_call_id=payload.question_call_id,
                        )
        return {"ok": True, "answered_question": True}

    def guard_input(self, session_id: str, turn_id: str) -> None:
        """Ordinary input cannot supersede a claimed or ambiguous answer."""
        try:
            inbox = self.inbox()
        except Exception:
            return
        for q in inbox.questions.values():
            if q.session_id != session_id or q.state not in {"claimed", "uncertain"}:
                continue
            item = inbox.messages.get(q.answer_id or "")
            if item and item.turn_id != turn_id:
                raise HTTPException(409, "A question answer owns this Chat")

    async def web(self, session_id: str, payload: Any, user: User) -> dict[str, Any]:
        parsed = parse_ask_question_response(payload.text)
        if payload.attachments or parsed is None:
            raise HTTPException(400, "Invalid question answer")
        try:
            inbox = self.inbox()
        except Exception:
            return await self.runtime_web(session_id, payload)
        if len(parsed) != 1:
            return await self.runtime_web(session_id, payload)
        try:
            q = next(
                (
                    q
                    for q in inbox.questions.values()
                    if (q.session_id, q.turn_id, q.call_id)
                    == (session_id, payload.question_turn_id, payload.question_call_id)
                    and q.answer_id is not None
                    and inbox.messages[q.answer_id].call_id == payload.client_turn_id
                ),
                None,
            )
            if q is None:
                q = await self.capture(
                    session_id, payload.question_turn_id, payload.question_call_id
                )
            if parsed[0].get("questionId") != q.question_id:
                raise InboxError(409, "Question identity mismatch")
            receipt = await self.admit(
                q,
                user,
                QuestionAnswerCreate.model_validate(
                    {
                        "call_id": payload.client_turn_id,
                        "selected": parsed[0].get("selected"),
                    }
                ),
            )
            return {
                "ok": True,
                "answered_question": True,
                "receipt": receipt.model_dump(mode="json"),
            }
        except ValueError as exc:
            raise HTTPException(400, "Invalid question answer") from exc
        except InboxError as exc:
            if exc.detail == "Question requires Web Chat":
                return await self.runtime_web(session_id, payload)
            raise HTTPException(
                exc.status,
                exc.detail,
                headers={"X-Claude-Hub-Error-Reason": "stale_question"},
            ) from exc

    async def submit(self, item: ChatMessageEnvelope, before: Callable[[], None]) -> None:
        from ...api import agent_stream as stream
        from ...api.routes.agent_stream.dispatch import AgentStreamSendRequest
        from ..goal_run import get_goal_admission_lock

        inbox = self.inbox()
        q = inbox.questions[item.question_ref or ""]
        async with get_goal_admission_lock(q.tab_id):
            async with self.lock(q.session_id):
                async with AsyncExitStack() as admission:
                    if item.turn_metadata.get("answer_source") == "feishu":
                        from ...api import feishu_bot as bot
                        from ...api.routes.feishu_bot.card_actions import current_binding

                        if q.mirror is None:
                            raise InboxError(409, "Question mirror unavailable")
                        await admission.enter_async_context(bot._gate(q.mirror.bot_id))
                        current_binding(q)
                    session, manager, _ = self.live(q)
                    if q.mirror is not None and item.turn_metadata.get("answer_source") == "feishu":
                        from ...api.routes.feishu_bot.card_actions import current_binding

                        current_binding(q)
                    if q.mode == "followup" and await manager.turn_in_flight(session):
                        raise InboxError(409, "question_busy")
                    from ...api.routes.agent_stream.messages import _require_actor

                    _require_actor(item)
                    self.live(q)  # ownership may have changed during the busy probe
                    if q.session_id.startswith("terminal-tab-"):
                        from ...api.routes.agent_stream.dispatch import (
                            require_tab_input_available,
                        )

                        require_tab_input_available(q.tab_id)
                        if q.mode == "native":
                            before()
                            if not await manager.answer_pending_question(
                                session, item.message, q.turn_id, expected_call_id=q.call_id
                            ):
                                raise InboxError(409, "Question no longer pending")
                        else:
                            # Existing Goal path owns resume/steer semantics; otherwise
                            # use the same native sender with exact-card validation above.
                            before()
                            from ...api.routes.agent_stream.dispatch import (
                                dispatch_tab_stream_input,
                            )

                            await dispatch_tab_stream_input(
                                q.tab_id,
                                AgentStreamSendRequest(
                                    text=item.message,
                                    client_turn_id=item.turn_id,
                                    question_turn_id=q.turn_id,
                                    question_call_id=q.call_id,
                                ),
                                stream._agent_stream_bindings,
                                turn_metadata={"origin": "question_answer"},
                                goal_locked=True,
                            )
                            await manager.resolve_answered_questions(
                                session,
                                item.message,
                                expected_turn_id=q.turn_id,
                                expected_call_id=q.call_id,
                            )
                    else:
                        # Managed interaction retains Workspace's lock/incarnation and
                        # deterministic call fingerprint through the mailbox handoff.
                        async with stream._agent_stream_bindings.managed_stream_interaction(
                            session, require_chat=True
                        ) as live:
                            self.live(q)
                            before()
                            if q.mode == "native":
                                if not await manager.answer_pending_question(
                                    live,
                                    item.message,
                                    q.turn_id,
                                    expected_call_id=q.call_id,
                                ):
                                    raise InboxError(409, "Question no longer pending")
                            else:
                                await stream.workspace_manager.send_chat_session_message_locked(
                                    live.id,
                                    item.message,
                                    call_id=item.call_id,
                                    question_key=json.dumps(
                                        [q.turn_id, q.call_id], separators=(",", ":")
                                    ),
                                )
                                await manager.resolve_answered_questions(
                                    live,
                                    item.message,
                                    expected_turn_id=q.turn_id,
                                    expected_call_id=q.call_id,
                                )

    async def observe(self, event: AgentStreamEvent) -> None:
        thread = event.payload.get("thread_id")
        if thread and thread != event.payload.get("_hub_main_thread_id"):
            return
        if event.type == AgentStreamEventType.APPROVAL_REQUIRED and event.turn_id and event.call_id:
            await self.capture(event.session_id, event.turn_id, event.call_id)
        elif event.type == AgentStreamEventType.TURN_STARTED:
            inbox = self.inbox()
            for q in inbox.questions.values():
                if (
                    q.session_id == event.session_id
                    and q.turn_id != event.turn_id
                    and q.state == "open"
                ):
                    q.state = "stale"
            inbox._save()
        elif event.type in {
            AgentStreamEventType.APPROVAL_RESOLVED,
            AgentStreamEventType.TURN_COMPLETED,
        }:
            inbox = self.inbox()
            for q in inbox.questions.values():
                item = inbox.messages.get(q.answer_id or "")
                reply_turn = item.turn_id if item and q.mode == "followup" else q.turn_id
                if (
                    event.type == AgentStreamEventType.TURN_COMPLETED
                    and item
                    and q.session_id == event.session_id
                    and reply_turn == event.turn_id
                    and q.reply_state is None
                    and event.payload.get("status") in {"completed", "success", "ok"}
                ):
                    text = event.payload.get("final_text") or event.payload.get("assistant_text")
                    if isinstance(text, str) and text.strip():
                        q.reply_text, q.reply_state = text, "pending"
                if (q.session_id, q.turn_id) != (event.session_id, event.turn_id):
                    continue
                if (
                    event.type == AgentStreamEventType.APPROVAL_RESOLVED
                    and event.call_id == q.call_id
                ):
                    if q.state != "claimed":
                        q.state = "resolved"
                        item = inbox.messages.get(q.answer_id or "")
                        if item and item.state == "uncertain":
                            inbox._change(item.message_id, "completed")
                elif event.type == AgentStreamEventType.TURN_COMPLETED:
                    if q.mode == "native" or event.payload.get("status") not in {
                        "completed",
                        "success",
                        "ok",
                    }:
                        if q.state == "open":
                            q.state = "stale"
            inbox._save()
            inbox.wake()

    @staticmethod
    def managed_handoff_persisted(q: ChatQuestion, item: ChatMessageEnvelope) -> bool:
        """An in-memory mailbox claim after a failed save is not a handoff receipt."""
        from ...api import agent_stream as stream

        manager = stream.workspace_manager
        try:
            state = json.loads(manager._workspace_state_file(q.owner_workspace_id).read_text())
            row = next(row for row in state["sessions"] if row["id"] == q.session_id)
            owner = ManagedSession.model_validate(row)
            key = json.dumps([q.turn_id, q.call_id], separators=(",", ":"))
            return (
                str(owner.created_at) == q.owner_created_at
                and owner.workspace_id == q.owner_workspace_id
                and owner.tab_id == q.tab_id
                and owner.chat_question_answer_calls.get(key) == item.call_id
                and owner.call_payload_fingerprints.get(item.call_id)
                == manager._compute_payload_fingerprint(item.message, [])
                and item.call_id
                in (
                    owner.pending_call_ids
                    + owner.processing_call_ids
                    + owner.delivered_call_ids
                    + owner.uncertain_call_ids
                )
            )
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return False

    async def tick(self) -> None:
        inbox = self.inbox()
        for q in list(inbox.questions.values()):
            if (
                q.state == "uncertain"
                and not q.session_id.startswith("terminal-tab-")
                and q.mode == "followup"
            ):
                from ...api import agent_stream as stream

                session = stream.workspace_manager.sessions.get(q.session_id)
                item = inbox.messages.get(q.answer_id or "")
                key = json.dumps([q.turn_id, q.call_id], separators=(",", ":"))
                if (
                    session
                    and item
                    and q.owner_created_at == str(session.created_at)
                    and q.owner_workspace_id == session.workspace_id
                    and session.tab_id == q.tab_id
                    and session.chat_question_answer_calls.get(key) == item.call_id
                    and item.call_id in session.call_payload_fingerprints
                    and self.managed_handoff_persisted(q, item)
                ):
                    q.state = "resolved"
                    inbox._change(item.message_id, "completed")
            if q.state == "open":
                try:
                    self.live(q)
                except (InboxError, HTTPException):
                    q.state = "stale"
                    inbox._save()

        candidates = []
        for q in inbox.questions.values():
            mirror = q.mirror
            remote_state = "expired" if q.state == "open" and q.expires_at <= utc_now() else q.state
            if mirror is None:
                eligible = q.state == "open" and not q.mirror_checked
            else:
                eligible = mirror.state in {"queued", "sending"} or (
                    mirror.state == "sent"
                    and (
                        mirror.displayed_state != remote_state
                        or q.reply_state in {"pending", "sending"}
                    )
                )
                if mirror.retry_at and mirror.retry_at > utc_now():
                    eligible = False
            if eligible:
                candidates.append(q)
        if candidates:
            offset = self._mirror_cursor % len(candidates)
            ordered = candidates[offset:] + candidates[:offset]
            self._mirror_cursor += min(8, len(candidates))
        else:
            ordered = []
        for q in ordered:
            from ...api.routes.feishu_bot.card_actions import mirror_question

            if q.question_ref not in inbox._question_tasks and len(inbox._question_tasks) < 8:

                async def run_mirror(one: ChatQuestion) -> None:
                    try:
                        await mirror_question(one)
                    except Exception:
                        pass  # durable outbox retains state; no raw payload in logs
                    finally:
                        inbox._question_tasks.pop(one.question_ref, None)

                inbox._question_tasks[q.question_ref] = asyncio.create_task(
                    run_mirror(q), name="feishu-question-mirror"
                )


answers = QuestionAnswers()
