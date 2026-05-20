import asyncio
import json
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Generator, Optional

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSessionStatus,
    RemoteProfile,
    TerminalAgentStatus,
    TerminalTab,
    User,
    WorkspaceSessionRole,
    WorkspaceTaskStatus,
)
from claude_hub.services.workspace_manager import workspace_manager

workspace_module = import_module("claude_hub.services.workspace_manager")
PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGA"
    "WjR9awAAAABJRU5ErkJggg=="
)


def pass_task_review(client: TestClient, task_id: str, message: str = "Review passed"):
    reviewer_id = workspace_manager.tasks[task_id].review_session_id
    assert reviewer_id is not None
    return client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task_id,
            "state": "review_passed",
            "message": message,
        },
    )


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
        },
    )

    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "todo"
    assert task["agent_type"] == "codex"

    response = client.get(f"/api/workspaces/{workspace['id']}/board")

    assert response.status_code == 200
    board = response.json()
    assert board["workspace"]["id"] == workspace["id"]
    assert [item["id"] for item in board["tasks"]] == [task["id"]]
    assert board["sessions"] == []


def test_create_task_persists_pasted_image_attachment(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(workspace_module, "STATE_ROOT", tmp_path / "state")

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Image Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "image",
        },
    )

    response = client.post(
        f"/api/workspaces/{workspace_response.json()['id']}/tasks",
        json={
            "title": "Use screenshot",
            "prompt": "Inspect the pasted screenshot",
            "attachments": [
                {
                    "filename": "screen shot.png",
                    "mime_type": "image/png",
                    "data_url": PNG_DATA_URL,
                }
            ],
        },
    )

    assert response.status_code == 201
    task = response.json()
    assert task["attachments"][0]["filename"] == "screen-shot.png"
    attachment_path = Path(task["attachments"][0]["path"])
    assert attachment_path.exists()
    assert attachment_path.read_bytes().startswith(b"\x89PNG")


def test_spawn_worker_is_disabled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

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

    assert response.status_code == 400
    assert "Worker spawning is disabled" in response.json()["detail"]
    assert workspace_manager.sessions == {}


