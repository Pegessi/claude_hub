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

import logging
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
    from claude_hub.services.agent_stream import tailer

    monkeypatch.setattr(tailer, "_DELETION_FENCED_SESSION_IDS", set())
    monkeypatch.setattr(tailer, "_PROVIDER_FENCED_SESSION_IDS", set())
    workspace_manager.workspaces.clear()
    workspace_manager.tasks.clear()
    workspace_manager.sessions.clear()
    workspace_manager.reports.clear()
    workspace_manager._dispatch_locks.clear()
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)
    monkeypatch.setattr(workspace_manager, "_save_workspace_state", lambda _workspace_id: None)
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


def test_workspace_agent_request_rejects_managed_chat_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo-chat-mode-reject"
    repo.mkdir()
    client = TestClient(app)
    workspace = _make_workspace(client, repo)

    response = client.post(
        f"/api/workspaces/{workspace['id']}/agent",
        json={"session_kind": "chat", "chat_mode": "plan"},
    )

    assert response.status_code == 422
    assert "chat_mode" in response.text

    with pytest.raises(ValueError, match="always use Terminal"):
        SpawnWorkerRequest.model_validate({"session_kind": "chat"})


@pytest.mark.asyncio
async def test_managed_session_creation_preserves_requested_chat_surface(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repo = tmp_path / "repo-managed-chat"
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
            chat_mode=ChatMode.DEFAULT,
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
            session_kind=SessionKind.CHAT,
            role=WorkspaceSessionRole.WORKER,
        ),
    )

    assert session.session_kind == SessionKind.CHAT
    assert session.chat_mode == ChatMode.DEFAULT


@pytest.mark.asyncio
async def test_managed_session_records_requested_env_preset(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """The requested preset identity is persisted on the session so later
    sessions (auto-created reviewers) can inherit it. The merged KEY=VALUE
    pairs are forwarded to the launch tab; the preset name itself is non-secret
    and survives a state reload."""
    repo = tmp_path / "repo-env-preset"
    repo.mkdir()
    client = TestClient(app)
    workspace_payload = _make_workspace(client, repo)
    workspace = workspace_manager.workspaces[workspace_payload["id"]]
    launched: dict[str, object] = {}

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        launched.update(kwargs)
        return TerminalTab(  # type: ignore[call-arg]
            id="mock-preset-tab",
            name=str(kwargs["name"]),
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CLAUDE,
            target=ExecutionTarget.LOCAL,
            port=12347,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            workspace_role=WorkspaceSessionRole.WORKER,
            env=kwargs["env"],  # type: ignore[arg-type]
        )

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)

    session = await workspace_manager._create_managed_session(
        workspace,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            role=WorkspaceSessionRole.WORKER,
            env={"FOO": "bar"},
            env_preset="day1",
        ),
    )

    assert session.env_preset == "day1"
    assert launched["env"] == {"FOO": "bar"}

    # Round-trips through persistence (optional field; older sessions load None).
    reloaded = ManagedSession.model_validate(session.model_dump(mode="json"))
    assert reloaded.env_preset == "day1"


