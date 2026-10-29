"""Native consumer responsibilities for structured streams."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

from ....models import AgentStreamEvent, AgentStreamEventType
from ..base import NormalizeContext
from ..redaction import redact_event
from .session_state import SessionState

logger = logging.getLogger("claude_hub.services.agent_stream.tailer")


class NativeConsumerMixin(SessionState):
    """Native consumer behavior on the sole session owner."""

    def _note_final_text(self, text: str) -> None:
        """Text after a tool call starts a new last answer."""
        if self._final_text_after_tool:
            self._final_assistant_text = ""
            self._final_text_after_tool = False
        self._final_assistant_text += text

    async def _run_native(self) -> None:
        """Push consumer for native provider transports.

        Unlike the transcript poll loop, this continuously awaits
        ``transport.read_line()`` and processes each record immediately — no
        ``POLL_INTERVAL_S`` batching. One-shot providers (Claude/Cursor) emit
        an EOF (``None``) at the end of each turn; we simply continue waiting
        for the next turn. The persistent Codex app-server exiting (EOF) is
        fatal and fails the session closed.

        Idle reaping: ``read_line`` is wrapped in ``asyncio.wait_for`` with a
        short tick so we can periodically check whether the tailer has had
        zero subscribers for longer than ``IDLE_TTL_S`` and stop itself. The
        tick does not add latency to record delivery — ``wait_for`` returns as
        soon as a record is available.
        """
        transport = self._native_transport
        assert transport is not None
        while not self._stopped:
            session = self._session_getter()
            if session is None:
                await asyncio.sleep(0.1)
                continue
            if self._native_error is not None:
                self._hard_failed = True
                self._last_error = self._native_error
                self._deps._HARD_FAILED_SESSION_IDS.add(self.session_id)
                break
            if not transport._started:
                try:
                    await transport.start()
                except Exception as exc:
                    self._hard_failed = True
                    self._last_error = str(exc)
                    self._deps._HARD_FAILED_SESSION_IDS.add(self.session_id)
                    break
            self._hard_failed = False
            self._deps._HARD_FAILED_SESSION_IDS.discard(self.session_id)
            self._last_error = None
            # A client-Stop recovery restart is complete once the transport is
            # started and healthy here; the expected EOF of the killed process
            # has been consumed by this same consumer before it loops back. A
            # LATER EOF (new server) must again fail closed.
            if getattr(transport, "_client_requested_stop", False) and transport._started:
                transport._client_requested_stop = False
            # Track the current turn's last-activity time for the stream
            # inactivity watchdog. ``turn_in_flight`` is the authoritative
            # guard (released at TURN_COMPLETED), so this stamps the first tick
            # on which a turn is observed active and clears on completion.
            if transport.turn_in_flight:
                if self._turn_in_flight_since is None:
                    self._turn_in_flight_since = time.monotonic()
                    # Seed the watchdog clock on first observation, but do not
                    # clobber a stamp that already records real activity (or one
                    # a caller deliberately back-dated): overwriting it here used
                    # to reset a known-old silence to age 0 on the next tick.
                    if self._last_event_at is None:
                        self._last_event_at = time.monotonic()
            elif self._turn_in_flight_since is not None:
                self._turn_in_flight_since = None
                self._last_event_at = None
            # Every watchdog branch is skipped while a manual Stop's provider
            # teardown is running: the Hub turn is already terminal and the
            # background task owns the provider guard, so the watchdogs must
            # neither double-cancel nor race the kill/relaunch.
            watchdogs_armed = transport.turn_in_flight and not self._turn_teardown_pending
            # Goal terminal without a provider result record: the model already
            # emitted its trailing goal-status envelope, but a one-shot CLI never
            # followed with a completion record. After the grace period, finish
            # the turn from the model output so the Goal controller can route it
            # instead of cancelling it as silent ten minutes later.
            if watchdogs_armed and self._goal_terminal_grace_expired():
                try:
                    async with self._send_lock:
                        if (
                            transport.turn_in_flight
                            and not self._turn_teardown_pending
                            and self._goal_terminal_grace_expired()
                        ):
                            await self._synthesize_goal_completion_locked(transport)
                except Exception:
                    logger.exception(
                        "native Goal completion synthesis failed for session %s",
                        self.session_id,
                    )
                continue
            # Capacity queue, heartbeat stalled: the provider declared the turn
            # queued and then stopped reporting — a dead/wedged runtime mid-wait
            # (distinct from a slow-but-live queue, which keeps heartbeating and
            # is handled below). Reap it like any dead turn: kill, relaunch,
            # resume the thread so the tab stays live.
            if watchdogs_armed and self._queued_heartbeat_stalled():
                restarted_in_place = False
                try:
                    async with self._send_lock:
                        if (
                            transport.turn_in_flight
                            and not self._turn_teardown_pending
                            and self._queued_heartbeat_stalled()
                        ):
                            restarted_in_place = await self._reap_active_turn_locked(
                                transport,
                                self._deps._QUEUE_STALL_MESSAGE,
                            )
                except Exception:
                    logger.exception(
                        "native queue-heartbeat stall reap failed for session %s",
                        self.session_id,
                    )
                if not restarted_in_place:
                    break
                continue
            # Capacity queue, live but past the cap: the runtime is healthy and
            # keeps reporting its position, but model capacity never arrived.
            # Cancel (do NOT stop/restart — resending simply re-queues) so the
            # user is never locked out for hours. Stop remains available at any
            # time regardless of this bound.
            if watchdogs_armed and self._queued_wait_cap_expired():
                try:
                    async with self._send_lock:
                        if (
                            transport.turn_in_flight
                            and not self._turn_teardown_pending
                            and self._queued_wait_cap_expired()
                        ):
                            await self._cancel_active_turn_locked(
                                transport,
                                error_message=self._deps._QUEUE_CAP_TIMEOUT_MESSAGE,
                                await_teardown=True,
                            )
                except Exception:
                    logger.exception(
                        "native queued-turn cap cancel failed for session %s",
                        self.session_id,
                    )
                continue
            # Stream inactivity timeout: terminate only when the model itself
            # was expected to produce output and the provider went silent.
            # Outstanding tool calls and blocking approval questions are
            # legitimate external waits, and active long-running turns have no
            # absolute duration cap.
            if watchdogs_armed and self._stream_inactive():
                restarted_in_place = False
                try:
                    async with self._send_lock:
                        if (
                            transport.turn_in_flight
                            and not self._turn_teardown_pending
                            and self._stream_inactive()
                        ):
                            restarted_in_place = await self._reap_active_turn_locked(
                                transport,
                                self._deps._INACTIVITY_TIMEOUT_MESSAGE,
                            )
                except Exception:
                    logger.exception(
                        "native stream inactivity reap failed for session %s",
                        self.session_id,
                    )
                if not restarted_in_place:
                    break
                continue
            # Outer liveness bound: even with an outstanding tool, total
            # provider silence past the hard timeout means the runtime is wedged
            # or gone without an EOF. Terminalize the turn; when the provider
            # could not be interrupted in place it is restarted and the thread
            # resumed here, so the tab stays live. Otherwise the process is
            # reaped and the next send restarts it. Legitimately long work
            # keeps emitting SOME record and refreshes the stamp; an open
            # approval card exempts the turn because it is waiting on a human.
            if watchdogs_armed and self._turn_hard_liveness_expired():
                restarted_in_place = False
                try:
                    async with self._send_lock:
                        if (
                            transport.turn_in_flight
                            and not self._turn_teardown_pending
                            and self._turn_hard_liveness_expired()
                        ):
                            restarted_in_place = await self._reap_active_turn_locked(
                                transport,
                                self._deps._HARD_LIVENESS_TIMEOUT_MESSAGE,
                            )
                except Exception:
                    logger.exception(
                        "native hard liveness reap failed for session %s",
                        self.session_id,
                    )
                if not restarted_in_place:
                    break
                continue
            # Idle reaping: with no subscribers for IDLE_TTL_S, reap an idle
            # tailer. A healthy in-flight turn is NEVER cancelled just because
            # viewers vanished (e.g. mobile backgrounding kills SSE and the
            # long-poll self-expires) — it completes on its own and the next
            # idle check reaps it.
            if (
                not self._subscribers
                and not self._completion_subscribers
                and time.monotonic() - self._last_subscriber_at > self._deps.IDLE_TTL_S
            ):
                # A headless background turn (a scheduled chat_turn with no
                # viewer attached) must survive exactly like a watched one.
                # Re-check liveness UNDER the send lock: a concurrent send may
                # publish turn_started (setting ``_active_turn_id``) and raise
                # the provider guard between the outside check and acting on it.
                # Acting on the stale read used to stop the one-shot process a
                # cold scheduled turn had just spawned, orphaning it. This
                # mirrors the double-checked locking of the inactivity reap
                # above.
                async with self._send_lock:
                    turn_active = transport.turn_in_flight or self._active_turn_id is not None
                    if not turn_active:
                        # Stop the native transport so the provider subprocess
                        # (e.g. the Codex app-server) is reaped, not left orphaned.
                        try:
                            await transport.stop()
                        except Exception:
                            logger.exception(
                                "native transport stop failed during idle reap for session %s",
                                self.session_id,
                            )
                        idle_stop = True
                    else:
                        idle_stop = False
                if idle_stop:
                    break
                # Active (possibly viewer-less) turn: fall through and keep
                # consuming. Records are still persisted even with zero
                # subscribers, so nothing is lost while no viewer is attached.
            try:
                record = await asyncio.wait_for(
                    transport.read_line(), timeout=self._deps.POLL_INTERVAL_S
                )
            except asyncio.TimeoutError:
                # No record arrived within the tick; loop back to re-check
                # idle TTL and transport health.
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "agent_stream native read failed for session %s",
                    self.session_id,
                )
                self._last_error = str(exc)
                continue
            if record is None:
                # EOF. For one-shot providers this is the normal end of a
                # turn; keep waiting for the next turn. For Codex the
                # persistent app-server died — fail closed.
                if transport.eof_is_fatal:
                    # A client Stop (or the hard-liveness reaper) deliberately
                    # killed this persistent app-server because its turn could
                    # not be interrupted in place. That EOF is expected: the
                    # transport has already restarted and thread/resumed (or the
                    # loop-top start below does so), so keep the consumer alive
                    # instead of failing the session closed and stranding the
                    # tab on "Retry".
                    if getattr(transport, "_client_requested_stop", False):
                        continue
                    # The persistent provider died; the in-flight turn (if
                    # any) is abandoned. Emit an error and a failed
                    # turn_completed for the active turn so the frontend
                    # never leaves it pending, then fail the session. The
                    # identity recheck under the send lock prevents a race
                    # with a manual Stop that already terminalized this turn:
                    # only one of the two edges is ever persisted.
                    async with self._send_lock:
                        if self._active_turn_id is not None:
                            fatal_failure = await self._fail_active_turn(
                                "native transport process exited", transport
                            )
                        else:
                            fatal_failure = None
                        self._active_turn_id = None
                        transport.acknowledge_turn_complete()
                    if fatal_failure is not None:
                        self._notify_post_persist(fatal_failure)
                    self._hard_failed = True
                    self._last_error = "native transport process exited"
                    self._deps._HARD_FAILED_SESSION_IDS.add(self.session_id)
                    break
                # One-shot turn complete. If the provider exited nonzero AND
                # never emitted a terminal turn_completed, synthesize an
                # error + failed turn_completed for the active turn. If the
                # provider DID emit a terminal turn_completed, its status
                # (completed/failed/cancelled from the result record) is the
                # single source of truth — we must NOT emit a second
                # completion, even on a nonzero exit, because that would
                # produce two terminal events for the same turn.
                exit_error = transport.exit_error
                failed: Optional[AgentStreamEvent] = None
                if not self._turn_completed_seen and self._goal_terminal_at is not None:
                    # One-shot exited before the Goal-terminal grace elapsed but
                    # after the model emitted a complete, controller-valid
                    # goal-status envelope, and it exited cleanly. This is the
                    # same missing-result disease as a held-open stdout, just a
                    # faster death: finish the turn as completed so the Goal
                    # controller routes it instead of marking it failed. A
                    # nonzero exit still falls through to the failure path.
                    if exit_error is None:
                        try:
                            async with self._send_lock:
                                await self._synthesize_goal_completion_locked(transport)
                        except Exception:
                            logger.exception(
                                "native Goal completion synthesis on EOF failed for session %s",
                                self.session_id,
                            )
                        continue
                if not self._turn_completed_seen and exit_error is None:
                    # Ordinary (non-Goal) one-shot turn reached a CLEAN EOF after
                    # streaming a complete, non-empty answer but never wrote a
                    # completion record, with no outstanding tool call, blocking
                    # approval, or capacity-queue wait. The streamed text is the
                    # authoritative result: synthesize a completed turn (the
                    # ordinary-turn counterpart of the Goal synthesis above) so
                    # dispatch_tab_chat_and_wait returns the answer instead of
                    # raising and making the external bridge (e.g. Feishu)
                    # report failure after the reply was already delivered. A
                    # nonzero exit is excluded up front; an empty answer or a
                    # pending external wait makes the helper return False, so the
                    # failure synthesis below still runs.
                    try:
                        async with self._send_lock:
                            synthesized = await self._synthesize_clean_eof_completion_locked(
                                transport
                            )
                    except Exception:
                        synthesized = False
                        logger.exception(
                            "native clean-EOF completion synthesis failed for session %s",
                            self.session_id,
                        )
                    if synthesized:
                        continue
                if not self._turn_completed_seen:
                    # A cancelled reader cannot reach this branch: the
                    # transport retires its generation before publishing the
                    # cancellation sentinel, so ``read_line`` discards it and
                    # never surfaces it as EOF. Reaching here therefore means
                    # the provider's stdout ended without a completion record —
                    # a real failure, and one this branch used to swallow
                    # silently, leaving no trace beyond the synthesized event.
                    # "ended" and not "closed": the drain also stops reading
                    # after a non-cancellation crash, and then the provider may
                    # still be alive (its ``native transport stdout drain
                    # failed`` line precedes this one).
                    logger.warning(
                        "provider stdout ended with no completion record for "
                        "session %s (exit_error=%r); synthesizing a failed turn",
                        self.session_id,
                        exit_error,
                    )
                    failed = await self._fail_active_turn(
                        exit_error or "provider exited without a completion record",
                        transport,
                    )
                elif exit_error is not None:
                    # The provider emitted a terminal completion but still
                    # exited nonzero. The completion's status already
                    # reflects the outcome (failed/cancelled), so retain the
                    # bounded stderr in diagnostics without emitting a second
                    # terminal event.
                    logger.warning(
                        "provider exited nonzero after turn_completed for session %s: %s",
                        self.session_id,
                        exit_error,
                    )
                # The active turn id and turn guard are normally released at
                # ``TURN_COMPLETED`` (which is the provider's final record).
                # If the process exited without ever emitting a completion
                # record, ``_fail_active_turn`` above has already published a
                # synthetic failed ``turn_completed``; clear the turn id and
                # release the guard here so the next send can proceed. If a
                # completion WAS emitted, these are no-ops.
                self._active_turn_id = None
                self._active_tool_call_ids.clear()
                self._blocking_approval_call_ids.clear()
                self._reset_queue_wait_state()
                self._goal_terminal_at = None
                transport.acknowledge_turn_complete()
                if failed is not None:
                    self._notify_post_persist(failed)
                continue
            # Stop may retire a turn after read_line dequeues its record but
            # before wait_for resumes this consumer. Recheck at consumption.
            if not transport.accepts_notification(record):
                continue
            # Accepted record. A non-queue record is real progress and refreshes
            # the model-stream / hard-liveness clocks. A capacity-queue
            # heartbeat is NOT liveness (generation has not started), so it must
            # not reset the clocks — otherwise a turn queued for hours is kept
            # "alive" forever by its own ~1/s status notifications.
            queued = self._note_raw_provider_wait(record)
            if not queued:
                self._last_event_at = time.monotonic()
            elif self._active_turn_id is None and self._turn_completed_seen:
                # A capacity-queue heartbeat with no owning turn, after this
                # consumer already terminalized a turn: a Stop/cap-cancelled
                # turn whose process still had a heartbeat in flight. Drop it
                # before normalization so it cannot mint an unattributed status
                # legacy turn that re-locks the composer.
                continue
            transport.maybe_capture_conversation_id(record)
            ctx = NormalizeContext(
                session_id=self.session_id,
                tab_id=session.tab_id,
                agent_type=session.agent_type,
                run_epoch=self._run_epoch,
                turn_id=self._active_turn_id,
                main_thread_id=getattr(transport, "active_thread_id", None),
            )
            try:
                events = self.adapter.normalize_line(record, ctx)
            except Exception:
                logger.exception(
                    "agent_stream adapter %s failed on native record for session %s; skipping",
                    self.adapter.adapter_id,
                    self.session_id,
                )
                continue
            for event in events:
                if event.type == AgentStreamEventType.TURN_STARTED:
                    # The authoritative turn_started was already published by
                    # send_message with the frontend's client_turn_id. Provider
                    # records that also signal a turn start (Claude
                    # message_start, Codex turn/started) must NOT create a
                    # second turn — skip them.
                    if self._active_turn_id is not None:
                        continue
                    provider_turn_id = event.payload.get("provider_turn_id")
                    stable_key = (
                        provider_turn_id
                        if isinstance(provider_turn_id, str) and provider_turn_id
                        else f"{self._run_epoch + 1}:{event.created_at.isoformat()}"
                    )
                    self._active_turn_id = str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"claude-hub:{self.session_id}:{stable_key}")
                    )
                    self._run_epoch += 1
                    self._reset_turn_watchdog_state()
                    self._turn_completed_seen = False
                    # Native continuation starts a new turn, not a continuation
                    # of Hub's previous output protocol or terminal status.
                    self._native_terminal_status = None
                    self._goal_protocol_turn = False
                    self._goal_protocol_sanitizer = None
                    transport.acknowledge_provider_turn_started()
                    event.turn_id = self._active_turn_id
                    event.message_id = f"{self._active_turn_id}:user"
                    event.run_epoch = self._run_epoch
                # Defensive boundary for persistent app-servers (TraeX/Codex):
                # a turn-interior record arriving with NO owning Hub turn is an
                # unreferenceable replay or background/goal execution, not the
                # beginning of a new turn. Persisting it minted a null-turn
                # legacy row on the frontend that never completed and pinned the
                # composer lock forever — a duplicate ``item/completed`` for a
                # nested code-mode tool, re-emitted hours after its turn had
                # ended, was the observed wedge. Drop it before watchdog and
                # persistence bookkeeping. ``turn_started`` is handled above and
                # can legitimately open a provider-initiated turn; ``status``
                # and ``error`` carry control-plane information with no turn.
                if (
                    self._active_turn_id is None
                    and event.turn_id is None
                    and event.type in self._deps._UNATTRIBUTED_DROP_TYPES
                    # Only persistent app-servers guarantee every interior
                    # record belongs to a Hub-opened turn. One-shot providers
                    # (Claude/Cursor) legitimately stream records with no Hub
                    # turn id (provider-initiated / transcript paths), and at a
                    # cold start a resumed thread may replay the still-active
                    # turn before its turn/started notification — so only drop
                    # after a turn has already completed in this consumer.
                    and transport.eof_is_fatal
                    and self._turn_completed_seen
                ):
                    logger.warning(
                        "dropping unattributed %s record with no active turn for session %s",
                        event.type.value,
                        self.session_id,
                    )
                    continue
                is_turn_completed = event.type == AgentStreamEventType.TURN_COMPLETED
                if event.run_epoch is None:
                    event.run_epoch = self._run_epoch
                # Drop identical per-second capacity-queue snapshots at the
                # source: they share one stable message id and the UI replaces
                # them in place, so persisting only when the rendered text
                # changes keeps the durable stream / poll payload free of a
                # ~1/s flood. The raw heartbeat above still drives the queue
                # watchdog, so dropping the persisted event does not hide a
                # stall.
                if self._is_duplicate_queue_snapshot(event):
                    continue
                self._record_approval_card(event)
                self._record_watchdog_activity(event)
                if (
                    event.type == AgentStreamEventType.TEXT_DELTA
                    and not event.payload.get("plan")
                    and not event.payload.get("subagent_thread")
                ):
                    # Child text remains in its own persisted timeline. It
                    # cannot become the parent's final answer/Goal envelope.
                    text = event.payload.get("text")
                    if isinstance(text, str):
                        self._assistant_text += text
                        self._note_assistant_text(transport)
                        if self._goal_protocol_sanitizer is not None:
                            visible = self._goal_protocol_sanitizer.feed(text)
                            if not visible:
                                continue
                            event.payload["text"] = visible
                            self._visible_assistant_text += visible
                        self._note_final_text(event.payload["text"])
                elif event.type == AgentStreamEventType.TOOL_CALL_STARTED and not event.payload.get(
                    "subagent_thread"
                ):
                    self._final_text_after_tool = True
                goal_protocol_text: Optional[str] = None
                if is_turn_completed and self._assistant_text:
                    if self._goal_protocol_sanitizer is None:
                        event.payload.setdefault("assistant_text", self._assistant_text)
                    else:
                        # The Goal controller needs the raw control envelope,
                        # but it must never enter the durable/public event.
                        goal_protocol_text = self._assistant_text
                if is_turn_completed and self._goal_protocol_sanitizer is not None:
                    raw_summary = event.payload.get("summary")
                    if isinstance(raw_summary, str) and raw_summary:
                        # Codex/TraeX repeat the final assistant message in
                        # task_complete.last_agent_message. Treat that copy as
                        # protocol input too; otherwise the control envelope
                        # would still enter the durable completion summary.
                        goal_protocol_text = raw_summary
                        summary_sanitizer = self._deps._GoalProtocolSanitizer(
                            trailing_only=not self._goal_protocol_turn
                        )
                        event.payload["summary"] = summary_sanitizer.feed(raw_summary, final=True)
                    visible = self._goal_protocol_sanitizer.feed("", final=True)
                    if visible:
                        self._visible_assistant_text += visible
                        self._note_final_text(visible)
                        await self._publish(
                            ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": visible})
                        )
                    event.payload.setdefault("assistant_text", self._visible_assistant_text)
                if is_turn_completed:
                    final = self._final_assistant_text.strip()
                    whole = event.payload.get("assistant_text")
                    if final and isinstance(whole, str) and final != whole.strip():
                        event.payload.setdefault("final_text", final)
                observer_event = (
                    event.model_copy(
                        deep=True,
                        update={
                            "payload": {
                                **event.payload,
                                "_goal_protocol_text": goal_protocol_text,
                            }
                        },
                    )
                    if goal_protocol_text is not None
                    else event
                )
                event = redact_event(event)
                if is_turn_completed:
                    # The provider's own terminal record is persisted and the
                    # guard released under ``_send_lock`` so it cannot interleave
                    # with a manual Stop's ``cancelled`` edge for the same turn.
                    try:
                        consumed = await self._consume_turn_completion_record(
                            event, observer_event, transport
                        )
                    except Exception:
                        logger.exception(
                            "agent_stream turn_completed finalize failed for session %s",
                            self.session_id,
                        )
                        continue
                    if not consumed:
                        continue
                    continue
                try:
                    await self._publish(event)
                except Exception:
                    logger.exception(
                        "agent_stream store append failed for session %s; dropping event",
                        self.session_id,
                    )
                    continue
                notification = event.payload.get("provider_notification")
                goal_update = (
                    event.type == AgentStreamEventType.STATUS
                    and isinstance(notification, str)
                    and notification.startswith(("thread/goal/", "goal/"))
                )
                if not event.payload.get("subagent_thread") and (
                    event.type == AgentStreamEventType.TURN_STARTED or goal_update
                ):
                    self._notify_post_persist(event)
