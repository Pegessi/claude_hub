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
    # Never run => due immediately (bootstrap).
    assert manager._resident_agent_due(_due_workspace(None), now) is True
    # Just run => not due.
    assert manager._resident_agent_due(_due_workspace(now), now) is False
    # No activity recorded, so the activity fast-path never fires; only the
    # interval+jitter backstop applies. For id "ws" / interval 60 the jitter is
    # ~342s, so the backstop is ~65.7min. 61min ago is still inside it.
    interval_seconds = 60 * 60
    jitter = manager._resident_jitter_seconds(_due_workspace(None), interval_seconds)
    backstop = timedelta(seconds=interval_seconds + jitter)
    just_inside = now - (backstop - timedelta(seconds=30))
    assert manager._resident_agent_due(_due_workspace(just_inside), now) is False
    # Past the backstop => due even with no activity.
    past_backstop = now - (backstop + timedelta(seconds=30))
    assert manager._resident_agent_due(_due_workspace(past_backstop), now) is True
    # Within the interval => not due.
    recent = now - timedelta(minutes=5)
    assert manager._resident_agent_due(_due_workspace(recent), now) is False
    # Disabled => never due even if overdue.
    assert manager._resident_agent_due(_due_workspace(past_backstop, enabled=False), now) is False


def test_resident_jitter_is_deterministic_and_in_range() -> None:
    interval_seconds = 60 * 60
    ws_a = _due_workspace(None)  # id "ws"
    # Deterministic: same id => same offset across calls.
    first = WorkspaceManager._resident_jitter_seconds(ws_a, interval_seconds)
    second = WorkspaceManager._resident_jitter_seconds(ws_a, interval_seconds)
    assert first == second
    # In range [0, interval_seconds).
    assert 0 <= first < interval_seconds
    # Differs across ids (so workspaces desynchronize).
    ws_b = ws_a.model_copy(update={"id": "ws-other"})
    other = WorkspaceManager._resident_jitter_seconds(ws_b, interval_seconds)
    assert other != first
    assert 0 <= other < interval_seconds


def _activity_task(
    workspace: Workspace,
    *,
    updated_at: datetime,
    system_internal: bool = False,
    completed_at: datetime | None = None,
    reviewed_at: datetime | None = None,
    human_accepted_at: datetime | None = None,
    session_id: str | None = None,
) -> WorkspaceTask:
    return WorkspaceTask(
        id=f"task-{updated_at.timestamp()}-{system_internal}",
        workspace_id=workspace.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        task_mode=WorkspaceTaskMode.REVIEWED,
        status=WorkspaceTaskStatus.WORKING,
        system_internal=system_internal,
        session_id=session_id,
        completed_at=completed_at,
        reviewed_at=reviewed_at,
        human_accepted_at=human_accepted_at,
        created_at=updated_at,
        updated_at=updated_at,
    )


def test_resident_activity_gate_fires_after_debounce(manager: WorkspaceManager) -> None:
    """Recent non-system task OUTCOME + elapsed >= debounce => due via fast path."""
    now = datetime.now()
    last_run = now - timedelta(seconds=_wm.RESIDENT_ACTIVITY_DEBOUNCE_SECONDS + 60)
    workspace = _due_workspace(last_run)
    # A task that actually COMPLETED after last_run (real work to learn from).
    manager.tasks["task-1"] = _activity_task(
        workspace,
        updated_at=last_run + timedelta(seconds=10),
        completed_at=last_run + timedelta(seconds=10),
    )
    assert manager._resident_agent_due(workspace, now) is True


def test_resident_activity_gate_held_within_debounce(manager: WorkspaceManager) -> None:
    """Outcome present but elapsed < debounce => NOT due (burst coalescing)."""
    now = datetime.now()
    last_run = now - timedelta(seconds=_wm.RESIDENT_ACTIVITY_DEBOUNCE_SECONDS - 60)
    workspace = _due_workspace(last_run)
    manager.tasks["task-1"] = _activity_task(
        workspace,
        updated_at=last_run + timedelta(seconds=10),
        completed_at=last_run + timedelta(seconds=10),
    )
    assert manager._resident_agent_due(workspace, now) is False