def test_completed_report_creates_temporary_reviewer(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    created_tabs: list[str] = []
    sent_messages: list[tuple[str, str]] = []
    renamed_tabs: list[tuple[str, str | None]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(name)
        return TerminalTab(
            id=f"review-tab-{len(created_tabs)}",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12400 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_update_tab(tab_id: str, name: Optional[str] = None, **_: object) -> TerminalTab:
        renamed_tabs.append((tab_id, name))
        return TerminalTab(
            id=tab_id,
            name=name or "unchanged",
            shell=None,
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CODEX,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12499,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
        )

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_module.ttyd_manager, "update_tab", fake_update_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Review Loop", "path": str(repo), "session_prefix": "rl"},
    ).json()
    worker = client.post(f"/api/workspaces/{workspace['id']}/agent", json={}).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Review me", "prompt": "Implement then review"},
    ).json()
    started = client.post(
        f"/api/workspaces/tasks/{task['id']}/start",
        json={"target_session_id": worker["id"]},
    ).json()

    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Done",
            "changed_files": ["frontend/src/App.vue"],
            "validation": "pnpm build",
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.WORKING
    assert updated.review_attempts == 1
    assert updated.review_session_id is not None
    reviewer = workspace_manager.sessions[updated.review_session_id]
    assert reviewer.role == WorkspaceSessionRole.REVIEWER
    assert reviewer.ephemeral is True
    assert reviewer.current_task_id == task["id"]
    assert reviewer.title == "Review me"
    assert renamed_tabs[-1] == (reviewer.tab_id, "Review me")
    assert "independent reviewer agent" in sent_messages[-2][1]
    assert "Review workspace task" in sent_messages[-1][1]


@pytest.mark.parametrize("report_state", ["ready_for_review", "blocked", "needs_input"])
def test_agent_review_gate_states_trigger_reviewer(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    report_state: str,
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
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(name)
        return TerminalTab(
            id=f"manual-review-tab-{len(created_tabs)}",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12500 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Manual Review", "path": str(repo), "session_prefix": "manual"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Manual gate", "prompt": "Stop at human review"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()

    response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": report_state,
            "message": "Needs reviewer gate",
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.WORKING
    assert updated.review_session_id is not None
    assert (
        workspace_manager.sessions[updated.review_session_id].role == WorkspaceSessionRole.REVIEWER
    )


def test_review_passed_keeps_task_in_review(
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
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(name)
        return TerminalTab(
            id=f"pass-tab-{len(created_tabs)}",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12600 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Pass Review", "path": str(repo), "session_prefix": "pass"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Pass task", "prompt": "Complete cleanly"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={"task_id": task["id"], "state": "completed", "message": "Done"},
    )
    reviewer_id = workspace_manager.tasks[task["id"]].review_session_id

    response = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_passed",
            "message": "Looks good",
            "validation": "Reviewed reported checks",
        },
    )

    assert response.status_code == 201
    assert workspace_manager.tasks[task["id"]].status == WorkspaceTaskStatus.REVIEW
    assert workspace_manager.tasks[task["id"]].review_completed_at is not None
    assert workspace_manager.sessions[reviewer_id].current_task_id is None


def test_review_failed_returns_feedback_to_original_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    created_tabs: list[str] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(name)
        return TerminalTab(
            id=f"fail-tab-{len(created_tabs)}",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12700 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Fail Review", "path": str(repo), "session_prefix": "fail"},
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Fail task", "prompt": "Needs a fix"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={"task_id": task["id"], "state": "completed", "message": "Done"},
    )
    reviewer_id = workspace_manager.tasks[task["id"]].review_session_id

    response = client.post(
        f"/api/workspaces/sessions/{reviewer_id}/reports",
        json={
            "task_id": task["id"],
            "state": "review_failed",
            "message": "Required fixes: add the missing assertion.",
        },
    )

    assert response.status_code == 201
    updated = workspace_manager.tasks[task["id"]]
    assert updated.status == WorkspaceTaskStatus.WORKING
    assert workspace_manager.sessions[started["session_id"]].current_task_id == task["id"]
    assert "Reviewer requested changes" in sent_messages[-1][1]
    assert "add the missing assertion" in sent_messages[-1][1]


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
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
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
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12346,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    sent_messages: list[tuple[str, str]] = []
    tab_metadata_updates: list[tuple[str, str | None, str | None, WorkspaceSessionRole | None]] = []
    renamed_tabs: list[tuple[str, str | None]] = []

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_update_tab(tab_id: str, name: Optional[str] = None, **_: object) -> TerminalTab:
        renamed_tabs.append((tab_id, name))
        return TerminalTab(
            id=tab_id,
            name=name or "unchanged",
            shell=None,
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CODEX,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12346,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_response.json()["id"],
            workspace_name="Resident Repo",
            workspace_role=WorkspaceSessionRole.ORCHESTRATOR,
        )

    def fake_set_tab_workspace_metadata(
        tab_id: str,
        workspace_id: str | None,
        workspace_name: str | None,
        workspace_role: WorkspaceSessionRole | None,
    ) -> bool:
        tab_metadata_updates.append((tab_id, workspace_id, workspace_name, workspace_role))
        return True

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_module.ttyd_manager, "update_tab", fake_update_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "set_tab_workspace_metadata",
        fake_set_tab_workspace_metadata,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

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
    started_task = response.json()
    assert started_task["status"] == "working"
    assert started_task["session_id"] == "resident-agent-1"
    session = workspace_manager.sessions[started_task["session_id"]]
    assert session.role == WorkspaceSessionRole.ORCHESTRATOR
    assert session.title == "Resident task"
    assert session.workspace_path == str(repo)
    assert renamed_tabs == [("tab-agent", "Resident task")]
    assert len(sent_messages) == 2
    assert "resident workspace agent" in sent_messages[0][1]
    assert "Run this in the resident terminal" in sent_messages[1][1]

    board_response = client.get(f"/api/workspaces/{workspace_response.json()['id']}/board")
    board = board_response.json()
    assert board["workspace"]["dispatcher_session_id"] is None
    assert board["tasks"][0]["status"] == "working"
    assert board["tasks"][0]["session_id"] == "resident-agent-1"
    assert tab_metadata_updates[-1] == (
        "tab-agent",
        workspace_response.json()["id"],
        "Resident Repo",
        WorkspaceSessionRole.ORCHESTRATOR,
    )

    report_response = client.post(
        "/api/workspaces/sessions/resident-agent-1/reports",
        json={
            "task_id": task_response.json()["id"],
            "state": "started",
            "message": "Started resident task",
        },
    )

    assert report_response.status_code == 201
    board_response = client.get(f"/api/workspaces/{workspace_response.json()['id']}/board")
    assert board_response.json()["tasks"][0]["status"] == "working"


def test_start_task_prefers_related_task_agent(
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
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        tab_id = f"related{len(created_tabs) + 1}-tab"
        created_tabs.append(tab_id)
        return TerminalTab(
            id=tab_id,
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12360 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    sent_messages: list[tuple[str, str]] = []

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Related Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "related",
        },
    )
    workspace_id = workspace_response.json()["id"]
    first_agent = client.post(f"/api/workspaces/{workspace_id}/agent", json={}).json()
    second_agent = client.post(f"/api/workspaces/{workspace_id}/agent", json={}).json()

    original_task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Original task", "prompt": "Use the second agent"},
    ).json()
    original_start = client.post(
        f"/api/workspaces/tasks/{original_task['id']}/start",
        json={"target_session_id": second_agent["id"]},
    )
    assert original_start.status_code == 201
    assert original_start.json()["session_id"] == second_agent["id"]

    done_response = client.patch(
        f"/api/workspaces/tasks/{original_task['id']}",
        json={"status": "done"},
    )
    assert done_response.status_code == 200

    related_task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Follow-up task",
            "prompt": "Continue with the related agent",
            "related_task_id": original_task["id"],
        },
    ).json()
    related_start = client.post(
        f"/api/workspaces/tasks/{related_task['id']}/start",
        json={},
    )

    assert related_start.status_code == 201
    started_related = related_start.json()
    assert first_agent["id"] != second_agent["id"]
    assert started_related["status"] == "working"
    assert started_related["session_id"] == second_agent["id"]
    assert started_related["dispatch_reason"] == f"Related to task {original_task['id']}"
    assert sent_messages[-1][0] == "claude-hub-related2"
    assert "Continue with the related agent" in sent_messages[-1][1]


