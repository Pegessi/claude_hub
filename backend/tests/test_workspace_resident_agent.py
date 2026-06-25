"""Tests for the per-workspace resident self-driven agent and workspace deletion."""

import asyncio
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Generator

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentReport,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    Workspace,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
    WorkspaceUpdate,
)
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")


@pytest.fixture()
def state_root(monkeypatch: MonkeyPatch, tmp_path: Path) -> Generator[Path, None, None]:
    """Redirect all workspace persistence to a hermetic tmp directory."""
    root = tmp_path / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    index_file = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    # _save_state / _load_state resolve INDEX_FILE from the submodule globals
    # (populated via ``from ._constants import *``); patch them too.
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)
    yield root


@pytest.fixture()
def manager(state_root: Path) -> WorkspaceManager:
    return WorkspaceManager()


def _make_workspace(
    manager: WorkspaceManager, tmp_path: Path, name: str = "Resident WS"
) -> Workspace:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return manager.create_workspace(
        WorkspaceCreate(name=name, path=str(repo), session_prefix="res")
    )


def test_create_workspace_resident_defaults(manager: WorkspaceManager, tmp_path: Path) -> None:
    workspace = _make_workspace(manager, tmp_path)
    assert workspace.resident_agent_enabled is False
    assert workspace.resident_agent_interval_minutes == 60
    assert workspace.resident_agent_directive is None
    assert workspace.resident_agent_session_id is None
    assert workspace.resident_agent_last_run_at is None


def test_create_workspace_resident_config(manager: WorkspaceManager, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_interval_minutes=30,
            resident_agent_directive="  daily lint sweep  ",
        )
    )
    assert workspace.resident_agent_enabled is True
    assert workspace.resident_agent_interval_minutes == 30
    assert workspace.resident_agent_directive == "daily lint sweep"


