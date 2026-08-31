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
from pathlib import Path
from typing import List, Optional

from ...models import AgentStreamEvent, AgentStreamEventPage


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

    @property
    def path(self) -> Path:
        return self._path

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
            self._next_seq += 1
            event = event.model_copy(update={"stream_sequence": seq})
            self._ensure_dir()
            await asyncio.to_thread(self._write_line, event)
            return event

    def _write_line(self, event: AgentStreamEvent) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")

    async def replace_all(self, events: List[AgentStreamEvent]) -> None:
        """Replace the entire store with ``events`` (snapshot sources)."""
        async with self._lock:
            self._ensure_dir()
            lines: List[str] = []
            for i, ev in enumerate(events):
                ev = ev.model_copy(update={"stream_sequence": i})
                lines.append(ev.model_dump_json())
            await asyncio.to_thread(self._write_all, lines)
            self._next_seq = len(events)

    def _write_all(self, lines: List[str]) -> None:
        with self._path.open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")

    async def read_since(self, since_sequence: int = -1, limit: int = 500) -> AgentStreamEventPage:
        """Return events with ``stream_sequence > since_sequence``.

        ``since_sequence=-1`` returns from the beginning.
        """
        if limit < 1:
            limit = 1
        events: List[AgentStreamEvent] = []
        next_seq = since_sequence
        has_more = False

        def _read() -> None:
            nonlocal next_seq, has_more
            if not self._path.exists():
                return
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        seq = obj.get("stream_sequence")
                        if not isinstance(seq, int) or seq <= since_sequence:
                            continue
                        if len(events) >= limit:
                            has_more = True
                            break
                        events.append(AgentStreamEvent.model_validate(obj))
                        if seq > next_seq:
                            next_seq = seq
                    except (json.JSONDecodeError, ValueError):
                        continue

        await asyncio.to_thread(_read)
        return AgentStreamEventPage(events=events, next_sequence=next_seq, has_more=has_more)

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
