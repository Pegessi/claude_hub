from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Generator, Optional

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import (
    AgentType,
    ManagedSessionStatus,
    TerminalTab,
    User,
    Workspace,
    WorkspaceSessionRole,
)
from claude_hub.services.workspace_manager import workspace_manager

workspace_module = import_module("claude_hub.services.workspace_manager")


@pytest.fixture(autouse=True)
def isolated_workspace_manager(monkeypatch: MonkeyPatch) -> Generator[None, None, None]:
    workspace_manager.workspaces.clear()
    workspace_manager.tasks.clear()
    workspace_manager.sessions.clear()
    workspace_manager.reports.clear()
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)

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


def test_workspace_task_flow(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    client = TestClient(app)
    response = client.post(
        "/api/workspaces",
        json={
            "name": "Test Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "test",
        },
    )

    assert response.status_code == 201
    workspace = response.json()
    assert workspace["name"] == "Test Repo"

    response = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Implement thing",
            "prompt": "Make a focused change",
            "agent_type": "codex",
        },
    )

    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "todo"

    response = client.get(f"/api/workspaces/{workspace['id']}/board")

    assert response.status_code == 200
    board = response.json()
    assert board["workspace"]["id"] == workspace["id"]
    assert [item["id"] for item in board["tasks"]] == [task["id"]]
    assert board["sessions"] == []


def test_spawn_worker_creates_managed_session(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_create_worktree(
        workspace: Workspace,
        session_id: str,
        branch: str,
    ) -> Path:
        worktree = tmp_path / "worktrees" / session_id
        worktree.mkdir(parents=True)
        return worktree

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-worker",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            port=12345,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    sent_messages: list[tuple[str, str]] = []

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    monkeypatch.setattr(workspace_manager, "_create_worktree", fake_create_worktree)
    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Spawn Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "spawn",
        },
    )
    task_response = client.post(
        f"/api/workspaces/{workspace_response.json()['id']}/tasks",
        json={
            "title": "Worker task",
            "prompt": "Do worker work",
            "agent_type": "codex",
        },
    )

    response = client.post(
        f"/api/workspaces/tasks/{task_response.json()['id']}/spawn",
        json={},
    )

    assert response.status_code == 201
    session = response.json()
    assert session["id"].startswith("spawn-")
    assert session["role"] == "worker"
    assert session["tab_id"] == "tab-worker"
    assert session["agent_type"] == "codex"
    assert workspace_manager.sessions[session["id"]].role == WorkspaceSessionRole.WORKER
    assert sent_messages
    assert "Do worker work" in sent_messages[0][1]

    report_response = client.post(
        f"/api/workspaces/sessions/{session['id']}/reports",
        json={
            "state": "completed",
            "message": "Implemented the worker change",
            "changed_files": ["backend/example.py"],
            "validation": "pytest passed",
            "risks": "None",
        },
    )

    assert report_response.status_code == 201
    report = report_response.json()
    assert report["session_id"] == session["id"]
    assert report["state"] == "completed"

    board_response = client.get(f"/api/workspaces/{workspace_response.json()['id']}/board")
    board = board_response.json()
    assert board_response.status_code == 200
    assert board["reports"][0]["message"] == "Implemented the worker change"
    assert board["tasks"][0]["status"] == "review"


def test_start_task_dispatches_to_resident_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            port=12346,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    sent_messages: list[tuple[str, str]] = []
    tab_metadata_updates: list[tuple[str, str | None, str | None, WorkspaceSessionRole | None]] = []

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    def fake_set_tab_workspace_metadata(
        tab_id: str,
        workspace_id: str | None,
        workspace_name: str | None,
        workspace_role: WorkspaceSessionRole | None,
    ) -> bool:
        tab_metadata_updates.append((tab_id, workspace_id, workspace_name, workspace_role))
        return True

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "set_tab_workspace_metadata",
        fake_set_tab_workspace_metadata,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Resident Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "resident",
        },
    )
    task_response = client.post(
        f"/api/workspaces/{workspace_response.json()['id']}/tasks",
        json={
            "title": "Resident task",
            "prompt": "Run this in the resident terminal",
            "agent_type": "codex",
        },
    )

    response = client.post(
        f"/api/workspaces/tasks/{task_response.json()['id']}/start",
        json={},
    )

    assert response.status_code == 201
    session = response.json()
    assert session["id"] == "resident-agent"
    assert session["role"] == "orchestrator"
    assert session["workspace_path"] == str(repo)
    assert workspace_manager.sessions[session["id"]].role == WorkspaceSessionRole.ORCHESTRATOR
    assert len(sent_messages) == 2
    assert "resident Claude Hub workspace agent" in sent_messages[0][1]
    assert "Run this in the resident terminal" in sent_messages[1][1]

    board_response = client.get(f"/api/workspaces/{workspace_response.json()['id']}/board")
    board = board_response.json()
    assert board["workspace"]["agent_session_id"] == "resident-agent"
    assert board["tasks"][0]["status"] == "assigned"
    assert board["tasks"][0]["session_id"] == "resident-agent"
    assert tab_metadata_updates[-1] == (
        "tab-agent",
        workspace_response.json()["id"],
        "Resident Repo",
        WorkspaceSessionRole.ORCHESTRATOR,
    )

    report_response = client.post(
        "/api/workspaces/sessions/resident-agent/reports",
        json={
            "task_id": task_response.json()["id"],
            "state": "started",
            "message": "Started resident task",
        },
    )

    assert report_response.status_code == 201
    board_response = client.get(f"/api/workspaces/{workspace_response.json()['id']}/board")
    assert board_response.json()["tasks"][0]["status"] == "working"


