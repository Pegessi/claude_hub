"""Indexed SQLite event persistence, with one-time legacy JSONL import.

Sequences remain stable through lossless compression and raw-history paging.
STATE_ROOT is resolved at call time to preserve isolated runtime ownership.
"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from itertools import groupby
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, TypeVar

from ...models import AgentStreamEvent, AgentStreamEventPage, AgentStreamEventType
from ..local_storage import (
    connection,
    delete_document,
    document_exists,
    pack,
    read_document,
    storage_location,
    unpack,
    write_document,
)
from .history import (
    ConversationHistoryIndex,
    conversation_event_visible,
    replay_tool_identity,
)

_COMPACTABLE_HISTORY_TYPES = frozenset(
    {AgentStreamEventType.TEXT_DELTA, AgentStreamEventType.THINKING_DELTA}
)
_HISTORY_CHUNK_COUNT_KEY = "_history_chunk_count"
_IOResult = TypeVar("_IOResult")


async def _complete_io(operation: Callable[..., _IOResult], *args: Any) -> _IOResult:
    """Join disk work before releasing its owner's locks, even on cancellation.

    Cancellation cannot stop a thread. Keep the caller alive until the operation
    settles so lifecycle teardown cannot clear/reuse its path while it can still
    write. A disk error takes precedence over cancellation; callers invalidate
    derived state on either outcome before releasing their locks.
    """
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    cancellation: Optional[asyncio.CancelledError] = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


class TurnLifecycleState(str, Enum):
    """Durable lifecycle state for one stable Hub turn id."""

    NOT_STARTED = "not_started"
    UNFINISHED = "unfinished"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class TurnLifecycleSnapshot:
    """One consistent scan of an expected turn and the latest orphan."""

    state: TurnLifecycleState
    latest_unfinished: Optional[AgentStreamEvent]


def compact_history_events(events: List[AgentStreamEvent]) -> List[AgentStreamEvent]:
    """Collapse adjacent historical text deltas without changing semantics.

    Live delivery is already coalesced, but older logs created before that
    optimization can contain thousands of tiny deltas.  Returning those rows
    individually makes Chat cold-start pay for thousands of JSON objects and
    reducer iterations even though adjacent rows render as one text part.

    Keep the first event's identity and concatenate only the same fields used
    by the live coalescer.  The page cursor remains the raw durable cursor, so
    callers can resume live delivery without rewriting the append-only log.
    """

    def payload_text(event: AgentStreamEvent) -> str:
        value = event.payload.get("text", "")
        return value if isinstance(value, str) else ""

    def history_chunk_count(event: AgentStreamEvent) -> int:
        value = event.payload.get(_HISTORY_CHUNK_COUNT_KEY, 1)
        return value if type(value) is int and value > 0 else 1

    compacted: List[AgentStreamEvent] = []
    for event in events:
        previous = compacted[-1] if compacted else None
        if (
            previous is not None
            and event.type in _COMPACTABLE_HISTORY_TYPES
            and previous.type == event.type
            and previous.turn_id == event.turn_id
            and previous.message_id == event.message_id
            and previous.run_epoch == event.run_epoch
            and previous.payload.get("plan") == event.payload.get("plan")
            and previous.payload.get("plan_kind") == event.payload.get("plan_kind")
            and previous.payload.get("snapshot") is not True
            and event.payload.get("snapshot") is not True
        ):
            payload = dict(previous.payload)
            payload["text"] = payload_text(previous) + payload_text(event)
            payload[_HISTORY_CHUNK_COUNT_KEY] = history_chunk_count(previous) + history_chunk_count(
                event
            )
            compacted[-1] = previous.model_copy(update={"payload": payload})
        else:
            compacted.append(event)
    return compacted


def _state_root() -> Path:
    """Resolve STATE_ROOT from workspace_manager at call time."""
    wm = importlib.import_module("claude_hub.services.workspace_manager")
    return Path(wm.STATE_ROOT)


class AgentStreamStore:
    """Indexed, losslessly compressed SQLite events with legacy JSONL import."""

    def __init__(self, workspace_id: str, session_id: str) -> None:
        self.workspace_id = workspace_id
        self.session_id = session_id
        self._dir = _state_root() / workspace_id / "agent_streams"
        self._path = self._dir / f"{session_id}.jsonl"
        self._lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._history = ConversationHistoryIndex()
        self._dictionary = b""

    @property
    def path(self) -> Path:
        # Logical identity also anchors isolated cleanup and legacy cursors.
        return self._path

    @property
    def cursor_path(self) -> Path:
        return self._path.with_name(self._path.stem + ".cursor.json")

    def _insert(self, db: Any, name: int, event: AgentStreamEvent) -> None:
        identity = replay_tool_identity(event)
        replay = (
            not event.turn_id
            and identity
            and db.execute(
                "SELECT 1 FROM stream_events WHERE name=? AND identity=? AND turn_id IS NOT NULL LIMIT 1",
                (name, identity),
            ).fetchone()
        )
        db.execute(
            "INSERT INTO stream_events VALUES (?,?,?,?,?,?,?)",
            (
                name,
                event.stream_sequence,
                event.turn_id,
                event.type.value,
                int(conversation_event_visible(event) and not replay),
                identity,
                pack(
                    event.model_dump(mode="json", exclude_defaults=True, exclude_none=True),
                    self._dictionary,
                ),
            ),
        )

    def _database(self) -> Any:
        return connection(self._path)

    def _import(self, db: Any) -> int:
        _, logical_name = storage_location(self._path)
        row = db.execute(
            "SELECT id,dictionary FROM streams WHERE name=?", (logical_name,)
        ).fetchone()
        if row:
            self._dictionary = row[1]
            return int(row[0])
        self._dictionary = self.session_id.encode()
        if self._path.exists():
            with self._path.open("rb") as source:
                self._dictionary = source.read(32768)
        name = int(
            db.execute(
                "INSERT INTO streams(name,dictionary) VALUES (?,?)",
                (logical_name, self._dictionary),
            ).lastrowid
        )
        # Caller owns BEGIN IMMEDIATE; events, malformed evidence and completion
        # marker commit together. Sources are untouched for recovery/export.
        if self._path.exists():
            with self._path.open("rb") as handle:
                for number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        event = AgentStreamEvent.model_validate_json(line)
                        if event.stream_sequence < 0:
                            raise ValueError("missing durable stream sequence")
                        self._insert(db, name, event)
                    except (ValueError, __import__("sqlite3").IntegrityError):
                        db.execute(
                            "INSERT INTO stream_errors VALUES (?,?,?)",
                            (name, number, line),
                        )
        return name

    @contextmanager
    def _read_database(self) -> Iterator[Tuple[Any, int]]:
        """Use WAL read snapshots; reserve the writer only for first import."""
        _, logical_name = storage_location(self._path)
        with self._database() as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT id,dictionary FROM streams WHERE name=?", (logical_name,)
            ).fetchone()
            if row is None:
                db.rollback()
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    name = self._import(db)
                db.execute("BEGIN")
            else:
                name = int(row[0])
                self._dictionary = row[1]
            yield db, name

    def iter_events(
        self,
        since: int = -1,
        limit: int | None = None,
        turn_id: str | None = None,
        through: int | None = None,
        kind: str | None = None,
    ) -> Iterator[AgentStreamEvent]:
        with self._read_database() as (db, name):
            query = "SELECT body FROM stream_events WHERE name=? AND sequence>?"
            args: List[Any] = [name, since]
            for column, value in (("turn_id", turn_id), ("kind", kind)):
                if value is not None:
                    query += f" AND {column}=?"
                    args.append(value)
            if through is not None:
                query += " AND sequence<=?"
                args.append(through)
            query += " ORDER BY sequence"
            if limit is not None:
                query += " LIMIT ?"
                args.append(limit)
            for (body,) in db.execute(query, args):
                yield AgentStreamEvent.model_validate(unpack(body, self._dictionary))

    def _read_rows(
        self,
        since: int = -1,
        limit: int | None = None,
        turn_id: str | None = None,
        through: int | None = None,
        kind: str | None = None,
    ) -> List[AgentStreamEvent]:
        return list(self.iter_events(since, limit, turn_id, through, kind))

    async def append(self, event: AgentStreamEvent) -> AgentStreamEvent:
        def write() -> AgentStreamEvent:
            with self._database() as db, db:
                db.execute("BEGIN IMMEDIATE")
                name = self._import(db)
                seq = db.execute(
                    "SELECT coalesce(max(sequence),-1)+1 FROM stream_events WHERE name=?",
                    (name,),
                ).fetchone()[0]
                result = event.model_copy(update={"stream_sequence": seq})
                self._insert(db, name, result)
                return result

        async with self._lock:
            return await _complete_io(write)

    def _ensure_stream(self, db: Any, *, seed_events: List[AgentStreamEvent] | None = None) -> int:
        """Ensure a stream row exists without importing legacy JSONL rows being replaced/cleared."""
        _, logical_name = storage_location(self._path)
        row = db.execute(
            "SELECT id,dictionary FROM streams WHERE name=?", (logical_name,)
        ).fetchone()
        if row:
            self._dictionary = row[1]
            return int(row[0])
        if seed_events:
            raw = b"".join(
                pack(
                    ev.model_dump(mode="json", exclude_defaults=True, exclude_none=True),
                    b"",
                )
                for ev in seed_events[:16]
            )
            self._dictionary = raw[:32768] or self.session_id.encode()
        else:
            self._dictionary = self.session_id.encode()
        return int(
            db.execute(
                "INSERT INTO streams(name,dictionary) VALUES (?,?)",
                (logical_name, self._dictionary),
            ).lastrowid
        )

    def _clear_rows(self) -> None:
        with self._database() as db, db:
            db.execute("BEGIN IMMEDIATE")
            name = self._ensure_stream(db)
            db.execute("UPDATE streams SET generation=generation+1 WHERE id=?", (name,))
            db.execute("DELETE FROM stream_events WHERE name=?", (name,))
            db.execute("DELETE FROM stream_errors WHERE name=?", (name,))
        delete_document(self.cursor_path)
        delete_document(self._path.with_name(self._path.name + ".edit-bak"))
        self._path.unlink(missing_ok=True)

    async def clear(self) -> None:
        async with self._lock, self._read_lock:
            await _complete_io(self._clear_rows)
            self._history.reset()

    def _replace_rows(self, events: List[AgentStreamEvent]) -> None:
        with self._database() as db, db:
            db.execute("BEGIN IMMEDIATE")
            name = self._ensure_stream(db, seed_events=events)
            db.execute("UPDATE streams SET generation=generation+1 WHERE id=?", (name,))
            db.execute("DELETE FROM stream_events WHERE name=?", (name,))
            db.execute("DELETE FROM stream_errors WHERE name=?", (name,))
            for seq, event in enumerate(events):
                self._insert(db, name, event.model_copy(update={"stream_sequence": seq}))

    async def replace_all(self, events: List[AgentStreamEvent]) -> None:
        async with self._lock, self._read_lock:
            await _complete_io(self._replace_rows, events)
            self._history.reset()

    async def read_since(
        self, since_sequence: int = -1, limit: int = 500, *, compact: bool = False
    ) -> AgentStreamEventPage:
        rows = await _complete_io(self._read_rows, since_sequence, max(1, limit) + 1)
        events = rows[: max(1, limit)]
        return AgentStreamEventPage(
            events=compact_history_events(events) if compact else events,
            next_sequence=events[-1].stream_sequence if events else since_sequence,
            has_more=len(rows) > len(events),
        )

    async def read_conversation(
        self, since_sequence: int = -1, limit: int = 500, *, compact: bool = False
    ) -> AgentStreamEventPage:
        # Import before handing the durable SQLite index to the history projection.
        await _complete_io(self._read_rows, -1, 0)
        async with self._read_lock:
            page = await _complete_io(
                self._history.conversation_page,
                self._path,
                since_sequence,
                max(1, limit),
            )
            if compact:
                events = []
                for _, run in groupby(
                    enumerate(page.events),
                    lambda item: item[1].stream_sequence - item[0],
                ):
                    events.extend(compact_history_events([event for _, event in run]))
                page = page.model_copy(update={"events": events})
            return page

    async def read_turn_since(
        self,
        turn_id: str,
        since_sequence: int,
        through_sequence: int,
        limit: int = 500,
        *,
        compact: bool = False,
    ) -> AgentStreamEventPage:
        rows = await _complete_io(
            self._read_rows,
            since_sequence,
            max(1, limit) + 1,
            turn_id,
            through_sequence,
        )
        events = rows[: max(1, limit)]
        return AgentStreamEventPage(
            events=compact_history_events(events) if compact else events,
            next_sequence=events[-1].stream_sequence if events else since_sequence,
            has_more=len(rows) > len(events),
        )

    def has_events(self) -> bool:
        with self._read_database() as (db, name):
            return (
                db.execute("SELECT 1 FROM stream_events WHERE name=? LIMIT 1", (name,)).fetchone()
                is not None
                or db.execute(
                    "SELECT 1 FROM stream_errors WHERE name=? LIMIT 1", (name,)
                ).fetchone()
                is not None
            )

    async def count(self) -> int:
        def count_rows() -> int:
            with self._read_database() as (db, name):
                return int(
                    db.execute(
                        "SELECT count(*) FROM stream_events WHERE name=?", (name,)
                    ).fetchone()[0]
                )

        return await _complete_io(count_rows)

    def _scan_turn_lifecycle(self, turn_id: str, strict: bool) -> TurnLifecycleSnapshot:
        try:
            with self._read_database() as (db, name):
                if (
                    strict
                    and db.execute(
                        "SELECT 1 FROM stream_errors WHERE name=? LIMIT 1", (name,)
                    ).fetchone()
                ):
                    raise RuntimeError(f"cannot inspect corrupt agent stream {self._path}")
                rows = list(
                    db.execute(
                        "SELECT body FROM stream_events WHERE name=? AND kind IN ('turn_started','turn_completed','error') ORDER BY sequence",
                        (name,),
                    )
                )
        except RuntimeError:
            raise
        except OSError as exc:
            if strict:
                raise RuntimeError(f"cannot inspect agent stream {self._path}") from exc
            return TurnLifecycleSnapshot(
                state=TurnLifecycleState.NOT_STARTED, latest_unfinished=None
            )
        state = TurnLifecycleState.NOT_STARTED
        candidate = None
        for (body,) in rows:
            try:
                event = AgentStreamEvent.model_validate(unpack(body, self._dictionary))
            except (ValueError, KeyError, OSError) as exc:
                if strict:
                    raise RuntimeError(f"cannot inspect corrupt agent stream {self._path}") from exc
                continue
            if event.type == AgentStreamEventType.TURN_STARTED:
                candidate = event
                if event.turn_id == turn_id and state != TurnLifecycleState.TERMINAL:
                    state = TurnLifecycleState.UNFINISHED
            else:
                if event.turn_id == turn_id and state == TurnLifecycleState.UNFINISHED:
                    state = TurnLifecycleState.TERMINAL
                if candidate is not None and (
                    event.turn_id == candidate.turn_id
                    if candidate.turn_id is not None
                    else event.run_epoch == candidate.run_epoch
                ):
                    candidate = None
        return TurnLifecycleSnapshot(state=state, latest_unfinished=candidate)

    async def inspect_turn_lifecycle(self, turn_id: str) -> TurnLifecycleSnapshot:
        if not turn_id:
            raise ValueError("turn_id must not be empty")
        return await _complete_io(self._scan_turn_lifecycle, turn_id, True)

    async def latest_unfinished_turn(self) -> Optional[AgentStreamEvent]:
        snapshot = await _complete_io(self._scan_turn_lifecycle, "__latest__", False)
        return snapshot.latest_unfinished

    async def last_event_at(self) -> Optional[str]:
        return await self._last_time(None)

    async def latest_turn_completed_at(self) -> Optional[str]:
        return await self._last_time("turn_completed")

    async def _last_time(self, kind: str | None) -> Optional[str]:
        def last() -> Optional[str]:
            with self._read_database() as (db, name):
                query = "SELECT body FROM stream_events WHERE name=?"
                args: List[Any] = [name]
                if kind:
                    query += " AND kind=?"
                    args.append(kind)
                row = db.execute(query + " ORDER BY sequence DESC LIMIT 1", args).fetchone()
                return str(unpack(row[0], self._dictionary)["created_at"]) if row else None

        return await _complete_io(last)

    async def truncate_before(self, sequence: int) -> int:
        def truncate() -> int:
            with self._database() as db, db:
                db.execute("BEGIN IMMEDIATE")
                name = self._import(db)
                db.execute("UPDATE streams SET generation=generation+1 WHERE id=?", (name,))
                return int(
                    db.execute(
                        "DELETE FROM stream_events WHERE name=? AND sequence>=?",
                        (name, sequence),
                    ).rowcount
                )

        async with self._lock, self._read_lock:
            result = await _complete_io(truncate)
            self._history.reset()
            return result

    async def snapshot(self) -> Optional[Path]:
        async with self._lock:
            events = await _complete_io(self._read_rows)
            if not events:
                return None
            backup = self._path.with_name(self._path.name + ".edit-bak")
            await _complete_io(
                write_document,
                backup,
                [event.model_dump(mode="json") for event in events],
            )
            return backup

    async def discard_snapshot(self, backup: Optional[Path]) -> None:
        """Delete this Hub store's SQLite snapshot, never a provider file."""
        if backup is not None:
            await _complete_io(delete_document, backup)

    async def restore(self, backup: Optional[Path]) -> None:
        if backup is None or not document_exists(backup):
            return
        events = await _complete_io(read_document, backup)

        # Preserve cursors, including gaps, across rollback.
        def restore_rows() -> None:
            with self._database() as db, db:
                db.execute("BEGIN IMMEDIATE")
                name = self._ensure_stream(db)
                db.execute("UPDATE streams SET generation=generation+1 WHERE id=?", (name,))
                db.execute("DELETE FROM stream_events WHERE name=?", (name,))
                for event in events:
                    self._insert(db, name, AgentStreamEvent.model_validate(event))

        async with self._lock, self._read_lock:
            await _complete_io(restore_rows)
            self._history.reset()

    async def find_turn(self, turn_id: str) -> Optional[Tuple[int, int, str, List[Dict[str, Any]]]]:
        """Locate a turn while preserving the established four-field contract."""

        result = await self.find_turn_with_metadata(turn_id)
        if result is None:
            return None
        sequence, turn_index, text, attachments, _metadata = result
        return sequence, turn_index, text, attachments

    async def find_turn_with_metadata(
        self, turn_id: str
    ) -> Optional[Tuple[int, int, str, List[Dict[str, Any]], Dict[str, Any]]]:
        """Locate a turn by its ``turn_id``.

        Returns ``(stream_sequence, turn_index, turn_text, attachments,
        metadata)``. ``find_turn`` retains the historical four-field result.

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
        - ``metadata`` is the server-authored turn metadata used to reconstruct
          provider-visible text without consulting mutable binding state.

        Returns ``None`` if no ``turn_started`` event carries ``turn_id``.
        """
        events = await _complete_io(self._read_rows, -1, None, None, None, "turn_started")
        for index, event in enumerate(events):
            if event.turn_id != turn_id:
                continue
            payload = event.payload
            summary = payload.get("summary")
            attachments = payload.get("attachments")
            metadata = payload.get("metadata")
            return (
                event.stream_sequence,
                index,
                summary if isinstance(summary, str) else "",
                (
                    [item for item in attachments if isinstance(item, dict)]
                    if isinstance(attachments, list)
                    else []
                ),
                dict(metadata) if isinstance(metadata, dict) else {},
            )
        return None
