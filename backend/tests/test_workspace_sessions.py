"""Tests for the workspace-session lifecycle endpoints not exercised elsewhere.

The large ``test_workspaces.py`` suite drives task CRUD, the board, and the
review/dispatch state machine, but a handful of session-management routes are
only reachable when a ``ManagedSession`` already exists. These tests build a
session directly in the in-memory manager (mirroring the helper used in
``test_workspaces.py``) and exercise:

* ``GET /api/workspaces``            – list workspaces
* ``DELETE /api/workspaces/sessions/{id}``       – 404 / 400-blocked / success
* ``POST  /api/workspaces/sessions/{id}/send``   – 404 / success
* ``POST  /api/workspaces/tasks/{id}/dispatch-decision`` – 404 unknown task

``ttyd_manager.delete_tab`` and the tmux send path are stubbed so nothing
touches a real terminal.
"""

from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ChatMode,
    EnsureWorkspaceAgentRequest,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    SpawnWorkerRequest,
    TerminalTab,
    User,
    WorkspaceSessionRole,
    WorkspaceTaskStatus,
)
from claude_hub.services.workspace_manager import workspace_manager

workspace_module = import_module("claude_hub.services.workspace_manager")


@pytest.fixture(autouse=True)
def isolated_workspace_manager(monkeypatch: MonkeyPatch) -> Generator[None, None, None]:
    """Clear the in-memory manager and bypass auth/state persistence per test."""
    workspace_manager.workspaces.clear()
    workspace_manager.tasks.clear()
    workspace_manager.sessions.clear()
    workspace_manager.reports.clear()
    workspace_manager._dispatch_locks.clear()
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)
    monkeypatch.setattr(workspace_manager, "_write_task_record", lambda _task: None)

    async def fake_current_user() -> User:
        return User(open_id="local", name="Local User", email="local@localhost", avatar_url=None)

    app.dependency_overrides[get_current_user] = fake_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)
    workspace_manager.workspaces.clear()
    workspace_manager.tasks.clear()
    workspace_manager.sessions.clear()
    workspace_manager.reports.clear()
    workspace_manager._dispatch_locks.clear()


def _make_workspace(client: TestClient, repo: Path, *, name: str = "Sess Repo") -> dict:
    result: dict = client.post(
        "/api/workspaces",
        json={"name": name, "path": str(repo), "session_prefix": "sess"},
    ).json()
    return result


def _make_session(
    workspace_id: str, repo: Path, *, session_id: str = "sess-worker-1"
) -> ManagedSession:
    now = datetime.now()
    session = ManagedSession(
        id=session_id,
        workspace_id=workspace_id,
        task_id=None,
        tab_id=f"tab-{session_id}",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CODEX,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Worker 1",
        branch=None,
        workspace_path=str(repo),
        tmux_session="claude-hub-tab-sess",
        target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_cwd=None,
        remote_reconnect=True,
        solo_mode=True,
        remote_forward_port=None,
        created_at=now,
        updated_at=now,
    )
    workspace_manager.sessions[session_id] = session
    return session


# ── GET /api/workspaces ─────────────────────────────────────────────────────


def test_list_workspaces_returns_created_workspaces(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)

    created = _make_workspace(client, repo, name="Listed Repo")

    response = client.get("/api/workspaces")

    assert response.status_code == 200
    ids = [ws["id"] for ws in response.json()]
    assert created["id"] in ids


def test_workspace_agent_request_rejects_chat_surface_fields(tmp_path: Path) -> None:
    repo = tmp_path / "repo-chat-reject"
    repo.mkdir()
    client = TestClient(app)
    workspace = _make_workspace(client, repo)

    response = client.post(
        f"/api/workspaces/{workspace['id']}/agent",
        json={"session_kind": "chat", "chat_mode": "plan"},
    )

    assert response.status_code == 422
    assert "session_kind" in response.text

    with pytest.raises(ValueError, match="always use Terminal"):
        SpawnWorkerRequest.model_validate({"session_kind": "chat"})


@pytest.mark.asyncio
async def test_managed_session_creation_never_inherits_chat_tab_surface(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repo = tmp_path / "repo-managed-terminal"
    repo.mkdir()
    client = TestClient(app)
    workspace_payload = _make_workspace(client, repo)
    workspace = workspace_manager.workspaces[workspace_payload["id"]]

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        return TerminalTab(
            id="mock-chat-tab",
            name=str(kwargs["name"]),
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            chat_mode=ChatMode.PLAN,
            target=ExecutionTarget.LOCAL,
            port=12345,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            workspace_role=WorkspaceSessionRole.WORKER,
        )

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)

    session = await workspace_manager._create_managed_session(
        workspace,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            role=WorkspaceSessionRole.WORKER,
        ),
    )

    assert session.session_kind == SessionKind.TERMINAL
    assert session.chat_mode == ChatMode.DEFAULT


