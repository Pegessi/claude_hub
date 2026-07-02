"""Tests for the per-workspace resident self-driven agent and workspace deletion."""

import asyncio
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any, Generator

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentReport,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    EnsureWorkspaceAgentRequest,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    ResidentPeriodicTask,
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

    async def fake_ensure(
        workspace_id: str, payload: EnsureWorkspaceAgentRequest
    ) -> ManagedSession:
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


def test_create_workspace_resident_placement_parity(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """title/target/remote_profile_id/cwd/remote_reconnect persist and round-trip."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_title="  My Resident  ",
            resident_agent_target=ExecutionTarget.REMOTE,
            resident_agent_remote_profile_id="prof-1",
            resident_agent_cwd="  ~/work/proj  ",
            resident_agent_remote_reconnect=False,
        )
    )
    # Title and cwd are stripped; empty -> None.
    assert workspace.resident_agent_title == "My Resident"
    assert workspace.resident_agent_target == ExecutionTarget.REMOTE
    assert workspace.resident_agent_remote_profile_id == "prof-1"
    assert workspace.resident_agent_cwd == "~/work/proj"
    assert workspace.resident_agent_remote_reconnect is False

    # Defaults when unspecified.
    other = manager.create_workspace(
        WorkspaceCreate(name="WS2", path=str(repo), session_prefix="res2")
    )
    assert other.resident_agent_title is None
    assert other.resident_agent_target == ExecutionTarget.LOCAL
    assert other.resident_agent_remote_profile_id is None
    assert other.resident_agent_cwd is None
    assert other.resident_agent_remote_reconnect is True

    # Round-trip from disk.
    reloaded = WorkspaceManager()
    persisted = reloaded.workspaces[workspace.id]
    assert persisted.resident_agent_title == "My Resident"
    assert persisted.resident_agent_target == ExecutionTarget.REMOTE
    assert persisted.resident_agent_remote_profile_id == "prof-1"
    assert persisted.resident_agent_cwd == "~/work/proj"
    assert persisted.resident_agent_remote_reconnect is False


def test_update_workspace_resident_placement_parity(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """update can change placement fields; None leaves them unchanged; blanks clear."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_title="Orig",
            resident_agent_target=ExecutionTarget.LOCAL,
            resident_agent_cwd="/tmp/a",
            resident_agent_remote_reconnect=True,
        )
    )

    updated = manager.update_workspace(
        workspace.id,
        WorkspaceUpdate(
            resident_agent_title="New",
            resident_agent_target=ExecutionTarget.REMOTE,
            resident_agent_remote_profile_id="prof-9",
            resident_agent_cwd="/tmp/b",
            resident_agent_remote_reconnect=False,
        ),
    )
    assert updated.resident_agent_title == "New"
    assert updated.resident_agent_target == ExecutionTarget.REMOTE
    assert updated.resident_agent_remote_profile_id == "prof-9"
    assert updated.resident_agent_cwd == "/tmp/b"
    assert updated.resident_agent_remote_reconnect is False

    # Blank title/cwd clear to None.
    cleared = manager.update_workspace(
        workspace.id,
        WorkspaceUpdate(resident_agent_title="   ", resident_agent_cwd="  "),
    )
    assert cleared.resident_agent_title is None
    assert cleared.resident_agent_cwd is None

    # None leaves placement unchanged.
    unchanged = manager.update_workspace(
        workspace.id, WorkspaceUpdate(resident_agent_directive="noop")
    )
    assert unchanged.resident_agent_target == ExecutionTarget.REMOTE
    assert unchanged.resident_agent_remote_profile_id == "prof-9"
    assert unchanged.resident_agent_remote_reconnect is False