def test_start_task_skips_offline_related_task_agent(
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
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        tab_id = f"offline-related{len(created_tabs) + 1}"
        created_tabs.append(tab_id)
        return TerminalTab(
            id=tab_id,
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12400 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Offline Related Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "offrel",
        },
    ).json()
    workspace_id = workspace["id"]
    first_agent = client.post(f"/api/workspaces/{workspace_id}/agent", json={}).json()
    second_agent = client.post(f"/api/workspaces/{workspace_id}/agent", json={}).json()

    original_task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Original offline context", "prompt": "Use first agent"},
    ).json()
    original_start = client.post(
        f"/api/workspaces/tasks/{original_task['id']}/start",
        json={"target_session_id": first_agent["id"]},
    )
    assert original_start.status_code == 201
    assert (
        client.patch(
            f"/api/workspaces/tasks/{original_task['id']}",
            json={"status": "done"},
        ).status_code
        == 200
    )

    workspace_manager.sessions[first_agent["id"]] = workspace_manager.sessions[
        first_agent["id"]
    ].model_copy(
        update={
            "status": ManagedSessionStatus.STOPPED,
            "runtime_status": AgentRuntimeStatus.OFFLINE,
        }
    )

    related_task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Related task",
            "prompt": "Continue without the offline agent",
            "related_task_id": original_task["id"],
        },
    ).json()
    related_start = client.post(
        f"/api/workspaces/tasks/{related_task['id']}/start",
        json={},
    )

    assert related_start.status_code == 201
    started_related = related_start.json()
    assert started_related["status"] == "working"
    assert started_related["session_id"] == second_agent["id"]
    assert started_related["dispatch_reason"] == "Only one workspace agent is available"


def test_tmux_pending_input_detection_matches_codex_paste_prompt() -> None:
    message = "New workspace task assigned.\n\nTask description"

    assert workspace_manager._message_still_in_input(
        "\n› N[Pasted Content 1360 chars]\n  gpt-5.5 medium · ~/repo\n",
        message,
    )
    assert workspace_manager._message_still_in_input(
        "\n› New workspace task assigned.\n\n  Task description\n",
        message,
    )
    assert not workspace_manager._message_still_in_input(
        "\n› N[Pasted Content 1360 chars]\n\n• Working\n",
        message,
    )
    assert not workspace_manager._message_still_in_input(
        "\n❯ /clear\n  ⎿ \xa0(no content)\n\n❯\xa0\n",
        "/clear",
    )


def test_auto_continue_ignores_stale_interruption_before_latest_continue() -> None:
    output = "\n".join(
        [
            "API Error: 400 unknown error",
            "",
            "› please continue",
            "",
            "⏺ 没有新指令，等待用户输入。",
            "",
            "❯ ",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) ·",
        ]
    )

    assert workspace_manager._auto_continue_interruption_reason(output) is None


def test_auto_continue_detects_current_interruption_segment() -> None:
    output = "\n".join(
        [
            "› New workspace task assigned",
            "",
            "⏺ Bash(command)",
            "  ⎿ API Error: 400 unknown error",
            "",
            "❯ ",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) ·",
        ]
    )

    assert workspace_manager._auto_continue_interruption_reason(output) == "api error"


def test_auto_continue_detects_missing_final_report_segment() -> None:
    output = "\n".join(
        [
            "› New workspace task assigned",
            "",
            "Implemented the fix.",
            "Validation: backend tests passed.",
            "Risks: no known follow-up risk.",
            "",
            "❯ ",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) ·",
        ]
    )

    assert workspace_manager._auto_continue_interruption_reason(output) is None
    assert workspace_manager._auto_continue_completion_reason(output) == "validation:"


