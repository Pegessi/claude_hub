"""Append-only per-session JSONL event store.

Each managed session gets one ``{session_id}.jsonl`` file under
``STATE_ROOT / workspace_id / agent_streams /``. Events are assigned a
monotonic ``stream_sequence`` (session-local) on append; reads are paged by
``since_sequence`` (exclusive).

``STATE_ROOT`` is read at call time so tests can monkeypatch it.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...models import AgentStreamEvent, AgentStreamEventPage

_READ_INDEX_STRIDE = 256


def _state_root() -> Path:
    """Resolve STATE_ROOT from workspace_manager at call time."""
    wm = importlib.import_module("claude_hub.services.workspace_manager")
    return Path(wm.STATE_ROOT)


class AgentStreamStore:
    """Append-only JSONL store for one managed session's structured events."""

    def __init__(self, workspace_id: str, session_id: str) -> None:
        self.workspace_id = workspace_id
        self.session_id = session_id
        self._dir = _state_root() / workspace_id / "agent_streams"
        self._path = self._dir / f"{session_id}.jsonl"
        self._next_seq: Optional[int] = None
        self._lock = asyncio.Lock()
        # Replayed history is commonly read through many consecutive pages.
        # Keep a sparse in-memory sequence -> text-stream offset index so page
        # N resumes near its cursor instead of parsing pages 0..N-1 again.
        # The index is process-local and rebuilt lazily after a restart.
        self._read_lock = asyncio.Lock()
        self._read_offsets: Dict[int, int] = {-1: 0}
        self._read_inode: Optional[int] = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def cursor_path(self) -> Path:
        """Path for the paired tailer cursor checkpoint."""
        return self._path.with_name(self._path.stem + ".cursor.json")

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def _recover_next_seq(self) -> int:
        """Scan the file for the max stream_sequence and return max + 1."""
        if not self._path.exists():
            return 0
        max_seq = -1
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        seq = obj.get("stream_sequence")
                        if isinstance(seq, int) and seq > max_seq:
                            max_seq = seq
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            return 0
        return max_seq + 1

    async def append(self, event: AgentStreamEvent) -> AgentStreamEvent:
        """Assign a monotonic sequence and append one event.

        Returns the event with ``stream_sequence`` set.
        """
        async with self._lock:
            if self._next_seq is None:
                self._next_seq = self._recover_next_seq()
            seq = self._next_seq
            event = event.model_copy(update={"stream_sequence": seq})
            self._ensure_dir()
            await asyncio.to_thread(self._write_line, event)
            # Only advance the sequence counter after a successful write so a
            # failed append does not leave a permanent gap in the stream.
            self._next_seq = seq + 1
            return event

    async def clear(self) -> None:
        """Remove this session's event log and cursor checkpoint.

        Managed-session identifiers can be reused after an explicit delete, so
        retaining either file would leak an old conversation into a new tab.
        Missing files are an expected no-op.
        """
        async with self._lock:
            async with self._read_lock:
                await asyncio.to_thread(self._unlink_session_files)
                self._next_seq = None
                self._reset_read_index()

    def _unlink_session_files(self) -> None:
        for path in (self._path, self.cursor_path):
            try:
                path.unlink()
            except FileNotFoundError:
                continue

    def _write_line(self, event: AgentStreamEvent) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")

    async def replace_all(self, events: List[AgentStreamEvent]) -> None:
        """Replace the entire store with ``events`` (snapshot sources)."""
        async with self._lock:
            async with self._read_lock:
                self._ensure_dir()
                lines: List[str] = []
                for i, ev in enumerate(events):
                    ev = ev.model_copy(update={"stream_sequence": i})
                    lines.append(ev.model_dump_json())
                await asyncio.to_thread(self._write_all, lines)
                self._next_seq = len(events)
                self._reset_read_index()

    def _write_all(self, lines: List[str]) -> None:
        with self._path.open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")

    def _reset_read_index(self) -> None:
        self._read_offsets = {-1: 0}
        self._read_inode = None

    def _prepare_read_index(self) -> None:
        """Invalidate cached offsets if the append-only file was replaced."""

        try:
            stat = self._path.stat()
        except OSError:
            self._reset_read_index()
            return
        furthest_offset = max(self._read_offsets.values(), default=0)
        if self._read_inode not in (None, stat.st_ino) or stat.st_size < furthest_offset:
            self._reset_read_index()
        self._read_inode = stat.st_ino

    def _nearest_read_offset(self, since_sequence: int) -> Tuple[int, int]:
        checkpoint = max(
            (seq for seq in self._read_offsets if seq <= since_sequence),
            default=-1,
        )
        return checkpoint, self._read_offsets[checkpoint]

    def _remember_read_offset(self, sequence: int, offset: int, *, force: bool = False) -> None:
        if sequence < 0:
            return
        if force or sequence % _READ_INDEX_STRIDE == 0:
            self._read_offsets[sequence] = offset

    async def read_since(self, since_sequence: int = -1, limit: int = 500) -> AgentStreamEventPage:
        """Return events with ``stream_sequence > since_sequence``.

        ``since_sequence=-1`` returns from the beginning.
        """
        if limit < 1:
            limit = 1
        async with self._read_lock:
            events: List[AgentStreamEvent] = []
            next_seq = since_sequence
            has_more = False

            def _read() -> None:
                nonlocal next_seq, has_more
                if not self._path.exists():
                    return
                self._prepare_read_index()
                _, start_offset = self._nearest_read_offset(since_sequence)
                next_offset = start_offset
                with self._path.open("r", encoding="utf-8") as f:
                    f.seek(start_offset)
                    while True:
                        line = f.readline()
                        if not line:
                            break
                        after_offset = f.tell()
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            seq = obj.get("stream_sequence")
                            if not isinstance(seq, int):
                                continue
                            self._remember_read_offset(seq, after_offset)
                            if seq <= since_sequence:
                                continue
                            if len(events) >= limit:
                                has_more = True
                                break
                            events.append(AgentStreamEvent.model_validate(obj))
                            if seq > next_seq:
                                next_seq = seq
                                next_offset = after_offset
                        except (json.JSONDecodeError, ValueError):
                            continue
                self._remember_read_offset(next_seq, next_offset, force=True)

            await asyncio.to_thread(_read)
            return AgentStreamEventPage(
                events=events,
                next_sequence=next_seq,
                has_more=has_more,
            )

    async def count(self) -> int:
        """Return the number of persisted events."""
        if not self._path.exists():
            return 0
        n = 0
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        n += 1
        except OSError:
            return 0
        return n

    async def latest_unfinished_turn(self) -> Optional[AgentStreamEvent]:
        """Return the latest turn whose durable lifecycle has no terminal edge.

        Native Chat runtime state is process-local, while this event log
        survives backend reloads and crashes. A previous process can therefore
        leave ``turn_started`` as the latest lifecycle edge even though the new
        process has no provider turn to cancel. The Stop path uses this
        bounded-purpose scan to repair that orphan on demand.

        ``error`` is terminal here because the frontend uses the same rule to
        unlock its composer when a provider fails without a following
        ``turn_completed`` event.
        """
        async with self._read_lock:
            candidate: Optional[AgentStreamEvent] = None

            def _read() -> None:
                nonlocal candidate
                if not self._path.exists():
                    return
                try:
                    with self._path.open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except (json.JSONDecodeError, ValueError):
                                continue
                            event_type = obj.get("type")
                            if event_type == "turn_started":
                                try:
                                    candidate = AgentStreamEvent.model_validate(obj)
                                except ValueError:
                                    candidate = None
                                continue
                            if candidate is None or event_type not in {
                                "turn_completed",
                                "error",
                            }:
                                continue
                            candidate_turn_id = candidate.turn_id
                            same_turn = (
                                obj.get("turn_id") == candidate_turn_id
                                if candidate_turn_id is not None
                                else obj.get("run_epoch") == candidate.run_epoch
                            )
                            if same_turn:
                                candidate = None
                except OSError:
                    candidate = None

            await asyncio.to_thread(_read)
            return candidate

    async def last_event_at(self) -> Optional[str]:
        """Return the ``created_at`` of the last persisted event, or ``None``."""
        if not self._path.exists():
            return None
        last: Optional[str] = None
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        ca = obj.get("created_at")
                        if isinstance(ca, str):
                            last = ca
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            return None
        return last

    async def truncate_before(self, sequence: int) -> int:
        """Remove all events with ``stream_sequence >= sequence``.

        Used by edit-resend to discard the event suffix from the edited
        turn onward.  Returns the number of events removed.
        """
        async with self._lock:
            async with self._read_lock:
                if not self._path.exists():
                    self._next_seq = 0
                    return 0

                kept: List[str] = []
                removed = 0

                def _read() -> None:
                    nonlocal removed
                    with self._path.open("r", encoding="utf-8") as f:
                        for line in f:
                            stripped = line.strip()
                            if not stripped:
                                continue
                            try:
                                obj = json.loads(stripped)
                                seq = obj.get("stream_sequence")
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if isinstance(seq, int) and seq >= sequence:
                                removed += 1
                                continue
                            kept.append(stripped)

                await asyncio.to_thread(_read)
                await asyncio.to_thread(self._write_all, kept)
                self._next_seq = len(kept)
                self._reset_read_index()
                return removed

    async def snapshot(self) -> Optional[Path]:
        """Create a ``.edit-bak`` sidecar copy of the event store for rollback.

        Used by edit-resend to capture the pre-edit state so a failed
        truncate/fork/send can be fully undone.  Returns the backup path, or
        ``None`` if the store file does not exist (nothing to snapshot).
        """
        if not self._path.exists():
            return None
        backup = self._path.with_name(self._path.name + ".edit-bak")
        await asyncio.to_thread(shutil.copy2, self._path, backup)
        return backup

    async def restore(self, backup: Optional[Path]) -> None:
        """Restore the event store from a snapshot created by :meth:`snapshot`.

        After restoring, the in-memory sequence counter and read index are
        reset so subsequent appends/reads observe the restored content rather
        than stale cached state.
        """
        if backup is None or not backup.exists():
            return
        await asyncio.to_thread(shutil.copy2, backup, self._path)
        self._next_seq = None
        self._reset_read_index()

    async def find_turn(self, turn_id: str) -> Optional[Tuple[int, int, str, List[Dict[str, Any]]]]:
        """Locate a turn by its ``turn_id``.

        Returns ``(stream_sequence, turn_index, turn_text, attachments)`` where:

        - ``stream_sequence`` is the ``stream_sequence`` of the matching
          ``turn_started`` event (the truncation point for the Hub store).
        - ``turn_index`` is the 0-based count of ``turn_started`` events up to
          and including the matching one.
        - ``turn_text`` is the ``payload.summary`` of the ``turn_started``
          event (the user's message text, possibly redacted).  It is used to
          match the Hub turn to its provider user message by content rather
          than by ordinal count, so a failed delivery cannot shift the
          mapping onto the wrong provider message.
        - ``attachments`` is the ``payload.attachments`` list of the
          ``turn_started`` event (opaque attachment metadata: id, mime_type,
          bytes, width, height).  Edit-resend uses it to re-reference the
          original turn's image attachments in the new turn so they are not
          silently dropped.

        Returns ``None`` if no ``turn_started`` event carries ``turn_id``.
        """
        if not self._path.exists():
            return None
        result: Optional[Tuple[int, int, str, List[Dict[str, Any]]]] = None

        def _read() -> None:
            nonlocal result
            turn_count = 0
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if obj.get("type") != "turn_started":
                        continue
                    turn_count += 1
                    if obj.get("turn_id") == turn_id:
                        seq = obj.get("stream_sequence")
                        if isinstance(seq, int):
                            text = ""
                            attachments: List[Dict[str, Any]] = []
                            payload = obj.get("payload")
                            if isinstance(payload, dict):
                                summary = payload.get("summary")
                                if isinstance(summary, str):
                                    text = summary
                                atts = payload.get("attachments")
                                if isinstance(atts, list):
                                    attachments = [a for a in atts if isinstance(a, dict)]
                            result = (seq, turn_count - 1, text, attachments)
                        return

        await asyncio.to_thread(_read)
        return result