def test_update_workspace_placement_change_clears_resident_session(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Changing target/cwd/remote_profile/remote_reconnect invalidates the live
    session so the next tick recreates the resident with the new placement."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    for field, value in (
        ("resident_agent_target", ExecutionTarget.REMOTE),
        ("resident_agent_remote_profile_id", "prof-2"),
        ("resident_agent_cwd", "/tmp/other"),
        ("resident_agent_remote_reconnect", False),
    ):
        workspace = manager.create_workspace(
            WorkspaceCreate(
                name=f"WS-{field}",
                path=str(repo),
                session_prefix="res",
                resident_agent_enabled=True,
                resident_agent_type=AgentType.CLAUDE,
                resident_agent_target=ExecutionTarget.LOCAL,
                resident_agent_cwd="/tmp/orig",
                resident_agent_remote_reconnect=True,
            )
        )
        session = _resident_session(workspace)
        manager.sessions[session.id] = session
        workspace = workspace.model_copy(update={"resident_agent_session_id": session.id})
        manager.workspaces[workspace.id] = workspace

        update_kwargs: dict[str, Any] = {field: value}
        updated = manager.update_workspace(workspace.id, WorkspaceUpdate(**update_kwargs))

        assert updated.resident_agent_session_id is None, field
        assert session.id not in manager.sessions, field


def test_update_workspace_placement_noop_keeps_resident_session(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Re-writing the SAME placement values must NOT clear the tracked session id —
    locks in the !=-comparison branch of _resident_launch_config_changed."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_type=AgentType.CLAUDE,
            resident_agent_target=ExecutionTarget.REMOTE,
            resident_agent_remote_profile_id="prof-1",
            resident_agent_cwd="/tmp/keep",
            resident_agent_remote_reconnect=False,
        )
    )
    session = _resident_session(workspace)
    manager.sessions[session.id] = session
    workspace = workspace.model_copy(update={"resident_agent_session_id": session.id})
    manager.workspaces[workspace.id] = workspace

    updated = manager.update_workspace(
        workspace.id,
        WorkspaceUpdate(
            resident_agent_target=ExecutionTarget.REMOTE,
            resident_agent_remote_profile_id="prof-1",
            resident_agent_cwd="/tmp/keep",
            resident_agent_remote_reconnect=False,
        ),
    )

    assert updated.resident_agent_session_id == session.id
    assert session.id in manager.sessions


def test_run_resident_agent_carries_placement_to_ensure_request(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """The ensure request mirrors the workspace's resident placement (title/target/
    cwd/remote_profile/remote_reconnect), with cwd reused as remote_cwd."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_type=AgentType.CLAUDE,
            resident_agent_title="Custom Resident",
            resident_agent_target=ExecutionTarget.REMOTE,
            resident_agent_remote_profile_id="prof-7",
            resident_agent_cwd="~/proj",
            resident_agent_remote_reconnect=False,
        )
    )
    workspace = manager.workspaces[workspace.id]

    created = _resident_session(workspace)
    captured: list[EnsureWorkspaceAgentRequest] = []

    async def fake_ensure(
        workspace_id: str, payload: EnsureWorkspaceAgentRequest
    ) -> ManagedSession:
        captured.append(payload)
        manager.sessions[created.id] = created
        return created

    async def fake_send(session_id: str, message: str) -> None:
        return None

    monkeypatch.setattr(manager, "ensure_workspace_agent", fake_ensure)
    monkeypatch.setattr(manager, "send_session_message", fake_send)

    asyncio.run(manager._run_resident_agent(workspace))

    assert len(captured) == 1
    req = captured[0]
    assert req.title == "Custom Resident"
    assert req.target == ExecutionTarget.REMOTE
    assert req.remote_profile_id == "prof-7"
    assert req.cwd == "~/proj"
    assert req.remote_cwd == "~/proj"
    assert req.remote_reconnect is False


def test_run_resident_agent_default_title_when_unset(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """With no resident title, the ensure request falls back to '<name> Resident'."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="Alpha",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_type=AgentType.CLAUDE,
        )
    )
    workspace = manager.workspaces[workspace.id]

    created = _resident_session(workspace)
    captured: list[EnsureWorkspaceAgentRequest] = []

    async def fake_ensure(
        workspace_id: str, payload: EnsureWorkspaceAgentRequest
    ) -> ManagedSession:
        captured.append(payload)
        manager.sessions[created.id] = created
        return created

    async def fake_send(session_id: str, message: str) -> None:
        return None

    monkeypatch.setattr(manager, "ensure_workspace_agent", fake_ensure)
    monkeypatch.setattr(manager, "send_session_message", fake_send)

    asyncio.run(manager._run_resident_agent(workspace))

    assert len(captured) == 1
    assert captured[0].title == "Alpha Resident"
    assert captured[0].target == ExecutionTarget.LOCAL


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
    captured: list[EnsureWorkspaceAgentRequest] = []

    async def fake_ensure(
        workspace_id: str, payload: EnsureWorkspaceAgentRequest
    ) -> ManagedSession:
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
    captured: list[EnsureWorkspaceAgentRequest] = []

    async def fake_ensure(
        workspace_id: str, payload: EnsureWorkspaceAgentRequest
    ) -> ManagedSession:
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


