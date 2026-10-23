"""Board responses derive Task lifecycles from the live manager. Isolated tmp state only."""

from __future__ import annotations

import uuid
from importlib import import_module
from pathlib import Path
from typing import Any, Generator

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import AgentReport, AgentReportState, User
from claude_hub.services.workspace_manager import workspace_manager
from tests.test_workspaces import stub_workspace_terminal

workspace_module = import_module("claude_hub.services.workspace_manager")


def _clear_manager() -> None:
    workspace_manager.workspaces.clear()
    workspace_manager.tasks.clear()
    workspace_manager.sessions.clear()
    workspace_manager.reports.clear()
    workspace_manager._dispatch_locks.clear()
    workspace_manager._feedback_summary_locks.clear()


@pytest.fixture(autouse=True)
def isolated_hub(monkeypatch: MonkeyPatch, tmp_path: Path) -> Generator[Path, None, None]:
    _clear_manager()
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)
    monkeypatch.setattr(workspace_manager, "_save_workspace_state", lambda _workspace_id: None)
    monkeypatch.setattr(workspace_manager, "_write_task_record", lambda _task: None)
    state_root = tmp_path / "workspaces"
    state_root.mkdir()
    monkeypatch.setattr(workspace_module, "STATE_ROOT", state_root)
    monkeypatch.setattr(workspace_module, "INDEX_FILE", state_root / "index.json")
    repo = tmp_path / "repo"
    repo.mkdir()
    stub_workspace_terminal(monkeypatch, repo, tab_id="lifecycle-tab", port=18791)

    async def fake_current_user() -> User:
        return User(open_id="local", name="Local", email="local@localhost", avatar_url=None)

    app.dependency_overrides[get_current_user] = fake_current_user
    yield repo
    app.dependency_overrides.pop(get_current_user, None)
    _clear_manager()


def _workspace(client: TestClient, repo: Path) -> dict[str, Any]:
    workspace: dict[str, Any] = client.post(
        "/api/workspaces",
        json={"name": "Lifecycle", "path": str(repo), "session_prefix": "lc"},
    ).json()
    return workspace


def _lifecycle(client: TestClient, workspace_id: str, task_id: str) -> dict[str, Any]:
    board = client.get(f"/api/workspaces/{workspace_id}/board").json()
    lifecycle: dict[str, Any] = board["task_lifecycles"][task_id]
    return lifecycle


def test_board_reports_running_task_owned_by_its_agent(isolated_hub: Path) -> None:
    client = TestClient(app)
    workspace = _workspace(client, isolated_hub)
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Run", "prompt": "work"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{task['id']}/start",
        json={"target_session_id": worker["id"]},
    )

    lifecycle = _lifecycle(client, workspace["id"], task["id"])
    assert lifecycle["phase"] == "running"
    assert lifecycle["owner"] == {"kind": "session", "id": worker["id"], "state": "active"}
    assert lifecycle["holds_session"] is True
    assert lifecycle["attention"] is False


def test_prior_round_review_failure_does_not_mask_current_acceptance(
    isolated_hub: Path,
) -> None:
    client = TestClient(app)
    workspace = _workspace(client, isolated_hub)
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Second round", "prompt": "finish", "task_mode": "subagent"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    # Round 1 ended in review_failed; round 2 completed with review skipped.
    stored = workspace_manager.tasks[task["id"]]
    workspace_manager.tasks[task["id"]] = stored.model_copy(update={"review_cycle": 2})
    stale = AgentReport(
        id=str(uuid.uuid4()),
        workspace_id=workspace["id"],
        task_id=task["id"],
        session_id="reviewer-gone",
        state=AgentReportState.REVIEW_FAILED,
        message="round 1 needs changes",
        review_cycle=1,
        created_at=stored.created_at,
    )
    workspace_manager.reports[stale.id] = stale
    client.post(
        f"/api/workspaces/sessions/{worker['id']}/reports",
        json={"task_id": task["id"], "state": "completed", "message": "done"},
    )
    assert workspace_manager.tasks[task["id"]].review_skipped_at is not None

    lifecycle = _lifecycle(client, workspace["id"], task["id"])
    assert (lifecycle["phase"], lifecycle["reason"]) == (
        "awaiting_acceptance",
        "awaiting_acceptance",
    )
    assert lifecycle["attention"] is True