def test_start_task_replaces_stopped_resident_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    created_tabs: list[str] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        tab_id = f"tab-agent-{len(created_tabs) + 1}"
        created_tabs.append(tab_id)
        return TerminalTab(
            id=tab_id,
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            port=12346 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Restart Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "restart",
        },
    )
    workspace_id = workspace_response.json()["id"]

    first_agent = client.post(f"/api/workspaces/{workspace_id}/agent", json={}).json()
    workspace_manager.sessions[first_agent["id"]] = workspace_manager.sessions[
        first_agent["id"]
    ].model_copy(update={"status": ManagedSessionStatus.STOPPED})

    task_response = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Restart task",
            "prompt": "Dispatch after restart",
            "agent_type": "codex",
        },
    )
    response = client.post(
        f"/api/workspaces/tasks/{task_response.json()['id']}/start",
        json={},
    )

    assert response.status_code == 201
    restarted = response.json()
    assert restarted["id"] == "restart-agent"
    assert restarted["tab_id"] == "tab-agent-2"
    assert restarted["status"] == "spawning"
    assert len(created_tabs) == 2


def test_delete_task_removes_reports_and_unlinks_session(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-delete-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            port=12349,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Delete Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "delete",
        },
    )
    workspace_id = workspace_response.json()["id"]
    task_response = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Delete task",
            "prompt": "Delete me",
            "agent_type": "codex",
        },
    )
    task_id = task_response.json()["id"]
    client.post(f"/api/workspaces/tasks/{task_id}/start", json={})
    client.post(
        "/api/workspaces/sessions/delete-agent/reports",
        json={
            "task_id": task_id,
            "state": "started",
            "message": "Started before delete",
        },
    )

    response = client.delete(f"/api/workspaces/tasks/{task_id}")

    assert response.status_code == 204
    board = client.get(f"/api/workspaces/{workspace_id}/board").json()
    assert board["tasks"] == []
    assert board["reports"] == []
    assert board["sessions"][0]["task_id"] is None


def test_workspace_routes_validate_missing_resources(tmp_path: Path) -> None:
    client = TestClient(app)

    missing_path_response = client.post(
        "/api/workspaces",
        json={
            "name": "Missing Repo",
            "path": str(tmp_path / "missing"),
        },
    )
    assert missing_path_response.status_code == 400

    task_response = client.post(
        "/api/workspaces/missing-workspace/tasks",
        json={
            "title": "No workspace",
            "prompt": "Should not be created",
            "agent_type": "codex",
        },
    )
    assert task_response.status_code == 404

    update_response = client.patch("/api/workspaces/tasks/missing-task", json={"status": "done"})
    assert update_response.status_code == 404

    delete_response = client.delete("/api/workspaces/tasks/missing-task")
    assert delete_response.status_code == 404


def test_update_task_requires_status_and_persists_valid_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Patch Repo",
            "path": str(repo),
        },
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Patch task",
            "prompt": "Move task",
            "agent_type": "codex",
        },
    ).json()

    empty_response = client.patch(f"/api/workspaces/tasks/{task['id']}", json={})
    assert empty_response.status_code == 400

    done_response = client.patch(f"/api/workspaces/tasks/{task['id']}", json={"status": "done"})
    assert done_response.status_code == 200
    assert done_response.json()["status"] == "done"