# ---------------------------------------------------------------------------
# Three-state lifecycle: ENABLE master switch, PAUSE, DELETE
# ---------------------------------------------------------------------------


def test_resident_due_false_when_paused(manager: WorkspaceManager) -> None:
    """Paused => not due even if enabled and otherwise due; unpausing restores it."""
    now = datetime.now()
    # Overdue (last_run None bootstraps to due) but paused => not due.
    paused = _due_workspace(None).model_copy(update={"resident_agent_paused": True})
    # Paused suppresses the bootstrap due.
    assert manager._resident_agent_due(paused, now) is False
    # Unpaused + due again.
    unpaused = paused.model_copy(update={"resident_agent_paused": False})
    assert manager._resident_agent_due(unpaused, now) is True


def test_update_workspace_disable_tears_down_resident(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Disabling (enabled True->False) clears the session id, drops the
    ManagedSession (orphan tab -> pruner), and resets last_run_at to None."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
        )
    )
    session = _resident_session(workspace)
    manager.sessions[session.id] = session
    workspace = workspace.model_copy(
        update={
            "resident_agent_session_id": session.id,
            "resident_agent_last_run_at": datetime.now(),
        }
    )
    manager.workspaces[workspace.id] = workspace

    updated = manager.update_workspace(workspace.id, WorkspaceUpdate(resident_agent_enabled=False))

    assert updated.resident_agent_enabled is False
    assert updated.resident_agent_session_id is None
    assert updated.resident_agent_last_run_at is None
    assert session.id not in manager.sessions


def test_update_workspace_pause_keeps_resident_session(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Pausing must NOT clear/pop the session — it stays alive for manual chat."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
        )
    )
    session = _resident_session(workspace)
    manager.sessions[session.id] = session
    last_run = datetime.now()
    workspace = workspace.model_copy(
        update={
            "resident_agent_session_id": session.id,
            "resident_agent_last_run_at": last_run,
        }
    )
    manager.workspaces[workspace.id] = workspace

    updated = manager.update_workspace(workspace.id, WorkspaceUpdate(resident_agent_paused=True))

    assert updated.resident_agent_paused is True
    assert updated.resident_agent_enabled is True
    # Session and pointer untouched; last_run not reset.
    assert updated.resident_agent_session_id == session.id
    assert session.id in manager.sessions
    assert updated.resident_agent_last_run_at == last_run


def test_delete_session_clears_resident_pointer_and_disables(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Deleting the resident session clears the workspace resident pointer, resets
    last_run_at, and sets resident_agent_enabled=False (Delete means stop, not
    restart-next-tick)."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
        )
    )
    session = _resident_session(workspace)
    manager.sessions[session.id] = session
    workspace = workspace.model_copy(
        update={
            "resident_agent_session_id": session.id,
            "resident_agent_last_run_at": datetime.now(),
        }
    )
    manager.workspaces[workspace.id] = workspace

    deleted_tabs: list[str] = []

    async def fake_delete_tab(tab_id: str) -> None:
        deleted_tabs.append(tab_id)

    original = _wm.ttyd_manager.delete_tab
    _wm.ttyd_manager.delete_tab = fake_delete_tab  # type: ignore[assignment]
    try:
        asyncio.run(manager.delete_session(session.id))
    finally:
        _wm.ttyd_manager.delete_tab = original  # type: ignore[assignment]

    assert session.id not in manager.sessions
    persisted = manager.workspaces[workspace.id]
    assert persisted.resident_agent_session_id is None
    assert persisted.resident_agent_last_run_at is None
    assert persisted.resident_agent_enabled is False
    assert deleted_tabs == [session.tab_id]


# ---------------------------------------------------------------------------
# Resident master mode: persistence, prompt branch, heartbeat, no-respawn
# ---------------------------------------------------------------------------


