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
_SUBAGENT_LIFECYCLE_STATUSES = frozenset(
    {
        "active",
        "completed",
        "failed",
        "cancelled",
    }
)
_SUBAGENT_TOOL_NAMES = frozenset(
    {
        "Agent",
        "Task",
        "Fork",
        "spawnAgent",
        "sendInput",
        "SendMessage",
        "collabAgentToolCall",
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


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_oneof_type(value: object) -> bool:
    if _non_empty_str(value):
        return True
    if not isinstance(value, dict) or len(value) != 1:
        return False
    only_key = next(iter(value))
    return isinstance(only_key, str) and only_key != "unspecified"


def _is_subagent_delegation_tool(tool_name: object, args: object, *, has_subthread: bool) -> bool:
    if not isinstance(tool_name, str) or tool_name not in _SUBAGENT_TOOL_NAMES:
        return False
    if not isinstance(args, dict):
        return False
    if has_subthread:
        return (
            tool_name == "SendMessage"
            and _non_empty_str(args.get("agent_id"))
            and _non_empty_str(args.get("message"))
        )
    if tool_name == "Agent":
        return _non_empty_str(args.get("prompt")) and (
            _non_empty_str(args.get("subagent_type")) or _non_empty_str(args.get("description"))
        )
    if tool_name == "Task":
        if _non_empty_str(args.get("prompt")):
            return _has_oneof_type(args.get("subagentType")) or _non_empty_str(args.get("agentId"))
        tasks = args.get("tasks")
        return (
            _non_empty_str(args.get("description"))
            and isinstance(tasks, list)
            and len(tasks) > 0
            and all(
                isinstance(item, dict)
                and _non_empty_str(item.get("prompt"))
                and _non_empty_str(item.get("subagent_name"))
                and _non_empty_str(item.get("subagent_type"))
                for item in tasks
            )
        )
    if tool_name == "Fork":
        return _non_empty_str(args.get("prompt")) and (
            _non_empty_str(args.get("agent_name"))
            or _non_empty_str(args.get("fork_from"))
            or _non_empty_str(args.get("workspace"))
            or isinstance(args.get("nested_spawn"), bool)
        )
    if tool_name in ("spawnAgent", "sendInput"):
        receivers = args.get("receiverThreadIds")
        return (
            _non_empty_str(args.get("prompt"))
            and isinstance(receivers, list)
            and any(isinstance(item, str) and len(item) > 0 for item in receivers)
        )
    if tool_name == "SendMessage":
        agent_id = args.get("agent_id")
        return (
            _non_empty_str(agent_id)
            and str(agent_id).strip() not in ("main", "root")
            and _non_empty_str(args.get("message"))
        )
    return False


def conversation_event_visible(event: AgentStreamEvent) -> bool:
    """Keep legacy events intact when no stable turn identity exists."""
    if not event.turn_id:
        return True
    if event.type in _CONTROL_TYPES:
        return True
    if event.type == AgentStreamEventType.STATUS and (
        event.payload.get("provider_status") == "subagent_lifecycle"
        or event.payload.get("subagent_status") in _SUBAGENT_LIFECYCLE_STATUSES
    ):
        return True
    thread = event.payload.get("subagent_thread")
    has_subthread = isinstance(thread, str) and bool(thread.strip())
    if event.type in (
        AgentStreamEventType.TOOL_CALL_STARTED,
        AgentStreamEventType.TOOL_CALL_COMPLETED,
    ):
        return _is_subagent_delegation_tool(
            event.payload.get("name"),
            event.payload.get("args"),
            has_subthread=has_subthread,
        )
    if event.type in _PROCESS_TYPES:
        return False
    return not has_subthread


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
            checked_tool_starts: Dict[tuple[str, str], bool] = {}
            next_sequence = since
            has_more = False
            for sequence, turn_id, visible, identity, kind, body in db.execute(
                "SELECT sequence,turn_id,visible,identity,kind,CASE WHEN visible OR kind IN ('status','tool_call_started') THEN body END FROM stream_events WHERE name=? AND sequence>? ORDER BY sequence",
                (name, since),
            ):
                tool_identity = identity if turn_id else None
                candidate: Optional[AgentStreamEvent] = None
                is_visible = bool(visible)
                if not is_visible and turn_id:
                    if kind == "status" and body is not None:
                        ev = AgentStreamEvent.model_validate(unpack(body, dictionary))
                        if conversation_event_visible(ev):
                            candidate = ev
                            is_visible = True
                    elif kind == "tool_call_started" and body is not None:
                        raw_dict = unpack(body, dictionary)
                        payload_obj = (
                            raw_dict.get("payload") if isinstance(raw_dict, dict) else None
                        )
                        if (
                            isinstance(payload_obj, dict)
                            and payload_obj.get("name") in _SUBAGENT_TOOL_NAMES
                        ):
                            ev = AgentStreamEvent.model_validate(raw_dict)
                            if conversation_event_visible(ev):
                                candidate = ev
                                is_visible = True
                        if tool_identity:
                            checked_tool_starts[(turn_id, tool_identity)] = is_visible
                    elif kind == "tool_call_completed" and tool_identity:
                        key = (turn_id, tool_identity)
                        start_visible = checked_tool_starts.get(key)
                        if start_visible is None:
                            start_row = db.execute(
                                "SELECT visible,body FROM stream_events WHERE name=? AND turn_id=? AND identity=? AND kind='tool_call_started' AND sequence<? ORDER BY sequence DESC LIMIT 1",
                                (name, turn_id, tool_identity, sequence),
                            ).fetchone()
                            if start_row is None:
                                start_visible = False
                            elif start_row[0]:
                                start_visible = True
                            elif start_row[1] is not None:
                                start_ev = AgentStreamEvent.model_validate(
                                    unpack(start_row[1], dictionary)
                                )
                                start_visible = conversation_event_visible(start_ev)
                            else:
                                start_visible = False
                            checked_tool_starts[key] = start_visible
                        if start_visible:
                            row = db.execute(
                                "SELECT body FROM stream_events WHERE name=? AND sequence=?",
                                (name, sequence),
                            ).fetchone()
                            if row is not None and row[0] is not None:
                                candidate = AgentStreamEvent.model_validate(
                                    unpack(row[0], dictionary)
                                )
                                is_visible = True
                elif is_visible and kind == "tool_call_started" and turn_id and tool_identity:
                    checked_tool_starts[(turn_id, tool_identity)] = True
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
                if is_visible:
                    events.append(
                        candidate
                        if candidate is not None
                        else AgentStreamEvent.model_validate(unpack(body, dictionary))
                    )
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
