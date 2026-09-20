"""Placement and fail-closed contracts for remote Chat / agent / reviewer."""

from datetime import datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    EnsureWorkspaceAgentRequest,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    RemoteProfile,
    SessionKind,
    TerminalTabCreate,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)
from claude_hub.models.schemas import CHAT_REMOTE_UNSUPPORTED, STDIN_SHELL_REMOTE_UNSUPPORTED
from claude_hub.services import remote_profiles as remote_profiles_module
from claude_hub.services.remote_profiles import RemoteProfileManager
from claude_hub.services.ttyd_manager import TTYDManager
from claude_hub.services.workspace_manager import WorkspaceManager


def test_terminal_tab_create_rejects_chat_remote() -> None:
    with pytest.raises(ValidationError) as exc:
        TerminalTabCreate(
            name="remote-chat",
            session_kind=SessionKind.CHAT,
            agent_type=AgentType.CODEX,
            target=ExecutionTarget.REMOTE,
            remote_profile_id="mac_mini",
            remote_cwd="~",
        )
    assert CHAT_REMOTE_UNSUPPORTED in str(exc.value)


def test_terminal_tab_create_allows_terminal_remote() -> None:
    tab = TerminalTabCreate(
        name="remote-term",
        session_kind=SessionKind.TERMINAL,
        agent_type=AgentType.TERMINAL,
        target=ExecutionTarget.REMOTE,
        remote_profile_id="mac_mini",
        remote_cwd="~",
    )
    assert tab.target == ExecutionTarget.REMOTE


@pytest.mark.asyncio
async def test_tabs_api_rejects_chat_remote(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/tabs",
        json={
            "name": "remote-chat",
            "session_kind": "chat",
            "agent_type": "codex",
            "target": "remote",
            "remote_profile_id": "mac_mini",
            "remote_cwd": "~",
        },
    )
    assert resp.status_code == 422
    assert "Chat sessions run on the Hub host" in resp.text