def test_create_update_workspace_master_mode_persists(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """master_mode defaults False, persists through create + round-trips from disk;
    update with None leaves it unchanged; setting True then reloading persists True."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)

    # Default is False.
    default_ws = manager.create_workspace(
        WorkspaceCreate(name="WS-default", path=str(repo), session_prefix="res")
    )
    assert default_ws.resident_agent_master_mode is False

    # Explicit True persists on create and round-trips from disk.
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_master_mode=True,
        )
    )
    assert workspace.resident_agent_master_mode is True
    reloaded = WorkspaceManager()
    assert reloaded.workspaces[workspace.id].resident_agent_master_mode is True

    # update with None leaves it unchanged.
    unchanged = manager.update_workspace(
        workspace.id, WorkspaceUpdate(resident_agent_directive="noop")
    )
    assert unchanged.resident_agent_master_mode is True

    # Flip a default-False workspace to True via update, then reload -> persists True.
    toggled = manager.update_workspace(
        default_ws.id, WorkspaceUpdate(resident_agent_master_mode=True)
    )
    assert toggled.resident_agent_master_mode is True
    reloaded2 = WorkspaceManager()
    assert reloaded2.workspaces[default_ws.id].resident_agent_master_mode is True


def test_build_resident_prompt_master_off_is_legacy(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """master OFF returns the existing read-only maintenance prompt (propose/TODO +
    Hard constraints) and does NOT mention self-provisioning a worktree."""
    workspace = _make_workspace(manager, tmp_path)
    assert workspace.resident_agent_master_mode is False
    prompt = _wm.build_resident_agent_prompt(workspace, "http://localhost:9999", "sid")
    assert "RESIDENT self-driven maintenance agent" in prompt
    assert "PROPOSE new tasks" in prompt
    assert "TODO status only" in prompt
    assert "Hard constraints" in prompt
    # Proposed tasks are tagged as agent-created so the UI can distinguish them.
    assert '"origin":"resident"' in prompt
    # No worktree self-provisioning in legacy mode.
    assert "git worktree add" not in prompt
    # And no orchestrator-mode dispatch machinery leaks into the read-only prompt.
    assert "target_session_id" not in prompt
    assert "task_mode" not in prompt


def test_build_resident_prompt_master_on_is_orchestrator(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """master ON returns the orchestrator prompt: read the board, create tasks
    (default reviewed mode), dispatch them to existing orchestrator workers via
    target_session_id, and accept the work itself once review has passed (PATCH
    status=done) — never writing code or provisioning worker agents."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_master_mode=True,
        )
    )
    prompt = _wm.build_resident_agent_prompt(workspace, "http://localhost:9999", "sid")
    assert "RESIDENT MASTER agent" in prompt
    assert "/board" in prompt
    assert "/tasks" in prompt
    assert "target_session_id" in prompt
    assert "orchestrator" in prompt
    assert "NEVER create or delete orchestrator worker sessions" in prompt
    assert "PATCH" in prompt
    assert '"status":"done"' in prompt
    assert "no worker agents available" in prompt
    # Tasks use the default reviewed mode (a reviewer agent vets the work); the
    # resident does the final acceptance, so it must NOT force direct mode.
    assert '"task_mode":"direct"' not in prompt
    assert "default (reviewed)" in prompt
    # Created tasks are tagged origin=resident so the UI can mark them as
    # agent-created (vs human-created tasks).
    assert '"origin":"resident"' in prompt
    # Acceptance is gated on the post-review awaiting-acceptance signal, not raw
    # status == review (which is also true while the reviewer is still working).
    assert "human_acceptance_requested_at" in prompt
    # Degrade-to-proposal-only guardrail when there is no orchestrator worker.
    assert "MUST NOT create one" in prompt
    assert "never auto-creates a default" in prompt
    # Reviewer auto-spawn by the backend is explicitly allowed (not forbidden).
    assert "allowed" in prompt
    # Session-scoped heartbeat endpoint, with the session id interpolated.
    assert "sessions/sid/reports" in prompt
    # No worktree / self-provisioning language in orchestrator mode.
    assert "git worktree add" not in prompt
    assert "work ONLY" not in prompt


