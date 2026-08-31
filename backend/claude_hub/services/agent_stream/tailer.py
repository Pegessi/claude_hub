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
  offset, run_epoch)`` so a tailer restarted after idle (or after a backend
  restart) resumes without re-processing or duplicating already-persisted
  events. Written atomically (tmp + rename).
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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ...models import AgentStreamEvent, AgentStreamEventType, ManagedSession
from .base import (
    AgentStreamAdapter,
    NormalizeContext,
    discover_source_cached,
    invalidate_source,
)
from .redaction import redact_event
from .store import AgentStreamStore

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.0
IDLE_TTL_S = 300.0
DISCOVERY_GRACE_S = 30.0
SUBSCRIBER_QUEUE_MAX = 2000
_STOP_JOIN_TIMEOUT_S = 5.0

_HARD_FAILED_SESSION_IDS: Set[str] = set()


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
    ) -> None:
        self.workspace_id = workspace_id
        self.session_id = session_id
        self.adapter = adapter
        self._session_getter = session_getter
        self._store = store or AgentStreamStore(workspace_id, session_id)
        self._cursor_path = self._store.path.with_name(self._store.path.stem + ".cursor.json")

        self._offset = 0
        self._inode: Optional[int] = None
        self._run_epoch = 0
        self._loaded_cursor = False
        self._snapshot_source_ids: List[str] = []
        self._snapshot_digest: Optional[str] = None
        self._is_live = False

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

    async def _run(self) -> None:
        try:
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
                if not self._subscribers and (
                    time.monotonic() - self._last_subscriber_at > IDLE_TTL_S
                ):
                    break
                await asyncio.sleep(POLL_INTERVAL_S)
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._stopped = True

    async def _poll_once(self) -> None:
        session = self._session_getter()
        if session is None:
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

    async def _tail_file(self, path: Path, session: ManagedSession) -> None:
        self._current_source = path
        if not self._loaded_cursor:
            self._load_cursor(path)
            self._loaded_cursor = True
        if self.adapter.supports_snapshot():
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
                    event.run_epoch = self._run_epoch
                event = redact_event(event)
                try:
                    await self._store.append(event)
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

        Append-only growth persists only the new suffix. Any divergence
        rebuilds the normalized replay store atomically.
        """
        snapshot = await asyncio.to_thread(self.adapter.read_snapshot, session)
        # For this wave, snapshot sources (Cursor) are fail-closed, so this
        # path is a no-op placeholder. The interface is kept for future use.
        if not snapshot:
            self._is_live = True
            return
        self._is_live = True

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
        snapshot_digest = data.get("snapshot_digest")
        if isinstance(snapshot_ids, list) and all(isinstance(item, str) for item in snapshot_ids):
            self._snapshot_source_ids = list(snapshot_ids)
        if isinstance(snapshot_digest, str):
            self._snapshot_digest = snapshot_digest
        if self._offset > 0:
            self._is_live = True

    def _save_cursor(self) -> None:
        payload = {
            "path": str(self._current_source) if self._current_source else "",
            "offset": self._offset,
            "inode": self._inode,
            "run_epoch": self._run_epoch,
            "snapshot_source_ids": self._snapshot_source_ids,
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
        self._snapshot_digest = None
        self._is_live = False
        invalidate_source(self.session_id)


class TailerManager:
    """Process-wide registry of per-session tailers (one tailer per session)."""

    def __init__(self, session_getter: Callable[[str], Optional[ManagedSession]]) -> None:
        self._session_getter = session_getter
        self._tailers: Dict[str, SessionTailer] = {}
        self._lock = asyncio.Lock()

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
                tailer = SessionTailer(
                    workspace_id=session.workspace_id,
                    session_id=session.id,
                    adapter=adapter,
                    session_getter=lambda: self._session_getter(session.id),
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

    async def stop_all(self) -> None:
        async with self._lock:
            for tailer in list(self._tailers.values()):
                await tailer.stop()
                _HARD_FAILED_SESSION_IDS.discard(tailer.session_id)
            self._tailers.clear()