def test_resident_system_internal_activity_ignored(manager: WorkspaceManager) -> None:
    """system_internal-only activity does not trigger the fast path; backstop rules."""
    now = datetime.now()
    last_run = now - timedelta(seconds=_wm.RESIDENT_ACTIVITY_DEBOUNCE_SECONDS + 60)
    workspace = _due_workspace(last_run)
    manager.tasks["task-1"] = _activity_task(
        workspace, updated_at=last_run + timedelta(seconds=10), system_internal=True
    )
    # Debounce has passed but the only activity is system_internal, and the
    # backstop is far off, so it must NOT be due.
    assert manager._resident_agent_due(workspace, now) is False


def test_resident_newly_created_todo_does_not_trip_gate(manager: WorkspaceManager) -> None:
    """A freshly-PROPOSED TODO task (no outcome timestamps) must NOT count as
    activity — this directly encodes the self-retrigger bug fix.

    created_at/updated_at are after last_run but completed_at/reviewed_at/
    human_accepted_at are all None, mirroring a task the resident just POSTed.
    The activity fast path must stay closed; only the far-off backstop applies,
    so the workspace is NOT due.
    """
    now = datetime.now()
    last_run = now - timedelta(seconds=_wm.RESIDENT_ACTIVITY_DEBOUNCE_SECONDS + 60)
    workspace = _due_workspace(last_run)
    # Newly-created TODO: created/updated after last_run, but no outcome yet.
    manager.tasks["task-1"] = _activity_task(
        workspace,
        updated_at=last_run + timedelta(seconds=10),
    )
    # Activity gate must NOT fire; falls through to the (far-off) backstop.
    assert manager._workspace_activity_since(workspace.id, last_run) is False
    assert manager._resident_agent_due(workspace, now) is False


def test_resident_completed_task_trips_gate(manager: WorkspaceManager) -> None:
    """A task that COMPLETED since last_run + elapsed >= debounce => due."""
    now = datetime.now()
    last_run = now - timedelta(seconds=_wm.RESIDENT_ACTIVITY_DEBOUNCE_SECONDS + 60)
    workspace = _due_workspace(last_run)
    manager.tasks["task-1"] = _activity_task(
        workspace,
        updated_at=last_run + timedelta(seconds=10),
        completed_at=last_run + timedelta(seconds=20),
    )
    assert manager._workspace_activity_since(workspace.id, last_run) is True
    assert manager._resident_agent_due(workspace, now) is True


def test_resident_activity_since_none_with_outcome(manager: WorkspaceManager) -> None:
    """since=None => any existing outcome/report counts as activity."""
    now = datetime.now()
    workspace = _due_workspace(None)
    # No outcome yet => no activity.
    manager.tasks["task-1"] = _activity_task(workspace, updated_at=now)
    assert manager._workspace_activity_since(workspace.id, None) is False
    # An outcome on a task => activity.
    manager.tasks["task-1"] = _activity_task(workspace, updated_at=now, completed_at=now)
    assert manager._workspace_activity_since(workspace.id, None) is True
    # A report also counts.
    del manager.tasks["task-1"]
    manager.reports["rep-1"] = AgentReport(
        id="rep-1",
        workspace_id=workspace.id,
        task_id="task-1",
        session_id="worker-1",
        state=AgentReportState.WORKING,
        message="m",
        created_at=now,
    )
    assert manager._workspace_activity_since(workspace.id, None) is True


def test_resident_own_report_not_activity(manager: WorkspaceManager) -> None:
    """A report whose session_id == the resident's own session must NOT count.

    Defense-in-depth against a future prompt that makes the resident post
    reports: such a report cannot re-arm the activity fast path.
    """
    now = datetime.now()
    last_run = now - timedelta(seconds=_wm.RESIDENT_ACTIVITY_DEBOUNCE_SECONDS + 60)
    workspace = _due_workspace(last_run).model_copy(
        update={"resident_agent_session_id": "res-agent-1"}
    )
    manager.workspaces[workspace.id] = workspace
    # Report posted by the resident's own session after last_run => ignored.
    manager.reports["rep-self"] = AgentReport(
        id="rep-self",
        workspace_id=workspace.id,
        task_id=None,
        session_id="res-agent-1",
        state=AgentReportState.WORKING,
        message="self",
        created_at=last_run + timedelta(seconds=10),
    )
    assert manager._workspace_activity_since(workspace.id, last_run) is False
    assert manager._resident_agent_due(workspace, now) is False
    # A report from a real worker session DOES count.
    manager.reports["rep-worker"] = AgentReport(
        id="rep-worker",
        workspace_id=workspace.id,
        task_id=None,
        session_id="worker-1",
        state=AgentReportState.WORKING,
        message="work",
        created_at=last_run + timedelta(seconds=20),
    )
    assert manager._workspace_activity_since(workspace.id, last_run) is True
    assert manager._resident_agent_due(workspace, now) is True


