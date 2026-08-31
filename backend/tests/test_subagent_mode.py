"""Subagent create/review-skip/failed detection. Isolated tmp state only."""

from __future__ import annotations

from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Generator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    User,
    WorkspaceSessionRole,
    WorkspaceTaskStatus,
)
from claude_hub.services.session_seat import SessionSeatMismatch
from claude_hub.services.workspace_manager import workspace_manager
from tests.test_workspaces import stub_workspace_terminal

workspace_module = import_module("claude_hub.services.workspace_manager")


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


@pytest.fixture()
def isolated_hub(monkeypatch: MonkeyPatch, tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_root = tmp_path / "workspaces"
    state_root.mkdir()
    monkeypatch.setattr(workspace_module, "STATE_ROOT", state_root)
    monkeypatch.setattr(workspace_module, "INDEX_FILE", state_root / "index.json")
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="subagent-tab",
        port=18774,
    )
    return repo


def test_create_persists_timeout_and_defaults_none(isolated_hub: Path) -> None:
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Subagent Isolated", "path": str(isolated_hub), "session_prefix": "sa"},
    ).json()
    bounded = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Bounded",
            "prompt": "do a thing",
            "task_mode": "subagent",
            "timeout_seconds": 90,
        },
    ).json()
    assert bounded["task_mode"] == "subagent"
    assert bounded["timeout_seconds"] == 90
    assert bounded["status"] == "todo"

    unbounded = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Open", "prompt": "do another", "task_mode": "subagent"},
    ).json()
    assert unbounded["timeout_seconds"] is None


def test_subagent_completion_skips_ai_review(isolated_hub: Path) -> None:
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Subagent Review Skip", "path": str(isolated_hub), "session_prefix": "sas"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Skip review", "prompt": "finish", "task_mode": "subagent"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    client.post(
        f"/api/workspaces/sessions/{worker['id']}/reports",
        json={"task_id": task["id"], "state": "completed", "message": "done"},
    )
    stored = workspace_manager.tasks[task["id"]]
    assert stored.status == WorkspaceTaskStatus.REVIEW
    assert stored.review_skipped_at is not None
    assert stored.review_session_id is None
    assert stored.human_acceptance_requested_at is not None


@pytest.mark.asyncio
async def test_subagent_session_death_marks_failed(isolated_hub: Path) -> None:
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Subagent Fail", "path": str(isolated_hub), "session_prefix": "saf"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Die", "prompt": "work", "task_mode": "subagent", "timeout_seconds": 30},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    session = workspace_manager.sessions[worker["id"]]
    workspace_manager.sessions[worker["id"]] = session.model_copy(
        update={"status": ManagedSessionStatus.STOPPED}
    )
    assert workspace_manager.tasks[task["id"]].status == WorkspaceTaskStatus.WORKING
    await workspace_manager._refresh_session_statuses(workspace["id"])
    failed = workspace_manager.tasks[task["id"]]
    assert failed.status == WorkspaceTaskStatus.FAILED
    assert failed.failure_reason == "session died"


@pytest.mark.asyncio
async def test_reviewed_session_death_does_not_mark_failed(isolated_hub: Path) -> None:
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Reviewed Stay", "path": str(isolated_hub), "session_prefix": "rs"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Reviewed", "prompt": "work", "task_mode": "reviewed"},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    session = workspace_manager.sessions[worker["id"]]
    workspace_manager.sessions[worker["id"]] = session.model_copy(
        update={"status": ManagedSessionStatus.STOPPED}
    )
    await workspace_manager._refresh_session_statuses(workspace["id"])
    stored = workspace_manager.tasks[task["id"]]
    assert stored.status == WorkspaceTaskStatus.WORKING
    assert stored.failure_reason is None