def test_list_workspaces_empty_when_none_exist(tmp_path: Path) -> None:
    # The autouse fixture clears the manager, so the list starts empty.
    client = TestClient(app)
    response = client.get("/api/workspaces")
    assert response.status_code == 200
    assert response.json() == []


# ── DELETE /api/workspaces/sessions/{id} ────────────────────────────────────


def test_delete_session_unknown_returns_404(tmp_path: Path) -> None:
    client = TestClient(app)
    response = client.delete("/api/workspaces/sessions/ghost-session")
    assert response.status_code == 404


def test_delete_session_succeeds_for_idle_session(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)

    deleted_tabs: list[str] = []

    async def fake_delete_tab(tab_id: str) -> None:
        deleted_tabs.append(tab_id)

    monkeypatch.setattr(workspace_module.ttyd_manager, "delete_tab", fake_delete_tab)

    workspace = _make_workspace(client, repo)
    session = _make_session(workspace["id"], repo)

    response = client.delete(f"/api/workspaces/sessions/{session.id}")

    assert response.status_code == 204
    assert session.id not in workspace_manager.sessions
    assert session.tab_id in deleted_tabs


def test_delete_session_removes_agent_when_tab_cleanup_fails(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)

    async def fake_delete_tab(_tab_id: str) -> None:
        raise RuntimeError("cli unregister failed")

    monkeypatch.setattr(workspace_module.ttyd_manager, "delete_tab", fake_delete_tab)

    workspace = _make_workspace(client, repo)
    session = _make_session(workspace["id"], repo)

    response = client.delete(f"/api/workspaces/sessions/{session.id}")

    assert response.status_code == 204
    assert session.id not in workspace_manager.sessions


def test_delete_session_blocked_by_active_task_returns_400(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)

    async def fake_delete_tab(tab_id: str) -> None:  # pragma: no cover - must not run
        raise AssertionError("delete_tab should not be called when blocked")

    monkeypatch.setattr(workspace_module.ttyd_manager, "delete_tab", fake_delete_tab)

    workspace = _make_workspace(client, repo)
    session = _make_session(workspace["id"], repo)

    # Attach a non-DONE task owned by this session so deletion is refused.
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks",
        json={"title": "Busy", "prompt": "work", "agent_type": "codex"},
    ).json()
    stored = workspace_manager.tasks[task["id"]]
    workspace_manager.tasks[task["id"]] = stored.model_copy(
        update={"session_id": session.id, "status": WorkspaceTaskStatus.WORKING}
    )

    response = client.delete(f"/api/workspaces/sessions/{session.id}")

    assert response.status_code == 400
    # Session is preserved when deletion is refused.
    assert session.id in workspace_manager.sessions


# ── POST /api/workspaces/sessions/{id}/send ─────────────────────────────────


def test_send_session_message_unknown_returns_404(tmp_path: Path) -> None:
    client = TestClient(app)
    response = client.post(
        "/api/workspaces/sessions/ghost-session/send",
        json={"message": "hello"},
    )
    assert response.status_code == 404


def test_send_session_message_delivers_to_session(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)

    sent: list[tuple[str, str]] = []

    async def fake_send_tmux_message(tmux_session: str, message: str) -> None:
        sent.append((tmux_session, message))

    async def fake_ensure_ready(_session: object) -> None:
        return None

    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send_tmux_message)
    monkeypatch.setattr(workspace_manager, "_ensure_session_ready_for_send", fake_ensure_ready)

    workspace = _make_workspace(client, repo)
    session = _make_session(workspace["id"], repo)

    response = client.post(
        f"/api/workspaces/sessions/{session.id}/send",
        json={"message": "continue please"},
    )

    assert response.status_code == 204
    assert sent and sent[-1][1] == "continue please"


# ── POST /api/workspaces/tasks/{id}/dispatch-decision ───────────────────────


def test_dispatch_decision_unknown_task_returns_404(tmp_path: Path) -> None:
    client = TestClient(app)
    response = client.post(
        "/api/workspaces/tasks/missing-task/dispatch-decision",
        json={"target_session_id": "whatever"},
    )
    assert response.status_code == 404