def test_background_monitor_auto_continues_interrupted_idle_working_agent(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-api-error",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12358,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    async def fake_capture_output(_tmux_session: str) -> str:
        return "\n".join(
            [
                "› New workspace task assigned",
                "",
                '⏺ Bash(ssh merlin_dev "grep -n _rr_counter file")',
                "  ⎿ API Error: 400 unknown error",
                "",
                "❯ ",
            ]
        )

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )
    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "API Retry Repo", "path": str(repo), "session_prefix": "retry"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Retry task", "prompt": "Handle flaky API"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    sampled_at = datetime.now()
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-api-error",
            tab_name="API Retry Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.IDLE,
            status_text="Idle",
            detail="agent prompt visible",
            tmux_session="claude-hub-tab-api-",
            last_changed_at=sampled_at - timedelta(seconds=30),
            sampled_at=sampled_at,
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    assert sent_messages == []
    assert board["tasks"][0]["status"] == "working"
    assert board["sessions"][0]["runtime_status"] == "idle"
    assert board["sessions"][0]["auto_continue_task_id"] == started["id"]
    assert board["sessions"][0]["auto_continue_attempts"] == 0

    asyncio.run(
        workspace_manager._refresh_session_statuses(
            workspace["id"],
            run_auto_continue=True,
        )
    )

    session = workspace_manager.sessions[started["session_id"]]
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "claude-hub-tab-api-"
    assert "continue from the last actionable step" in sent_messages[0][1]
    assert "ready_for_review or completed report" in sent_messages[0][1]
    assert workspace_manager.tasks[started["id"]].status == WorkspaceTaskStatus.WORKING
    assert session.runtime_status == AgentRuntimeStatus.WORKING
    assert session.auto_continue_task_id == started["id"]
    assert session.auto_continue_attempts == 1


def test_non_interrupted_idle_working_agent_is_not_auto_continued(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-idle-no-error",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12362,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    async def fake_capture_output(_tmux_session: str) -> str:
        return "3 tasks (2 done, 1 in progress, 0 open)\n› "

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )
    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Idle No Error Repo", "path": str(repo), "session_prefix": "idle"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Idle task", "prompt": "Handle a normal stopped task"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    sampled_at = datetime.now()
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-idle-no-error",
            tab_name="Idle No Error Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.IDLE,
            status_text="Idle",
            detail="agent prompt visible",
            tmux_session="claude-hub-tab-idle",
            last_changed_at=sampled_at - timedelta(seconds=30),
            sampled_at=sampled_at,
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()

    assert sent_messages == []
    assert board["tasks"][0]["status"] == "working"
    assert board["sessions"][0]["runtime_status"] == "idle"
    assert board["sessions"][0]["auto_continue_task_id"] == started["id"]
    assert board["sessions"][0]["auto_continue_attempts"] == 0

    asyncio.run(
        workspace_manager._refresh_session_statuses(
            workspace["id"],
            run_auto_continue=True,
        )
    )

    session = workspace_manager.sessions[started["session_id"]]
    assert sent_messages == []
    assert workspace_manager.tasks[started["id"]].status == WorkspaceTaskStatus.WORKING
    assert session.runtime_status == AgentRuntimeStatus.IDLE
    assert session.auto_continue_task_id == started["id"]
    assert session.auto_continue_attempts == 0


def test_completed_idle_working_agent_is_prompted_to_report(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-report-missing",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12365,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    async def fake_capture_output(_tmux_session: str) -> str:
        return "\n".join(
            [
                "Implemented status transition fix.",
                "Validation: tests passed.",
                "Risks: no known risk.",
                "",
                "› ",
            ]
        )

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )
    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Report Missing Repo", "path": str(repo), "session_prefix": "report"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Report task", "prompt": "Finish but forget report"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    sent_messages.clear()

    sampled_at = datetime.now()
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-report-missing",
            tab_name="Report Missing Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.IDLE,
            status_text="Idle",
            detail="agent prompt visible",
            tmux_session="claude-hub-tab-report",
            last_changed_at=sampled_at - timedelta(seconds=30),
            sampled_at=sampled_at,
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    assert board["tasks"][0]["status"] == "working"
    assert board["sessions"][0]["runtime_status"] == "idle"

    asyncio.run(
        workspace_manager._refresh_session_statuses(
            workspace["id"],
            run_auto_continue=True,
        )
    )

    session = workspace_manager.sessions[started["session_id"]]
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == "claude-hub-tab-repo"
    assert "no workspace report was recorded" in sent_messages[0][1]
    assert "changed_files, validation, and risks" in sent_messages[0][1]
    assert workspace_manager.tasks[started["id"]].status == WorkspaceTaskStatus.WORKING
    assert session.runtime_status == AgentRuntimeStatus.WORKING
    assert session.auto_continue_attempts == 1