@pytest.mark.asyncio
async def test_subagent_timeout_marks_failed(isolated_hub: Path) -> None:
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Subagent Timeout", "path": str(isolated_hub), "session_prefix": "sat"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Slow", "prompt": "work", "task_mode": "subagent", "timeout_seconds": 10},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    stored = workspace_manager.tasks[task["id"]]
    stale = stored.updated_at - timedelta(seconds=30)
    workspace_manager.tasks[task["id"]] = stored.model_copy(update={"updated_at": stale})
    await workspace_manager._refresh_session_statuses(workspace["id"])
    failed = workspace_manager.tasks[task["id"]]
    assert failed.status == WorkspaceTaskStatus.FAILED
    assert failed.failure_reason == "timeout after 10s"


@pytest.mark.asyncio
async def test_reviewer_same_task_rereview_does_not_clear(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_root = tmp_path / "workspaces"
    state_root.mkdir()
    monkeypatch.setattr(workspace_module, "STATE_ROOT", state_root)
    monkeypatch.setattr(workspace_module, "INDEX_FILE", state_root / "index.json")
    sent_messages: list[tuple[str, str]] = []
    stub_workspace_terminal(
        monkeypatch,
        repo,
        tab_id="rev-reclear-tab",
        port=18775,
        sent_messages=sent_messages,
    )
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "No Reclear", "path": str(repo), "session_prefix": "nr"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    client.post(
        f"/api/workspaces/{workspace['id']}/agent",
        json={"role": "reviewer", "reuse_existing": False},
    )
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Clear once", "prompt": "implement", "clear_context": True},
    ).json()
    client.post(
        f"/api/workspaces/tasks/{task['id']}/start",
        json={"target_session_id": worker["id"]},
    )
    sent_messages.clear()
    client.post(
        f"/api/workspaces/sessions/{worker['id']}/reports",
        json={"task_id": task["id"], "state": "completed", "message": "Done"},
    )
    first = [message for _session, message in sent_messages]
    assert "/clear" in first

    stored = workspace_manager.tasks[task["id"]]
    report = next(item for item in workspace_manager.reports.values() if item.task_id == stored.id)
    sent_messages.clear()
    await workspace_manager._request_task_review(stored, report)
    second = [message for _session, message in sent_messages]
    assert "/clear" not in second
    assert any("Review workspace task" in message for message in second)


@pytest.mark.asyncio
async def test_send_clear_refuses_mismatched_seat(isolated_hub: Path) -> None:
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Seat Guard", "path": str(isolated_hub), "session_prefix": "sg"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    session = workspace_manager.sessions[worker["id"]]
    workspace_manager.sessions[worker["id"]] = session.model_copy(
        update={"tmux_session": "claude-hub-deadbeef"}
    )
    with pytest.raises(SessionSeatMismatch):
        await workspace_manager.send_session_message(worker["id"], "/clear")


@pytest.mark.asyncio
async def test_ensure_ready_refuses_to_recreate_missing_pane(
    monkeypatch: MonkeyPatch,
) -> None:
    tm = import_module("claude_hub.services.ttyd_manager")

    created: list[str] = []

    async def fake_ensure(tab_id: str) -> bool:
        created.append(tab_id)
        return True

    monkeypatch.setattr(tm, "_tmux_session_exists_async", AsyncMock(return_value=False))
    monkeypatch.setattr(tm.ttyd_manager, "ensure_tab_tmux_session", fake_ensure)

    tab_id = "aabbccdd-missing"
    now = datetime.utcnow()
    session = ManagedSession(
        id="sess-missing",
        workspace_id="ws",
        tab_id=tab_id,
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        title="missing pane",
        workspace_path="/tmp",
        tmux_session="claude-hub-aabbccdd",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    tm.ttyd_manager.processes[tab_id] = SimpleNamespace(tmux_session="claude-hub-aabbccdd")
    try:
        with pytest.raises(SessionSeatMismatch, match="gone"):
            await workspace_manager._ensure_session_ready_for_send(session)
        assert created == []
    finally:
        tm.ttyd_manager.processes.pop(tab_id, None)