def test_run_resident_agent_master_mode_reuse_sends_heartbeat_prompt(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Reuse path in master mode: the single self-drive prompt is the master prompt
    and references the session-scoped report endpoint for THIS session."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_master_mode=True,
        )
    )
    workspace = manager.workspaces[workspace.id]
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
    prompt = sends[0][1]
    assert "RESIDENT MASTER agent" in prompt
    assert "target_session_id" in prompt
    assert "human_acceptance_requested_at" in prompt
    assert f"sessions/{session.id}/reports" in prompt


def test_update_workspace_master_mode_keeps_resident_session(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Toggling resident_agent_master_mode must NOT clear the tracked session id and
    must NOT drop the ManagedSession — the prompt is recomputed every cycle, so no
    respawn is needed (opposite of a placement/launch-config change)."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_type=AgentType.CLAUDE,
            resident_agent_master_mode=False,
        )
    )
    session = _resident_session(workspace)
    manager.sessions[session.id] = session
    workspace = workspace.model_copy(update={"resident_agent_session_id": session.id})
    manager.workspaces[workspace.id] = workspace

    updated = manager.update_workspace(
        workspace.id, WorkspaceUpdate(resident_agent_master_mode=True)
    )

    assert updated.resident_agent_master_mode is True
    # No respawn: pointer and live ManagedSession are preserved.
    assert updated.resident_agent_session_id == session.id
    assert session.id in manager.sessions


# ---------------------------------------------------------------------------
# Periodic tasks: persistence, normalization, prompt rendering
# ---------------------------------------------------------------------------


def test_create_workspace_periodic_tasks_normalized(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Periodic tasks persist, trim text, drop blanks, keep ids/order, round-trip."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_periodic_tasks=[
                ResidentPeriodicTask(id="t1", text="  run the linter  ", enabled=True),
                ResidentPeriodicTask(id="t2", text="   ", enabled=True),  # blank -> dropped
                ResidentPeriodicTask(id="t3", text="check deps", enabled=False),
            ],
        )
    )
    tasks = workspace.resident_agent_periodic_tasks
    assert [t.id for t in tasks] == ["t1", "t3"]  # blank dropped, order kept
    assert tasks[0].text == "run the linter"  # trimmed
    assert tasks[0].enabled is True
    assert tasks[1].enabled is False  # disabled flag preserved

    # Round-trip from disk.
    reloaded = WorkspaceManager()
    persisted = reloaded.workspaces[workspace.id].resident_agent_periodic_tasks
    assert [t.id for t in persisted] == ["t1", "t3"]
    assert persisted[0].text == "run the linter"


def test_create_workspace_periodic_tasks_default_empty(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Unspecified periodic tasks default to an empty list (backward compat)."""
    workspace = _make_workspace(manager, tmp_path)
    assert workspace.resident_agent_periodic_tasks == []


def test_update_workspace_periodic_tasks(manager: WorkspaceManager, tmp_path: Path) -> None:
    """update replaces the list wholesale; None leaves it unchanged; [] clears it."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_periodic_tasks=[ResidentPeriodicTask(id="a", text="first")],
        )
    )

    updated = manager.update_workspace(
        workspace.id,
        WorkspaceUpdate(
            resident_agent_periodic_tasks=[
                ResidentPeriodicTask(id="b", text="  second  "),
                ResidentPeriodicTask(id="c", text="third", enabled=False),
            ]
        ),
    )
    assert [t.id for t in updated.resident_agent_periodic_tasks] == ["b", "c"]
    assert updated.resident_agent_periodic_tasks[0].text == "second"

    # None leaves the list unchanged.
    unchanged = manager.update_workspace(
        workspace.id, WorkspaceUpdate(resident_agent_directive="noop")
    )
    assert [t.id for t in unchanged.resident_agent_periodic_tasks] == ["b", "c"]

    # Empty list clears all tasks.
    cleared = manager.update_workspace(
        workspace.id, WorkspaceUpdate(resident_agent_periodic_tasks=[])
    )
    assert cleared.resident_agent_periodic_tasks == []