def test_interrupted_idle_working_agent_auto_continue_stops_after_limit(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []
    sent_messages: list[tuple[str, str]] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-api-limit",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12359,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    async def fake_capture_output(_tmux_session: str) -> str:
        return "API Error: connection reset by peer\n\n› "

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )
    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture_output)

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "API Limit Repo", "path": str(repo), "session_prefix": "limit"},
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Limit task", "prompt": "Handle repeated API failures"},
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session = workspace_manager.sessions[started["session_id"]]
    workspace_manager.sessions[session.id] = session.model_copy(
        update={
            "auto_continue_task_id": started["id"],
            "auto_continue_attempts": 10,
        }
    )
    sent_messages.clear()

    sampled_at = datetime.now()
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-api-limit",
            tab_name="API Limit Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.IDLE,
            status_text="Idle",
            detail="agent prompt visible",
            tmux_session="claude-hub-tab-api-",
            last_changed_at=sampled_at - timedelta(seconds=30),
            sampled_at=sampled_at,
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    assert sent_messages == []
    assert board["tasks"][0]["status"] == "working"
    assert board["sessions"][0]["runtime_status"] == "idle"

    asyncio.run(
        workspace_manager._refresh_session_statuses(
            workspace["id"],
            run_auto_continue=True,
        )
    )

    session = workspace_manager.sessions[started["session_id"]]
    task_after_limit = workspace_manager.tasks[started["id"]]
    assert len(sent_messages) == 2
    assert "independent reviewer agent" in sent_messages[0][1]
    assert "Review workspace task" in sent_messages[1][1]
    assert task_after_limit.status == WorkspaceTaskStatus.WORKING
    assert task_after_limit.session_id == started["session_id"]
    assert task_after_limit.review_session_id is not None
    assert session.runtime_status == AgentRuntimeStatus.ATTENTION
    assert session.auto_continue_attempts == 10


def test_review_task_stays_in_review_when_agent_runtime_is_working(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-review-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12357,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Review Repo",
            "path": str(repo),
            "session_prefix": "review",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Review task",
            "prompt": "Do work then wait for review",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session_id = started["session_id"]

    review_response = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert review_response.status_code == 201
    pass_response = pass_task_review(client, task["id"])
    assert pass_response.status_code == 201
    assert (
        client.get(f"/api/workspaces/{workspace['id']}/board").json()["tasks"][0]["status"]
        == "review"
    )
    reviewed_at = workspace_manager.tasks[task["id"]].reviewed_at
    assert reviewed_at is not None
    workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
        update={
            "status": WorkspaceTaskStatus.WORKING,
            "updated_at": reviewed_at,
        }
    )

    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-review-agent",
            tab_name="Review Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.WORKING,
            status_text="Working",
            detail="agent is processing",
            tmux_session="claude-hub-tab-revi",
            last_changed_at=reviewed_at,
            sampled_at=reviewed_at,
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()

    assert board["tasks"][0]["status"] == "review"
    assert board["tasks"][0]["session_id"] == session_id
    assert board["sessions"][0]["runtime_status"] == "working"
    assert board["sessions"][0]["current_task_id"] == task["id"]


