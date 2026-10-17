"""Retirement responsibilities for structured streams."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ....models import AgentStreamEvent, AgentStreamEventType, AgentType
from ..base import NormalizeContext
from ..native import ProviderSession
from ..redaction import redact_event
from ..store import TurnLifecycleState
from .session_state import SessionState
from .values import ExternalTurnRetirement

logger = logging.getLogger("claude_hub.services.agent_stream.tailer")


class RetirementMixin(SessionState):
    """Retirement behavior on the sole session owner."""

    async def cancel_turn(self, expected_turn_id: Optional[str] = None) -> bool:
        """Cancel the active native turn or close a durable orphan.

        A backend restart loses the process-local ``_active_turn_id`` and
        provider guard, but an interrupted turn can remain open in the durable
        stream. In that state the frontend still renders Stop/Queue. Treat a
        Stop request as an explicit request to terminalize that latest orphan
        so the durable UI state and the actual idle runtime converge again.
        """
        transport = self._native_transport
        # A second Stop while the first one's provider teardown is still
        # running is idempotent: the cancelled edge is already durable and the
        # guard is released, so report success without awaiting the teardown.
        if self._turn_teardown_pending:
            return True
        async with self._send_lock:
            # The first Stop's background teardown can win the race with a
            # second caller that was already queued on the lock. Do NOT cancel
            # again (that would persist a second terminal edge); report the
            # in-flight Stop as the idempotent success it is.
            if self._turn_teardown_pending:
                return True
            if transport is not None and transport.turn_in_flight:
                if expected_turn_id is not None and self._active_turn_id != expected_turn_id:
                    return False
                await self._cancel_active_turn_locked(transport)
                return True

            return await self._recover_orphaned_turn_locked(expected_turn_id)

    async def retire_external_turn(self, expected_turn_id: str) -> ExternalTurnRetirement:
        """Retire one external turn and classify whether retry is safe.

        The normal Stop API intentionally returns only a boolean. External
        delivery deduplication needs a stronger contract: an absent active turn
        might never have started, might already have completed, or might have
        been superseded by another turn. Inspect and act under the send lock so
        completion and a newer send cannot change that answer between steps.
        """
        transport = self._native_transport
        async with self._send_lock:
            active_turn_id = self._active_turn_id
            turn_in_flight = bool(transport is not None and transport.turn_in_flight)
            if active_turn_id is not None:
                if active_turn_id != expected_turn_id:
                    return ExternalTurnRetirement.DIFFERENT_TURN
                if not turn_in_flight or transport is None:
                    return ExternalTurnRetirement.UNKNOWN
                await self._cancel_active_turn_locked(
                    transport,
                    await_teardown=True,
                    require_teardown=True,
                )
                return ExternalTurnRetirement.CANCELLED_MATCHING
            if turn_in_flight:
                return ExternalTurnRetirement.UNKNOWN

            snapshot = await self._store.inspect_turn_lifecycle(expected_turn_id)
            if snapshot.state == TurnLifecycleState.TERMINAL:
                return ExternalTurnRetirement.ALREADY_TERMINAL

            orphan = snapshot.latest_unfinished
            if orphan is not None:
                if orphan.turn_id != expected_turn_id:
                    return ExternalTurnRetirement.DIFFERENT_TURN
                if snapshot.state != TurnLifecycleState.UNFINISHED:
                    return ExternalTurnRetirement.UNKNOWN
                await self._terminalize_orphaned_turn_locked(orphan)
                return ExternalTurnRetirement.CANCELLED_MATCHING

            if snapshot.state == TurnLifecycleState.NOT_STARTED:
                return ExternalTurnRetirement.NOT_STARTED
            return ExternalTurnRetirement.UNKNOWN

    async def _recover_orphaned_turn_locked(self, expected_turn_id: Optional[str] = None) -> bool:
        """Explain and terminalize a turn owned by an earlier backend."""
        orphan = await self._store.latest_unfinished_turn()
        if orphan is None:
            return False
        if expected_turn_id is not None and orphan.turn_id != expected_turn_id:
            return False
        await self._terminalize_orphaned_turn_locked(orphan)
        return True

    async def _terminalize_orphaned_turn_locked(self, orphan: AgentStreamEvent) -> None:
        """Persist one already-identified orphan's interrupted terminal edge."""
        if orphan.agent_type == AgentType.MASO:
            # A fresh tailer has no in-memory cards. Rebuild only the orphan's
            # pending confirmations from durable events before retiring them.
            sequence = orphan.stream_sequence
            while True:
                page = await self._store.read_since(sequence, 500, compact=False)
                for event in page.events:
                    if event.turn_id == orphan.turn_id and event.run_epoch == orphan.run_epoch:
                        self._record_approval_card(event)
                if not page.has_more:
                    break
                sequence = page.next_sequence
        # The terminal edges are persisted by ``_publish_turn_completion``, but
        # persistence alone does not wake completion owners: Goal admission and
        # scheduled Chat runs only advance through the post-persist observers.
        # Every other terminalization path notifies after publishing; the orphan
        # path must do the same, or a scheduled turn whose runtime died stays
        # RUNNING forever and blocks its FIFO backlog.
        completed = await self._publish_turn_completion(
            turn_id=orphan.turn_id,
            run_epoch=orphan.run_epoch,
            status="cancelled",
            error_message=self._deps._RUNTIME_INTERRUPTED_MESSAGE,
        )
        self._notify_post_persist(completed)

    async def _publish_turn_completion(
        self,
        *,
        turn_id: Optional[str],
        run_epoch: Optional[int],
        status: str,
        error_message: Optional[str] = None,
    ) -> AgentStreamEvent:
        """Persist and fan out one authoritative terminal lifecycle edge."""
        session = self._session_getter()
        if session is None:
            raise RuntimeError("session no longer exists")
        ctx = NormalizeContext(
            session_id=self.session_id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            run_epoch=run_epoch,
            turn_id=turn_id,
        )
        completed = ctx.event(
            AgentStreamEventType.TURN_COMPLETED,
            {"status": status},
        )
        # Runtime loss is not a user-requested cancellation. Persist a visible
        # terminal error before the completion so the reconstructed timeline
        # explains why output stopped. ``error`` is itself terminal on the
        # frontend and in orphan detection, so a later completion-write
        # failure still cannot leave the composer locked forever.
        if error_message is not None:
            error_payload: dict[str, Any] = {"message": error_message}
            if error_message == self._deps._RUNTIME_INTERRUPTED_MESSAGE:
                # Private diagnostic redaction may change the visible message.
                # Recovery must retain a machine-readable interruption cause.
                error_payload["runtime_interrupted"] = True
            interrupted = ctx.event(
                AgentStreamEventType.ERROR,
                error_payload,
            )
            try:
                await self._publish(redact_event(interrupted))
            except Exception:
                # The provider turn is already being retired. If even the
                # explanatory ERROR cannot be saved, no later provider record
                # can release a direct completion owner such as the Feishu
                # FIFO. Wake that process-local owner without inventing a
                # non-durable timeline edge.
                if self._is_live:
                    self._fanout_volatile_terminal_failure(completed)
                raise
        await self._publish(redact_event(completed))
        self._turn_completed_seen = True
        self._terminalize_native_runtime(status)
        return completed

    async def _cancel_active_turn_locked(
        self,
        transport: ProviderSession,
        *,
        error_message: Optional[str] = None,
        await_teardown: bool = False,
        require_teardown: bool = False,
    ) -> None:
        """Cancel the in-flight turn while ``_send_lock`` is held.

        The durable terminal edge is persisted and the Hub active-turn guard is
        released BEFORE the provider's interrupt/teardown. A persistent
        app-server can take its bounded time to confirm an interrupt or to
        kill+relaunch+resume; releasing first means a capacity-queued or wedged
        turn the user (or the watchdog) cancelled is never left holding the
        guard while that teardown runs, and the persisted/fanned ``cancelled``
        edge unlocks the composer immediately.

        ``await_teardown=False`` (manual user Stop): provider teardown runs in
        a background task — the cancel HTTP returns as soon as the terminal
        edge is durable, never waiting for the provider process to actually
        exit. Sends and the watchdog are serialized behind the teardown via the
        ``_turn_teardown_task`` barrier. ``await_teardown=True`` (steer,
        watchdog reap, shutdown) keeps the previous inline behavior because the
        caller immediately needs the torn-down transport. ``require_teardown``
        additionally propagates a provider stop failure when an external
        delivery claim must not be released without that confirmation.
        """
        turn_id = self._active_turn_id
        publish_error: Optional[Exception] = None
        completed: Optional[AgentStreamEvent] = None
        if turn_id is not None:
            try:
                completed = await self._publish_turn_completion(
                    turn_id=turn_id,
                    run_epoch=self._run_epoch,
                    status="cancelled",
                    error_message=error_message,
                )
            except Exception as exc:
                logger.exception(
                    "agent_stream store append failed for cancelled turn session %s",
                    self.session_id,
                )
                publish_error = exc
        # Release the Hub-side guard now. Late records from the turn being
        # retired are filtered by the transport's discard-turn id; any that slip
        # through arrive with no owning turn after the terminal edge and are
        # dropped as unattributed, so they cannot pin the next turn.
        self._active_turn_id = None
        # A cancelled turn can no longer answer a pending card or own an
        # outstanding tool wait; drop stale tracking so it cannot be resolved
        # against a later turn.
        self._pending_approvals.clear()
        self._active_tool_call_ids.clear()
        self._blocking_approval_call_ids.clear()
        self._reset_queue_wait_state()
        self._goal_terminal_at = None
        if turn_id is not None and publish_error is None and completed is not None:
            self._notify_post_persist(completed)
        # Provider teardown. It is bounded (a native interrupt waits on a short
        # grace and otherwise kill+relaunches) and never re-raises into a wedged
        # guard: the turn is already terminal Hub-side. Manual Stop backgrounds
        # it so the user keeps a responsive composer while the persistent
        # app-server is being killed/resumed; inline callers await it here.
        if await_teardown:
            try:
                await transport.cancel_active_turn()
            except Exception as exc:
                logger.exception(
                    "native provider cancel after terminalizing turn failed for session %s",
                    self.session_id,
                )
                if require_teardown:
                    raise RuntimeError("native provider turn cancellation failed") from exc
            finally:
                self._turn_teardown_pending = False
        else:
            self._spawn_turn_teardown(transport)
        if publish_error is not None:
            raise RuntimeError("turn stopped but its cancelled state could not be persisted") from (
                publish_error
            )

    def _spawn_turn_teardown(self, transport: ProviderSession) -> None:
        """Run ``transport.cancel_active_turn`` after the send lock is freed.

        The cancelled edge is already durable and the Hub guard is released;
        only the provider-side interrupt / kill+relaunch+resume remains, and it
        must not block the cancel response or hold the composer lock. The task
        re-acquires ``_send_lock`` (FIFO behind anyone already waiting) so the
        teardown completes before the watchdog can act and — together with the
        barrier awaited in ``send_message`` — before a new turn reaches the
        provider being replaced.
        """
        if self._turn_teardown_task is not None or self._turn_teardown_pending:
            return
        self._turn_teardown_pending = True
        task = asyncio.create_task(
            self._run_turn_teardown(transport),
            name=f"agent-stream-cancel-teardown-{self.session_id[:8]}",
        )
        self._turn_teardown_task = task
        task.add_done_callback(self._turn_teardown_done)

    async def _run_turn_teardown(self, transport: ProviderSession) -> None:
        try:
            async with self._send_lock:
                try:
                    await transport.cancel_active_turn()
                except Exception:
                    logger.exception(
                        "native provider cancel teardown failed for session %s",
                        self.session_id,
                    )
                self._turn_teardown_pending = False
        except asyncio.CancelledError:
            self._turn_teardown_pending = False
            raise

    def _turn_teardown_done(self, task: "asyncio.Task[None]") -> None:
        if self._turn_teardown_task is task:
            self._turn_teardown_task = None
        self._turn_teardown_pending = False
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception(
                "agent_stream cancel teardown task failed for session %s",
                self.session_id,
                exc_info=exc,
            )

    async def _await_turn_teardown(self) -> None:
        """Block until any backgrounded Stop teardown has finished."""
        task = self._turn_teardown_task
        if task is not None:
            try:
                await task
            except Exception:
                logger.exception(
                    "waiting on cancel teardown failed for session %s", self.session_id
                )

    async def _reap_active_turn_locked(
        self, transport: ProviderSession, error_message: str
    ) -> bool:
        """Terminalize a wedged active turn while the send lock is held.

        Returns ``True`` when the provider was restarted in place (a TraeX/Codex
        interrupt could not be confirmed, so ``cancel_active_turn`` replaced the
        app-server and resumed the thread) and the push consumer should keep
        running; ``False`` when the process was stopped and the consumer should
        exit (the next send/subscribe restarts it).
        """
        await self._cancel_active_turn_locked(
            transport,
            error_message=error_message,
            await_teardown=True,
        )
        if getattr(transport, "_client_requested_stop", False):
            return True
        await transport.stop()
        return False

    async def _fail_active_turn(
        self, message: str, transport: ProviderSession
    ) -> Optional[AgentStreamEvent]:
        """Emit an ``error`` event and a failed ``turn_completed``.

        Called when a turn ends without a successful provider completion
        (nonzero exit, early EOF, or the persistent Codex server dying). The
        error message is surfaced from the provider's bounded stderr. Exactly
        one ``turn_completed(status=failed)`` is appended for the active turn
        so the frontend never leaves it pending.

        This does NOT release the turn guard — the caller (EOF handler) is
        responsible for clearing ``_active_turn_id`` and calling
        ``acknowledge_turn_complete`` after this returns, so the failed
        completion is persisted before a new turn can start.
        """
        turn_id = self._active_turn_id
        if turn_id is None:
            return None
        session = self._session_getter()
        if session is None:
            return None
        ctx = NormalizeContext(
            session_id=self.session_id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            run_epoch=self._run_epoch,
            turn_id=turn_id,
        )
        held = (
            self._goal_protocol_sanitizer.feed("", final=True)
            if self._goal_protocol_sanitizer is not None
            else ""
        )
        if held:
            self._visible_assistant_text += held
            try:
                await self._publish(
                    redact_event(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": held}))
                )
            except Exception:
                logger.exception(
                    "agent_stream store append failed for held text session %s",
                    self.session_id,
                )
        err_event = ctx.event(
            AgentStreamEventType.ERROR,
            {"message": message},
        )
        err_event = redact_event(err_event)
        try:
            await self._publish(err_event)
        except Exception:
            logger.exception(
                "agent_stream store append failed for error session %s",
                self.session_id,
            )
        completed = ctx.event(
            AgentStreamEventType.TURN_COMPLETED,
            {"status": "failed"},
        )
        completed = redact_event(completed)
        try:
            await self._publish(completed)
        except Exception:
            logger.exception(
                "agent_stream store append failed for turn_completed session %s",
                self.session_id,
            )
            return None
        self._turn_completed_seen = True
        self._terminalize_native_runtime("failed")
        return completed
