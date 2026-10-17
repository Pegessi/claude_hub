"""Publication responsibilities for structured streams."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from ....models import AgentStreamEvent, AgentStreamEventType, AgentType
from ..base import NormalizeContext
from ..native import ProviderSession
from ..redaction import redact_event
from .session_state import SessionState
from .values import PostPersistObserver, _PendingApproval

logger = logging.getLogger("claude_hub.services.agent_stream.tailer")


class PublicationMixin(SessionState):
    """Publication behavior on the sole session owner."""

    async def _consume_turn_completion_record(
        self,
        event: AgentStreamEvent,
        observer_event: AgentStreamEvent,
        transport: ProviderSession,
    ) -> bool:
        """Persist the provider's terminal ``turn_completed`` and release the
        guard, atomically against a concurrent manual Stop.

        The provider's own terminal record can race a user Stop that has
        already persisted a ``cancelled`` edge for the same Hub turn. Both run
        under the tailer's ``_send_lock`` and re-check turn identity:

        * a completion for the currently active Hub turn → persist + release;
        * a null-id completion with no active turn → provider/process terminal
          edge (cold-start replay, one-shot result); persist + release;
        * anything else (Stop already terminalized, or a newer turn active) →
          dropped, so a turn never gets two terminal edges and a late
          completion can never clear a newer turn's guard.

        Returns ``False`` when the record was dropped.
        """
        completion_turn_id = event.turn_id
        async with self._send_lock:
            active = self._active_turn_id
            if completion_turn_id is not None and active != completion_turn_id:
                logger.warning(
                    "dropping stale turn_completed for turn %s while active=%s session %s",
                    completion_turn_id,
                    active,
                    self.session_id,
                )
                # Stop already retired this turn; cancel_active_turn's teardown
                # owns provider-guard release. Only release an orphaned provider
                # guard when no newer Hub turn is running on the transport.
                if active is None and transport.turn_in_flight:
                    transport.acknowledge_turn_complete()
                return False
            persisted = False
            try:
                await self._publish(event)
                persisted = True
            except Exception:
                logger.exception(
                    "agent_stream store append failed for turn_completed session %s",
                    self.session_id,
                )
                # Persistence failed, but the provider turn is over: release
                # anyway rather than wedging the composer.
            # Pending cards are deliberately NOT dropped on completion: a card
            # routinely outlives its turn (Claude/Cursor have no blocking
            # question channel, so the user answers the card after the agent
            # ends the turn). Each card carries its own turn id/epoch, so a late
            # answer is stamped with the owning turn, not the current one.
            self._turn_completed_seen = True
            self._terminalize_native_runtime(event.payload.get("status"))
            # ``TURN_COMPLETED`` is every shipped adapter's final record for the
            # turn (Claude ``result``, Cursor ``turn_ended``, Codex/TraeX
            # ``turn/completed``), so releasing immediately is safe for both
            # persistent app-servers and one-shot CLIs whose subprocess lingers
            # (e.g. a long-running tool child). Done under the send lock so a
            # concurrent send cannot start a turn before the release.
            self._active_turn_id = None
            self._active_tool_call_ids.clear()
            self._blocking_approval_call_ids.clear()
            self._waiting_for_model_capacity = False
            self._goal_terminal_at = None
            transport.acknowledge_turn_complete()
            if persisted:
                self._notify_post_persist(observer_event)
            self._assistant_text = ""
            self._visible_assistant_text = ""
            self._goal_protocol_sanitizer = None
            self._goal_protocol_turn = False
        return True

    def _notify_post_persist(self, event: AgentStreamEvent) -> None:
        """Schedule observers after completion persistence and guard release."""
        for observer in tuple(self._post_persist_observers):
            task = asyncio.create_task(self._run_observer(observer, event))
            task.add_done_callback(self._log_observer_failure)

    @staticmethod
    async def _run_observer(observer: PostPersistObserver, event: AgentStreamEvent) -> None:
        await observer(event)

    @staticmethod
    def _log_observer_failure(task: "asyncio.Task[None]") -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("agent_stream post-persist observer failed")

    def _record_approval_card(self, event: AgentStreamEvent) -> None:
        """Track an ``approval_required`` card (or drop a resolved one) so its
        durable ``approval_resolved`` can be stamped with the card's own turn.

        Called from ``_run_native`` as normalized events flow through, before
        they are persisted. Only native sessions reach this path; transcript
        sessions have no answer-intercept flow.
        """
        if event.type == AgentStreamEventType.APPROVAL_REQUIRED and event.call_id:
            self._pending_approvals[event.call_id] = _PendingApproval(
                call_id=event.call_id,
                turn_id=event.turn_id,
                run_epoch=event.run_epoch,
                question_ids=frozenset(
                    question["id"]
                    for question in event.payload.get("questions", [])
                    if isinstance(question, dict) and isinstance(question.get("id"), str)
                ),
            )
            self._blocking_approval_call_ids.add(event.call_id)
        elif event.type == AgentStreamEventType.APPROVAL_RESOLVED and event.call_id:
            self._pending_approvals.pop(event.call_id, None)
            self._blocking_approval_call_ids.discard(event.call_id)
            # Codex/TraeX blocking questions are announced as a synthetic
            # TOOL_CALL_STARTED that never gets a matching completed event.
            # Drop it on resolve so the answer does not leave the watchdog
            # suppressed for the rest of the turn.
            self._active_tool_call_ids.discard(event.call_id)

    async def _emit_approval_resolved(
        self,
        answers: Optional[List[Dict[str, Any]]] = None,
        *,
        expected_turn_id: Optional[str] = None,
        expected_call_id: Optional[str] = None,
    ) -> None:
        """Persist + fan out ``approval_resolved`` for the answered cards.

        Snapshot + clear before any await so a concurrent answer cannot emit a
        duplicate resolved event for the same card (mirrors the snapshot+clear
        guard in ``CodexNativeSession.answer_pending_question``). Each event is
        stamped with the captured card identity (``call_id``/``tool_call_id``)
        and the card's own ``turn_id``/``run_epoch`` — not the active turn at
        emit time — so the frontend routes it to the turn that owns the card
        even when the answer was delivered as a steer that cancelled that turn.
        """
        if not self._pending_approvals:
            return
        if expected_call_id is not None:
            card = self._pending_approvals.get(expected_call_id)
            pending = (
                {expected_call_id: card}
                if card is not None
                and (expected_turn_id is None or card.turn_id == expected_turn_id)
                and answers is not None
                and self._answer_covers_card(answers, card)
                else {}
            )
        else:
            answered_ids = {
                answer["questionId"]
                for answer in answers or []
                if isinstance(answer, dict) and isinstance(answer.get("questionId"), str)
            }
            pending = {
                key: card
                for key, card in self._pending_approvals.items()
                if (expected_turn_id is None or card.turn_id == expected_turn_id)
                and (not answers or not card.question_ids or card.question_ids <= answered_ids)
            }
        for key in pending:
            self._pending_approvals.pop(key)
            self._blocking_approval_call_ids.discard(key)
            self._active_tool_call_ids.discard(key)
        session = self._session_getter()
        if session is None:
            return
        for card in pending.values():
            ctx = NormalizeContext(
                session_id=self.session_id,
                tab_id=session.tab_id,
                agent_type=session.agent_type,
                run_epoch=card.run_epoch if card.run_epoch is not None else self._run_epoch,
                turn_id=card.turn_id,
            )
            event = ctx.event(
                AgentStreamEventType.APPROVAL_RESOLVED,
                {"tool_call_id": card.call_id},
                call_id=card.call_id,
            )
            event = redact_event(event)
            try:
                await self._publish(event)
            except Exception:
                logger.exception(
                    "agent_stream store append failed for approval_resolved session %s",
                    self.session_id,
                )

    async def _persist_and_fanout(self, event: AgentStreamEvent) -> None:
        """Persist a (possibly coalesced) event and fan it out to subscribers.

        This is the coalescer's ``on_flush`` callback. It is the single
        persistence+fanout path for text deltas. Sequence numbers are
        assigned here by ``store.append``, so each coalesced event gets
        exactly one sequence number — no holes, no duplicates.

        During backfill (``_is_live`` is False for transcript/snapshot
        sessions) events are persisted but NOT fanned out: live subscribers
        replay history from the store, so fanning out backfill events would
        cause duplicate delivery. Native sessions are always live.

        Raises if ``store.append`` fails so callers (e.g. ``turn_started``)
        can react. The coalescer's drain loop catches and logs exceptions
        from this callback so a single failed persist does not abort the
        whole flush.
        """
        self._require_publishable_incarnation()
        event = await self._store.append(event)
        image_paths = self._image_references.paths(event) if self._is_live else []
        if image_paths:
            # Store append can yield while deletion or friendly-id replacement
            # retires this owner. Never capture files for a stale incarnation.
            self._require_publishable_incarnation()
            session = self._session_getter()
            if session is not None:
                try:
                    await self._deps.cache_image_paths(
                        session,
                        image_paths,
                        owner_is_current=self._publishable_incarnation_is_current,
                    )
                except Exception:
                    # Image retention cannot turn a successful agent event
                    # into a failed turn or suppress delivery to the Chat.
                    logger.warning("Could not retain agent image for session %s", self.session_id)
            # Capture is awaited and may yield to deletion. Revalidate before
            # any fanout or task observation derived from the old owner.
            self._require_publishable_incarnation()
        if self._is_live:
            self._fanout(event)
            if event.type in {
                AgentStreamEventType.APPROVAL_REQUIRED,
                AgentStreamEventType.ERROR,
                AgentStreamEventType.TURN_COMPLETED,
            }:
                self._fanout_completion(event)
        if (
            self._is_live
            and self._task_activity_observer is not None
            and event.type
            in {
                AgentStreamEventType.TURN_STARTED,
                AgentStreamEventType.TURN_COMPLETED,
                AgentStreamEventType.TOOL_CALL_STARTED,
                AgentStreamEventType.TOOL_CALL_COMPLETED,
                AgentStreamEventType.APPROVAL_REQUIRED,
                AgentStreamEventType.APPROVAL_RESOLVED,
                AgentStreamEventType.ERROR,
            }
        ):
            main_thread = (
                self._native_transport.active_thread_id
                if self._native_transport is not None
                else None
            )
            observation = event.model_copy(
                update={"payload": {**event.payload, "_hub_main_thread_id": main_thread}}
            )
            try:
                await self._task_activity_observer(observation)
            except Exception as exc:
                logger.warning("Task activity observer failed: %s", type(exc).__name__)

    async def _publish(self, event: AgentStreamEvent) -> None:
        """Publish an event, coalescing text deltas along the way.

        Coalescable events (``text_delta``, ``thinking_delta``) are buffered
        by the coalescer and flushed on the leading edge or trailing timer.
        Non-coalescable events force a flush of any pending text first, so
        text never appears after a terminal marker.

        Raises if persistence of a non-coalescable event fails.
        """
        if event.type == AgentStreamEventType.TURN_COMPLETED and event.agent_type == AgentType.MASO:
            # Retire service confirmations with their owning turn, durably, so
            # reload cannot present an actionable card after Stop or failure.
            await self._emit_approval_resolved([])
        if await self._coalescer.handle(event):
            return  # buffered; the coalescer will flush it
        # Non-coalescable: drain pending text before emitting so ordering is
        # preserved (no text after a terminal event).
        try:
            await self._coalescer.flush_async()
            await self._persist_and_fanout(event)
        except Exception:
            if event.type == AgentStreamEventType.TURN_COMPLETED and self._is_live:
                self._fanout_volatile_terminal_failure(event)
            raise

    def _fanout_volatile_terminal_failure(self, event: AgentStreamEvent) -> None:
        """Wake live completion owners when a terminal write fails.

        A provider turn is already over at this point, so waiting for another
        record can never converge.  The dedicated completion subscription
        keeps this process-local failure out of durable SSE/long-poll
        timelines, while direct owners such as the Feishu bridge can fail and
        release their FIFO.

        Terminal delivery is forced even when a completion-owner queue is
        full; non-terminal errors may be dropped because the authoritative
        terminal event remains queued behind them.
        """
        failure = event.model_copy(
            deep=True,
            update={
                "stream_sequence": -1,
                "payload": {
                    **event.payload,
                    "status": "failed",
                    "error_message": self._deps._TERMINAL_PERSISTENCE_FAILURE_MESSAGE,
                    "_hub_volatile_terminal_failure": True,
                },
            },
        )
        self._fanout_completion(failure)

    def _fanout_completion(self, event: AgentStreamEvent) -> None:
        for queue in list(self._completion_subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                if event.type not in {
                    AgentStreamEventType.APPROVAL_REQUIRED,
                    AgentStreamEventType.TURN_COMPLETED,
                }:
                    continue
                # Queue operations are synchronous on this event loop: after
                # removing one stale item the blocking approval or terminal
                # signal is guaranteed to fit in the bounded queue.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(event)

    def _fanout(self, event: AgentStreamEvent) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                if not self._drop_warned:
                    logger.warning(
                        "agent_stream tailer: subscriber queue full for session %s; "
                        "dropping events (slow client)",
                        self.session_id,
                    )
                    self._drop_warned = True