def test_review_task_moves_to_working_when_agent_has_new_working_activity(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-review-continued",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12361,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Review Continued Repo",
            "path": str(repo),
            "session_prefix": "continued",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Review continued task",
            "prompt": "Do work then wait for review",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session_id = started["session_id"]

    review_response = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert review_response.status_code == 201
    pass_response = pass_task_review(client, task["id"])
    assert pass_response.status_code == 201
    reviewed_at = workspace_manager.tasks[task["id"]].reviewed_at
    assert reviewed_at is not None
    assert (
        client.get(f"/api/workspaces/{workspace['id']}/board").json()["tasks"][0]["status"]
        == "review"
    )

    continued_at = reviewed_at + timedelta(seconds=30)
    assert continued_at > reviewed_at
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-review-continued",
            tab_name="Review Continued Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.WORKING,
            status_text="Working",
            detail="agent is processing follow-up",
            tmux_session="claude-hub-tab-cont",
            last_changed_at=continued_at,
            sampled_at=continued_at,
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()

    assert board["tasks"][0]["status"] == "working"
    assert board["tasks"][0]["session_id"] == session_id
    assert board["sessions"][0]["runtime_status"] == "working"
    assert board["sessions"][0]["current_task_id"] == task["id"]


def test_latest_ready_report_does_not_override_later_working_activity(
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
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-stale-working",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12364,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return []

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Stale Working Repo",
            "path": str(repo),
            "session_prefix": "stale",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Stale working task",
            "prompt": "Do work then report review",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()

    review_response = client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert review_response.status_code == 201
    pass_response = pass_task_review(client, task["id"])
    assert pass_response.status_code == 201
    review_created_at = datetime.fromisoformat(pass_response.json()["created_at"])
    stale_started_at = review_created_at + timedelta(seconds=30)
    workspace_manager.tasks[task["id"]] = workspace_manager.tasks[task["id"]].model_copy(
        update={
            "status": WorkspaceTaskStatus.WORKING,
            "started_at": stale_started_at,
            "updated_at": stale_started_at,
        }
    )

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()

    assert board["tasks"][0]["status"] == "working"
    assert board["tasks"][0]["reviewed_at"] == pass_response.json()["created_at"]


def test_fresh_ready_report_is_not_immediately_reopened_by_runtime_working(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-fresh-ready",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12366,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Fresh Ready Repo",
            "path": str(repo),
            "session_prefix": "fresh",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Fresh ready task",
            "prompt": "Report ready while terminal still updates",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session_id = started["session_id"]

    review_response = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert review_response.status_code == 201
    pass_response = pass_task_review(client, task["id"])
    assert pass_response.status_code == 201
    reviewed_at = workspace_manager.tasks[task["id"]].reviewed_at
    assert reviewed_at is not None
    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-fresh-ready",
            tab_name="Fresh Ready Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.WORKING,
            status_text="Working",
            detail="agent is still finalizing report output",
            tmux_session="claude-hub-tab-fresh",
            last_changed_at=reviewed_at + timedelta(seconds=5),
            sampled_at=reviewed_at + timedelta(seconds=5),
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()

    assert board["tasks"][0]["status"] == "review"
    assert board["sessions"][0]["runtime_status"] == "working"


@pytest.mark.parametrize(
    ("activity_delay_seconds", "expected_task_status"),
    [(5, "review"), (30, "working")],
)
def test_completed_review_task_reopens_only_after_runtime_grace(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    activity_delay_seconds: int,
    expected_task_status: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    status_samples: list[TerminalAgentStatus] = []

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-completed-review",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12363,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return status_samples

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Completed Review Repo",
            "path": str(repo),
            "session_prefix": "completed",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Completed review task",
            "prompt": "Do work then report completion",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session_id = started["session_id"]

    completed_response = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Ready for review",
        },
    )
    assert completed_response.status_code == 201
    pass_response = pass_task_review(client, task["id"])
    assert pass_response.status_code == 201
    reviewed_at = workspace_manager.tasks[task["id"]].reviewed_at
    assert reviewed_at is not None

    status_samples[:] = [
        TerminalAgentStatus(
            tab_id="tab-completed-review",
            tab_name="Completed Review Repo Agent 1",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.WORKING,
            status_text="Working",
            detail="agent is processing follow-up",
            tmux_session="claude-hub-tab-done",
            last_changed_at=reviewed_at + timedelta(seconds=activity_delay_seconds),
            sampled_at=reviewed_at + timedelta(seconds=activity_delay_seconds),
        )
    ]

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()

    assert board["tasks"][0]["status"] == expected_task_status
    assert board["tasks"][0]["session_id"] == session_id
    assert board["sessions"][0]["runtime_status"] == "working"


def test_continue_task_marks_working_before_send_verification_failure(
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
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-continue-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12360,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    async def fake_list_statuses(*_args, **_kwargs) -> list[TerminalAgentStatus]:
        return []

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_module.ttyd_manager,
        "list_tab_agent_statuses",
        fake_list_statuses,
    )
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Continue Repo",
            "path": str(repo),
            "session_prefix": "continue",
        },
    ).json()
    client.post(f"/api/workspaces/{workspace['id']}/agent", json={})
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Continue task",
            "prompt": "Do work then wait for review",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    session_id = started["session_id"]

    review_response = client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
        json={
            "task_id": task["id"],
            "state": "ready_for_review",
            "message": "Ready for review",
        },
    )
    assert review_response.status_code == 201
    pass_response = pass_task_review(client, task["id"])
    assert pass_response.status_code == 201
    assert (
        client.get(f"/api/workspaces/{workspace['id']}/board").json()["tasks"][0]["status"]
        == "review"
    )

    async def fake_send_session_message(_session_id: str, _message: str) -> None:
        raise RuntimeError("submit verification failed after delivery")

    monkeypatch.setattr(workspace_manager, "send_session_message", fake_send_session_message)

    continue_response = client.post(
        f"/api/workspaces/tasks/{task['id']}/continue",
        json={"message": "Please address review feedback"},
    )
    assert continue_response.status_code == 400

    board = client.get(f"/api/workspaces/{workspace['id']}/board").json()
    assert board["tasks"][0]["status"] == "working"
    assert board["sessions"][0]["status"] == "working"
    assert board["sessions"][0]["runtime_status"] == "working"
    assert board["sessions"][0]["current_task_id"] == task["id"]
    assert [report["state"] for report in board["reports"]] == [
        "ready_for_review",
        "review_passed",
        "working",
    ]


