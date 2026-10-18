"""Session responsibilities for structured streams."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ....models import AgentStreamEvent, ManagedSession
from ..attachments import AgentStreamAttachmentStore
from ..native import ProviderSession
from ..store import AgentStreamStore
from .input import InputMixin
from .native_consumer import NativeConsumerMixin
from .publication import PublicationMixin
from .retirement import RetirementMixin
from .transcript_consumer import TranscriptConsumerMixin
from .values import NativeRuntimeSnapshot, StructuredSourceUnavailable
from .watchdog import WatchdogMixin

logger = logging.getLogger("claude_hub.services.agent_stream.tailer")


class SessionTailer(
    InputMixin,
    RetirementMixin,
    WatchdogMixin,
    NativeConsumerMixin,
    TranscriptConsumerMixin,
    PublicationMixin,
):
    """Session behavior on the sole session owner."""

    @property
    def hard_failed(self) -> bool:
        return self._hard_failed

    @property
    def store(self) -> AgentStreamStore:
        return self._store

    @property
    def attachment_store(self) -> AgentStreamAttachmentStore:
        """Lazily-created bounded preview cache for this session."""
        if self._attachment_store is None:
            self._attachment_store = AgentStreamAttachmentStore(self.workspace_id, self.session_id)
        return self._attachment_store

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def current_source(self) -> Optional[Path]:
        return self._current_source

    @property
    def native_transport(self) -> Optional[ProviderSession]:
        """The live native provider transport, or ``None``.

        The send endpoint uses this to deliver composer input directly to the
        provider subprocess — never to a separate PTY (which would create a
        dual-session split-brain).
        """
        return self._native_transport

    @property
    def native_started(self) -> bool:
        """Whether the native consumer has started its current transport."""
        transport = self._native_transport
        return transport is not None and bool(getattr(transport, "_started", False))

    @property
    def native_ready(self) -> bool:
        """Whether transport startup and any required handshake have completed.

        This is a read-only startup probe, not send admission: callers still
        check runtime failure and the send path retains its own turn guard.
        One-shot providers do not require a handshake before their first send.
        """
        transport = self._native_transport
        return (
            transport is not None
            and self.native_started
            and (
                not transport.eof_is_fatal or bool(getattr(transport, "_handshake_complete", False))
            )
        )

    @property
    def input_busy(self) -> bool:
        """Whether a send or provider turn is active, without waking the runtime."""
        transport = self._native_transport
        return self._send_lock.locked() or (transport is not None and transport.turn_in_flight)

    @property
    def native_error(self) -> Optional[str]:
        return self._native_error

    @property
    def native_terminal_status(self) -> Optional[NativeRuntimeSnapshot]:
        """Latest persisted native turn outcome, if the turn terminalized."""

        return self._native_terminal_status

    async def poll_once(self) -> None:
        async with self._poll_lock:
            await self._poll_once()

    async def subscribe(self) -> "asyncio.Queue[AgentStreamEvent]":
        if self._hard_failed:
            raise StructuredSourceUnavailable(
                self._last_error or "structured transcript source unavailable"
            )
        queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue(
            maxsize=self._deps.SUBSCRIBER_QUEUE_MAX
        )
        self._subscribers.add(queue)
        self._last_subscriber_at = time.monotonic()
        try:
            await self.start()
        except BaseException:
            self._subscribers.discard(queue)
            self._last_subscriber_at = time.monotonic()
            raise
        return queue

    async def subscribe_completion(self) -> "asyncio.Queue[AgentStreamEvent]":
        """Subscribe to future approval/error/terminal events without timeline traffic."""
        if self._hard_failed:
            raise StructuredSourceUnavailable(
                self._last_error or "structured transcript source unavailable"
            )
        queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue(
            maxsize=self._deps.COMPLETION_QUEUE_MAX
        )
        self._completion_subscribers.add(queue)
        self._last_subscriber_at = time.monotonic()
        try:
            await self.start()
        except BaseException:
            self._completion_subscribers.discard(queue)
            self._last_subscriber_at = time.monotonic()
            raise
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[AgentStreamEvent]") -> None:
        self._subscribers.discard(queue)
        self._last_subscriber_at = time.monotonic()

    def unsubscribe_completion(self, queue: "asyncio.Queue[AgentStreamEvent]") -> None:
        self._completion_subscribers.discard(queue)
        self._last_subscriber_at = time.monotonic()

    def matches_session(self, session: ManagedSession) -> bool:
        """Whether ``session`` is this tailer's exact durable owner incarnation."""
        return self._session_identity == (
            session.workspace_id,
            session.tab_id,
            session.created_at,
        )

    def _incarnation_is_current(self) -> bool:
        session = self._session_getter()
        return bool(session is not None and self.matches_session(session))

    def _require_startable_incarnation(self) -> None:
        if self._retirement_pending:
            raise StructuredSourceUnavailable("structured runtime teardown is in progress")
        if (
            self.session_id in self._deps._DELETION_FENCED_SESSION_IDS
            or not self._incarnation_is_current()
        ):
            raise StructuredSourceUnavailable("structured runtime owner was deleted or replaced")

    def _require_publishable_incarnation(self) -> None:
        if not self._incarnation_is_current() or (
            self.session_id in self._deps._DELETION_FENCED_SESSION_IDS
            and id(self) not in self._deps._RETIRING_TAILER_IDS
        ):
            raise StructuredSourceUnavailable("structured runtime owner was deleted or replaced")

    def _publishable_incarnation_is_current(self) -> bool:
        try:
            self._require_publishable_incarnation()
        except StructuredSourceUnavailable:
            return False
        return True

    async def start(self) -> None:
        self._require_startable_incarnation()
        if self.is_running():
            return
        async with self._send_lock:
            self._require_startable_incarnation()
            # ``start`` is the first touch after a backend restart. A new
            # native transport cannot own a durable turn from the old process,
            # so repair that lifecycle immediately; history loading will then
            # show the interruption without requiring the user to press Stop.
            if self.is_running():
                return
            if self._native_transport is not None and not self._native_transport.turn_in_flight:
                await self._recover_orphaned_turn_locked()
            self._require_startable_incarnation()
            self._stopped = False
            # (Re)starting the consumer is explicit liveness evidence from
            # either a UI subscriber or a headless background dispatch (a
            # scheduled ``chat_turn`` has no viewer). An idle-reaped tailer
            # stays registered with a stale clock, so without this reset the
            # fresh consumer is born already past IDLE_TTL_S and its first tick
            # reaps the cold turn it was just restarted to deliver.
            self._last_subscriber_at = time.monotonic()
            self._task = asyncio.create_task(
                self._run(), name=f"agent-stream-tail-{self.session_id[:8]}"
            )

    async def stop(self, *, raise_errors: bool = False) -> None:
        # Concurrent deletion callers must not treat another in-progress stop as
        # an absent runtime. Observers remain in this caller's task because it
        # may own a task-reentrant Workspace mutation lock.
        async with self._stop_lock:
            self._retirement_pending = True
            if self.session_id in self._deps._DELETION_FENCED_SESSION_IDS:
                self._deps._RETIRING_TAILER_IDS.add(id(self))
            try:
                await self._stop(raise_errors=raise_errors)
                self._retirement_pending = False
            finally:
                self._deps._RETIRING_TAILER_IDS.discard(id(self))

    async def _stop(self, *, raise_errors: bool) -> None:
        errors: List[BaseException] = []
        self._stopped = True
        task = self._task
        if task is not None:
            task.cancel()
            # Disk writes now join through cancellation. wait_for would wait
            # past its own timeout trying to cancel that join. A timed-out
            # consumer stays attached, so the owner can retry but not be freed.
            done, _ = await asyncio.wait({task}, timeout=self._deps._STOP_JOIN_TIMEOUT_S)
            if not done:
                raise RuntimeError(
                    f"structured runtime teardown still pending for session {self.session_id}"
                )
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                errors.append(exc)
        self._task = None
        # Flush any coalesced text deltas that haven't been persisted yet so
        # no trailing content is lost on shutdown.
        try:
            await self._coalescer.flush_async()
        except Exception as exc:
            errors.append(exc)
            logger.exception(
                "agent_stream coalescer flush failed during stop for session %s",
                self.session_id,
            )
        if self._native_transport is not None:
            # ``ProviderSession.stop`` releases only process-local state. If
            # the backend reloads while a turn is active, persist its terminal
            # edge first so the next process does not replay an immortal
            # Stop/Queue turn from history.
            # Finish (or inherit) a manual Stop's backgrounded provider
            # teardown before shutting the transport down so it is not killed
            # mid kill+relaunch and a fresh resume does not outlive the tailer.
            await self._await_turn_teardown()
            if self._native_transport.turn_in_flight:
                try:
                    async with self._send_lock:
                        if self._native_transport.turn_in_flight:
                            await self._cancel_active_turn_locked(
                                self._native_transport,
                                error_message=self._deps._RUNTIME_INTERRUPTED_MESSAGE,
                                await_teardown=True,
                                require_teardown=raise_errors,
                            )
                except Exception as exc:
                    errors.append(exc)
                    logger.exception(
                        "native turn terminalization failed during stop for session %s",
                        self.session_id,
                    )
            try:
                await self._native_transport.stop()
            except Exception as exc:
                errors.append(exc)
                logger.exception("native transport stop failed for session %s", self.session_id)
        if raise_errors and errors:
            raise RuntimeError(
                f"could not safely stop structured runtime for session {self.session_id}"
            ) from errors[0]

    async def _run(self) -> None:
        try:
            if self._native_transport is not None:
                await self._run_native()
            else:
                await self._run_poll()
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._stopped = True

    async def _run_poll(self) -> None:
        while not self._stopped:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "agent_stream tailer poll failed for session %s",
                    self.session_id,
                )
            if self._hard_failed:
                break
            if (
                not self._subscribers
                and not self._completion_subscribers
                and time.monotonic() - self._last_subscriber_at > self._deps.IDLE_TTL_S
            ):
                break
            await asyncio.sleep(self._deps.POLL_INTERVAL_S)

    def peek_task_execution_ref(self, tab_id: str) -> Optional[Dict[str, Any]]:
        transport = self._native_transport
        if (
            transport is None
            or transport.session.tab_id != tab_id
            or transport.session.id != self.session_id
            or not self.is_running()
            or not transport.turn_in_flight
            or not self._active_turn_id
            or self._hard_failed
        ):
            return None
        return {
            "provider": transport.session.agent_type.value,
            "session_id": self.session_id,
            "thread_id": transport.active_thread_id,
            "turn_id": self._active_turn_id,
            "run_epoch": self._run_epoch,
        }