def test_resident_own_proposed_task_not_activity(manager: WorkspaceManager) -> None:
    """Defense-in-depth: a task whose session_id == the resident's session is
    excluded from the activity scan even if it later gains an outcome."""
    now = datetime.now()
    last_run = now - timedelta(seconds=_wm.RESIDENT_ACTIVITY_DEBOUNCE_SECONDS + 60)
    workspace = _due_workspace(last_run).model_copy(
        update={"resident_agent_session_id": "res-agent-1"}
    )
    manager.workspaces[workspace.id] = workspace
    manager.tasks["task-1"] = _activity_task(
        workspace,
        updated_at=last_run + timedelta(seconds=10),
        completed_at=last_run + timedelta(seconds=10),
        session_id="res-agent-1",
    )
    assert manager._workspace_activity_since(workspace.id, last_run) is False
    assert manager._resident_agent_due(workspace, now) is False


def test_resident_backstop_fires_without_activity(manager: WorkspaceManager) -> None:
    """No activity, elapsed >= interval + jitter => due via backstop."""
    now = datetime.now()
    workspace = _due_workspace(None)
    interval_seconds = 60 * 60
    jitter = manager._resident_jitter_seconds(workspace, interval_seconds)
    last_run = now - timedelta(seconds=interval_seconds + jitter + 30)
    workspace = _due_workspace(last_run)
    assert manager._resident_agent_due(workspace, now) is True


def test_resident_not_due_between_debounce_and_backstop(manager: WorkspaceManager) -> None:
    """No activity, debounce < elapsed < interval + jitter => NOT due."""
    now = datetime.now()
    workspace = _due_workspace(None)
    interval_seconds = 60 * 60
    jitter = manager._resident_jitter_seconds(workspace, interval_seconds)
    # Past the debounce window but short of the backstop, with no activity.
    last_run = now - timedelta(seconds=_wm.RESIDENT_ACTIVITY_DEBOUNCE_SECONDS + 120)
    assert _wm.RESIDENT_ACTIVITY_DEBOUNCE_SECONDS + 120 < interval_seconds + jitter
    workspace = _due_workspace(last_run)
    assert manager._resident_agent_due(workspace, now) is False


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


# ---------------------------------------------------------------------------
# Resident agent_type / env / solo_mode parity (configurable like normal agents)
# ---------------------------------------------------------------------------


def test_create_workspace_resident_agent_config_parity(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """agent_type / env / solo_mode persist and round-trip from disk."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_type=AgentType.CURSOR,
            resident_agent_env={"FOO": "bar"},
            resident_agent_solo_mode=False,
        )
    )
    assert workspace.resident_agent_type == AgentType.CURSOR
    assert workspace.resident_agent_env == {"FOO": "bar"}
    assert workspace.resident_agent_solo_mode is False

    # Defaults when unspecified.
    other = manager.create_workspace(
        WorkspaceCreate(name="WS2", path=str(repo), session_prefix="res2")
    )
    assert other.resident_agent_type == AgentType.CLAUDE
    assert other.resident_agent_env == {}
    assert other.resident_agent_solo_mode is True

    # Round-trip from disk.
    reloaded = WorkspaceManager()
    persisted = reloaded.workspaces[workspace.id]
    assert persisted.resident_agent_type == AgentType.CURSOR
    assert persisted.resident_agent_env == {"FOO": "bar"}
    assert persisted.resident_agent_solo_mode is False


def test_update_workspace_resident_agent_config_parity(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """update can change type/env/solo_mode; None leaves them unchanged; env replaces."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_type=AgentType.CLAUDE,
            resident_agent_env={"A": "1", "B": "2"},
            resident_agent_solo_mode=True,
        )
    )

    updated = manager.update_workspace(
        workspace.id,
        WorkspaceUpdate(
            resident_agent_type=AgentType.CODEX,
            resident_agent_env={"C": "3"},
            resident_agent_solo_mode=False,
        ),
    )
    assert updated.resident_agent_type == AgentType.CODEX
    # env is replaced wholesale, not merged.
    assert updated.resident_agent_env == {"C": "3"}
    assert updated.resident_agent_solo_mode is False

    # None leaves all three unchanged.
    unchanged = manager.update_workspace(
        workspace.id, WorkspaceUpdate(resident_agent_directive="noop")
    )
    assert unchanged.resident_agent_type == AgentType.CODEX
    assert unchanged.resident_agent_env == {"C": "3"}
    assert unchanged.resident_agent_solo_mode is False


