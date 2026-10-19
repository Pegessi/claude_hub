"""Watchdog responsibilities for structured streams."""

from __future__ import annotations

import time
from typing import Any, Dict

from ....models import AgentRuntimeStatus, AgentStreamEvent, AgentStreamEventType
from ..base import NormalizeContext
from ..native import ProviderSession
from ..redaction import redact_event
from .session_state import SessionState
from .values import NativeRuntimeSnapshot


class WatchdogMixin(SessionState):
    """Watchdog behavior on the sole session owner."""

    def _reset_turn_watchdog_state(self) -> None:
        """Clear per-turn output buffers and external-wait tracking."""
        self._assistant_text = ""
        self._visible_assistant_text = ""
        self._final_assistant_text = ""
        self._final_text_after_tool = False
        self._active_tool_call_ids.clear()
        self._blocking_approval_call_ids.clear()
        self._reset_queue_wait_state()
        self._goal_terminal_at = None

    def _reset_queue_wait_state(self) -> None:
        """Clear the capacity-queue waiting tracking for the active turn.

        Called on every turn boundary (reset, completion, cancel, EOF) and the
        moment real model output proves generation has begun, so a queue stretch
        can never leak across turns or linger after the model starts producing.
        """
        self._waiting_for_model_capacity = False
        self._queued_since = None
        self._last_queue_at = None
        self._last_persisted_queue_text = None

    def _terminalize_native_runtime(self, status: Any) -> None:
        if status == "completed":
            self._native_terminal_status = NativeRuntimeSnapshot(
                status=AgentRuntimeStatus.IDLE,
                detail="native provider turn completed",
            )
            return
        self._native_terminal_status = NativeRuntimeSnapshot(
            status=AgentRuntimeStatus.ATTENTION,
            detail=f"native provider turn {status or 'failed'}",
        )

    def _waiting_for_external_activity(self) -> bool:
        """True while the model is legitimately blocked on a tool or question.

        Those waits are not model-stream gaps: a test suite, shell command,
        subagent, or approval answer can take far longer than the streaming
        timeout without producing model deltas.
        """
        return bool(
            self._active_tool_call_ids
            or self._blocking_approval_call_ids
            or self._waiting_for_model_capacity
        )

    def _stream_inactive(self) -> bool:
        """True if model output has been silent past the inactivity timeout.

        Every accepted provider record refreshes the activity stamp, while
        outstanding tool calls and blocking questions suppress the watchdog
        entirely. A Goal turn whose terminal envelope already arrived is owned
        by the shorter Goal-terminal grace instead. Total wall-clock duration
        is intentionally not capped: an active agent can legitimately work for
        more than an hour.
        """
        if self._waiting_for_external_activity() or self._goal_terminal_at is not None:
            return False
        last = self._last_event_at
        if last is None:
            return False
        silence_for = time.monotonic() - last
        return (
            silence_for > self._deps.STREAM_INACTIVITY_TIMEOUT_S
            and silence_for >= self._deps._MIN_TURN_SILENCE_S
        )

    def _turn_hard_liveness_expired(self) -> bool:
        """True when an active turn has emitted NOTHING past the outer bound.

        This is the fail-safe behind :meth:`_stream_inactive`: it is not
        suppressed by an outstanding tool call, because a persistent
        app-server frozen mid-tool (or a process that vanished without an EOF)
        keeps that tool open indefinitely. It is refreshed by every accepted
        provider record of any kind, so a legitimately long turn — including a
        long-running command that streams output or completes within the
        window — never trips it. A blocking approval card (a human wait) does
        suppress it.
        """
        if self._blocking_approval_call_ids:
            return False
        last = self._last_event_at
        if last is None:
            return False
        silence_for = time.monotonic() - last
        return (
            silence_for > self._deps.ACTIVE_TURN_HARD_LIVENESS_TIMEOUT_S
            and silence_for >= self._deps._MIN_TURN_SILENCE_S
        )

    def _note_assistant_text(self, transport: ProviderSession) -> None:
        """Track a complete trailing Goal terminal signal in the raw text.

        The signal lives in the raw (sanitizer-hidden) assistant text. Only
        one-shot providers can finish their model output but never send a
        result record because a lingering child holds stdout open. Persistent
        app-servers (Codex/TraeX) and multi-stage service transports (MASO)
        emit their own authoritative completion and must never synthesize one.

        Re-evaluated on every delta: if the model emits an envelope and then
        keeps writing (a protocol violation), the latch clears until the
        signal is trailing again.
        """
        if (
            not self._goal_protocol_turn
            or transport.eof_is_fatal
            or transport.goal_completion_requires_provider_event
        ):
            return
        tail = self._assistant_text[-self._deps._GOAL_TAIL_SCAN_CHARS :]
        if "goal-status" not in tail:
            return
        if self._deps._GOAL_STATUS_TERMINAL_RE.search(tail):
            if self._goal_terminal_at is None:
                self._goal_terminal_at = time.monotonic()
        else:
            self._goal_terminal_at = None

    def _goal_terminal_grace_expired(self) -> bool:
        # Do not declare the turn finished while a tool or blocking question is
        # still outstanding: a model may legitimately emit its envelope-shaped
        # text and then keep acting, and the external wait is authoritative.
        at = self._goal_terminal_at
        if at is None or self._waiting_for_external_activity():
            return False
        return (time.monotonic() - at) > self._deps.GOAL_TERMINAL_GRACE_S

    async def _synthesize_goal_completion_locked(self, transport: ProviderSession) -> None:
        """Complete a one-shot Goal turn whose provider result never arrived.

        After the model's trailing ``goal-status`` envelope plus a grace period,
        a one-shot CLI (Claude/Cursor) can still be alive only because a
        lingering child holds stdout open. Treat the model output as the
        authoritative completion: publish a completed turn so the Goal
        controller parses the checkpoint and routes the Goal (continue / pause /
        block / complete), then terminate the stale provider process. The
        tailer loop stays alive; the next turn spawns a fresh one-shot.

        Persistent app-servers and multi-stage service transports never reach
        this: they emit their own terminal completion and are not safe to retire
        early.
        """
        turn_id = self._active_turn_id
        if (
            turn_id is None
            or self._goal_terminal_at is None
            or transport.eof_is_fatal
            or transport.goal_completion_requires_provider_event
        ):
            return
        session = self._session_getter()
        if session is None:
            raise RuntimeError("session no longer exists")
        ctx = NormalizeContext(
            session_id=self.session_id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            run_epoch=self._run_epoch,
            turn_id=turn_id,
        )
        payload: Dict[str, Any] = {"status": "completed"}
        if self._visible_assistant_text:
            payload["assistant_text"] = self._visible_assistant_text
        event = ctx.event(AgentStreamEventType.TURN_COMPLETED, payload)
        observer_event = event.model_copy(
            deep=True,
            update={
                "payload": {
                    **event.payload,
                    "_goal_protocol_text": self._assistant_text,
                }
            },
        )
        await self._publish(redact_event(event))
        self._turn_completed_seen = True
        self._terminalize_native_runtime("completed")
        self._active_turn_id = None
        self._active_tool_call_ids.clear()
        self._blocking_approval_call_ids.clear()
        self._reset_queue_wait_state()
        self._goal_terminal_at = None
        # Retire the one-shot reader generation and terminate the lingering
        # process so stale records/EOF cannot be attributed to the next turn and
        # spawned children do not hold ports indefinitely. The next
        # send_message spawns a fresh process.
        await transport.stop()
        self._notify_post_persist(observer_event)
        self._assistant_text = ""
        self._visible_assistant_text = ""
        self._final_assistant_text = ""
        self._final_text_after_tool = False
        self._goal_protocol_sanitizer = None
        self._goal_protocol_turn = False

    def _record_watchdog_activity(self, event: AgentStreamEvent) -> None:
        """Track conditions that legitimately pause model streaming."""
        # Any model output or tool activity proves the turn left the capacity
        # queue and is genuinely executing: clear the contiguous queue wait so
        # a later re-queue starts a fresh stretch (and re-persists its marker).
        if event.type in self._deps._GENERATION_ACTIVITY_TYPES:
            self._reset_queue_wait_state()
        call_id = event.call_id or (
            event.payload.get("tool_call_id") if isinstance(event.payload, dict) else None
        )
        if not isinstance(call_id, str) or not call_id:
            return
        if event.type == AgentStreamEventType.TOOL_CALL_STARTED:
            self._active_tool_call_ids.add(call_id)
        elif event.type == AgentStreamEventType.TOOL_CALL_COMPLETED:
            self._active_tool_call_ids.discard(call_id)

    def _note_raw_provider_wait(self, record: Any) -> bool:
        """Track provider-level capacity queueing from a raw JSON-RPC record.

        TraeX/Codex app-servers emit ``queue/status`` notifications while the
        turn waits for model capacity; no model deltas arrive during that wait,
        so a queued heartbeat must not count as model-stream liveness. Returns
        ``True`` only while the provider reports the turn as still queued
        (``state`` of ``queued``/``waiting``); ``ready`` and any later model
        delta clear the wait. This reads the raw record rather than adding an
        internal field to the persisted, user-visible STATUS payload.
        """
        if not isinstance(record, dict) or record.get("method") != "queue/status":
            return False
        params = record.get("params")
        state = params.get("state") if isinstance(params, dict) else None
        now = time.monotonic()
        if state in {"queued", "waiting"}:
            if self._queued_since is None:
                self._queued_since = now
            self._last_queue_at = now
            self._waiting_for_model_capacity = True
            return True
        # ``ready`` (capacity granted, generation starting) or an unknown state:
        # the wait is over; the first model delta also clears it defensively.
        self._reset_queue_wait_state()
        return False

    def _is_duplicate_queue_snapshot(self, event: AgentStreamEvent) -> bool:
        """Suppress identical per-second capacity-queue STATUS snapshots.

        A queued turn emits ``queue/status`` with one stable ``message_id``
        roughly every second. The frontend replaces that single status part in
        place, so persisting an event only when the rendered text changes (the
        queue position moves, or the state enters/leaves the queue) keeps the
        durable stream and the 2.5s poll payload from flooding with thousands
        of identical rows while preserving every visible update.
        """
        if event.type is not AgentStreamEventType.STATUS:
            return False
        payload = event.payload
        if (
            not isinstance(payload, dict)
            or payload.get("provider_status") != "queue/status"
            or payload.get("snapshot") is not True
        ):
            return False
        text = payload.get("text")
        text = text if isinstance(text, str) else ""
        if self._active_turn_id is not None and text and text == self._last_persisted_queue_text:
            return True
        self._last_persisted_queue_text = text or None
        return False

    def _queued_heartbeat_stalled(self) -> bool:
        """True when a declared queue stopped heartbeating past the stall bound.

        The provider said the turn was queued but then went silent: a wedged or
        vanished runtime mid-wait, reaped (kill + relaunch + resume) like any
        other dead turn. A live queue that merely moves slowly does NOT trip
        this — only a queue whose heartbeats actually stop does.
        """
        if not self._waiting_for_model_capacity or self._blocking_approval_call_ids:
            return False
        last = self._last_queue_at
        return last is not None and (time.monotonic() - last > self._deps.QUEUE_HEARTBEAT_STALL_S)

    def _queued_wait_cap_expired(self) -> bool:
        """True when a LIVE queue never reached generation within the cap.

        Distinct from :meth:`_queued_heartbeat_stalled`: here the provider is
        healthy and keeps reporting its position, but capacity never arrived.
        We cancel (never restart — resending simply re-queues) so the user is
        not locked out for hours. Stop is available regardless; a cap of 0
        disables the bound for sites that want to wait indefinitely.
        """
        if self._deps.QUEUED_TURN_MAX_WAIT_S <= 0:
            return False
        if not self._waiting_for_model_capacity or self._blocking_approval_call_ids:
            return False
        since = self._queued_since
        return since is not None and (time.monotonic() - since > self._deps.QUEUED_TURN_MAX_WAIT_S)
