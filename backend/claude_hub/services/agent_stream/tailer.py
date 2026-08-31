"""Per-session transcript tailer for the structured observation plane.

One :class:`SessionTailer` per session, owned by a process-wide
:class:`TailerManager`. The tailer polls the agent's transcript file (found by
the provider adapter), normalizes each new line through the adapter, redacts +
persists the resulting events to the per-session store, and fans them out to
any live SSE/wait subscribers.

Design points:

- **One tailer per session** — the manager dedupes; subscribers share one loop.
- **Close the file every poll** — ``_read_new_lines`` opens, reads, and closes
  within a worker thread, so no fd is held open between polls.
- **Persisted cursor** — ``{session}.cursor.json`` records ``(path, inode,
  offset, run_epoch)`` plus snapshot identities when necessary, so a tailer
  restarted after idle (or after a backend restart) resumes without
  re-processing or duplicating already-persisted events. Written atomically
  (tmp + rename).
- **run_epoch** — incremented for each ``turn_started`` and stamped on every
  event of that turn, so the frontend can group a prompt's full response.
- **Backfill vs live** — the first read from offset 0 is a backfill: events are
  persisted but NOT fanned out (live subscribers use history for replay; SSE
  is for live only).
- **Fail-closed** — if the source cannot be discovered within a grace period,
  the tailer marks itself ``hard_failed``; the capabilities endpoint then
  reports ``structured=False`` and the frontend falls back to the raw terminal.
- **Idle reaping** — a tailer with zero subscribers stops after ``IDLE_TTL_S``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ...models import (
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    ManagedSession,
    SessionKind,
)
from .base import (
    AgentStreamAdapter,
    NormalizeContext,
    discover_source_cached,
    invalidate_source,
)
from .native import ProviderSession, _detect_image_mime, create_native_session
from .redaction import redact_event
from .store import AgentStreamStore

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.0
IDLE_TTL_S = 300.0
DISCOVERY_GRACE_S = 30.0
SUBSCRIBER_QUEUE_MAX = 2000
_STOP_JOIN_TIMEOUT_S = 5.0

_HARD_FAILED_SESSION_IDS: Set[str] = set()
_TAILER_MANAGERS: Any = weakref.WeakSet()


def structured_source_hard_failed(session_id: str) -> bool:
    """Return the process-local terminal discovery state for ``session_id``."""
    return session_id in _HARD_FAILED_SESSION_IDS


class StructuredSourceUnavailable(RuntimeError):
    """The adapter exists, but its transcript source is terminally unavailable."""


class SessionTailer:
    """Tails one session's transcript and fans out normalized events."""

    def __init__(
        self,
        workspace_id: str,
        session_id: str,
        adapter: AgentStreamAdapter,
        session_getter: Callable[[], Optional[ManagedSession]],
        store: Optional[AgentStreamStore] = None,
        native_transport: Optional[ProviderSession] = None,
        native_error: Optional[str] = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.session_id = session_id
        self.adapter = adapter
        self._session_getter = session_getter
        self._store = store or AgentStreamStore(workspace_id, session_id)
        self._cursor_path = self._store.cursor_path
        self._native_transport = native_transport
        # When set, the agent session required a native transport but it could
        # not be created. Fail-closed: never fall back to transcript as a
        # real-time source for agent sessions.
        self._native_error = native_error

        self._offset = 0
        self._inode: Optional[int] = None
        self._run_epoch = 0
        self._loaded_cursor = False
        self._snapshot_source_ids: List[str] = []
        self._snapshot_source_kinds: List[str] = []
        self._snapshot_digest: Optional[str] = None
        self._is_live = False

        # The active user turn's stable id. Set by ``send_message`` the moment
        # a user turn is submitted (before the provider runs), and cleared when
        # the turn completes. Every provider event normalized while this is set
        # is stamped with it so the frontend can upsert by identity.
        self._active_turn_id: Optional[str] = None
        # Whether the provider has emitted a terminal ``turn_completed`` event
        # for the active turn. Used at EOF to decide whether a failed
        # ``turn_completed`` must be synthesized (nonzero exit or early EOF
        # with no completion record).
        self._turn_completed_seen: bool = False

        self._task: Optional[asyncio.Task[Any]] = None
        self._subscribers: Set[asyncio.Queue[AgentStreamEvent]] = set()
        self._last_subscriber_at = time.monotonic()
        self._hard_failed = False
        self._discovery_deadline: Optional[float] = None
        self._stopped = False
        self._drop_warned = False
        self._current_source: Optional[Path] = None
        self._last_error: Optional[str] = None
        self._poll_lock = asyncio.Lock()
        # Serializes composer input so two concurrent ``send_message`` calls
        # cannot race on ``_active_turn_id`` / ``_run_epoch``. The lock is
        # held across the busy-check, the authoritative ``turn_started``
        # publish, and the transport send so a busy second send never mutates
        # state that the in-flight first turn still depends on.
        self._send_lock = asyncio.Lock()

    @property
    def hard_failed(self) -> bool:
        return self._hard_failed

    @property
    def store(self) -> AgentStreamStore:
        return self._store

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
    def native_error(self) -> Optional[str]:
        return self._native_error

    async def send_message(
        self,
        text: str,
        images: List[bytes],
        client_turn_id: str,
    ) -> None:
        """Atomically deliver a user turn (text + images) to the native transport.

        This is the composer's single input path for AGENT sessions. The same
        ``ProviderSession`` that produces the structured stream also consumes
        the turn, so input and output never diverge across two sessions.

        Stable turn identity: the frontend supplies ``client_turn_id``. Before
        the provider runs, we append an authoritative ``turn_started`` event
        stamped with that id (and ``message_id={turn_id}:user``) so the user's
        message is immediately visible and can never be confused with a later
        identical message. ``self._active_turn_id`` is set so every provider
        event normalized during this turn inherits the same ``turn_id``.

        Concurrency: the whole method runs under ``self._send_lock``. The
        first thing we do inside the lock is check ``transport.turn_in_flight``;
        if a turn is already active we raise ``RuntimeError`` (mapped to HTTP
        409) WITHOUT touching ``_active_turn_id``, ``_run_epoch``, or the
        store. This guarantees a busy second send can never clobber the
        in-flight turn's identity.

        Fail-closed: no native transport raises ``RuntimeError``; the caller
        maps it to an HTTP error rather than falling back to tmux. On send
        failure we append an ``error`` and a ``turn_completed(status=failed)``
        for the same ``turn_id`` so the frontend never leaves the turn pending.
        """
        transport = self._native_transport
        if transport is None:
            raise RuntimeError("no native transport for this session; use the terminal send path")
        if self._native_error is not None:
            raise RuntimeError(self._native_error)
        if not transport._started:
            await transport.start()

        async with self._send_lock:
            # Busy check BEFORE any state mutation. If a turn is already in
            # flight, fail fast so the in-flight turn's _active_turn_id and
            # _run_epoch are never overwritten.
            if transport.turn_in_flight:
                raise RuntimeError(
                    "a turn is already in flight; wait for it to complete before "
                    "sending another message"
                )

            # Authoritative turn start: publish the user's message before the
            # provider does anything. This guarantees the turn exists in the store
            # and is fanned out to subscribers, even if the provider never echoes
            # the user message back (e.g. Codex app-server notifications).
            session = self._session_getter()
            if session is None:
                raise RuntimeError("session no longer exists")
            self._active_turn_id = client_turn_id
            self._run_epoch += 1
            self._turn_completed_seen = False
            ctx = NormalizeContext(
                session_id=self.session_id,
                tab_id=session.tab_id,
                agent_type=session.agent_type,
                run_epoch=self._run_epoch,
                turn_id=client_turn_id,
            )
            attachments_meta = [
                {"bytes": len(img), "mime": _detect_image_mime(img)} for img in images
            ]
            turn_started = ctx.event(
                AgentStreamEventType.TURN_STARTED,
                {"summary": text, "attachments": attachments_meta},
            )
            turn_started = redact_event(turn_started)
            try:
                turn_started = await self._store.append(turn_started)
            except Exception:
                logger.exception(
                    "agent_stream store append failed for turn_started session %s",
                    self.session_id,
                )
                # If we cannot persist the authoritative turn_started, the turn
                # has no identity. Fail the send rather than fan out an unpersisted
                # seq=0 event that the frontend will drop.
                self._active_turn_id = None
                raise
            self._fanout(turn_started)

            try:
                await transport.send_message(text, images)
            except Exception:
                # The provider rejected or failed to start the turn. Publish an
                # error and a failed completion for the same turn_id so the
                # frontend can mark it failed instead of leaving it pending.
                err_event = ctx.event(
                    AgentStreamEventType.ERROR,
                    {"message": "failed to deliver turn to provider"},
                )
                err_event = redact_event(err_event)
                try:
                    err_event = await self._store.append(err_event)
                except Exception:
                    logger.exception(
                        "agent_stream store append failed for error session %s",
                        self.session_id,
                    )
                else:
                    self._fanout(err_event)
                completed = ctx.event(
                    AgentStreamEventType.TURN_COMPLETED,
                    {"status": "failed"},
                )
                completed = redact_event(completed)
                try:
                    completed = await self._store.append(completed)
                except Exception:
                    logger.exception(
                        "agent_stream store append failed for turn_completed session %s",
                        self.session_id,
                    )
                else:
                    self._fanout(completed)
                self._active_turn_id = None
                raise

    async def _fail_active_turn(self, message: str, transport: ProviderSession) -> None:
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
            return
        session = self._session_getter()
        if session is None:
            return
        ctx = NormalizeContext(
            session_id=self.session_id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            run_epoch=self._run_epoch,
            turn_id=turn_id,
        )
        err_event = ctx.event(
            AgentStreamEventType.ERROR,
            {"message": message},
        )
        err_event = redact_event(err_event)
        try:
            err_event = await self._store.append(err_event)
        except Exception:
            logger.exception(
                "agent_stream store append failed for error session %s",
                self.session_id,
            )
        else:
            self._fanout(err_event)
        completed = ctx.event(
            AgentStreamEventType.TURN_COMPLETED,
            {"status": "failed"},
        )
        completed = redact_event(completed)
        try:
            completed = await self._store.append(completed)
        except Exception:
            logger.exception(
                "agent_stream store append failed for turn_completed session %s",
                self.session_id,
            )
        else:
            self._fanout(completed)
        self._turn_completed_seen = True

    async def poll_once(self) -> None:
        async with self._poll_lock:
            await self._poll_once()

    async def subscribe(self) -> "asyncio.Queue[AgentStreamEvent]":
        if self._hard_failed:
            raise StructuredSourceUnavailable(
                self._last_error or "structured transcript source unavailable"
            )
        queue: asyncio.Queue[AgentStreamEvent] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self._subscribers.add(queue)
        self._last_subscriber_at = time.monotonic()
        await self.start()
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[AgentStreamEvent]") -> None:
        self._subscribers.discard(queue)
        self._last_subscriber_at = time.monotonic()

    async def start(self) -> None:
        if self.is_running():
            return
        self._stopped = False
        self._task = asyncio.create_task(
            self._run(), name=f"agent-stream-tail-{self.session_id[:8]}"
        )

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=_STOP_JOIN_TIMEOUT_S)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        self._task = None
        if self._native_transport is not None:
            try:
                await self._native_transport.stop()
            except Exception:
                logger.exception("native transport stop failed for session %s", self.session_id)

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
            if not self._subscribers and (time.monotonic() - self._last_subscriber_at > IDLE_TTL_S):
                break
            await asyncio.sleep(POLL_INTERVAL_S)

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
                _HARD_FAILED_SESSION_IDS.add(self.session_id)
                break
            if not transport._started:
                try:
                    await transport.start()
                except Exception as exc:
                    self._hard_failed = True
                    self._last_error = str(exc)
                    _HARD_FAILED_SESSION_IDS.add(self.session_id)
                    break
            self._hard_failed = False
            _HARD_FAILED_SESSION_IDS.discard(self.session_id)
            self._last_error = None
            # Idle reaping: stop if we've had no subscribers for IDLE_TTL_S.
            if not self._subscribers and (time.monotonic() - self._last_subscriber_at > IDLE_TTL_S):
                # Stop the native transport so the provider subprocess (e.g.
                # the Codex app-server) is reaped, not left orphaned.
                try:
                    await transport.stop()
                except Exception:
                    logger.exception(
                        "native transport stop failed during idle reap for session %s",
                        self.session_id,
                    )
                break
            try:
                record = await asyncio.wait_for(transport.read_line(), timeout=POLL_INTERVAL_S)
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
                    # The persistent provider died; the in-flight turn (if
                    # any) is abandoned. Emit an error and a failed
                    # turn_completed for the active turn so the frontend
                    # never leaves it pending, then fail the session.
                    await self._fail_active_turn("native transport process exited", transport)
                    self._hard_failed = True
                    self._last_error = "native transport process exited"
                    _HARD_FAILED_SESSION_IDS.add(self.session_id)
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
                if not self._turn_completed_seen:
                    await self._fail_active_turn(
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
                # Release the active turn id so the next ``send_message`` can
                # set a new one. We do NOT clear it on ``TURN_COMPLETED``
                # (which the provider may emit at ``message_stop``, before the
                # final ``assistant`` snapshot) because trailing records of
                # the same turn must still inherit this turn's id.
                self._active_turn_id = None
                # Now that we have consumed the EOF (and all preceding records
                # of this turn), release the turn guard so a new send can
                # proceed. This must happen after ``_active_turn_id`` is
                # cleared, never before.
                transport.acknowledge_turn_complete()
                continue
            transport.maybe_capture_conversation_id(record)
            ctx = NormalizeContext(
                session_id=self.session_id,
                tab_id=session.tab_id,
                agent_type=session.agent_type,
                run_epoch=self._run_epoch,
                turn_id=self._active_turn_id,
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
                    continue
                is_turn_completed = event.type == AgentStreamEventType.TURN_COMPLETED
                if event.run_epoch is None:
                    event.run_epoch = self._run_epoch
                event = redact_event(event)
                try:
                    event = await self._store.append(event)
                except Exception:
                    logger.exception(
                        "agent_stream store append failed for session %s; dropping event",
                        self.session_id,
                    )
                    continue
                # Native transport has no backfill concept: every record is
                # live and must be fanned out to subscribers immediately.
                self._fanout(event)
                if is_turn_completed:
                    # Mark that the provider emitted a terminal completion for
                    # the active turn. At EOF we use this to decide whether a
                    # failed turn_completed must be synthesized.
                    self._turn_completed_seen = True
                    # For persistent transports (Codex app-server) there is no
                    # per-turn EOF; ``TURN_COMPLETED`` is the provider's
                    # explicit turn-end signal. Release the active turn id and
                    # the turn guard ONLY after the completion event has been
                    # persisted and fanned out, so a concurrent
                    # ``send_message`` cannot publish a new turn_started that
                    # sequences ahead of this turn's completion.
                    if transport.eof_is_fatal:
                        self._active_turn_id = None
                        transport.acknowledge_turn_complete()

    async def _poll_once(self) -> None:
        session = self._session_getter()
        if session is None:
            return

        # Agent sessions require a native transport. If it could not be
        # created, fail closed — never use the transcript as a real-time
        # source for an agent session.
        if self._native_error is not None:
            self._hard_failed = True
            self._last_error = self._native_error
            _HARD_FAILED_SESSION_IDS.add(self.session_id)
            return

        # Native transport is driven by the push consumer in ``_run_native``,
        # not by this poll loop. There is no transcript to backfill from, so
        # ``poll_once`` is a no-op for native sessions.
        if self._native_transport is not None:
            return

        path = discover_source_cached(self.adapter, session)
        if path is None:
            if self._discovery_deadline is None:
                self._discovery_deadline = time.monotonic() + DISCOVERY_GRACE_S
            elif time.monotonic() > self._discovery_deadline:
                self._hard_failed = True
                self._last_error = "structured source not found within discovery grace period"
                _HARD_FAILED_SESSION_IDS.add(self.session_id)
                invalidate_source(self.session_id)
            return
        self._discovery_deadline = None
        self._hard_failed = False
        _HARD_FAILED_SESSION_IDS.discard(self.session_id)
        self._last_error = None
        self._current_source = path
        await self._tail_file(path, session)

    async def _poll_native(self, session: ManagedSession) -> None:
        """Drain one batch of records from the native transport stdout."""
        transport = self._native_transport
        assert transport is not None
        if not transport._started:
            try:
                await transport.start()
            except Exception as exc:
                self._hard_failed = True
                self._last_error = str(exc)
                _HARD_FAILED_SESSION_IDS.add(self.session_id)
                return
        self._hard_failed = False
        _HARD_FAILED_SESSION_IDS.discard(self.session_id)
        self._last_error = None

        ctx = NormalizeContext(
            session_id=self.session_id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            run_epoch=self._run_epoch,
        )
        # Drain whatever is currently available without blocking forever.
        drained = 0
        while drained < 200:
            try:
                record = await asyncio.wait_for(transport.read_line(), timeout=0.05)
            except asyncio.TimeoutError:
                break
            if record is None:
                # EOF: the provider process exited.
                self._last_error = "native transport process exited"
                break
            drained += 1
            # Let the transport capture any provider conversation id from the
            # raw record (e.g. Claude/Cursor message_start.message.id).
            transport.maybe_capture_conversation_id(record)
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
                    self._run_epoch += 1
                event.run_epoch = self._run_epoch
                event = redact_event(event)
                try:
                    event = await self._store.append(event)
                except Exception:
                    logger.exception(
                        "agent_stream store append failed for session %s; dropping event",
                        self.session_id,
                    )
                    continue
                if self._is_live:
                    self._fanout(event)
        self._is_live = True

    async def _tail_file(self, path: Path, session: ManagedSession) -> None:
        self._current_source = path
        if not self._loaded_cursor:
            self._load_cursor(path)
            self._loaded_cursor = True
        if self.adapter.supports_snapshot(session):
            await self._tail_snapshot(path, session)
            return
        try:
            lines, new_offset, inode, rotated = await asyncio.to_thread(self._read_new_lines, path)
        except OSError:
            invalidate_source(self.session_id)
            return
        if rotated:
            logger.warning(
                "agent_stream tailer: source %s rotated for session %s; rebuilding",
                path,
                self.session_id,
            )
            self._reset_for_rotation(path)
            return
        if not lines:
            self._is_live = True
            return

        ctx = NormalizeContext(
            session_id=self.session_id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            run_epoch=self._run_epoch,
        )
        for raw in lines:
            try:
                events = self.adapter.normalize_line(raw, ctx)
            except Exception:
                logger.exception(
                    "agent_stream adapter %s failed on a line for session %s; skipping",
                    self.adapter.adapter_id,
                    self.session_id,
                )
                continue
            for event in events:
                if event.type == AgentStreamEventType.TURN_STARTED:
                    self._run_epoch += 1
                # Normalizers receive a context for the source row, but a turn
                # can span many rows. Stamp every emitted event with the
                # current epoch so assistant output remains grouped with its
                # preceding user turn.
                event.run_epoch = self._run_epoch
                event = redact_event(event)
                try:
                    event = await self._store.append(event)
                except Exception:
                    logger.exception(
                        "agent_stream store append failed for session %s; dropping event",
                        self.session_id,
                    )
                    continue
                if self._is_live:
                    self._fanout(event)
        self._offset = new_offset
        self._inode = inode
        self._is_live = True
        self._save_cursor()

    async def _tail_snapshot(self, path: Path, session: ManagedSession) -> None:
        """Reconcile one bounded authoritative whole-file snapshot.

        The event store and SSE protocol are append-only. A changed snapshot is
        therefore safe to publish only when the previously persisted source
        identities are an exact prefix of the new snapshot. In that case we
        append and fan out only the suffix. A compaction that rewrites history
        cannot be expressed safely to an already-connected client, so it
        fails closed and the UI returns to the raw terminal rather than showing
        duplicated or silently replaced turns.
        """
        try:
            snapshot = await asyncio.to_thread(self.adapter.read_snapshot, path, session)
        except Exception:
            logger.exception(
                "agent_stream snapshot read failed for session %s; failing closed",
                self.session_id,
            )
            invalidate_source(self.session_id)
            return

        if snapshot is None:
            return

        if not snapshot.records:
            # A temporary empty file can be observed while a producer rewrites
            # its checkpoint. Keep the last good cursor and retry next poll.
            if self._snapshot_source_ids:
                return
            self._snapshot_digest = snapshot.digest
            self._is_live = True
            self._save_cursor()
            return

        if snapshot.digest == self._snapshot_digest:
            self._is_live = True
            return

        source_ids = [record.source_id for record in snapshot.records]
        prior_count = len(self._snapshot_source_ids)
        if source_ids[:prior_count] == self._snapshot_source_ids:
            pass
        elif (
            prior_count
            and len(self._snapshot_source_kinds) == prior_count
            and self._snapshot_source_kinds[-1] == "turn_ended"
            and source_ids[: prior_count - 1] == self._snapshot_source_ids[: prior_count - 1]
        ):
            # Cursor writes a terminal ``turn_ended`` row, then replaces only
            # that tail marker with the next user row. The completed event was
            # already published and remains valid; treat this exact observed
            # marker replacement as the next append point. Any other rewrite
            # stays fail-closed below.
            prior_count -= 1
        else:
            self._hard_failed = True
            self._last_error = (
                "Cursor transcript rewrote previously published history; "
                "structured view safely returned to the raw terminal"
            )
            _HARD_FAILED_SESSION_IDS.add(self.session_id)
            invalidate_source(self.session_id)
            return

        new_records = snapshot.records[prior_count:]
        if not new_records:
            self._snapshot_digest = snapshot.digest
            self._is_live = True
            self._save_cursor()
            return

        ctx = NormalizeContext(
            session_id=self.session_id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            run_epoch=self._run_epoch,
        )
        run_epoch = self._run_epoch
        for record in new_records:
            try:
                record_events = self.adapter.normalize_line(record.raw, ctx)
            except Exception:
                logger.exception(
                    "agent_stream adapter %s failed on snapshot record for session %s; skipping",
                    self.adapter.adapter_id,
                    self.session_id,
                )
                continue
            for ev in record_events:
                if ev.type == AgentStreamEventType.TURN_STARTED:
                    run_epoch += 1
                ev.run_epoch = run_epoch
                ev = redact_event(ev)
                try:
                    ev = await self._store.append(ev)
                except Exception:
                    logger.exception(
                        "agent_stream store append failed for snapshot session %s; failing closed",
                        self.session_id,
                    )
                    self._hard_failed = True
                    self._last_error = (
                        "structured event persistence failed; returned to raw terminal"
                    )
                    _HARD_FAILED_SESSION_IDS.add(self.session_id)
                    return
                if self._is_live:
                    self._fanout(ev)

        self._run_epoch = run_epoch
        self._snapshot_digest = snapshot.digest
        self._snapshot_source_ids = source_ids
        self._snapshot_source_kinds = [record.source_kind for record in snapshot.records]
        self._is_live = True
        self._save_cursor()

    def _read_new_lines(self, path: Path) -> Tuple[List[Dict[str, Any]], int, int, bool]:
        st = os.stat(path)
        inode = st.st_ino
        size = st.st_size
        if self._inode is not None and inode != self._inode:
            return [], 0, inode, True
        if size < self._offset:
            return [], 0, inode, True
        if size == self._offset:
            return [], self._offset, inode, False
        with open(path, "rb") as f:
            f.seek(self._offset)
            data = f.read(size - self._offset)
        complete = data
        new_offset = self._offset + len(data)
        if data and not data.endswith(b"\n"):
            last_nl = data.rfind(b"\n")
            if last_nl == -1:
                complete = b""
                new_offset = self._offset
            else:
                complete = data[: last_nl + 1]
                new_offset = self._offset + len(complete)
        lines: List[Dict[str, Any]] = []
        for chunk in complete.split(b"\n"):
            if not chunk.strip():
                continue
            try:
                record = json.loads(chunk.decode("utf-8", errors="ignore"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                lines.append(record)
        return lines, new_offset, inode, False

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

    def _load_cursor(self, path: Path) -> None:
        try:
            data = json.loads(self._cursor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict) or data.get("path") != str(path):
            return
        inode = data.get("inode")
        try:
            st = os.stat(path)
        except OSError:
            return
        if inode is not None and inode != st.st_ino:
            return
        try:
            self._offset = int(data.get("offset", 0))
            self._run_epoch = int(data.get("run_epoch", 0))
        except (TypeError, ValueError):
            return
        self._inode = st.st_ino
        snapshot_ids = data.get("snapshot_source_ids")
        snapshot_kinds = data.get("snapshot_source_kinds")
        snapshot_digest = data.get("snapshot_digest")
        if isinstance(snapshot_ids, list) and all(isinstance(item, str) for item in snapshot_ids):
            self._snapshot_source_ids = list(snapshot_ids)
        if isinstance(snapshot_digest, str):
            self._snapshot_digest = snapshot_digest
        if isinstance(snapshot_kinds, list) and all(
            isinstance(item, str) for item in snapshot_kinds
        ):
            self._snapshot_source_kinds = list(snapshot_kinds)
        if self._offset > 0:
            self._is_live = True

    def _save_cursor(self) -> None:
        payload = {
            "path": str(self._current_source) if self._current_source else "",
            "offset": self._offset,
            "inode": self._inode,
            "run_epoch": self._run_epoch,
            "snapshot_source_ids": self._snapshot_source_ids,
            "snapshot_source_kinds": self._snapshot_source_kinds,
            "snapshot_digest": self._snapshot_digest,
        }
        try:
            self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cursor_path.with_suffix(".cursor.json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._cursor_path)
        except OSError:
            logger.exception(
                "agent_stream tailer: failed to persist cursor for session %s",
                self.session_id,
            )

    def _reset_for_rotation(self, path: Path) -> None:
        self._offset = 0
        self._inode = None
        self._run_epoch = 0
        self._snapshot_source_ids = []
        self._snapshot_source_kinds = []
        self._snapshot_digest = None
        self._is_live = False
        invalidate_source(self.session_id)


class TailerManager:
    """Process-wide registry of per-session tailers (one tailer per session)."""

    def __init__(
        self,
        session_getter: Callable[[str], Optional[ManagedSession]],
        persist_session_id: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._session_getter = session_getter
        # Optional durable persistence callback for the provider conversation
        # id. When provided, it is invoked with (session_id, conversation_id)
        # so the id survives a cold restart. When None, the id is only set on
        # the in-memory ManagedSession (used by tests).
        self._persist_session_id_cb = persist_session_id
        self._tailers: Dict[str, SessionTailer] = {}
        self._lock = asyncio.Lock()
        _TAILER_MANAGERS.add(self)

    async def _get_or_create(self, session: ManagedSession) -> SessionTailer:
        existing: Optional[SessionTailer] = None
        async with self._lock:
            tailer = self._tailers.get(session.id)
            if tailer is not None:
                existing = tailer
            else:
                from .registry import get_adapter_for_session

                adapter = get_adapter_for_session(session)
                if adapter is None:
                    raise ValueError(f"no structured adapter for agent_type={session.agent_type}")

                # Agent sessions use the native provider transport as their
                # real-time source. Terminal sessions keep the transcript-file
                # tailer.
                #
                # Fail-closed: if the native transport cannot be created for an
                # agent session, surface the error and do NOT silently fall
                # back to the transcript. The only exception is Cursor's
                # explicit ``terminal_transcript`` compatibility mode.
                native_transport: Optional[ProviderSession] = None
                native_error: Optional[str] = None
                is_agent = session.session_kind == SessionKind.AGENT
                cursor_transcript_fallback = (
                    session.agent_type == AgentType.CURSOR
                    and session.cursor_transport == "terminal_transcript"
                )

                if is_agent and not cursor_transcript_fallback:
                    try:
                        native_transport = create_native_session(
                            session,
                            conversation_id_persist=lambda cid: self._persist_session_id(
                                session.id, cid
                            ),
                        )
                    except ValueError as exc:
                        native_error = (
                            f"native transport unavailable for {session.agent_type.value}: {exc}"
                        )

                tailer = SessionTailer(
                    workspace_id=session.workspace_id,
                    session_id=session.id,
                    adapter=adapter,
                    session_getter=lambda: self._session_getter(session.id),
                    native_transport=native_transport,
                    native_error=native_error,
                )
                self._tailers[session.id] = tailer
        if existing is not None:
            if existing.hard_failed:
                await existing.poll_once()
                if existing.hard_failed:
                    raise StructuredSourceUnavailable(
                        existing.last_error or "structured transcript source unavailable"
                    )
                await existing.start()
            return existing
        try:
            await tailer.poll_once()
        except Exception:
            logger.exception("agent_stream: first-touch backfill failed for session %s", session.id)
        await tailer.start()
        return tailer

    async def subscribe(self, session: ManagedSession) -> "asyncio.Queue[AgentStreamEvent]":
        tailer = await self._get_or_create(session)
        return await tailer.subscribe()

    async def send_message(
        self,
        session: ManagedSession,
        text: str,
        images: List[bytes],
        client_turn_id: str,
    ) -> None:
        """Atomically deliver a user turn (text + images) for ``session``.

        The composer's single input path for AGENT sessions. Delegates to the
        tailer's native transport so the same ``ProviderSession`` owns both
        the stream and the turn. ``client_turn_id`` is the frontend-generated
        stable turn id; the tailer publishes an authoritative ``turn_started``
        with it before the provider runs.
        """
        tailer = await self._get_or_create(session)
        await tailer.send_message(text, images, client_turn_id)

    async def ensure_started(self, session: ManagedSession) -> SessionTailer:
        return await self._get_or_create(session)

    def get_tailer(self, session_id: str) -> Optional[SessionTailer]:
        return self._tailers.get(session_id)

    def unsubscribe(self, session_id: str, queue: "asyncio.Queue[AgentStreamEvent]") -> None:
        tailer = self._tailers.get(session_id)
        if tailer is not None:
            tailer.unsubscribe(queue)

    def hard_failed(self, session_id: str) -> bool:
        tailer = self._tailers.get(session_id)
        return tailer is not None and tailer.hard_failed

    def is_structured_available(self, session: ManagedSession) -> bool:
        from .registry import get_adapter_for_session

        if get_adapter_for_session(session) is None:
            return False
        tailer = self._tailers.get(session.id)
        return tailer is None or not tailer.hard_failed

    def get_store(self, workspace_id: str, session_id: str) -> AgentStreamStore:
        return AgentStreamStore(workspace_id, session_id)

    def _persist_session_id(self, session_id: str, conversation_id: str) -> None:
        """Persist the provider conversation id back to the managed session.

        This lets a cold restart resume the same provider conversation via
        ``--resume`` (Claude/Cursor) or ``thread/resume`` (Codex).

        This is only invoked after the provider has emitted the conversation id
        in a system/init record, so ``agent_session_id_verified`` is set to
        True alongside the id. A cold restart seeds ``_conversation_id_verified``
        from this flag, never from the mere presence of a UUID.

        If a durable persistence callback was supplied at construction, it is
        invoked so the id is written to disk (ttyd tab state for direct tabs,
        workspace session state for workspace sessions). Otherwise the id is
        set on the in-memory ManagedSession only (test mode).
        """
        session = self._session_getter(session_id)
        if session is None:
            return
        try:
            session.agent_session_id = conversation_id
            session.agent_session_id_verified = True
        except Exception:
            logger.exception(
                "failed to set conversation id %s on session %s",
                conversation_id,
                session_id,
            )
            return
        if self._persist_session_id_cb is not None:
            try:
                self._persist_session_id_cb(session_id, conversation_id)
            except Exception:
                logger.exception(
                    "failed to durably persist conversation id %s for session %s",
                    conversation_id,
                    session_id,
                )

    async def forget_session(self, session_id: str) -> None:
        """Stop and discard an in-process tailer for a deleted session."""
        async with self._lock:
            tailer = self._tailers.pop(session_id, None)
        if tailer is not None:
            await tailer.stop()
        _HARD_FAILED_SESSION_IDS.discard(session_id)
        invalidate_source(session_id)

    async def stop_all(self) -> None:
        async with self._lock:
            for tailer in list(self._tailers.values()):
                await tailer.stop()
                _HARD_FAILED_SESSION_IDS.discard(tailer.session_id)
            self._tailers.clear()


async def discard_session_stream(workspace_id: str, session_id: str) -> None:
    """Purge all structured state when a managed session is deleted.

    The workspace allocator may reuse a friendly session id (for example,
    ``prefix-agent-1``). Both on-disk events and an in-process tailer therefore
    must be forgotten before that id can identify another conversation.
    """
    for manager in list(_TAILER_MANAGERS):
        await manager.forget_session(session_id)
    await AgentStreamStore(workspace_id, session_id).clear()
