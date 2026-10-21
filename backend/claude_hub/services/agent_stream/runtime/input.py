"""Input responsibilities for structured streams."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ....models import AgentStreamEventType, AgentType
from ..base import NativeInputNotSubmittedError, NormalizeContext
from ..redaction import redact_event
from .session_state import SessionState
from .values import _PendingApproval

logger = logging.getLogger("claude_hub.services.agent_stream.tailer")


class InputMixin(SessionState):
    """Input behavior on the sole session owner."""

    async def answer_pending_question(
        self,
        text: str,
        expected_turn_id: str,
        *,
        expected_call_id: Optional[str] = None,
    ) -> bool:
        """Answer one exact native question without ever starting a turn.

        Legacy callers may omit ``expected_call_id``. Managed Chat supplies the
        card call id so a delayed click can never answer a newer provider
        request that happens to reuse the same question ids.
        """
        answers = self._deps.parse_ask_question_response(text)
        transport = self._native_transport
        if (
            answers is None
            or transport is None
            or self._active_turn_id != expected_turn_id
            or not transport.turn_in_flight
        ):
            return False
        if expected_call_id is not None:
            card = self._pending_approvals.get(expected_call_id)
            if (
                card is None
                or card.turn_id != expected_turn_id
                or not self._answer_covers_card(answers, card)
            ):
                return False
            consumed = await transport.answer_pending_question(
                answers,
                expected_call_id=expected_call_id,
            )
        else:
            consumed = await transport.answer_pending_question(answers)
        if consumed:
            await self._emit_approval_resolved(
                answers,
                expected_turn_id=expected_turn_id if expected_call_id is not None else None,
                expected_call_id=expected_call_id,
            )
        return consumed

    async def resolve_answered_questions(
        self,
        text: str,
        *,
        expected_turn_id: Optional[str] = None,
        expected_call_id: Optional[str] = None,
    ) -> None:
        """Close question cards whose answer travels inside another turn."""
        answers = self._deps.parse_ask_question_response(text)
        if answers is not None and self._pending_approvals:
            await self._emit_approval_resolved(
                answers,
                expected_turn_id=expected_turn_id,
                expected_call_id=expected_call_id,
            )

    def accepts_question_followup(
        self,
        text: str,
        expected_turn_id: str,
        *,
        expected_call_id: Optional[str] = None,
    ) -> bool:
        """Claude/Cursor answer questions with a new turn, not in-turn."""
        session = self._session_getter()
        answers = self._deps.parse_ask_question_response(text)
        if session is None or session.agent_type not in {AgentType.CLAUDE, AgentType.CURSOR}:
            return False
        if answers is None:
            return False
        if expected_call_id is not None:
            card = self._pending_approvals.get(expected_call_id)
            return bool(
                card is not None
                and card.turn_id == expected_turn_id
                and self._answer_covers_card(answers, card)
            )
        return any(
            card.turn_id == expected_turn_id and self._answer_covers_card(answers, card)
            for card in self._pending_approvals.values()
        )

    async def send_message(
        self,
        text: str,
        images: List[bytes],
        client_turn_id: str,
        *,
        previews: Optional[List[Optional[bytes]]] = None,
        delivery: str = "normal",
        reuse_attachments: Optional[List[Dict[str, Any]]] = None,
        visible_text: Optional[str] = None,
        turn_metadata: Optional[Dict[str, Any]] = None,
        before_send: Optional[Callable[[], Awaitable[None]]] = None,
        before_delivery: Optional[Callable[[], None]] = None,
        allow_question_answer: bool = True,
    ) -> None:
        """Atomically deliver a user turn (text + images) to the native transport.

        This is the composer's single input path for CHAT sessions. The same
        ``ProviderSession`` that produces the structured stream also consumes
        the turn, so input and output never diverge across two sessions.

        ``images`` are the original (full-resolution) bytes sent to the
        provider. ``previews`` are the display images the frontend uploaded
        (the original itself when it fits the cache cap); only the previews
        are persisted to the attachment cache.

        If ``previews`` is ``None`` the originals are still sent to the
        provider but **not** durably cached — the ``turn_started`` event
        carries non-renderable placeholder metadata so the turn is still
        visible but no image bytes are persisted.

        ``reuse_attachments`` (edit-resend only): when supplied, no new
        previews are persisted — the supplied metadata (with its existing
        attachment ids) is carried straight into the ``turn_started`` event so
        the new turn re-references the original turn's image attachments
        rather than dropping them.  The preview bytes are passed in as
        ``images`` (the durable cache holds only previews, not originals) and
        delivered to the provider.  Steps 3–4 are skipped for this path.

        Transaction ordering (all under ``self._send_lock``):

        1. busy check (``transport.turn_in_flight``)
        2. session existence check
        3. preview count validation (``len(previews) == len(images)``)
        4. persist bounded previews to the attachment cache
        5. publish ``turn_started`` (durable)
        6. deliver to provider

        If step 4 partially fails, any already-saved preview ids are deleted.
        If step 5 fails, all newly saved preview ids are deleted (the turn has
        no durable identity). Once step 5 succeeds, the previews are retained
        even if step 6 fails — the user turn exists and must remain renderable.

        Stable turn identity: the frontend supplies ``client_turn_id``. Before
        the provider runs, we append an authoritative ``turn_started`` event
        stamped with that id (and ``message_id={turn_id}:user``) so the user's
        message is immediately visible and can never be confused with a later
        identical message. ``self._active_turn_id`` is set so every provider
        event normalized during this turn inherits the same ``turn_id``.

        Fail-closed: no native transport raises ``RuntimeError``; the caller
        maps it to an HTTP error rather than falling back to tmux. On send
        failure we append an ``error`` and a ``turn_completed(status=failed)``
        for the same ``turn_id`` so the frontend never leaves the turn pending.
        """
        self._require_startable_incarnation()
        transport = self._native_transport
        if transport is None:
            raise RuntimeError("no native transport for this session; use the terminal send path")
        if self._native_error is not None:
            raise RuntimeError(self._native_error)
        if self._send_admission is not None and not self._send_admission():
            raise NativeInputNotSubmittedError(
                "service is shutting down; try again after it reconnects"
            )
        # A prior manual Stop may still be killing/relaunching the provider.
        # Wait for that bounded teardown BEFORE taking the send lock so the new
        # turn/start reaches the fresh app-server rather than the process being
        # killed. The composer-side lock was already released by the cancelled
        # edge, so this only bounds a resend sent within the teardown window.
        await self._await_turn_teardown()
        self._require_startable_incarnation()
        if not transport._started:
            await transport.start()
        self._require_startable_incarnation()

        # A structured answer to a provider-native blocking question (e.g.
        # Codex ``requestUserInput``) must be routed as the question's JSON-RPC
        # response — NOT as a new turn. The turn is blocked waiting for that
        # answer, so the busy-check below would either raise or steer-cancel
        # the very turn we are answering. Intercept before the send lock;
        # providers without a question channel return False and fall through.
        if allow_question_answer and not images:
            parsed_answers = self._deps.parse_ask_question_response(text)
            if parsed_answers is not None:
                consumed = await transport.answer_pending_question(parsed_answers)
                # The card is answered either way: Codex consumed the answer
                # as the blocking JSON-RPC response; Claude/Cursor have no
                # question channel and deliver it as a steer/follow-up below.
                # Persist an ``approval_resolved`` for each answered card so a
                # reload marks it resolved instead of rendering an open card
                # inside a completed turn (and re-submitting a stale card no
                # longer sends the raw JSON as a genuine new turn).
                if consumed or self._pending_approvals:
                    await self._emit_approval_resolved(parsed_answers)
                if consumed:
                    return

        async with self._send_lock:
            self._require_startable_incarnation()
            if self._send_admission is not None and not self._send_admission():
                raise NativeInputNotSubmittedError(
                    "service is shutting down; try again after it reconnects"
                )
            # 1. Busy check BEFORE any state mutation. If a turn is already in
            #    flight, fail fast so the in-flight turn's _active_turn_id and
            #    _run_epoch are never overwritten — unless the caller steers.
            if transport.turn_in_flight:
                if delivery == "steer":
                    # Steer immediately re-sends on the same transport, so its
                    # cancel must finish tearing down the old provider turn
                    # (kill one-shot / interrupt persistent) before we deliver.
                    await self._cancel_active_turn_locked(transport, await_teardown=True)
                else:
                    raise RuntimeError(
                        "a turn is already in flight; wait for it to complete before "
                        "sending another message"
                    )

            # 2. Session existence check.
            session = self._session_getter()
            if session is None:
                raise RuntimeError("session no longer exists")
            if before_send is not None:
                await before_send()

            # 3. Preview count validation. When the frontend supplies previews,
            #    there must be exactly one per image. When previews is None,
            #    originals are sent but NOT cached (placeholder metadata only).
            if previews is not None and len(previews) != len(images):
                raise ValueError(
                    f"preview count ({len(previews)}) must match image count ({len(images)})"
                )

            # 4. Persist the uploaded display images.
            attachment_metas: List[Dict[str, Any]] = []
            saved_ids: List[str] = []
            try:
                if reuse_attachments is not None:
                    # Edit-resend: re-reference the original turn's attachment
                    # ids without persisting new previews.  The preview bytes
                    # were passed in as ``images`` and are delivered to the
                    # provider below.  ``saved_ids`` stays empty so a later
                    # failure does not delete the (already-existing) previews.
                    attachment_metas = [dict(m) for m in reuse_attachments if isinstance(m, dict)]
                elif previews is not None:
                    for image, preview in zip(images, previews):
                        if preview is None:
                            attachment_metas.append(
                                {
                                    "id": None,
                                    "mime_type": self._deps._detect_image_mime(image)
                                    or "image/png",
                                    "bytes": len(image),
                                    "width": None,
                                    "height": None,
                                }
                            )
                            continue
                        mime = self._deps._detect_image_mime(preview) or "image/png"
                        meta = await self.attachment_store.save(mime, preview)
                        saved_ids.append(meta["id"])
                        attachment_metas.append(meta)
                else:
                    # No previews supplied: emit non-renderable placeholder
                    # metadata so the turn is visible but no original bytes
                    # are persisted. The frontend renders a "no preview"
                    # placeholder for entries without an id.
                    for img in images:
                        attachment_metas.append(
                            {
                                "id": None,
                                "mime_type": self._deps._detect_image_mime(img) or "image/png",
                                "bytes": len(img),
                                "width": None,
                                "height": None,
                            }
                        )
                if self._send_admission is not None and not self._send_admission():
                    raise NativeInputNotSubmittedError(
                        "service is shutting down; try again after it reconnects"
                    )
            except Exception:
                # Partial save failure: delete any previews already persisted
                # in this turn so we don't leak unreferenced cache entries.
                for att_id in saved_ids:
                    try:
                        await self.attachment_store._delete_by_id(att_id)
                    except Exception:
                        logger.exception("failed to clean up preview %s after partial save", att_id)
                raise

            # 5. Authoritative turn start: publish the user's message before
            #    the provider does anything. This guarantees the turn exists in
            #    the store and is fanned out to subscribers.
            self._active_turn_id = client_turn_id
            self._reset_turn_watchdog_state()
            self._goal_protocol_turn = bool(
                turn_metadata and turn_metadata.get("protocol") == "goal-continuation-v1"
            )
            # Goal history is not permission to rewrite ordinary assistant
            # output. Only this turn's trusted continuation metadata opts in.
            self._goal_protocol_sanitizer = (
                self._deps._GoalProtocolSanitizer() if self._goal_protocol_turn else None
            )
            self._run_epoch += 1
            self._turn_completed_seen = False
            ctx = NormalizeContext(
                session_id=self.session_id,
                tab_id=session.tab_id,
                agent_type=session.agent_type,
                run_epoch=self._run_epoch,
                turn_id=client_turn_id,
            )
            # The event carries only opaque attachment metadata (id, mime_type,
            # bytes, width, height) — never raw bytes or local paths.
            turn_started_payload: Dict[str, Any] = {
                "summary": text if visible_text is None else visible_text,
                "attachments": attachment_metas,
                "mode": session.chat_mode.value,
            }
            if turn_metadata:
                turn_started_payload["metadata"] = dict(turn_metadata)
            turn_started = ctx.event(
                AgentStreamEventType.TURN_STARTED,
                turn_started_payload,
            )
            turn_started = redact_event(turn_started)
            try:
                await self._publish(turn_started)
            except Exception:
                logger.exception(
                    "agent_stream store append failed for turn_started session %s",
                    self.session_id,
                )
                # If we cannot persist the authoritative turn_started, the turn
                # has no identity. Delete all previews saved in this turn so we
                # don't leak unreferenced cache entries, then fail.
                for att_id in saved_ids:
                    try:
                        await self.attachment_store._delete_by_id(att_id)
                    except Exception:
                        logger.exception(
                            "failed to clean up preview %s after publish failure", att_id
                        )
                self._active_turn_id = None
                raise
            self._native_terminal_status = None

            # 6. Deliver to the provider. Previews are retained from here on:
            #    the user turn is durably recorded and must remain renderable
            #    even if the provider rejects the input.  Once the durable
            #    turn_started exists, this tailer owns submission to the
            #    provider: a client disconnect must not cancel a partially
            #    written provider request, skip Dot adoption/ack, or turn an
            #    accepted/uncertain delivery into a local rejection.  Shield
            #    only provider submission in a child task; the parent task owns
            #    failure publication and observers so workspace mutation locks
            #    remain re-entrant for managed Chat callers.
            async def submit_to_provider() -> None:
                if self._send_admission is not None and not self._send_admission():
                    raise NativeInputNotSubmittedError(
                        "service is shutting down; try again after it reconnects"
                    )
                if before_delivery is not None:
                    before_delivery()
                await transport.send_message(text, images)

            submit_task = asyncio.create_task(
                submit_to_provider(),
                name=f"agent-stream-submit-{self.session_id[:8]}",
            )
            submit_exception: Optional[Exception] = None
            while True:
                try:
                    await asyncio.shield(submit_task)
                    break
                except asyncio.CancelledError:
                    if submit_task.done():
                        if submit_task.cancelled():
                            raise
                        break
                    current = asyncio.current_task()
                    if current is None or current.cancelling() == 0:
                        raise
                    current.uncancel()
                    continue
                except Exception as exc:
                    submit_exception = exc
                    break
            if submit_exception is None:
                try:
                    submit_task.result()
                except Exception as exc:
                    submit_exception = exc
            if submit_exception is not None:
                delivery_uncertain = getattr(transport, "_delivery_uncertain", False)
                outcome_uncertain = getattr(transport, "_submission_outcome_uncertain", False)
                if delivery_uncertain or outcome_uncertain:
                    # The provider exposed a main turn before the request
                    # response failed. Keep its durable Hub turn active and
                    # avoid a second synthetic terminal edge. A contradictory
                    # post-completion rejection is a one-shot caller outcome;
                    # consume it here so it cannot leak into the next send.
                    if outcome_uncertain:
                        transport._submission_outcome_uncertain = False
                    raise submit_exception
                # The provider rejected or failed to start the turn. Publish an
                # error and a failed completion for the same turn_id so the
                # frontend can mark it failed instead of leaving it pending.
                err_event = ctx.event(
                    AgentStreamEventType.ERROR,
                    {"message": "failed to deliver turn to provider"},
                )
                err_event = redact_event(err_event)
                try:
                    await self._publish_delivery_rejection_event(err_event)
                except Exception:
                    logger.exception(
                        "agent_stream store append failed for error session %s",
                        self.session_id,
                    )
                completed = ctx.event(
                    AgentStreamEventType.TURN_COMPLETED,
                    {"status": "failed", "delivery_rejected": True},
                )
                completed = redact_event(completed)
                try:
                    await self._publish_delivery_rejection_event(completed)
                except Exception:
                    logger.exception(
                        "agent_stream store append failed for turn_completed session %s",
                        self.session_id,
                    )
                self._terminalize_native_runtime("failed")
                self._active_turn_id = None
                raise submit_exception
            # Provider submission reached its known accepted boundary. A
            # provider-owned uncertain response raises above while retaining
            # this durable turn for completion or explicit Stop.

    @staticmethod
    def _answer_covers_card(
        answers: List[Dict[str, Any]],
        card: _PendingApproval,
    ) -> bool:
        answered_ids = {
            answer["questionId"]
            for answer in answers
            if isinstance(answer, dict)
            and isinstance(answer.get("questionId"), str)
            and isinstance(answer.get("selected"), list)
        }
        return bool(card.question_ids) and card.question_ids <= answered_ids
