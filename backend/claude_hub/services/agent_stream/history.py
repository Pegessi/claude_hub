"""Question/answer history projection from the shared durable SQLite indexes."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from ...models import (
    AgentStreamEvent,
    AgentStreamEventPage,
    AgentStreamEventType,
    AgentStreamHistoryDetail,
)
from ..local_storage import connection, storage_location, unpack

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


class ConversationHistoryIndex:
    """Project the durable SQLite index without a second in-memory event index."""

    def reset(self) -> None:
        # Retained for callers that invalidate lifecycle projections. Reads use
        # a fresh SQLite snapshot, so no derived per-session cache needs repair.
        pass

    def conversation_page(self, path: Path, since: int, limit: int) -> AgentStreamEventPage:
        _, logical_name = storage_location(path)
        with connection(path) as db:
            db.execute("BEGIN")
            stream = db.execute(
                "SELECT id,dictionary FROM streams WHERE name=?", (logical_name,)
            ).fetchone()
            if stream is None:
                return AgentStreamEventPage(events=[], next_sequence=since, has_more=False)
            name, dictionary = stream
            events: List[AgentStreamEvent] = []
            touched: Dict[str, None] = {}
            attributed: Dict[str, None] = {}
            next_sequence = since
            has_more = False
            for sequence, turn_id, visible, identity, body in db.execute(
                "SELECT sequence,turn_id,visible,identity,CASE WHEN visible THEN body END FROM stream_events WHERE name=? AND sequence>? ORDER BY sequence",
                (name, since),
            ):
                tool_identity = identity if turn_id else None
                if (
                    len(events) >= limit
                    or (turn_id is not None and turn_id not in touched and len(touched) >= limit)
                    or (
                        tool_identity is not None
                        and tool_identity not in attributed
                        and len(attributed) >= limit
                    )
                ):
                    has_more = True
                    break
                if visible:
                    events.append(AgentStreamEvent.model_validate(unpack(body, dictionary)))
                if turn_id:
                    touched[turn_id] = None
                if tool_identity is not None:
                    attributed[tool_identity] = None
                next_sequence = sequence
            details = []
            for turn_id in touched:
                first, last, count, hidden = db.execute(
                    "SELECT min(sequence),max(sequence),count(*),sum(visible=0) FROM stream_events WHERE name=? AND turn_id=? AND sequence<=?",
                    (name, turn_id, next_sequence),
                ).fetchone()
                if hidden:
                    details.append(
                        AgentStreamHistoryDetail(
                            turn_id=turn_id,
                            first_sequence=first,
                            last_sequence=last,
                            event_count=count,
                        )
                    )
            return AgentStreamEventPage(
                events=events,
                next_sequence=next_sequence,
                has_more=has_more,
                history_details=details,
                attributed_tool_ids=list(attributed),
            )