def test_run_resident_agent_uses_workspace_agent_config(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """LLM resident (CURSOR): ensure request carries type/env/solo_mode; bootstrap
    delivers the self-drive prompt (so no second send on create)."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_type=AgentType.CURSOR,
            resident_agent_env={"FOO": "bar"},
            resident_agent_solo_mode=False,
        )
    )
    workspace = manager.workspaces[workspace.id]

    created = _resident_session(workspace)
    created = created.model_copy(update={"agent_type": AgentType.CURSOR})
    captured: list[object] = []

    async def fake_ensure(workspace_id: str, payload: object) -> ManagedSession:
        captured.append(payload)
        manager.sessions[created.id] = created
        return created

    sends: list[tuple[str, str]] = []

    async def fake_send(session_id: str, message: str) -> None:
        sends.append((session_id, message))

    monkeypatch.setattr(manager, "ensure_workspace_agent", fake_ensure)
    monkeypatch.setattr(manager, "send_session_message", fake_send)

    asyncio.run(manager._run_resident_agent(workspace))

    assert len(captured) == 1
    req = captured[0]
    assert req.agent_type == AgentType.CURSOR
    assert req.env == {"FOO": "bar"}
    assert req.solo_mode is False
    assert req.role == WorkspaceSessionRole.RESIDENT
    assert req.reuse_existing is False
    # Bootstrap delivers the resident prompt inside ensure_workspace_agent (mocked
    # here), so the create path must NOT send a second copy.
    assert sends == []
    persisted = manager.workspaces[workspace.id]
    assert persisted.resident_agent_session_id == created.id
    assert persisted.resident_agent_last_run_at is not None


def test_run_resident_agent_terminal_skips_self_drive_prompt(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A TERMINAL resident gets a tab but NO self-drive prompt on either path;
    last_run_at still advances and the session id persists."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_type=AgentType.TERMINAL,
        )
    )
    workspace = manager.workspaces[workspace.id]

    sends: list[tuple[str, str]] = []

    async def fake_send(session_id: str, message: str) -> None:
        sends.append((session_id, message))

    monkeypatch.setattr(manager, "send_session_message", fake_send)

    # --- Create path: no existing resident session. ---
    created = _resident_session(workspace).model_copy(update={"agent_type": AgentType.TERMINAL})
    captured: list[object] = []

    async def fake_ensure(workspace_id: str, payload: object) -> ManagedSession:
        captured.append(payload)
        manager.sessions[created.id] = created
        return created

    monkeypatch.setattr(manager, "ensure_workspace_agent", fake_ensure)

    asyncio.run(manager._run_resident_agent(workspace))

    assert len(captured) == 1
    assert captured[0].agent_type == AgentType.TERMINAL
    assert sends == []  # no self-drive prompt for TERMINAL
    persisted = manager.workspaces[workspace.id]
    assert persisted.resident_agent_session_id == created.id
    assert persisted.resident_agent_last_run_at is not None

    # --- Reuse path: existing idle TERMINAL resident, still no prompt. ---
    reuse_ws = persisted

    async def fail_ensure(*args: object, **kwargs: object) -> ManagedSession:
        raise AssertionError("ensure_workspace_agent must not be called on reuse path")

    monkeypatch.setattr(manager, "ensure_workspace_agent", fail_ensure)

    asyncio.run(manager._run_resident_agent(reuse_ws))

    assert sends == []  # reuse path also skips the prompt for TERMINAL
    persisted2 = manager.workspaces[workspace.id]
    assert persisted2.resident_agent_session_id == created.id
    assert persisted2.resident_agent_last_run_at is not None


# ---------------------------------------------------------------------------
# Resident launch-config change invalidates the live session (recreate next tick)
# ---------------------------------------------------------------------------


def test_update_workspace_type_change_clears_resident_session(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Changing resident_agent_type claude->terminal clears the tracked session id
    AND drops the stale ManagedSession, so the old claude session can no longer be
    prompted; the next tick recreates with the new type."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_type=AgentType.CLAUDE,
        )
    )
    # A live claude resident session is tracked.
    session = _resident_session(workspace)  # agent_type CLAUDE
    manager.sessions[session.id] = session
    workspace = workspace.model_copy(update={"resident_agent_session_id": session.id})
    manager.workspaces[workspace.id] = workspace

    updated = manager.update_workspace(
        workspace.id, WorkspaceUpdate(resident_agent_type=AgentType.TERMINAL)
    )

    assert updated.resident_agent_type == AgentType.TERMINAL
    # Session id cleared so the next tick recreates with the new type.
    assert updated.resident_agent_session_id is None
    # Stale claude session removed so it cannot keep receiving the self-drive prompt.
    assert session.id not in manager.sessions


def test_update_workspace_env_change_clears_resident_session(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Changing resident_agent_env clears the tracked session id (recreate next tick)."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_type=AgentType.CLAUDE,
            resident_agent_env={"A": "1"},
        )
    )
    session = _resident_session(workspace)
    manager.sessions[session.id] = session
    workspace = workspace.model_copy(update={"resident_agent_session_id": session.id})
    manager.workspaces[workspace.id] = workspace

    updated = manager.update_workspace(workspace.id, WorkspaceUpdate(resident_agent_env={"A": "2"}))

    assert updated.resident_agent_env == {"A": "2"}
    assert updated.resident_agent_session_id is None
    assert session.id not in manager.sessions


def test_update_workspace_solo_mode_change_clears_resident_session(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Changing resident_agent_solo_mode clears the tracked session id."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_type=AgentType.CLAUDE,
            resident_agent_solo_mode=True,
        )
    )
    session = _resident_session(workspace)
    manager.sessions[session.id] = session
    workspace = workspace.model_copy(update={"resident_agent_session_id": session.id})
    manager.workspaces[workspace.id] = workspace

    updated = manager.update_workspace(
        workspace.id, WorkspaceUpdate(resident_agent_solo_mode=False)
    )

    assert updated.resident_agent_solo_mode is False
    assert updated.resident_agent_session_id is None
    assert session.id not in manager.sessions


def test_update_workspace_unchanged_config_keeps_resident_session(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Re-writing the SAME type/env/solo (or unrelated fields) must NOT clear the
    tracked session id — no needless recreation."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_type=AgentType.CLAUDE,
            resident_agent_env={"A": "1"},
            resident_agent_solo_mode=True,
        )
    )
    session = _resident_session(workspace)
    manager.sessions[session.id] = session
    workspace = workspace.model_copy(update={"resident_agent_session_id": session.id})
    manager.workspaces[workspace.id] = workspace

    # No-op writes of identical values + an unrelated field change.
    updated = manager.update_workspace(
        workspace.id,
        WorkspaceUpdate(
            resident_agent_type=AgentType.CLAUDE,
            resident_agent_env={"A": "1"},
            resident_agent_solo_mode=True,
            resident_agent_directive="keep going",
        ),
    )

    assert updated.resident_agent_session_id == session.id
    assert session.id in manager.sessions
    assert updated.resident_agent_directive == "keep going"


def test_update_workspace_type_change_without_live_session_is_noop_on_sessions(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """When no resident session is tracked, a type change just updates config
    (nothing to clear/remove)."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_type=AgentType.CLAUDE,
        )
    )
    assert workspace.resident_agent_session_id is None

    updated = manager.update_workspace(
        workspace.id, WorkspaceUpdate(resident_agent_type=AgentType.TERMINAL)
    )
    assert updated.resident_agent_type == AgentType.TERMINAL
    assert updated.resident_agent_session_id is None