def test_build_resident_prompt_renders_enabled_periodic_tasks(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Enabled periodic tasks appear as a numbered checklist; disabled/blank omitted."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_periodic_tasks=[
                ResidentPeriodicTask(id="a", text="run the linter", enabled=True),
                ResidentPeriodicTask(id="b", text="update the changelog", enabled=False),
                ResidentPeriodicTask(id="c", text="triage open issues", enabled=True),
            ],
        )
    )
    prompt = _wm.build_resident_agent_prompt(workspace, "http://localhost:9999", "sid")
    assert "Recurring tasks to perform EVERY cycle" in prompt
    assert "1. run the linter" in prompt
    # Disabled task is not rendered; enabled ones are renumbered contiguously.
    assert "update the changelog" not in prompt
    assert "2. triage open issues" in prompt


def test_build_resident_prompt_no_periodic_block_when_empty(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """With no enabled periodic tasks the checklist block is absent (byte-compat)."""
    workspace = _make_workspace(manager, tmp_path)
    prompt = _wm.build_resident_agent_prompt(workspace, "http://localhost:9999", "sid")
    assert "Recurring tasks to perform EVERY cycle" not in prompt

    # A workspace whose only periodic tasks are disabled also renders no block.
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    disabled_only = manager.create_workspace(
        WorkspaceCreate(
            name="WS2",
            path=str(repo),
            session_prefix="res2",
            resident_agent_periodic_tasks=[
                ResidentPeriodicTask(id="a", text="paused chore", enabled=False),
            ],
        )
    )
    prompt2 = _wm.build_resident_agent_prompt(disabled_only, "http://localhost:9999", "sid")
    assert "Recurring tasks to perform EVERY cycle" not in prompt2


def test_build_resident_prompt_master_renders_periodic_tasks(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Master-mode prompt also embeds the recurring-tasks checklist."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
            resident_agent_master_mode=True,
            resident_agent_periodic_tasks=[
                ResidentPeriodicTask(id="a", text="review the backlog", enabled=True),
            ],
        )
    )
    prompt = _wm.build_resident_agent_prompt(workspace, "http://localhost:9999", "sid")
    assert "RESIDENT MASTER agent" in prompt
    assert "Recurring tasks to perform EVERY cycle" in prompt
    assert "1. review the backlog" in prompt


# ---------------------------------------------------------------------------
# Manual run-now: request flag, due override, consume-on-fire, next-run hint
# ---------------------------------------------------------------------------


def test_request_resident_run_stamps_flag(manager: WorkspaceManager, tmp_path: Path) -> None:
    """request_resident_run stamps the flag and persists it to disk."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS", path=str(repo), session_prefix="res", resident_agent_enabled=True
        )
    )
    assert workspace.resident_agent_run_requested_at is None

    updated = manager.request_resident_run(workspace.id)
    assert updated.resident_agent_run_requested_at is not None

    reloaded = WorkspaceManager()
    assert reloaded.workspaces[workspace.id].resident_agent_run_requested_at is not None


def test_request_resident_run_missing_workspace_raises_keyerror(
    manager: WorkspaceManager,
) -> None:
    with pytest.raises(KeyError):
        manager.request_resident_run("does-not-exist")


def test_request_resident_run_disabled_raises_valueerror(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Requesting a run on a disabled resident is a ValueError (nothing to run)."""
    workspace = _make_workspace(manager, tmp_path)
    assert workspace.resident_agent_enabled is False
    with pytest.raises(ValueError):
        manager.request_resident_run(workspace.id)


def test_resident_due_run_now_overrides_interval_and_pause(
    manager: WorkspaceManager,
) -> None:
    """run-now makes a just-run resident due, and fires even while paused."""
    now = datetime.now()
    # Just ran (not otherwise due) but run requested => due.
    ws = _due_workspace(now).model_copy(update={"resident_agent_run_requested_at": now})
    assert manager._resident_agent_due(ws, now) is True

    # Paused + run requested => still due (deliberate one-off overrides pause).
    paused = ws.model_copy(update={"resident_agent_paused": True})
    assert manager._resident_agent_due(paused, now) is True

    # Disabled + run requested => NOT due (enable is the master switch).
    disabled = ws.model_copy(
        update={"resident_agent_enabled": False, "resident_agent_paused": False}
    )
    assert manager._resident_agent_due(disabled, now) is False


