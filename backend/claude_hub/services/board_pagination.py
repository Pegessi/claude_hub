"""Stable cursor pagination for workspace board task history."""

from __future__ import annotations

import base64
import json
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..models.schemas import BoardTasksPagination, WorkspaceTask

MAX_BOARD_TASKS_LIMIT = 100
DEFAULT_BOARD_TASKS_LIMIT = 15


def board_task_sort_key(task: "WorkspaceTask") -> tuple[float, float, str]:
    """Sort tasks by updated_at DESC, then created_at ASC, then id ASC."""
    updated_ts = task.updated_at.timestamp() if task.updated_at else 0.0
    created_ts = task.created_at.timestamp() if task.created_at else 0.0
    return (-updated_ts, created_ts, task.id)


def encode_board_tasks_cursor(
    updated_at: datetime,
    created_at: datetime,
    task_id: str,
) -> str:
    payload = {
        "updated_at": updated_at.isoformat(),
        "created_at": created_at.isoformat(),
        "id": task_id,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_board_tasks_cursor(cursor: str) -> tuple[datetime, datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid tasks_cursor")
        if "created_at" not in payload:
            raise ValueError("Invalid tasks_cursor: missing created_at")
        updated_at = datetime.fromisoformat(str(payload["updated_at"]))
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        task_id = str(payload["id"])
        if not task_id:
            raise ValueError("Invalid tasks_cursor")
        return updated_at, created_at, task_id
    except ValueError as exc:
        if str(exc) == "Invalid tasks_cursor: missing created_at":
            raise
        raise ValueError("Invalid tasks_cursor") from exc
    except Exception as exc:
        raise ValueError("Invalid tasks_cursor") from exc


def _cursor_sort_key(
    updated_at: datetime,
    created_at: datetime,
    task_id: str,
) -> tuple[float, float, str]:
    updated_ts = updated_at.timestamp() if updated_at else 0.0
    created_ts = created_at.timestamp() if created_at else 0.0
    return (-updated_ts, created_ts, task_id)


def _task_is_after_cursor(
    task: "WorkspaceTask",
    cursor_updated_at: datetime,
    cursor_created_at: datetime,
    cursor_id: str,
) -> bool:
    cursor_key = _cursor_sort_key(cursor_updated_at, cursor_created_at, cursor_id)
    return board_task_sort_key(task) > cursor_key


def validate_tasks_limit(limit: int) -> None:
    if limit < 1 or limit > MAX_BOARD_TASKS_LIMIT:
        raise ValueError(f"tasks_limit must be between 1 and {MAX_BOARD_TASKS_LIMIT}")


def paginate_board_tasks(
    tasks: list["WorkspaceTask"],
    *,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> tuple[list["WorkspaceTask"], Optional["BoardTasksPagination"]]:
    """Return a sorted task slice and optional pagination metadata."""
    from ..models.schemas import BoardTasksPagination

    if limit is None:
        return list(tasks), None

    sorted_tasks = sorted(tasks, key=board_task_sort_key)
    total = len(sorted_tasks)
    status_counts = dict(sorted(Counter(task.status.value for task in sorted_tasks).items()))

    validate_tasks_limit(limit)
    if cursor is not None and not cursor.strip():
        raise ValueError("tasks_cursor must not be empty")

    start_index = 0
    if cursor:
        cursor_updated_at, cursor_created_at, cursor_id = decode_board_tasks_cursor(cursor)
        for index, task in enumerate(sorted_tasks):
            if _task_is_after_cursor(
                task,
                cursor_updated_at,
                cursor_created_at,
                cursor_id,
            ):
                start_index = index
                break
        else:
            start_index = total

    page = sorted_tasks[start_index : start_index + limit]
    has_more = start_index + limit < total
    next_cursor: Optional[str] = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_board_tasks_cursor(
            last.updated_at,
            last.created_at,
            last.id,
        )

    pagination = BoardTasksPagination(
        total_count=total,
        has_more=has_more,
        next_cursor=next_cursor,
        limit=limit,
        status_counts=status_counts,
    )
    return page, pagination
