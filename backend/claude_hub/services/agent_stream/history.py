"""Derived, incremental indexes for question/answer history and lazy turn reads.

The owning AgentStreamStore serializes these operations with its read lock.
Only visible event bodies are retained; process details stay in the JSONL file.
"""

from __future__ import annotations

import os
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, Tuple

from ...models import (
    AgentStreamEvent,
    AgentStreamEventPage,
    AgentStreamEventType,
    AgentStreamHistoryDetail,
)

_PROCESS_TYPES = frozenset(
    {
        AgentStreamEventType.THINKING_DELTA,
        AgentStreamEventType.TOOL_CALL_STARTED,
        AgentStreamEventType.TOOL_CALL_COMPLETED,
    }
)
_CONTROL_TYPES = frozenset(
    {
        AgentStreamEventType.TURN_STARTED,
        AgentStreamEventType.TURN_COMPLETED,
        AgentStreamEventType.APPROVAL_REQUIRED,
        AgentStreamEventType.APPROVAL_RESOLVED,
        AgentStreamEventType.ERROR,
    }
)


_REPLAY_IDENTITY_TYPES = frozenset(
    {
        AgentStreamEventType.TOOL_CALL_STARTED,
        AgentStreamEventType.TOOL_CALL_COMPLETED,
        AgentStreamEventType.APPROVAL_REQUIRED,
        AgentStreamEventType.APPROVAL_RESOLVED,
    }
)


def replay_tool_identity(event: AgentStreamEvent) -> Optional[str]:
    if event.type not in _REPLAY_IDENTITY_TYPES:
        return None
    identity = event.payload.get("tool_call_id")
    if isinstance(identity, str) and identity:
        return identity
    return event.call_id if event.call_id else None


def conversation_event_visible(event: AgentStreamEvent) -> bool:
    """Keep legacy events intact when no stable turn identity exists."""
    if not event.turn_id:
        return True
    if event.type in _CONTROL_TYPES:
        return True
    if event.type == AgentStreamEventType.STATUS and event.payload.get("subagent_status") in (
        "failed",
        "cancelled",
    ):
        return True
    if event.type in _PROCESS_TYPES:
        return False
    thread = event.payload.get("subagent_thread")
    return not (isinstance(thread, str) and thread.strip())


@dataclass(frozen=True, slots=True)
class _HistoryRow:
    sequence: int
    offset: int
    length: int
    turn_id: Optional[str]
    visible: Optional[AgentStreamEvent]
    attributed_tool_id: Optional[str]


@dataclass
class _TurnRows:
    indices: List[int] = field(default_factory=list)
    sequences: List[int] = field(default_factory=list)
    first_hidden: Optional[int] = None


