"""Tests for workspace board task pagination."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import AgentType, User, WorkspaceTaskStatus
from claude_hub.models.schemas import WorkspaceTask
from claude_hub.services.board_pagination import (
    board_task_sort_key,
    decode_board_tasks_cursor,
    encode_board_tasks_cursor,
    paginate_board_tasks,
)
from claude_hub.services.workspace_manager import workspace_manager


def _task(
    task_id: str,
    *,
    updated_at: datetime,
    created_at: datetime | None = None,
    status: WorkspaceTaskStatus = WorkspaceTaskStatus.DONE,
) -> WorkspaceTask:
    created = created_at or updated_at
    return WorkspaceTask(
        id=task_id,
        workspace_id="ws-1",
        title=f"Task {task_id}",
        prompt="prompt",
        agent_type=AgentType.CODEX,
        status=status,
        created_at=created,
        updated_at=updated_at,
    )


@pytest.fixture(autouse=True)
def isolated_workspace_manager(monkeypatch: MonkeyPatch) -> Generator[None, None, None]:
    workspace_manager.workspaces.clear()
    workspace_manager.tasks.clear()
    workspace_manager.sessions.clear()
    workspace_manager.reports.clear()
    workspace_manager._dispatch_locks.clear()
    workspace_manager._feedback_summary_locks.clear()
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)
    monkeypatch.setattr(workspace_manager, "_write_task_record", lambda _task: None)

    async def fake_current_user() -> User:
        return User(
            open_id="local",
            name="Local User",
            email="local@localhost",
            avatar_url=None,
        )

    app.dependency_overrides[get_current_user] = fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)
    workspace_manager.workspaces.clear()
    workspace_manager.tasks.clear()
    workspace_manager.sessions.clear()
    workspace_manager.reports.clear()
    workspace_manager._dispatch_locks.clear()
    workspace_manager._feedback_summary_locks.clear()


def test_board_pagination_no_limit_preserves_input_order() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [
        _task("listed-first", updated_at=base),
        _task("listed-second", updated_at=base + timedelta(hours=1)),
        _task("listed-third", updated_at=base + timedelta(hours=2)),
    ]
    page, pagination = paginate_board_tasks(tasks)
    assert [task.id for task in page] == ["listed-first", "listed-second", "listed-third"]
    assert pagination is None


def test_board_task_sort_updated_at_desc_then_id() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [
        _task("b", updated_at=base + timedelta(hours=1)),
        _task("a", updated_at=base + timedelta(hours=1)),
        _task("c", updated_at=base),
    ]
    page, pagination = paginate_board_tasks(tasks, limit=3)
    assert [task.id for task in page] == ["a", "b", "c"]
    assert pagination is not None
    assert pagination.total_count == 3
    assert pagination.has_more is False


def test_board_pagination_first_page_limit_15() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [_task(f"t{i:02d}", updated_at=base + timedelta(minutes=i)) for i in range(20)]
    page, pagination = paginate_board_tasks(tasks, limit=15)
    assert len(page) == 15
    assert pagination is not None
    assert pagination.total_count == 20
    assert pagination.has_more is True
    assert pagination.next_cursor


def test_board_pagination_second_page_no_overlap() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [_task(f"t{i:02d}", updated_at=base + timedelta(minutes=i)) for i in range(20)]
    first, meta1 = paginate_board_tasks(tasks, limit=15)
    second, meta2 = paginate_board_tasks(
        tasks,
        limit=15,
        cursor=meta1.next_cursor if meta1 else None,
    )
    first_ids = {task.id for task in first}
    second_ids = {task.id for task in second}
    assert len(first_ids) == 15
    assert len(second_ids) == 5
    assert first_ids.isdisjoint(second_ids)
    assert meta2 is not None
    assert meta2.has_more is False


def test_board_pagination_exactly_15_tasks() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [_task(f"t{i}", updated_at=base + timedelta(minutes=i)) for i in range(15)]
    page, pagination = paginate_board_tasks(tasks, limit=15)
    assert len(page) == 15
    assert pagination is not None
    assert pagination.has_more is False
    assert pagination.next_cursor is None


def test_board_pagination_cursor_roundtrip() -> None:
    updated = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    created = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
    encoded = encode_board_tasks_cursor(updated, created, "abc-123")
    decoded_updated, decoded_created, decoded_id = decode_board_tasks_cursor(encoded)
    assert decoded_id == "abc-123"
    assert decoded_updated == updated
    assert decoded_created == created


def test_board_pagination_rejects_legacy_cursor_without_created_at() -> None:
    import base64
    import json

    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    legacy_payload = json.dumps({"updated_at": ts.isoformat(), "id": "legacy-id"})
    legacy_cursor = base64.urlsafe_b64encode(legacy_payload.encode("utf-8")).decode("ascii")
    with pytest.raises(ValueError, match="Invalid tasks_cursor: missing created_at"):
        decode_board_tasks_cursor(legacy_cursor)


def test_board_pagination_rejects_malformed_cursor_with_stable_message() -> None:
    with pytest.raises(ValueError, match="^Invalid tasks_cursor$"):
        decode_board_tasks_cursor("not-a-valid-cursor")


def test_board_pagination_rejects_legacy_cursor_missing_id() -> None:
    import base64
    import json

    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    payload = json.dumps({"updated_at": ts.isoformat(), "created_at": ts.isoformat(), "id": ""})
    cursor = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    with pytest.raises(ValueError, match="^Invalid tasks_cursor$"):
        decode_board_tasks_cursor(cursor)


def test_board_pagination_same_updated_at_cross_page_no_skip_dup() -> None:
    """Cross-page boundary with identical updated_at and created_at tie-break."""
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    tasks = [
        _task(
            f"t{i:02d}",
            updated_at=base,
            created_at=base + timedelta(minutes=i),
        )
        for i in range(20)
    ]
    full_sorted_ids = [task.id for task in sorted(tasks, key=board_task_sort_key)]
    first, meta1 = paginate_board_tasks(tasks, limit=15)
    assert meta1 is not None and meta1.next_cursor
    second, meta2 = paginate_board_tasks(
        tasks,
        limit=15,
        cursor=meta1.next_cursor,
    )
    combined_ids = [task.id for task in first] + [task.id for task in second]
    assert combined_ids == full_sorted_ids
    assert len(combined_ids) == len(set(combined_ids)) == 20
    assert meta2 is not None
    assert meta2.has_more is False


def test_board_pagination_same_updated_at_same_created_at_uses_id() -> None:
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    tasks = [
        _task("z-last", updated_at=base, created_at=base),
        _task("a-first", updated_at=base, created_at=base),
        _task("m-mid", updated_at=base, created_at=base),
    ]
    page, _ = paginate_board_tasks(tasks, limit=2)
    assert [task.id for task in page] == ["a-first", "m-mid"]
    _, meta = paginate_board_tasks(tasks, limit=2)
    assert meta is not None
    page2, _ = paginate_board_tasks(tasks, limit=2, cursor=meta.next_cursor)
    assert [task.id for task in page2] == ["z-last"]


def test_board_api_default_returns_full_history(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Full Board", "path": str(repo), "session_prefix": "full"},
    ).json()
    created_ids: list[str] = []
    for index in range(18):
        task = client.post(
            f"/api/workspaces/{workspace['id']}/tasks",
            json={"title": f"Task {index}", "prompt": "do work"},
        ).json()
        created_ids.append(task["id"])
    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    assert len(board["tasks"]) == 18
    assert board.get("tasks_pagination") is None
    assert [task["id"] for task in board["tasks"]] == created_ids


def test_board_api_paginated_initial_limit_15(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Paged Board", "path": str(repo), "session_prefix": "page"},
    ).json()
    created_ids = []
    for index in range(18):
        task = client.post(
            f"/api/workspaces/{workspace['id']}/tasks",
            json={"title": f"Task {index}", "prompt": "do work"},
        ).json()
        created_ids.append(task["id"])

    board = client.get(
        f"/api/workspaces/{workspace['id']}/board",
        params={"tasks_limit": 15},
    ).json()
    assert len(board["tasks"]) == 15
    pagination = board["tasks_pagination"]
    assert pagination["total_count"] == 18
    assert pagination["has_more"] is True
    assert pagination["next_cursor"]
    assert len(board["reports"]) <= 15

    page2 = client.get(
        f"/api/workspaces/{workspace['id']}/board",
        params={"tasks_limit": 15, "tasks_cursor": pagination["next_cursor"]},
    ).json()
    assert len(page2["tasks"]) == 3
    all_ids = {task["id"] for task in board["tasks"]} | {task["id"] for task in page2["tasks"]}
    assert all_ids == set(created_ids)


def test_board_api_rejects_cursor_without_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Bad Cursor", "path": str(repo), "session_prefix": "bad"},
    ).json()
    response = client.get(
        f"/api/workspaces/{workspace['id']}/board",
        params={"tasks_cursor": "invalid"},
    )
    assert response.status_code == 400


def test_board_api_rejects_invalid_cursor_with_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Bad Cursor 2", "path": str(repo), "session_prefix": "bad2"},
    ).json()
    response = client.get(
        f"/api/workspaces/{workspace['id']}/board",
        params={"tasks_limit": 15, "tasks_cursor": "not-a-valid-cursor"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid tasks_cursor"


def test_board_api_rejects_out_of_range_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Bad Limit", "path": str(repo), "session_prefix": "lim"},
    ).json()
    response = client.get(
        f"/api/workspaces/{workspace['id']}/board",
        params={"tasks_limit": 101},
    )
    assert response.status_code == 422