def test_agent_create_rolls_back_session_when_bootstrap_submit_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed bootstrap must not leave a registered idle session/tab."""
    repo = tmp_path / "repo-bootstrap-failure"
    repo.mkdir()
    client = TestClient(app, raise_server_exceptions=False)
    workspace = _make_workspace(client, repo, name="Bootstrap Failure Repo")
    deleted_tabs: list[str] = []

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        return TerminalTab(
            id="bootstrap-failure-tab",
            name=str(kwargs["name"]),
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CLAUDE,
            target=ExecutionTarget.LOCAL,
            port=12346,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=workspace["id"],
            workspace_name="Bootstrap Failure Repo",
            workspace_role=WorkspaceSessionRole.DISPATCHER,
        )

    async def fail_bootstrap(_session_id: str, _message: str) -> None:
        raise RuntimeError("Failed to submit workspace agent message")

    async def fake_delete_tab(tab_id: str) -> bool:
        deleted_tabs.append(tab_id)
        return True

    monkeypatch.setattr(workspace_module.ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(workspace_module.ttyd_manager, "delete_tab", fake_delete_tab)
    monkeypatch.setattr(workspace_manager, "send_session_message", fail_bootstrap)

    with caplog.at_level(logging.ERROR):
        response = client.post(
            f"/api/workspaces/{workspace['id']}/agent",
            json={
                "agent_type": "claude",
                "role": "dispatcher",
                "reuse_existing": False,
                "ephemeral": True,
            },
        )

    assert response.status_code == 502
    assert "failed to initialize" in response.json()["detail"].lower()
    assert workspace_manager.sessions == {}
    assert workspace_manager.workspaces[workspace["id"]].dispatcher_session_id is None
    assert deleted_tabs == ["bootstrap-failure-tab"]
    assert any(
        "Failed to initialize workspace agent" in record.getMessage() for record in caplog.records
    )


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


def test_delete_session_retains_agent_until_tab_cleanup_retry_succeeds(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    client = TestClient(app)
    delete_attempts = 0
    discarded: list[str] = []

    async def fake_delete_tab(_tab_id: str) -> None:
        nonlocal delete_attempts
        delete_attempts += 1
        if delete_attempts == 1:
            raise RuntimeError("cli unregister failed")

    async def fake_discard(_workspace_id: str, session_id: str, **_kwargs) -> None:
        discarded.append(session_id)

    import claude_hub.services.agent_stream as agent_stream

    monkeypatch.setattr(workspace_module.ttyd_manager, "delete_tab", fake_delete_tab)
    monkeypatch.setattr(agent_stream, "discard_session_stream", fake_discard)

    workspace = _make_workspace(client, repo)
    session = _make_session(workspace["id"], repo)

    failed = client.delete(f"/api/workspaces/sessions/{session.id}")
    assert failed.status_code == 400
    assert session.id in workspace_manager.sessions
    assert workspace_manager.sessions[session.id].runtime_status == AgentRuntimeStatus.ATTENTION
    assert discarded == []

    retried = client.delete(f"/api/workspaces/sessions/{session.id}")
    assert retried.status_code == 204
    assert session.id not in workspace_manager.sessions
    assert discarded == [session.id]
    assert delete_attempts == 2


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


@pytest.fixture
def archive_runtime(monkeypatch: MonkeyPatch) -> list[str]:
    """Stub resource owners, retaining the real stream fence and disk store."""
    import claude_hub.services.managed_chat as managed_chat

    # Own the observation manager instead of inheriting a shutdown/quiesced
    # singleton left by application-lifespan tests earlier in the full suite.
    stream_api = import_module("claude_hub.api.agent_stream")
    from claude_hub.services.agent_stream import TailerManager

    observation = TailerManager(session_getter=workspace_manager.sessions.get)
    monkeypatch.setattr(stream_api, "_get_tailer_manager", lambda: observation)
    calls: list[str] = []

    async def archive_tab(tab_id: str):
        calls.append(f"archive:{tab_id}")
        return object()

    async def unarchive_tab(tab_id: str):
        calls.append(f"unarchive:{tab_id}")
        return object()

    class Runtime:
        async def ensure_started(self, session):
            assert not session.archived
            assert session.agent_session_id == "provider-conversation"
            calls.append("restart")

    monkeypatch.setattr(workspace_module.ttyd_manager, "archive_tab", archive_tab)
    monkeypatch.setattr(workspace_module.ttyd_manager, "unarchive_tab", unarchive_tab)
    monkeypatch.setattr(managed_chat, "_manager", lambda: Runtime())
    return calls


def _archive_chat(client: TestClient, repo: Path) -> tuple[dict, ManagedSession, str]:
    repo.mkdir()
    workspace = _make_workspace(client, repo)
    session = _make_session(workspace["id"], repo)
    session = session.model_copy(
        update={
            "session_kind": SessionKind.CHAT,
            "agent_session_id": "provider-conversation",
            "agent_session_id_verified": True,
            "role": WorkspaceSessionRole.ORCHESTRATOR,
        }
    )
    workspace_manager.sessions[session.id] = session
    return workspace, session, f"/api/workspaces/{workspace['id']}/sessions/{session.id}"


@pytest.mark.asyncio
async def test_archive_restore_retains_history_and_provider_identity(
    tmp_path: Path, archive_runtime: list[str], monkeypatch: MonkeyPatch
) -> None:
    from claude_hub.models import AgentStreamEvent, AgentStreamEventType
    from claude_hub.services.agent_stream import AgentStreamStore
    from claude_hub.services.agent_stream.tailer import _DELETION_FENCED_SESSION_IDS

    client = TestClient(app)
    workspace, session, url = _archive_chat(client, tmp_path / "repo")
    store = AgentStreamStore(workspace["id"], session.id)
    await store.append(
        AgentStreamEvent(
            session_id=session.id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            stream_sequence=-1,
            created_at=datetime.now(),
            type=AgentStreamEventType.TEXT_DELTA,
            payload={"text": "Retain this conversation"},
        )
    )
    history = store.path.read_bytes()
    workspace_manager.workspaces[workspace["id"]] = workspace_manager.workspaces[
        workspace["id"]
    ].model_copy(
        update={
            "resident_agent_session_id": session.id,
            "resident_agent_enabled": True,
            "dispatcher_session_id": session.id,
        }
    )
    response = client.post(url + "/archive")
    assert response.status_code == 200, response.text
    archived_at = response.json()["archived_at"]
    assert response.json()["archived"] and archived_at
    assert session.id in _DELETION_FENCED_SESSION_IDS
    assert archive_runtime == [f"archive:{session.tab_id}"]
    assert client.post(url + "/archive").json()["archived_at"] == archived_at
    assert len(archive_runtime) == 1
    assert store.path.read_bytes() == history
    assert workspace_manager.sessions_for_workspace(workspace["id"]) == []
    assert (
        len(workspace_manager.sessions_for_workspace(workspace["id"], include_archived=True)) == 1
    )
    assert workspace_manager._workspace_agents(workspace["id"], include_stopped=True) == []
    assert not workspace_manager._can_assign_or_queue_to(workspace_manager.sessions[session.id])
    assert not workspace_manager._can_dispatch_to(workspace_manager.sessions[session.id])
    board = client.get(f"/api/workspaces/{workspace['id']}/board")
    assert board.status_code == 200 and board.json()["sessions"] == []
    assert (
        len(
            client.get(f"/api/workspaces/{workspace['id']}/board?include_archived=true").json()[
                "sessions"
            ]
        )
        == 1
    )
    current = workspace_manager.workspaces[workspace["id"]]
    assert current.dispatcher_session_id is None
    assert current.resident_agent_session_id is None and not current.resident_agent_enabled
    replay = client.get(f"/api/workspaces/sessions/{session.id}/stream/events")
    assert replay.status_code == 200, replay.text
    assert any(
        event["payload"].get("text") == "Retain this conversation"
        for event in replay.json()["events"]
    )
    assert (
        client.post(
            f"/api/workspaces/sessions/{session.id}/send", json={"message": "hello"}
        ).status_code
        == 400
    )
    restored = client.post(url + "/unarchive")
    assert restored.status_code == 200, restored.text
    assert not restored.json()["archived"] and restored.json()["archived_at"] is None
    assert restored.json()["agent_session_id"] == "provider-conversation"
    assert restored.json()["agent_session_id_verified"] is True
    assert session.id not in _DELETION_FENCED_SESSION_IDS
    assert archive_runtime[-2:] == [f"unarchive:{session.tab_id}", "restart"]
    assert len(workspace_manager.sessions_for_workspace(workspace["id"])) == 1
    assert client.post(url + "/unarchive").status_code == 200
    assert archive_runtime.count("restart") == 1
    assert store.path.read_bytes() == history


@pytest.mark.parametrize(
    "status", [WorkspaceTaskStatus.QUEUED, WorkspaceTaskStatus.WORKING, WorkspaceTaskStatus.REVIEW]
)
@pytest.mark.parametrize("binding", ["session_id", "review_session_id"])
def test_archive_refuses_nonterminal_task_bindings(
    tmp_path: Path, archive_runtime: list[str], status: WorkspaceTaskStatus, binding: str
) -> None:
    client = TestClient(app)
    workspace, session, url = _archive_chat(client, tmp_path / "repo")
    task = client.post(
        f"/api/workspaces/{workspace['id']}/tasks", json={"title": "Busy", "prompt": "work"}
    ).json()
    stored = workspace_manager.tasks[task["id"]]
    workspace_manager.tasks[task["id"]] = stored.model_copy(
        update={binding: session.id, "status": status}
    )
    response = client.post(url + "/archive")
    assert response.status_code == 409, response.text
    assert not workspace_manager.sessions[session.id].archived
    assert archive_runtime == []


def test_archive_rejects_wrong_workspace_and_terminal(
    tmp_path: Path, archive_runtime: list[str]
) -> None:
    client = TestClient(app)
    _, session, url = _archive_chat(client, tmp_path / "repo")
    assert client.post(f"/api/workspaces/wrong/sessions/{session.id}/archive").status_code == 404
    workspace_manager.sessions[session.id] = session.model_copy(
        update={"session_kind": SessionKind.TERMINAL}
    )
    for action in ("archive", "unarchive"):
        response = client.post(url + "/" + action)
        assert response.status_code == 409
        assert "Chat only" in response.json()["detail"]
    assert archive_runtime == []


def test_archive_old_state_normalization(tmp_path: Path) -> None:
    client = TestClient(app)
    _, session, _ = _archive_chat(client, tmp_path / "repo")
    legacy = session.model_dump(
        mode="json", exclude={"archived", "archived_at", "archive_provider_fenced"}
    )
    restored = ManagedSession.model_validate(workspace_manager._normalize_session_item(legacy))
    assert restored.archived is False and restored.archived_at is None
    assert not restored.archive_provider_fenced


@pytest.mark.asyncio
async def test_delete_archived_session_discards_retained_history(
    tmp_path: Path, archive_runtime: list[str], monkeypatch: MonkeyPatch
) -> None:
    from claude_hub.models import AgentStreamEvent, AgentStreamEventType
    from claude_hub.services.agent_stream import AgentStreamStore

    client = TestClient(app)
    workspace, session, url = _archive_chat(client, tmp_path / "repo")
    store = AgentStreamStore(workspace["id"], session.id)
    await store.append(
        AgentStreamEvent(
            session_id=session.id,
            tab_id=session.tab_id,
            agent_type=session.agent_type,
            stream_sequence=-1,
            created_at=datetime.now(),
            type=AgentStreamEventType.TEXT_DELTA,
            payload={"text": "history"},
        )
    )

    async def delete_tab(_tab_id: str) -> bool:
        return True

    monkeypatch.setattr(workspace_module.ttyd_manager, "delete_tab", delete_tab)
    assert client.post(url + "/archive").status_code == 200
    assert store.path.exists()
    assert client.delete(f"/api/workspaces/sessions/{session.id}").status_code == 204
    assert session.id not in workspace_manager.sessions
    assert not store.path.exists()


def test_archive_failure_retains_quarantined_owner(
    tmp_path: Path, archive_runtime: list[str], monkeypatch: MonkeyPatch
) -> None:
    client = TestClient(app)
    _, session, url = _archive_chat(client, tmp_path / "repo")

    async def fail(_tab_id: str):
        raise RuntimeError("runtime stop failed")

    monkeypatch.setattr(workspace_module.ttyd_manager, "archive_tab", fail)
    response = client.post(url + "/archive")
    assert response.status_code == 409
    retained = workspace_manager.sessions[session.id]
    assert not retained.archived and retained.chat_paused
    assert retained.runtime_status == AgentRuntimeStatus.ATTENTION
    assert retained.agent_session_id == session.agent_session_id


@pytest.mark.asyncio
async def test_archive_cancellation_finishes_under_lifecycle_locks(
    tmp_path: Path, archive_runtime: list[str], monkeypatch: MonkeyPatch
) -> None:
    import asyncio

    client = TestClient(app)
    workspace, session, _ = _archive_chat(client, tmp_path / "repo")
    entered, release = asyncio.Event(), asyncio.Event()

    async def archive_tab(_tab_id: str):
        entered.set()
        await release.wait()
        return object()

    monkeypatch.setattr(workspace_module.ttyd_manager, "archive_tab", archive_tab)
    archiving = asyncio.create_task(workspace_manager.archive_session(workspace["id"], session.id))
    await asyncio.wait_for(entered.wait(), 1)
    archiving.cancel()
    await asyncio.sleep(0)
    assert not archiving.done()
    assert workspace_manager._pump_locks[session.id].locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(archiving, 1)
    assert workspace_manager.sessions[session.id].archived
    assert not workspace_manager._pump_locks[session.id].locked()


def test_unarchive_restart_failure_is_best_effort(
    tmp_path: Path, archive_runtime: list[str], monkeypatch: MonkeyPatch
) -> None:
    import claude_hub.services.managed_chat as managed_chat

    client = TestClient(app)
    _, session, url = _archive_chat(client, tmp_path / "repo")
    assert client.post(url + "/archive").status_code == 200

    class UnavailableRuntime:
        async def ensure_started(self, _session):
            raise RuntimeError("provider offline")

    monkeypatch.setattr(managed_chat, "_manager", lambda: UnavailableRuntime())
    response = client.post(url + "/unarchive")
    assert response.status_code == 200
    assert not workspace_manager.sessions[session.id].archived
    assert response.json()["agent_session_id"] == session.agent_session_id


def test_archive_excludes_reuse_and_reviewer_selection_even_if_idle(tmp_path: Path) -> None:
    client = TestClient(app)
    workspace, session, _ = _archive_chat(client, tmp_path / "repo")
    archived = session.model_copy(update={"archived": True})
    workspace_manager.sessions[session.id] = archived
    request = EnsureWorkspaceAgentRequest(agent_type=AgentType.CODEX, session_kind=SessionKind.CHAT)
    assert (
        workspace_manager._find_compatible_workspace_agent(
            workspace_manager.workspaces[workspace["id"]], request
        )
        is None
    )
    workspace_manager.sessions[session.id] = archived.model_copy(
        update={"role": WorkspaceSessionRole.REVIEWER}
    )
    assert (
        workspace_manager._first_available_reviewer(
            workspace["id"],
            target=ExecutionTarget.LOCAL,
            cwd=str(tmp_path / "repo"),
            session_kind=SessionKind.CHAT,
        )
        is None
    )


@pytest.mark.parametrize("operation", ["archive", "unarchive"])
def test_archive_state_commit_failure_retains_owner_for_retry(
    tmp_path: Path, archive_runtime: list[str], monkeypatch: MonkeyPatch, operation: str
) -> None:
    client = TestClient(app)
    _, session, url = _archive_chat(client, tmp_path / "repo")
    if operation == "unarchive":
        assert client.post(url + "/archive").status_code == 200

    def fail_save() -> None:
        raise RuntimeError("state storage unavailable")

    monkeypatch.setattr(workspace_manager, "_save_state", fail_save)
    response = client.post(url + "/" + operation)
    assert response.status_code == 409
    retained = workspace_manager.sessions[session.id]
    assert retained.archived == (operation == "unarchive")
    assert retained.chat_paused
    assert retained.agent_session_id == "provider-conversation"
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)
    assert client.post(url + "/" + operation).status_code == 200
    assert workspace_manager.sessions[session.id].archived == (operation == "archive")


def test_archive_validates_delivery_before_retiring_tab(
    tmp_path: Path, archive_runtime: list[str], monkeypatch: MonkeyPatch
) -> None:
    import claude_hub.services.managed_chat as managed_chat

    client = TestClient(app)
    _, session, url = _archive_chat(client, tmp_path / "repo")

    def reject(*_args, **_kwargs):
        raise RuntimeError("delivery is uncertain")

    monkeypatch.setattr(managed_chat, "validate_delivery_state_discard", reject)
    response = client.post(url + "/archive")
    assert response.status_code == 409
    assert archive_runtime == []
    assert session.id in workspace_manager.sessions
    assert not workspace_manager.sessions[session.id].archived


def test_archive_preserves_provider_identity_captured_during_shutdown(
    tmp_path: Path, archive_runtime: list[str], monkeypatch: MonkeyPatch
) -> None:
    client = TestClient(app)
    _, session, url = _archive_chat(client, tmp_path / "repo")

    async def archive_tab(_tab_id: str) -> object:
        workspace_manager.set_session_agent_session_id(session.id, "captured-on-stop")
        return object()

    monkeypatch.setattr(workspace_module.ttyd_manager, "archive_tab", archive_tab)
    response = client.post(url + "/archive")
    assert response.status_code == 200
    assert response.json()["agent_session_id"] == "captured-on-stop"
    assert response.json()["agent_session_id_verified"] is True


@pytest.mark.parametrize("delete_scope", ["session", "workspace"])
def test_archived_session_delete_after_reload_retains_verified_fence(
    tmp_path: Path, archive_runtime: list[str], monkeypatch: MonkeyPatch, delete_scope: str
) -> None:
    import json

    from claude_hub.services.agent_stream import AgentStreamStore, tailer

    client = TestClient(app)
    workspace, session, url = _archive_chat(client, tmp_path / "repo")
    store = AgentStreamStore(workspace["id"], session.id)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = store.path.with_suffix(".maso-delivery.json")
    sidecar.write_text(json.dumps({"uncertain": False}))
    # A previously verified shutdown is the same evidence archive obtains from
    # a live owner; the native transport test covers that acquisition path.
    tailer._PROVIDER_FENCED_SESSION_IDS.add(session.id)
    assert client.post(url + "/archive").status_code == 200
    saved = tmp_path / "archived-session.json"
    saved.write_text(json.dumps(workspace_manager._workspace_state_payload(workspace["id"])))
    workspace_manager.sessions.clear()
    for item in json.loads(saved.read_text())["sessions"]:
        restored = ManagedSession.model_validate(workspace_manager._normalize_session_item(item))
        workspace_manager.sessions[restored.id] = restored
    assert workspace_manager.sessions[session.id].archive_provider_fenced
    tailer._PROVIDER_FENCED_SESSION_IDS.clear()
    tailer._DELETION_FENCED_SESSION_IDS.clear()

    async def delete_tab(_tab_id: str) -> bool:
        return True

    monkeypatch.setattr(workspace_module.ttyd_manager, "delete_tab", delete_tab)
    target = (
        f"/api/workspaces/sessions/{session.id}"
        if delete_scope == "session"
        else f"/api/workspaces/{workspace['id']}"
    )
    response = client.delete(target)
    assert response.status_code == 204, response.text
    assert session.id not in workspace_manager.sessions
    assert not sidecar.exists()


@pytest.mark.parametrize(
    "archived,verified,uncertain", [(True, True, True), (True, False, False), (False, True, False)]
)
def test_archived_fence_does_not_bypass_uncertain_or_unverified_delivery(
    tmp_path: Path, archive_runtime: list[str], archived: bool, verified: bool, uncertain: bool
) -> None:
    import json

    from claude_hub.services.agent_stream import AgentStreamStore

    client = TestClient(app)
    workspace, session, _ = _archive_chat(client, tmp_path / "repo")
    workspace_manager.sessions[session.id] = session.model_copy(
        update={"archived": archived, "archive_provider_fenced": verified}
    )
    store = AgentStreamStore(workspace["id"], session.id)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = store.path.with_suffix(".maso-delivery.json")
    sidecar.write_text(json.dumps({"uncertain": uncertain}))
    response = client.delete(f"/api/workspaces/sessions/{session.id}")
    assert response.status_code == 400, response.text
    assert session.id in workspace_manager.sessions
    assert sidecar.exists()