class ConversationHistoryIndex:
    """Per-store cache, invalidated with the durable store's lifecycle."""

    def __init__(self) -> None:
        self.rows: List[_HistoryRow] = []
        self.sequences: List[int] = []
        self.turns: Dict[str, _TurnRows] = {}
        self._attributed_tool_ids: set[str] = set()
        self._offset = 0
        self._identity: Optional[Tuple[int, int]] = None
        self._size = 0
        self._mtime_ns = 0

    def reset(self) -> None:
        self.rows.clear()
        self.sequences.clear()
        self.turns.clear()
        self._attributed_tool_ids.clear()
        self._offset = 0
        self._identity = None
        self._size = 0
        self._mtime_ns = 0

    def _index_line(self, line: bytes, offset: int) -> None:
        try:
            event = AgentStreamEvent.model_validate_json(line)
        except ValueError:
            return
        # The durable writer assigns increasing sequences. A corrupt duplicate
        # cannot be allowed to make the binary-search indexes non-monotonic.
        sequence = event.stream_sequence
        if sequence < 0 or (self.sequences and sequence <= self.sequences[-1]):
            return
        identity = replay_tool_identity(event)
        # Match the existing timeline's ordered replay guard. A legacy record
        # before the first stable attribution remains visible, even if this id
        # is attributed later in the same page or file.
        replay = not event.turn_id and identity in self._attributed_tool_ids
        visible = conversation_event_visible(event) and not replay
        attributed = identity if event.turn_id else None
        if attributed is not None:
            self._attributed_tool_ids.add(attributed)
        row = _HistoryRow(
            sequence, offset, len(line), event.turn_id, event if visible else None, attributed
        )
        row_index = len(self.rows)
        self.rows.append(row)
        self.sequences.append(sequence)
        if event.turn_id:
            turn = self.turns.setdefault(event.turn_id, _TurnRows())
            turn.indices.append(row_index)
            turn.sequences.append(sequence)
            if not visible and turn.first_hidden is None:
                turn.first_hidden = sequence

    def _refresh(self, handle: BinaryIO) -> None:
        stat = os.fstat(handle.fileno())
        identity = (stat.st_dev, stat.st_ino)
        if (
            self._identity not in (None, identity)
            or stat.st_size < self._offset
            or (stat.st_size == self._size and stat.st_mtime_ns != self._mtime_ns)
        ):
            self.reset()
        self._identity = identity
        handle.seek(self._offset)
        # Bound this scan to the observed file size, even while a provider is
        # appending continuously. An incomplete trailing write is retried later.
        while self._offset < stat.st_size:
            line = handle.readline(stat.st_size - self._offset)
            if not line or not line.endswith(b"\n"):
                break
            self._index_line(line, self._offset)
            self._offset += len(line)
        self._size = stat.st_size
        self._mtime_ns = stat.st_mtime_ns

    def _detail(self, turn_id: str, through_sequence: int) -> Optional[AgentStreamHistoryDetail]:
        turn = self.turns[turn_id]
        if turn.first_hidden is None or turn.first_hidden > through_sequence:
            return None
        end = bisect_right(turn.sequences, through_sequence)
        return AgentStreamHistoryDetail(
            turn_id=turn_id,
            first_sequence=turn.sequences[0],
            last_sequence=turn.sequences[end - 1],
            event_count=end,
        )

    def conversation_page(self, path: Path, since: int, limit: int) -> AgentStreamEventPage:
        try:
            with path.open("rb") as handle:
                self._refresh(handle)
        except FileNotFoundError:
            self.reset()
        start = bisect_right(self.sequences, since)
        events: List[AgentStreamEvent] = []
        touched: Dict[str, None] = {}
        attributed: Dict[str, None] = {}
        next_sequence = since
        end = start
        # Bound visible events, distinct turns, and replay identities separately.
        # A long omitted tool run costs index iteration, not JSON parsing or I/O.
        while end < len(self.rows):
            row = self.rows[end]
            if (
                len(events) >= limit
                or (
                    row.turn_id is not None and row.turn_id not in touched and len(touched) >= limit
                )
                or (
                    row.attributed_tool_id is not None
                    and row.attributed_tool_id not in attributed
                    and len(attributed) >= limit
                )
            ):
                break
            if row.visible is not None:
                events.append(row.visible)
            if row.turn_id:
                touched[row.turn_id] = None
            if row.attributed_tool_id is not None:
                attributed[row.attributed_tool_id] = None
            next_sequence = row.sequence
            end += 1
        details = [
            detail
            for turn_id in touched
            if (detail := self._detail(turn_id, next_sequence)) is not None
        ]
        return AgentStreamEventPage(
            events=events,
            next_sequence=next_sequence,
            has_more=end < len(self.rows),
            history_details=details,
            attributed_tool_ids=list(attributed),
        )

    def turn_page(
        self, path: Path, turn_id: str, since: int, through: int, limit: int
    ) -> AgentStreamEventPage:
        try:
            with path.open("rb") as handle:
                self._refresh(handle)
                turn = self.turns.get(turn_id)
                if turn is None:
                    return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)
                start = bisect_right(turn.sequences, since)
                end = bisect_right(turn.sequences, through)
                selected_end = min(start + limit, end)
                events: List[AgentStreamEvent] = []
                for row_index in turn.indices[start:selected_end]:
                    row = self.rows[row_index]
                    handle.seek(row.offset)
                    line = handle.read(row.length)
                    event = AgentStreamEvent.model_validate_json(line)
                    events.append(event)
                next_sequence = turn.sequences[selected_end - 1] if selected_end > start else since
                return AgentStreamEventPage(
                    events=events,
                    next_sequence=next_sequence,
                    has_more=selected_end < end,
                )
        except FileNotFoundError:
            self.reset()
            return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)