def test_remote_workspace_default_agent_uses_local_tab(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_dir = tmp_path / "workspace-state"
    local_dir.mkdir()
    created_tabs: list[dict[str, object]] = []

    monkeypatch.setattr(
        workspace_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: RemoteProfile(
            id=profile_id,
            name="DevBox",
            ssh_host="devbox",
            default_cwd="~/default",
        ),
    )

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(
            {
                "cwd": cwd,
                "solo_mode": solo_mode,
                "target": target,
                "remote_profile_id": remote_profile_id,
                "remote_cwd": remote_cwd,
                "remote_forward_port": remote_forward_port,
            }
        )
        return TerminalTab(
            id="tab-default-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12354,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Remote Env",
            "path": str(local_dir),
            "session_prefix": "remote",
            "target": "remote",
            "remote_profile_id": "devbox",
        },
    )
    workspace_id = workspace_response.json()["id"]

    agent_response = client.post(
        f"/api/workspaces/{workspace_id}/agent",
        json={"agent_type": "codex", "role": "orchestrator"},
    )

    assert agent_response.status_code == 201
    session = agent_response.json()
    assert session["workspace_path"] == str(local_dir)
    assert session["target"] == "local"
    assert session["remote_profile_id"] is None
    assert session["remote_cwd"] is None
    assert session["solo_mode"] is True
    assert session["remote_forward_port"] is None
    assert created_tabs[0]["cwd"] == str(local_dir)
    assert created_tabs[0]["target"] == ExecutionTarget.LOCAL
    assert created_tabs[0]["solo_mode"] is True
    assert created_tabs[0]["remote_profile_id"] is None
    assert created_tabs[0]["remote_forward_port"] is None


def test_remote_workspace_explicit_remote_agent_uses_remote_tab_and_forwarded_reports(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_dir = tmp_path / "workspace-state"
    local_dir.mkdir()
    created_tabs: list[dict[str, object]] = []
    sent_messages: list[str] = []

    monkeypatch.setattr(
        workspace_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: RemoteProfile(
            id=profile_id,
            name="DevBox",
            ssh_host="devbox",
            default_cwd="~/default",
        ),
    )

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(
            {
                "cwd": cwd,
                "target": target,
                "remote_profile_id": remote_profile_id,
                "remote_cwd": remote_cwd,
                "remote_reconnect": remote_reconnect,
                "remote_forward_port": remote_forward_port,
            }
        )
        return TerminalTab(
            id="tab-remote-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12355,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, message: str) -> None:
        sent_messages.append(message)

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Remote Env",
            "path": str(local_dir),
            "session_prefix": "remote",
            "target": "remote",
            "remote_profile_id": "devbox",
            "remote_cwd": "~/repo",
            "remote_reconnect": True,
        },
    )
    assert workspace_response.status_code == 201

    agent_response = client.post(
        f"/api/workspaces/{workspace_response.json()['id']}/agent",
        json={"agent_type": "codex", "role": "orchestrator", "target": "remote"},
    )

    assert agent_response.status_code == 201
    session = agent_response.json()
    assert session["workspace_path"] == "~/repo"
    assert session["target"] == "remote"
    assert session["remote_profile_id"] == "devbox"
    assert session["remote_cwd"] == "~/repo"
    assert session["solo_mode"] is True
    assert session["remote_forward_port"] == 18173
    assert created_tabs[0]["cwd"] is None
    assert created_tabs[0]["target"] == ExecutionTarget.REMOTE
    assert created_tabs[0]["remote_profile_id"] == "devbox"
    assert created_tabs[0]["remote_cwd"] == "~/repo"
    assert created_tabs[0]["remote_forward_port"] == 18173
    assert "SSH development target: DevBox (devbox)" in sent_messages[0]
    assert "Remote working directory: ~/repo" in sent_messages[0]
    assert "http://127.0.0.1:18173/api/workspaces" in sent_messages[0]