def test_update_workspace_persists_resident_config(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    updated = manager.update_workspace(
        workspace.id,
        WorkspaceUpdate(
            resident_agent_enabled=True,
            resident_agent_interval_minutes=15,
            resident_agent_directive="  keep deps current  ",
        ),
    )
    assert updated.resident_agent_enabled is True
    assert updated.resident_agent_interval_minutes == 15
    assert updated.resident_agent_directive == "keep deps current"

    # Empty/whitespace directive trims to None.
    cleared = manager.update_workspace(
        workspace.id, WorkspaceUpdate(resident_agent_directive="   ")
    )
    assert cleared.resident_agent_directive is None

    # Reload from disk to confirm persistence.
    reloaded = WorkspaceManager()
    persisted = reloaded.workspaces[workspace.id]
    assert persisted.resident_agent_enabled is True
    assert persisted.resident_agent_interval_minutes == 15


def test_update_workspace_rejects_interval_below_one(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    with pytest.raises(ValueError):
        manager.update_workspace(workspace.id, WorkspaceUpdate(resident_agent_interval_minutes=0))


def _due_workspace(last_run_at, *, enabled: bool = True, interval: int = 60) -> Workspace:
    now = datetime.now()
    return Workspace(
        id="ws",
        name="ws",
        path="/tmp",
        default_branch="main",
        session_prefix="ws",
        resident_agent_enabled=enabled,
        resident_agent_interval_minutes=interval,
        resident_agent_last_run_at=last_run_at,
        created_at=now,
        updated_at=now,
    )


def test_resident_due_check(manager: WorkspaceManager) -> None:
    now = datetime.now()
    # Never run => due immediately.
    assert manager._resident_agent_due(_due_workspace(None), now) is True
    # Just run => not due.
    assert manager._resident_agent_due(_due_workspace(now), now) is False
    # Ran longer ago than the interval => due.
    old = now - timedelta(minutes=61)
    assert manager._resident_agent_due(_due_workspace(old), now) is True
    # Within the interval => not due.
    recent = now - timedelta(minutes=5)
    assert manager._resident_agent_due(_due_workspace(recent), now) is False
    # Disabled => never due even if overdue.
    assert manager._resident_agent_due(_due_workspace(old, enabled=False), now) is False


def test_delete_workspace_purges_state_and_dir(
    manager: WorkspaceManager, tmp_path: Path, state_root: Path
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    other = _make_workspace(manager, tmp_path, name="Other WS")

    now = datetime.now()
    session = ManagedSession(
        id="res-agent-1",
        workspace_id=workspace.id,
        task_id=None,
        tab_id="tab-1",
        role=WorkspaceSessionRole.RESIDENT,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Resident",
        workspace_path=str(tmp_path),
        tmux_session="claude-hub-tab1",
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    task = WorkspaceTask(
        id="task-1",
        workspace_id=workspace.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        task_mode=WorkspaceTaskMode.REVIEWED,
        status=WorkspaceTaskStatus.WORKING,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    report = AgentReport(
        id="rep-1",
        workspace_id=workspace.id,
        task_id=task.id,
        session_id=session.id,
        state=AgentReportState.WORKING,
        message="m",
        created_at=now,
    )
    manager.reports[report.id] = report
    manager._save_state()

    deleted_tabs: list[str] = []

    async def fake_delete_tab(tab_id: str) -> None:
        deleted_tabs.append(tab_id)

    workspace_dir = state_root / workspace.id
    assert workspace_dir.exists()

    original = _wm.ttyd_manager.delete_tab
    _wm.ttyd_manager.delete_tab = fake_delete_tab  # type: ignore[assignment]
    try:
        asyncio.run(manager.delete_workspace(workspace.id))
    finally:
        _wm.ttyd_manager.delete_tab = original  # type: ignore[assignment]

    assert workspace.id not in manager.workspaces
    assert other.id in manager.workspaces  # untouched
    assert task.id not in manager.tasks
    assert report.id not in manager.reports
    assert session.id not in manager.sessions
    assert deleted_tabs == ["tab-1"]
    assert not workspace_dir.exists()


def test_delete_workspace_missing_raises_keyerror(manager: WorkspaceManager) -> None:
    with pytest.raises(KeyError):
        asyncio.run(manager.delete_workspace("does-not-exist"))


def _resident_session(
    workspace: Workspace,
    *,
    runtime_status: AgentRuntimeStatus = AgentRuntimeStatus.IDLE,
    status: ManagedSessionStatus = ManagedSessionStatus.IDLE,
) -> ManagedSession:
    now = datetime.now()
    return ManagedSession(
        id="res-agent-1",
        workspace_id=workspace.id,
        task_id=None,
        tab_id="tab-res-1",
        role=WorkspaceSessionRole.RESIDENT,
        agent_type=AgentType.CLAUDE,
        status=status,
        runtime_status=runtime_status,
        current_task_id=None,
        queued_count=0,
        title="Resident",
        workspace_path=workspace.path,
        tmux_session="claude-hub-tab-res-1",
        created_at=now,
        updated_at=now,
    )


def test_run_resident_agent_skips_when_busy(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A WORKING resident is skipped: no send, and the timer is not advanced."""
    workspace = _make_workspace(manager, tmp_path)
    workspace = manager.update_workspace(workspace.id, WorkspaceUpdate(resident_agent_enabled=True))
    session = _resident_session(workspace, runtime_status=AgentRuntimeStatus.WORKING)
    manager.sessions[session.id] = session
    workspace = workspace.model_copy(update={"resident_agent_session_id": session.id})
    manager.workspaces[workspace.id] = workspace

    sends: list[tuple[str, str]] = []

    async def fake_send(session_id: str, message: str) -> None:
        sends.append((session_id, message))

    async def fail_ensure(*args: object, **kwargs: object) -> ManagedSession:
        raise AssertionError("ensure_workspace_agent must not be called when busy")

    monkeypatch.setattr(manager, "send_session_message", fake_send)
    monkeypatch.setattr(manager, "ensure_workspace_agent", fail_ensure)

    asyncio.run(manager._run_resident_agent(workspace))

    assert sends == []
    # Timer not advanced so it retries next tick.
    assert manager.workspaces[workspace.id].resident_agent_last_run_at is None


def test_run_resident_agent_new_session_no_double_send(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A freshly created resident already got the bootstrap prompt; do not resend."""
    workspace = _make_workspace(manager, tmp_path)
    workspace = manager.update_workspace(workspace.id, WorkspaceUpdate(resident_agent_enabled=True))
    workspace = manager.workspaces[workspace.id]

    created = _resident_session(workspace)
    sends: list[tuple[str, str]] = []

    async def fake_send(session_id: str, message: str) -> None:
        sends.append((session_id, message))

    ensure_calls: list[object] = []

    async def fake_ensure(workspace_id: str, payload: object) -> ManagedSession:
        ensure_calls.append(payload)
        # Mirror real ensure_workspace_agent: register the session so it exists.
        manager.sessions[created.id] = created
        return created

    monkeypatch.setattr(manager, "send_session_message", fake_send)
    monkeypatch.setattr(manager, "ensure_workspace_agent", fake_ensure)

    asyncio.run(manager._run_resident_agent(workspace))

    assert len(ensure_calls) == 1
    # Bootstrap (delivered inside ensure_workspace_agent, mocked here) is the only
    # prompt; _run_resident_agent must NOT send a second copy on the create path.
    assert sends == []
    persisted = manager.workspaces[workspace.id]
    assert persisted.resident_agent_session_id == created.id
    assert persisted.resident_agent_last_run_at is not None


def test_run_resident_agent_reuse_sends_one_prompt(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Reusing an existing idle resident sends exactly one self-drive prompt."""
    workspace = _make_workspace(manager, tmp_path)
    workspace = manager.update_workspace(workspace.id, WorkspaceUpdate(resident_agent_enabled=True))
    session = _resident_session(workspace, runtime_status=AgentRuntimeStatus.IDLE)
    manager.sessions[session.id] = session
    workspace = workspace.model_copy(update={"resident_agent_session_id": session.id})
    manager.workspaces[workspace.id] = workspace

    sends: list[tuple[str, str]] = []

    async def fake_send(session_id: str, message: str) -> None:
        sends.append((session_id, message))

    async def fail_ensure(*args: object, **kwargs: object) -> ManagedSession:
        raise AssertionError("ensure_workspace_agent must not be called on reuse path")

    monkeypatch.setattr(manager, "send_session_message", fake_send)
    monkeypatch.setattr(manager, "ensure_workspace_agent", fail_ensure)

    asyncio.run(manager._run_resident_agent(workspace))

    assert len(sends) == 1
    assert sends[0][0] == session.id
    assert "RESIDENT self-driven maintenance agent" in sends[0][1]
    persisted = manager.workspaces[workspace.id]
    assert persisted.resident_agent_session_id == session.id
    assert persisted.resident_agent_last_run_at is not None


def test_run_resident_agent_persists_session_before_send_failure(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A send failure on the reuse path must not lose the persisted session/timer."""
    workspace = _make_workspace(manager, tmp_path)
    workspace = manager.update_workspace(workspace.id, WorkspaceUpdate(resident_agent_enabled=True))
    session = _resident_session(workspace, runtime_status=AgentRuntimeStatus.IDLE)
    manager.sessions[session.id] = session
    workspace = workspace.model_copy(update={"resident_agent_session_id": session.id})
    manager.workspaces[workspace.id] = workspace

    async def boom(session_id: str, message: str) -> None:
        raise RuntimeError("paste failed")

    monkeypatch.setattr(manager, "send_session_message", boom)

    with pytest.raises(RuntimeError):
        asyncio.run(manager._run_resident_agent(workspace))

    persisted = manager.workspaces[workspace.id]
    # Persisted before the send raised, so no respawn / immediate retry storm.
    assert persisted.resident_agent_session_id == session.id
    assert persisted.resident_agent_last_run_at is not None
