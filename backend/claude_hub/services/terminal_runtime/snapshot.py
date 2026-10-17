"""Pure tab snapshot validation, independent of persistence and runtime owners."""

import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Pattern, Protocol, Tuple, TypeVar


class PendingCleanupRecord(Protocol):
    @property
    def tab_id(self) -> str: ...

    @property
    def token(self) -> str: ...


PendingCleanupT = TypeVar("PendingCleanupT", bound=PendingCleanupRecord)


def parse_tabs_snapshot(
    value: Any,
    *,
    state_version: int,
    cleanup_authorization: str,
    tab_id_re: Pattern[str],
    state_error: type[RuntimeError],
    pending_cleanup_type: Callable[..., PendingCleanupT],
    datetime_type: type[datetime] = datetime,
) -> Tuple[List[Dict[str, Any]], Dict[str, PendingCleanupT]]:
    """Validate the whole snapshot before publishing any runtime owner."""
    if isinstance(value, list):
        tabs, pending = value, []
    elif (
        isinstance(value, dict)
        and set(value) == {"version", "tabs", "pending_stream_cleanup"}
        and type(value["version"]) is int
        and value["version"] == state_version
    ):
        tabs, pending = value["tabs"], value["pending_stream_cleanup"]
    else:
        raise state_error("unsupported or invalid tabs snapshot")
    if not isinstance(tabs, list) or not isinstance(pending, list):
        raise state_error("invalid tab or cleanup list")
    live_ids: set[str] = set()
    for tab in tabs:
        if (
            not isinstance(tab, dict)
            or not isinstance(tab.get("id"), str)
            or not tab_id_re.fullmatch(tab["id"])
            or tab["id"] in live_ids
            or not isinstance(tab.get("name"), str)
            or type(tab.get("port")) is not int
            or not isinstance(tab.get("created_at"), str)
        ):
            raise state_error("invalid or duplicate tab record")
        datetime_type.fromisoformat(tab["created_at"])
        live_ids.add(tab["id"])
    records: Dict[str, PendingCleanupT] = {}
    tokens: set[str] = set()
    for raw in pending:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"tab_id", "created_at", "token", "authorization"}
            or any(not isinstance(item, str) for item in raw.values())
            or not tab_id_re.fullmatch(raw["tab_id"])
            or not re.fullmatch(r"[0-9a-f]{32}", raw["token"])
            or raw["authorization"] != cleanup_authorization
            or raw["tab_id"] in records
            or raw["tab_id"] in live_ids
            or raw["token"] in tokens
        ):
            raise state_error("invalid, duplicate, or live-colliding cleanup obligation")
        datetime_type.fromisoformat(raw["created_at"])
        record = pending_cleanup_type(**raw)
        records[record.tab_id] = record
        tokens.add(record.token)
    return tabs, records