def test_ssh_config_strips_inline_host_comments(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    cfg = tmp_path / "config"
    cfg.write_text(
        "Host merlin_persistent                             # 自定义，必须为 ASCII 字符\n"
        "    HostName example.invalid\n"
        "Host merlin_dev\n"
        "    HostName example.invalid\n"
        "Host mac_mini\n"
        "    HostName 127.0.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(remote_profiles_module, "SSH_CONFIG_FILE", cfg)
    manager = RemoteProfileManager(path=tmp_path / "missing.json")
    profiles = {profile.id: profile for profile in manager.list_profiles()}
    assert "ASCII" not in profiles
    assert "#" not in profiles
    assert profiles["merlin_persistent"].stdin_shell is False
    assert profiles["merlin_dev"].stdin_shell is True
    assert profiles["mac_mini"].stdin_shell is False


def _session(
    *,
    session_id: str,
    workspace_id: str,
    role: WorkspaceSessionRole,
    target: ExecutionTarget = ExecutionTarget.LOCAL,
    remote_profile_id: str | None = None,
    remote_cwd: str | None = None,
    runtime_status: AgentRuntimeStatus = AgentRuntimeStatus.IDLE,
) -> ManagedSession:
    now = datetime.now()
    return ManagedSession(
        id=session_id,
        workspace_id=workspace_id,
        task_id=None,
        tab_id=f"tab-{session_id}",
        role=role,
        agent_type=AgentType.CODEX,
        status=ManagedSessionStatus.IDLE,
        runtime_status=runtime_status,
        title=session_id,
        workspace_path="/tmp/ws",
        tmux_session=f"claude-hub-{session_id[:8]}",
        target=target,
        remote_profile_id=remote_profile_id,
        remote_cwd=remote_cwd,
        created_at=now,
        updated_at=now,
    )


def _local_workspace_with_remote_worker(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> tuple[WorkspaceManager, object, WorkspaceTask, ManagedSession]:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = manager.create_workspace(
        WorkspaceCreate(name="Local WS", path=str(repo), session_prefix="localws")
    )
    worker = _session(
        session_id="localws-agent-1",
        workspace_id=workspace.id,
        role=WorkspaceSessionRole.ORCHESTRATOR,
        target=ExecutionTarget.REMOTE,
        remote_profile_id="mac_mini",
        remote_cwd="~",
    )
    manager.sessions[worker.id] = worker
    now = datetime.now()
    task = WorkspaceTask(
        id="task-remote-worker",
        workspace_id=workspace.id,
        title="Review me",
        prompt="do the thing",
        agent_type=AgentType.CODEX,
        task_mode=WorkspaceTaskMode.REVIEWED,
        status=WorkspaceTaskStatus.WORKING,
        session_id=worker.id,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    return manager, workspace, task, worker


def test_reviewer_placement_follows_remote_worker_not_local_workspace(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager, workspace, task, _worker = _local_workspace_with_remote_worker(tmp_path, monkeypatch)
    placement = manager._reviewer_placement(workspace, task)
    assert placement["target"] == ExecutionTarget.REMOTE
    assert placement["remote_profile_id"] == "mac_mini"
    assert placement["remote_cwd"] == "~"


@pytest.mark.asyncio
async def test_auto_reviewer_does_not_reuse_local_reviewer_for_remote_worker(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager, workspace, task, _worker = _local_workspace_with_remote_worker(tmp_path, monkeypatch)
    idle_local_reviewer = _session(
        session_id="localws-reviewer-1",
        workspace_id=workspace.id,
        role=WorkspaceSessionRole.REVIEWER,
        target=ExecutionTarget.LOCAL,
    )
    manager.sessions[idle_local_reviewer.id] = idle_local_reviewer
    created: list[object] = []

    async def fake_ensure(workspace_id: str, payload: object) -> ManagedSession:
        created.append(payload)
        session = _session(
            session_id="localws-reviewer-new",
            workspace_id=workspace_id,
            role=WorkspaceSessionRole.REVIEWER,
            target=payload.target,  # type: ignore[attr-defined]
            remote_profile_id=payload.remote_profile_id,  # type: ignore[attr-defined]
            remote_cwd=payload.remote_cwd,  # type: ignore[attr-defined]
        )
        manager.sessions[session.id] = session
        return session

    monkeypatch.setattr(manager, "ensure_workspace_agent", fake_ensure)

    reviewer = await manager._select_or_create_reviewer(workspace, task)

    assert reviewer.id != idle_local_reviewer.id
    assert reviewer.target == ExecutionTarget.REMOTE
    assert reviewer.remote_profile_id == "mac_mini"
    assert created
    assert created[0].target == ExecutionTarget.REMOTE  # type: ignore[attr-defined]
    assert created[0].remote_profile_id == "mac_mini"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_auto_reviewer_reuses_matching_remote_idle_reviewer(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager, workspace, task, _worker = _local_workspace_with_remote_worker(tmp_path, monkeypatch)
    idle_remote_reviewer = _session(
        session_id="localws-reviewer-remote",
        workspace_id=workspace.id,
        role=WorkspaceSessionRole.REVIEWER,
        target=ExecutionTarget.REMOTE,
        remote_profile_id="mac_mini",
        remote_cwd="~",
    )
    manager.sessions[idle_remote_reviewer.id] = idle_remote_reviewer

    async def fail_ensure(*_args: object, **_kwargs: object) -> ManagedSession:
        raise AssertionError("matching remote reviewer must be reused")

    monkeypatch.setattr(manager, "ensure_workspace_agent", fail_ensure)

    reviewer = await manager._select_or_create_reviewer(workspace, task)
    assert reviewer.id == idle_remote_reviewer.id


@pytest.mark.asyncio
async def test_create_tab_rejects_chat_remote() -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {}
    manager._tab_order = []

    with pytest.raises(ValueError, match="Chat sessions run on the Hub host"):
        await manager.create_tab(
            name="chat-remote",
            session_kind=SessionKind.CHAT,
            agent_type=AgentType.CODEX,
            target=ExecutionTarget.REMOTE,
            remote_profile_id="mac_mini",
            remote_cwd="~",
        )


CODEX_UPDATE_DIALOG = (
    "  ✨ Update available! 0.150.1 -> 0.152.0\n"
    "\n"
    "  Release notes: https://github.com/openai/codex/releases/latest\n"
    "\n"
    "› 1. Update now (runs `npm install -g @openai/codex`)\n"
    "  2. Skip\n"
    "  3. Skip until next version\n"
    "\n"
    "  Press enter to continue\n"
)
CODEX_READY_PROMPT = "OpenAI Codex\n? for shortcuts\n"


def test_agent_input_ready_rejects_codex_update_dialog() -> None:
    manager = WorkspaceManager()
    assert manager._agent_update_dialog(CODEX_UPDATE_DIALOG)
    assert not manager._agent_input_ready(CODEX_UPDATE_DIALOG)
    assert manager._agent_input_ready(CODEX_READY_PROMPT)


@pytest.mark.asyncio
async def test_wait_for_agent_prompt_skips_codex_update_dialog(
    monkeypatch: MonkeyPatch,
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    messaging = __import__(
        "claude_hub.services.workspace_manager._messaging", fromlist=["AGENT_PROMPT_WAIT_SECONDS"]
    )
    monkeypatch.setattr(messaging, "AGENT_PROMPT_WAIT_SECONDS", 2.0)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(manager, "_prompt_wait_sleep", no_sleep)

    panes = [CODEX_UPDATE_DIALOG, CODEX_UPDATE_DIALOG, CODEX_READY_PROMPT]
    keys: list[tuple[str, ...]] = []

    async def fake_capture(_tmux_session: str) -> str:
        return panes.pop(0) if panes else CODEX_READY_PROMPT

    async def fake_run(*args: str) -> None:
        keys.append(args)

    monkeypatch.setattr(manager, "_capture_tmux_output", fake_capture)
    monkeypatch.setattr(manager, "_run_tmux", fake_run)

    session = _session(
        session_id="ws-agent-1",
        workspace_id="ws",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        target=ExecutionTarget.REMOTE,
        remote_profile_id="mac_mini",
        remote_cwd="~",
    )
    await manager._wait_for_agent_prompt(session)
    assert ("send-keys", "-t", session.tmux_session, "Down") in keys
    assert ("send-keys", "-t", session.tmux_session, "Enter") in keys


@pytest.mark.asyncio
async def test_wait_for_agent_prompt_fails_closed_if_update_dialog_stays(
    monkeypatch: MonkeyPatch,
) -> None:
    from claude_hub.services.workspace_manager import WorkspaceAgentInitializationError

    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    messaging = __import__(
        "claude_hub.services.workspace_manager._messaging", fromlist=["AGENT_PROMPT_WAIT_SECONDS"]
    )
    monkeypatch.setattr(messaging, "AGENT_PROMPT_WAIT_SECONDS", 0.05)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(manager, "_prompt_wait_sleep", no_sleep)

    async def fake_capture(_tmux_session: str) -> str:
        return CODEX_UPDATE_DIALOG

    async def fake_run(*_args: str) -> None:
        return None

    monkeypatch.setattr(manager, "_capture_tmux_output", fake_capture)
    monkeypatch.setattr(manager, "_run_tmux", fake_run)
    session = _session(
        session_id="ws-agent-1",
        workspace_id="ws",
        role=WorkspaceSessionRole.ORCHESTRATOR,
    )
    with pytest.raises(WorkspaceAgentInitializationError, match="npm install -g"):
        await manager._wait_for_agent_prompt(session)


def test_next_remote_forward_port_uses_hub_port_plus_offset(
    monkeypatch: MonkeyPatch,
) -> None:
    from claude_hub.config import settings
    from claude_hub.services.workspace_manager._constants import (
        REMOTE_FORWARD_PORT_OFFSET,
    )

    monkeypatch.setattr(settings, "port", 18273)
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    assert manager._next_remote_forward_port() == 18273 + REMOTE_FORWARD_PORT_OFFSET
    occupied = _session(
        session_id="ws-agent-1",
        workspace_id="ws",
        role=WorkspaceSessionRole.ORCHESTRATOR,
    )
    occupied.remote_forward_port = 28273
    manager.sessions[occupied.id] = occupied
    assert manager._next_remote_forward_port() == 28274


@pytest.mark.asyncio
async def test_tabs_api_rejects_stdin_shell_remote(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    merlin = RemoteProfile(id="merlin_dev", name="merlin_dev", ssh_host="merlin_dev")
    monkeypatch.setattr(
        remote_profiles_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: merlin if profile_id == "merlin_dev" else None,
    )
    resp = await client.post(
        "/api/tabs",
        json={
            "name": "merlin-term",
            "session_kind": "terminal",
            "agent_type": "terminal",
            "target": "remote",
            "remote_profile_id": "merlin_dev",
            "remote_cwd": "~",
        },
    )
    assert resp.status_code == 400
    assert STDIN_SHELL_REMOTE_UNSUPPORTED in resp.text


@pytest.mark.asyncio
async def test_ensure_workspace_agent_rejects_stdin_shell(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    merlin = RemoteProfile(id="merlin_dev", name="merlin_dev", ssh_host="merlin_dev")
    monkeypatch.setattr(
        remote_profiles_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: merlin if profile_id == "merlin_dev" else None,
    )
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = manager.create_workspace(
        WorkspaceCreate(name="Merlin WS", path=str(repo), session_prefix="merlinws")
    )
    with pytest.raises(ValueError, match="no usable remote TTY"):
        await manager.ensure_workspace_agent(
            workspace.id,
            EnsureWorkspaceAgentRequest(
                agent_type=AgentType.TERMINAL,
                target=ExecutionTarget.REMOTE,
                remote_profile_id="merlin_dev",
                remote_cwd="~",
            ),
        )