def test_run_resident_agent_consumes_run_request_flag(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Firing a cycle clears the run-now flag and recomputes the next-run hint."""
    workspace = _make_workspace(manager, tmp_path)
    workspace = manager.update_workspace(workspace.id, WorkspaceUpdate(resident_agent_enabled=True))
    session = _resident_session(workspace, runtime_status=AgentRuntimeStatus.IDLE)
    manager.sessions[session.id] = session
    workspace = workspace.model_copy(
        update={
            "resident_agent_session_id": session.id,
            "resident_agent_run_requested_at": datetime.now(),
        }
    )
    manager.workspaces[workspace.id] = workspace

    async def fake_send(session_id: str, message: str) -> None:
        return None

    monkeypatch.setattr(manager, "send_session_message", fake_send)

    asyncio.run(manager._run_resident_agent(workspace))

    persisted = manager.workspaces[workspace.id]
    assert persisted.resident_agent_run_requested_at is None  # consumed
    assert persisted.resident_agent_last_run_at is not None
    # Enabled + not paused + last_run stamped => a future next-run hint is set.
    assert persisted.resident_agent_next_run_at is not None
    assert persisted.resident_agent_next_run_at > persisted.resident_agent_last_run_at


def test_run_resident_agent_busy_keeps_run_request_flag(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A WORKING resident defers the run-now: the flag stays set (not lost)."""
    workspace = _make_workspace(manager, tmp_path)
    workspace = manager.update_workspace(workspace.id, WorkspaceUpdate(resident_agent_enabled=True))
    session = _resident_session(workspace, runtime_status=AgentRuntimeStatus.WORKING)
    manager.sessions[session.id] = session
    requested_at = datetime.now()
    workspace = workspace.model_copy(
        update={
            "resident_agent_session_id": session.id,
            "resident_agent_run_requested_at": requested_at,
        }
    )
    manager.workspaces[workspace.id] = workspace

    async def fail_send(session_id: str, message: str) -> None:
        raise AssertionError("busy resident must not be sent a prompt")

    monkeypatch.setattr(manager, "send_session_message", fail_send)

    asyncio.run(manager._run_resident_agent(workspace))

    # Skipped without consuming the request, so a later idle tick still fires it.
    persisted = manager.workspaces[workspace.id]
    assert persisted.resident_agent_run_requested_at == requested_at


# ---------------------------------------------------------------------------
# Next-run hint: computed on update, cleared when paused/disabled
# ---------------------------------------------------------------------------


def test_resident_next_run_at_helper() -> None:
    """_resident_next_run_at = last_run + interval + jitter; None when disabled/
    paused/bootstrap."""
    manager = WorkspaceManager()
    now = datetime.now()
    ws = _due_workspace(now, interval=60)
    interval_seconds = 60 * 60
    jitter = manager._resident_jitter_seconds(ws, interval_seconds)
    expected = now + timedelta(seconds=interval_seconds + jitter)
    assert manager._resident_next_run_at(ws, now) == expected

    # Bootstrap (no last_run) => None.
    assert manager._resident_next_run_at(ws, None) is None
    # Paused => None.
    paused = ws.model_copy(update={"resident_agent_paused": True})
    assert manager._resident_next_run_at(paused, now) is None
    # Disabled => None.
    disabled = ws.model_copy(update={"resident_agent_enabled": False})
    assert manager._resident_next_run_at(disabled, now) is None


def test_update_workspace_recomputes_next_run_at(manager: WorkspaceManager, tmp_path: Path) -> None:
    """Enabling with a prior last_run sets a next-run hint; pausing clears it."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="WS",
            path=str(repo),
            session_prefix="res",
            resident_agent_enabled=True,
        )
    )
    last_run = datetime.now() - timedelta(minutes=5)
    workspace = workspace.model_copy(update={"resident_agent_last_run_at": last_run})
    manager.workspaces[workspace.id] = workspace

    # An update recomputes the hint from the existing last_run.
    updated = manager.update_workspace(
        workspace.id, WorkspaceUpdate(resident_agent_interval_minutes=30)
    )
    assert updated.resident_agent_next_run_at is not None
    interval_seconds = 30 * 60
    jitter = manager._resident_jitter_seconds(updated, interval_seconds)
    assert updated.resident_agent_next_run_at == last_run + timedelta(
        seconds=interval_seconds + jitter
    )

    # Pausing clears the hint (no automatic scheduling while paused).
    paused = manager.update_workspace(workspace.id, WorkspaceUpdate(resident_agent_paused=True))
    assert paused.resident_agent_next_run_at is None