def test_create_agent_can_override_target_and_yolo_mode(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_dir = tmp_path / "workspace-state"
    local_dir.mkdir()
    created_tabs: list[dict[str, object]] = []

    monkeypatch.setattr(
        workspace_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: RemoteProfile(
            id=profile_id,
            name="DevBox",
            ssh_host="devbox",
            default_cwd="~/default",
        ),
    )

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        created_tabs.append(
            {
                "name": name,
                "cwd": cwd,
                "solo_mode": solo_mode,
                "agent_type": agent_type,
                "target": target,
                "remote_profile_id": remote_profile_id,
                "remote_cwd": remote_cwd,
                "remote_reconnect": remote_reconnect,
                "remote_forward_port": remote_forward_port,
            }
        )
        return TerminalTab(
            id="tab-advanced-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12356,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(_tmux_session: str, _message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

    client = TestClient(app)
    workspace_response = client.post(
        "/api/workspaces",
        json={
            "name": "Mixed Env",
            "path": str(local_dir),
            "session_prefix": "mixed",
        },
    )
    workspace_id = workspace_response.json()["id"]

    agent_response = client.post(
        f"/api/workspaces/{workspace_id}/agent",
        json={
            "agent_type": "claude",
            "title": "Remote careful agent",
            "role": "orchestrator",
            "target": "remote",
            "remote_profile_id": "devbox",
            "remote_cwd": "~/agent-work",
            "remote_reconnect": False,
            "solo_mode": False,
        },
    )

    assert agent_response.status_code == 201
    session = agent_response.json()
    assert session["target"] == "remote"
    assert session["workspace_path"] == "~/agent-work"
    assert session["remote_profile_id"] == "devbox"
    assert session["remote_cwd"] == "~/agent-work"
    assert session["remote_reconnect"] is False
    assert session["solo_mode"] is False
    assert session["remote_forward_port"] == 18173
    assert created_tabs[0]["name"] == "Remote careful agent"
    assert created_tabs[0]["cwd"] is None
    assert created_tabs[0]["target"] == ExecutionTarget.REMOTE
    assert created_tabs[0]["solo_mode"] is False
    assert created_tabs[0]["remote_profile_id"] == "devbox"
    assert created_tabs[0]["remote_cwd"] == "~/agent-work"
    assert created_tabs[0]["remote_reconnect"] is False


def test_start_task_does_not_dispatch_to_stopped_resident_agent(
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
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
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
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12346 + len(created_tabs),
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    sent_messages: list[tuple[str, str]] = []

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent_messages.append((tmux_session, message))

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

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
    ].model_copy(
        update={
            "status": ManagedSessionStatus.STOPPED,
            "runtime_status": AgentRuntimeStatus.OFFLINE,
        }
    )

    task_response = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={
            "title": "Offline task",
            "prompt": "Do not dispatch to offline agent",
            "agent_type": "codex",
        },
    )
    response = client.post(
        f"/api/workspaces/tasks/{task_response.json()['id']}/start",
        json={},
    )

    assert response.status_code == 400
    assert "No idle or working workspace agent is available" in response.json()["detail"]
    assert "restart-agent-2" not in workspace_manager.sessions
    assert len(created_tabs) == 1
    assert len(sent_messages) == 1
    board = client.get(f"/api/workspaces/{workspace_id}/board").json()
    assert board["tasks"][0]["status"] == "todo"
    assert board["tasks"][0]["session_id"] is None


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
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
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
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12349,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )

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
    start_response = client.post(f"/api/workspaces/tasks/{task_id}/start", json={})
    session_id = start_response.json()["session_id"]
    client.post(
        f"/api/workspaces/sessions/{session_id}/reports",
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


def test_done_task_writes_delete_safe_task_record(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_root = tmp_path / "workspace-state"

    async def fake_create_tab(
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: WorkspaceSessionRole | None = None,
    ) -> TerminalTab:
        return TerminalTab(
            id="tab-record-agent",
            name=name,
            shell=shell,
            cwd=cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            port=12350,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
        )

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        return None

    async def fake_ensure_session_ready(_session) -> None:
        return None

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(
        workspace_manager,
        "_ensure_session_ready_for_send",
        fake_ensure_session_ready,
    )
    monkeypatch.setattr(
        workspace_manager,
        "_workspace_dir",
        lambda workspace_id: state_root / workspace_id,
    )

    client = TestClient(app)
    workspace = client.post(
        "/api/workspaces",
        json={
            "name": "Record Repo",
            "path": str(repo),
            "default_branch": "main",
            "session_prefix": "record",
        },
    ).json()
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={
            "title": "Record task",
            "prompt": "Archive this task",
            "agent_type": "codex",
        },
    ).json()
    started = client.post(f"/api/workspaces/tasks/{task['id']}/start", json={}).json()
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "working",
            "message": "Implementing archive",
            "changed_files": ["backend/claude_hub/services/workspace_manager.py"],
            "validation": "pytest planned",
        },
    )
    client.post(
        f"/api/workspaces/sessions/{started['session_id']}/reports",
        json={
            "task_id": task["id"],
            "state": "completed",
            "message": "Archive complete",
            "changed_files": ["backend/tests/test_workspaces.py"],
            "risks": "None",
        },
    )

    done_response = client.patch(
        f"/api/workspaces/tasks/{task['id']}",
        json={"status": "done"},
    )

    assert done_response.status_code == 200
    record_files = list((state_root / workspace["id"] / "task_records").glob("*.json"))
    assert len(record_files) == 1
    record = json.loads(record_files[0].read_text(encoding="utf-8"))
    assert record["schema_version"] == 1
    assert record["task"]["id"] == task["id"]
    assert record["task"]["status"] == "done"
    assert record["session"]["id"] == started["session_id"]
    assert [report["state"] for report in record["reports"]] == ["working", "completed"]
    assert record["artifacts"]["changed_files"] == [
        "backend/claude_hub/services/workspace_manager.py",
        "backend/tests/test_workspaces.py",
    ]
    assert record["artifacts"]["validation"] == ["pytest planned"]
    assert record["artifacts"]["risks"] == ["None"]
    assert record["artifacts"]["commits"] == []
    assert record["final_summary"] == "Archive complete"
    assert [event["type"] for event in record["timeline"]].count("agent_report") == 2

    delete_response = client.delete(f"/api/workspaces/tasks/{task['id']}")

    assert delete_response.status_code == 204
    assert record_files[0].exists()


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
