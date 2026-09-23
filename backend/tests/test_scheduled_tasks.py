"""Tests for scheduled tasks: cron parsing, scheduling, firing, auto-cleanup, CRUD, CLI.

Mirrors the hermetic-persistence pattern from ``test_workspace_resident_agent.py``:
all state is redirected to a tmp directory, and the tmux / ttyd side effects are
mocked. The scheduler's cross-mixin calls (``_send_tmux_message``,
``ensure_workspace_agent``, ``_dispatch_task_to_session``, ``delete_session``)
are monkeypatched so the tests exercise the scheduling logic without a live
terminal. Tab existence (``ttyd_manager.get_tab``) is stubbed per test.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import click
import httpx
import pytest
from click.testing import CliRunner
from pytest import MonkeyPatch

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.commands.schedule import _schedule_body
from claude_hub.cli.main import cli
from claude_hub.models import (
    AgentReport,
    AgentReportCreate,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    EnsureWorkspaceAgentRequest,
    ManagedSession,
    ManagedSessionStatus,
    ScheduledTask,
    ScheduledTaskCreate,
    ScheduledTaskKind,
    ScheduledTaskRunStatus,
    ScheduledTaskUpdate,
    SessionKind,
    Workspace,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTaskCreate,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)
from claude_hub.services.ttyd_manager import ttyd_manager
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


async def _async_noop(*args: Any, **kwargs: Any) -> None:
    return None


@pytest.fixture()
def state_root(monkeypatch: MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect all workspace + scheduled-task persistence to a tmp directory."""
    root = tmp_path / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    index_file = root / "index.json"
    sched_file = root / "scheduled_tasks.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm, "SCHEDULED_TASKS_FILE", sched_file)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)
    # _load/_save_scheduled_tasks resolve SCHEDULED_TASKS_FILE from the
    # _scheduling submodule globals (populated via ``from ._constants import *``).
    monkeypatch.setattr(_wm._scheduling, "SCHEDULED_TASKS_FILE", sched_file)
    return root


@pytest.fixture()
def manager(state_root: Path) -> WorkspaceManager:
    return WorkspaceManager()


def _make_workspace(manager: WorkspaceManager, tmp_path: Path, name: str = "Sched WS") -> Workspace:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return manager.create_workspace(
        WorkspaceCreate(name=name, path=str(repo), session_prefix="sched")
    )


def _make_session(
    workspace: Workspace,
    *,
    session_id: str = "sess-1",
    caller_owned_ephemeral: bool = False,
    role: WorkspaceSessionRole = WorkspaceSessionRole.ORCHESTRATOR,
) -> ManagedSession:
    now = datetime.now()
    return ManagedSession(
        id=session_id,
        workspace_id=workspace.id,
        task_id=None,
        tab_id=f"tab-{session_id}",
        role=role,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Session",
        workspace_path=workspace.path,
        tmux_session=f"claude-hub-tab-{session_id}",
        caller_owned_ephemeral=caller_owned_ephemeral,
        created_at=now,
        updated_at=now,
    )


def _stub_known_tabs(monkeypatch: MonkeyPatch, *tab_ids: str) -> None:
    """Make ``ttyd_manager.get_tab`` return a dummy for the given tab ids.

    The ``tab_message`` validation resolves tab existence through the
    ``ttyd_manager`` singleton; tests stub it so only the ids they care about
    are "live".
    """
    known = set(tab_ids)
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda tid: object() if tid in known else None)


def _chat_tab(
    tab_id: str = "chat-1",
    *,
    agent_type: AgentType = AgentType.CLAUDE,
    archived: bool = False,
    workspace_role: WorkspaceSessionRole | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=tab_id,
        agent_type=agent_type,
        session_kind=SessionKind.CHAT,
        workspace_role=workspace_role,
        archived=archived,
    )


def _stub_chat_target(
    monkeypatch: MonkeyPatch, tab: SimpleNamespace | None
) -> dict[str, SimpleNamespace]:
    tabs = {} if tab is None else {tab.id: tab}
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda tab_id: tabs.get(tab_id))
    return tabs


def _stub_goal(monkeypatch: MonkeyPatch, goal_ref: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep Chat scheduling tests isolated from the process-wide Goal store."""
    state = goal_ref if goal_ref is not None else {"goal": None}
    locks: dict[str, asyncio.Lock] = {}
    goal_module = import_module("claude_hub.services.goal_run")
    monkeypatch.setattr(
        goal_module,
        "get_goal_manager",
        lambda: SimpleNamespace(current=lambda _tab_id: state.get("goal")),
    )
    monkeypatch.setattr(
        goal_module,
        "get_goal_admission_lock",
        lambda tab_id: locks.setdefault(tab_id, asyncio.Lock()),
    )
    return state


def _weekday_at(hour: int, minute: int, target_weekday: int) -> datetime:
    """Return a datetime on the next occurrence of ``target_weekday`` (Mon=0)."""
    d = datetime(2026, 9, 7)  # anchor: a Monday
    while d.weekday() != target_weekday:
        d += timedelta(days=1)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Cron parser
# ---------------------------------------------------------------------------


def test_parse_cron_field_variants(manager: WorkspaceManager) -> None:
    assert manager._parse_cron_field("*", 0, 59) == frozenset(range(0, 60))
    assert manager._parse_cron_field("*/5", 0, 59) == frozenset(range(0, 60, 5))
    assert manager._parse_cron_field("1,15,30", 0, 59) == frozenset({1, 15, 30})
    assert manager._parse_cron_field("9-12", 0, 23) == frozenset({9, 10, 11, 12})
    assert manager._parse_cron_field("9-17/2", 0, 23) == frozenset({9, 11, 13, 15, 17})
    # A bare value with a step starts at the value and steps to the field max.
    assert manager._parse_cron_field("5/10", 0, 59) == frozenset({5, 15, 25, 35, 45, 55})


def test_parse_cron_field_rejects_invalid(manager: WorkspaceManager) -> None:
    with pytest.raises(ValueError):
        manager._parse_cron_field("*/0", 0, 59)  # step < 1
    with pytest.raises(ValueError):
        manager._parse_cron_field("99", 0, 59)  # out of range
    with pytest.raises(ValueError):
        manager._parse_cron_field("1,,2", 0, 59)  # empty list element
    with pytest.raises(ValueError):
        manager._parse_cron_field("abc", 0, 59)  # non-numeric
    with pytest.raises(ValueError):
        manager._parse_cron_field("5-2", 0, 59)  # start > end


def test_parse_cron_expression_field_count(manager: WorkspaceManager) -> None:
    parts = manager._parse_cron_expression("30 9 * * *")
    assert len(parts) == 5
    assert parts[0] == frozenset({30})
    assert parts[1] == frozenset({9})
    with pytest.raises(ValueError):
        manager._parse_cron_expression("30 9 * *")  # 4 fields
    with pytest.raises(ValueError):
        manager._parse_cron_expression("30 9 * * * *")  # 6 fields


def test_cron_matches_daily_at_930(manager: WorkspaceManager) -> None:
    parts = manager._parse_cron_expression("30 9 * * *")
    assert manager._cron_matches(parts, datetime(2026, 9, 8, 9, 30)) is True
    assert manager._cron_matches(parts, datetime(2026, 9, 8, 9, 31)) is False
    assert manager._cron_matches(parts, datetime(2026, 9, 8, 10, 30)) is False


def test_cron_matches_weekdays_at_9(manager: WorkspaceManager) -> None:
    parts = manager._parse_cron_expression("0 9 * * 1-5")
    monday = _weekday_at(9, 0, 0)  # Monday
    saturday = _weekday_at(9, 0, 5)  # Saturday
    assert monday.weekday() == 0
    assert saturday.weekday() == 5
    assert manager._cron_matches(parts, monday) is True
    assert manager._cron_matches(parts, saturday) is False
    assert manager._cron_matches(parts, monday.replace(hour=10)) is False


def test_cron_matches_dom_dow_or_rule(manager: WorkspaceManager) -> None:
    # Both DOM and DOW restricted => a day matches if EITHER matches (OR rule).
    parts = manager._parse_cron_expression("0 0 13 * 5")  # the 13th OR any Friday
    # 2026-09-13 is a Sunday; DOM=13 matches.
    assert manager._cron_matches(parts, datetime(2026, 9, 13, 0, 0)) is True
    # A Friday that is not the 13th matches via DOW.
    friday = _weekday_at(0, 0, 4)
    if friday.day != 13:
        assert manager._cron_matches(parts, friday) is True
    # A Saturday that is not the 13th matches neither.
    saturday = _weekday_at(0, 0, 5)
    if saturday.day != 13:
        assert manager._cron_matches(parts, saturday) is False


def test_next_cron_run_daily(manager: WorkspaceManager) -> None:
    after = datetime(2026, 9, 8, 9, 30, 0)
    # Strictly after => the next match is tomorrow at 09:30.
    assert manager._next_cron_run("30 9 * * *", after) == datetime(2026, 9, 9, 9, 30)


def test_next_cron_run_every_15_minutes(manager: WorkspaceManager) -> None:
    after = datetime(2026, 9, 8, 10, 0, 0)
    assert manager._next_cron_run("*/15 * * * *", after) == datetime(2026, 9, 8, 10, 15)


def test_next_cron_run_weekday(manager: WorkspaceManager) -> None:
    # After Sunday noon, the next weekday 09:00 is Monday.
    after = datetime(2026, 9, 6, 12, 0, 0)
    assert after.weekday() == 6  # Sunday
    assert manager._next_cron_run("0 9 * * 1-5", after) == datetime(2026, 9, 7, 9, 0)


def test_next_cron_run_specific_month(manager: WorkspaceManager) -> None:
    # Only fires in January.
    after = datetime(2026, 9, 8, 10, 0, 0)
    nxt = manager._next_cron_run("0 0 1 1 *", after)
    assert nxt == datetime(2027, 1, 1, 0, 0)


def test_next_cron_run_feb_29_reaches_beyond_one_year(manager: WorkspaceManager) -> None:
    # "0 0 29 2 *" only matches on Feb 29. From late 2026 the next occurrence is
    # Feb 29, 2028 — more than a year out. The ~4-year search window must reach
    # it (a 366-day window would falsely return None).
    after = datetime(2026, 9, 8, 0, 0, 0)
    nxt = manager._next_cron_run("0 0 29 2 *", after)
    assert nxt == datetime(2028, 2, 29, 0, 0)


# ---------------------------------------------------------------------------
# Next-run computation
# ---------------------------------------------------------------------------


def _bare_task(**kwargs: Any) -> ScheduledTask:
    now = datetime(2026, 9, 8, 12, 0, 0)
    defaults: Dict[str, Any] = dict(
        id="t1",
        name="t",
        kind=ScheduledTaskKind.TAB_MESSAGE,
        created_at=now,
        updated_at=now,
    )
    defaults.update(kwargs)
    return ScheduledTask(**defaults)


def test_compute_next_run_run_at(manager: WorkspaceManager) -> None:
    now = datetime(2026, 9, 8, 12, 0, 0)
    future = _bare_task(run_at=datetime(2026, 9, 9, 9, 0))
    assert manager._compute_next_run(future, now) == datetime(2026, 9, 9, 9, 0)
    # A past run_at fires immediately (next run = now).
    past = _bare_task(run_at=datetime(2026, 9, 7, 9, 0))
    assert manager._compute_next_run(past, now) == now


def test_compute_next_run_interval(manager: WorkspaceManager) -> None:
    now = datetime(2026, 9, 8, 12, 0, 0)
    task = _bare_task(interval_seconds=300)
    assert manager._compute_next_run(task, now) == now + timedelta(seconds=300)


def test_compute_next_run_cron(manager: WorkspaceManager) -> None:
    now = datetime(2026, 9, 8, 12, 0, 0)
    task = _bare_task(cron="30 9 * * *")
    assert manager._compute_next_run(task, now) == datetime(2026, 9, 9, 9, 30)


# ---------------------------------------------------------------------------
# CRUD + validation
# ---------------------------------------------------------------------------


def test_create_tab_message_task(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="nudge",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            cron="*/10 * * * *",
            tab_id=session.tab_id,
            message="ping",
        )
    )
    assert task.kind == ScheduledTaskKind.TAB_MESSAGE
    assert task.tab_id == session.tab_id
    assert task.enabled is True
    assert task.run_count == 0
    assert task.next_run_at is not None
    assert task.agent_type == AgentType.CLAUDE
    assert manager.get_scheduled_task(task.id).id == task.id
    assert manager.list_scheduled_tasks() == [task]


def test_create_requires_exactly_one_schedule(manager: WorkspaceManager, tmp_path: Path) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    with pytest.raises(ValueError, match="Exactly one"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="x",
                kind=ScheduledTaskKind.TAB_MESSAGE,
                tab_id=session.tab_id,
                message="m",
            )
        )
    with pytest.raises(ValueError, match="Exactly one"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="x",
                kind=ScheduledTaskKind.TAB_MESSAGE,
                cron="* * * * *",
                interval_seconds=60,
                tab_id=session.tab_id,
                message="m",
            )
        )


def test_create_tab_message_requires_tab_and_message(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    with pytest.raises(ValueError, match="tab_id is required"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="x", kind=ScheduledTaskKind.TAB_MESSAGE, cron="* * * * *", message="m"
            )
        )
    with pytest.raises(ValueError, match="message is required"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="x",
                kind=ScheduledTaskKind.TAB_MESSAGE,
                cron="* * * * *",
                tab_id=session.tab_id,
            )
        )
    with pytest.raises(ValueError, match="not found"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="x",
                kind=ScheduledTaskKind.TAB_MESSAGE,
                cron="* * * * *",
                tab_id="nope",
                message="m",
            )
        )


def test_create_new_session_requires_workspace(manager: WorkspaceManager, tmp_path: Path) -> None:
    _make_workspace(manager, tmp_path)
    run_at = datetime.now() + timedelta(hours=1)
    with pytest.raises(ValueError, match="workspace_id is required"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="x", kind=ScheduledTaskKind.NEW_SESSION, run_at=run_at, message="m"
            )
        )
    with pytest.raises(ValueError, match="not found"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="x",
                kind=ScheduledTaskKind.NEW_SESSION,
                run_at=run_at,
                workspace_id="nope",
                message="m",
            )
        )


def test_create_hub_task_requires_title_and_message(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    with pytest.raises(ValueError, match="task_title is required"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="x",
                kind=ScheduledTaskKind.HUB_TASK,
                cron="* * * * *",
                workspace_id=workspace.id,
                message="m",
            )
        )
    with pytest.raises(ValueError, match="task prompt"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="x",
                kind=ScheduledTaskKind.HUB_TASK,
                cron="* * * * *",
                workspace_id=workspace.id,
                task_title="t",
            )
        )
    # A valid hub_task creates cleanly.
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="nightly",
            kind=ScheduledTaskKind.HUB_TASK,
            cron="0 3 * * *",
            workspace_id=workspace.id,
            task_title="Nightly lint",
            message="Run the lint sweep",
        )
    )
    assert task.task_title == "Nightly lint"
    assert task.workspace_id == workspace.id


def test_create_rejects_invalid_cron(manager: WorkspaceManager, tmp_path: Path) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    with pytest.raises(ValueError):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="x",
                kind=ScheduledTaskKind.TAB_MESSAGE,
                cron="not a cron",
                tab_id=session.tab_id,
                message="m",
            )
        )
    with pytest.raises(ValueError):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="x",
                kind=ScheduledTaskKind.TAB_MESSAGE,
                cron="* * * *",  # 4 fields
                tab_id=session.tab_id,
                message="m",
            )
        )


def test_update_normalizes_schedule_switch(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="orig",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            cron="*/5 * * * *",
            tab_id=session.tab_id,
            message="m",
        )
    )
    next_before = task.next_run_at

    # A non-schedule update keeps the next-run hint.
    renamed = manager.update_scheduled_task(task.id, ScheduledTaskUpdate(name="renamed"))
    assert renamed.name == "renamed"
    assert renamed.next_run_at == next_before

    # Switching cron -> run_at clears the other schedule fields and recomputes.
    future = datetime.now() + timedelta(hours=2)
    switched = manager.update_scheduled_task(task.id, ScheduledTaskUpdate(run_at=future))
    assert switched.run_at == future
    assert switched.cron is None
    assert switched.interval_seconds is None
    assert switched.next_run_at == future


def test_get_update_delete_missing_raises(manager: WorkspaceManager, tmp_path: Path) -> None:
    _make_workspace(manager, tmp_path)
    with pytest.raises(KeyError):
        manager.get_scheduled_task("nope")
    with pytest.raises(KeyError):
        manager.update_scheduled_task("nope", ScheduledTaskUpdate(name="x"))
    assert manager.delete_scheduled_task("nope") is False


def test_delete_existing(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="x",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            interval_seconds=60,
            tab_id=session.tab_id,
            message="m",
        )
    )
    assert manager.delete_scheduled_task(task.id) is True
    assert manager.delete_scheduled_task(task.id) is False
    assert manager.list_scheduled_tasks() == []


def test_scheduled_tasks_persist_across_reload(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="persist",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            cron="30 9 * * *",
            tab_id=session.tab_id,
            message="m",
        )
    )
    task_id = task.id

    reloaded = WorkspaceManager()
    assert task_id in reloaded.scheduled_tasks
    rt = reloaded.scheduled_tasks[task_id]
    assert rt.name == "persist"
    assert rt.kind == ScheduledTaskKind.TAB_MESSAGE
    assert rt.cron == "30 9 * * *"
    assert rt.tab_id == session.tab_id


# ---------------------------------------------------------------------------
# Native Chat automations
# ---------------------------------------------------------------------------


def test_create_chat_turn_validates_and_pins_backend(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab(agent_type=AgentType.TRAEX)
    _stub_chat_target(monkeypatch, chat)

    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="continue chat",
            kind=ScheduledTaskKind.CHAT_TURN,
            cron="0 9 * * *",
            tab_id=chat.id,
            agent_type=AgentType.CLAUDE,
            message="Summarize overnight changes",
        )
    )

    assert task.kind == ScheduledTaskKind.CHAT_TURN
    assert task.agent_type == AgentType.TRAEX
    assert task.tab_id == chat.id

    _stub_chat_target(
        monkeypatch, SimpleNamespace(**{**vars(chat), "session_kind": SessionKind.TERMINAL})
    )
    with pytest.raises(ValueError, match="top-level Chat"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="invalid target",
                kind=ScheduledTaskKind.CHAT_TURN,
                interval_seconds=60,
                tab_id=chat.id,
                message="hello",
            )
        )


async def test_chat_turn_dispatches_and_completion_drains_fifo(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)
    dispatched: list[str] = []

    async def dispatch(run: Any, _task: ScheduledTask) -> None:
        dispatched.append(run.id)

    manager.configure_scheduled_chat_dispatch(dispatch)
    first_task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="first",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="first message",
        )
    )
    second_task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="second",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="second message",
        )
    )

    first_at = datetime.now()
    await manager._fire_scheduled_task(first_task, first_at, manual=True)
    second_at = first_at + timedelta(seconds=2)
    await manager._fire_scheduled_task(second_task, second_at, manual=True)

    first = manager.scheduled_task_runs[first_task.last_run_id or ""]
    second = manager.scheduled_task_runs[second_task.last_run_id or ""]
    assert dispatched == [first.id]
    assert first.status == ScheduledTaskRunStatus.RUNNING
    assert second.status == ScheduledTaskRunStatus.QUEUED

    await manager.on_scheduled_chat_turn_completed(chat.id, first.client_turn_id, "completed")

    assert first.status == ScheduledTaskRunStatus.COMPLETED
    assert second.status == ScheduledTaskRunStatus.RUNNING
    assert dispatched == [first.id, second.id]


async def test_chat_turn_busy_and_goal_wait_then_retry(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    goal_state = _stub_goal(monkeypatch)
    busy = True
    dispatched: list[str] = []

    async def dispatch(run: Any, _task: ScheduledTask) -> None:
        nonlocal busy
        if busy:
            raise RuntimeError(
                "a turn is already in flight; wait for it to complete before sending another message"
            )
        dispatched.append(run.id)

    manager.configure_scheduled_chat_dispatch(dispatch)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="wait safely",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="do this next",
        )
    )

    await manager._fire_scheduled_task(task, datetime.now(), manual=True)
    run = manager.scheduled_task_runs[task.last_run_id or ""]
    assert run.status == ScheduledTaskRunStatus.WAITING
    assert run.waiting_reason == "waiting for the current Chat response"

    busy = False
    goal_state["goal"] = SimpleNamespace(
        status=SimpleNamespace(value="active"),
        dispatch_state=SimpleNamespace(value="dispatched"),
    )
    await manager._drain_scheduled_chat_runs(chat.id)
    assert run.status == ScheduledTaskRunStatus.WAITING
    assert run.waiting_reason == "waiting for the active Goal"

    goal_state["goal"] = None
    await manager._drain_scheduled_chat_runs(chat.id)
    assert run.status == ScheduledTaskRunStatus.RUNNING
    assert dispatched == [run.id]


async def test_chat_turn_ready_runtime_dispatches_immediately(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    """A warm Chat (readiness True) dispatches on the first drain, no parking."""
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)
    readiness_calls: list[str] = []
    dispatched: list[str] = []

    async def ready(tab_id: str) -> bool:
        readiness_calls.append(tab_id)
        return True

    async def dispatch(run: Any, _task: ScheduledTask) -> None:
        dispatched.append(run.id)

    manager.configure_scheduled_chat_readiness(ready)
    manager.configure_scheduled_chat_dispatch(dispatch)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="warm",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="warm message",
        )
    )

    await manager._fire_scheduled_task(task, datetime.now(), manual=True)
    run = manager.scheduled_task_runs[task.last_run_id or ""]
    assert run.status == ScheduledTaskRunStatus.RUNNING
    assert run.dispatch_attempts == 0
    assert readiness_calls == [chat.id]
    assert dispatched == [run.id]


async def test_chat_turn_cold_runtime_parks_then_dispatches_when_ready(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    """A cold Chat is parked WAITING (not cancelled/delivered) until ready."""
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)
    # Keep the self-scheduled redrain inert; the test drives drains explicitly.
    manager._SCHEDULED_CHAT_COLD_REDRAIN_DELAY = timedelta(hours=1)
    is_ready = False
    readiness_calls = 0
    dispatched: list[str] = []

    async def ready(_tab_id: str) -> bool:
        nonlocal readiness_calls
        readiness_calls += 1
        return is_ready

    async def dispatch(run: Any, _task: ScheduledTask) -> None:
        dispatched.append(run.id)

    manager.configure_scheduled_chat_readiness(ready)
    manager.configure_scheduled_chat_dispatch(dispatch)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="cold",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="cold message",
        )
    )

    await manager._fire_scheduled_task(task, datetime.now(), manual=True)
    run = manager.scheduled_task_runs[task.last_run_id or ""]
    # Cold: parked with a bounded retry, the turn is never delivered to a
    # runtime that does not exist, and it is not cancelled.
    assert run.status == ScheduledTaskRunStatus.WAITING
    assert run.waiting_reason == "waiting for the Chat runtime to start"
    assert run.dispatch_attempts == 1
    assert dispatched == []
    assert run.dispatched_at is None

    # Still cold on the next drain: the attempt count grows but the run keeps
    # waiting rather than failing or double-delivering.
    await manager._drain_scheduled_chat_runs(chat.id)
    assert run.status == ScheduledTaskRunStatus.WAITING
    assert run.dispatch_attempts == 2
    assert dispatched == []

    # The runtime finishes starting: the very next drain delivers the turn.
    is_ready = True
    await manager._drain_scheduled_chat_runs(chat.id)
    assert run.status == ScheduledTaskRunStatus.RUNNING
    assert run.dispatch_attempts == 2
    assert dispatched == [run.id]

    await manager.on_scheduled_chat_turn_completed(chat.id, run.client_turn_id, "completed")
    assert run.status == ScheduledTaskRunStatus.COMPLETED


async def test_chat_turn_cold_runtime_bounded_fail_after_max_attempts(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    """A runtime that never comes up fails after a bounded number of attempts."""
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)
    manager._SCHEDULED_CHAT_COLD_REDRAIN_DELAY = timedelta(hours=1)
    manager._SCHEDULED_CHAT_COLD_MAX_ATTEMPTS = 3
    dispatched: list[str] = []

    async def never_ready(_tab_id: str) -> bool:
        return False

    async def dispatch(run: Any, _task: ScheduledTask) -> None:
        dispatched.append(run.id)

    manager.configure_scheduled_chat_readiness(never_ready)
    manager.configure_scheduled_chat_dispatch(dispatch)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="never warms",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="doomed",
        )
    )

    await manager._fire_scheduled_task(task, datetime.now(), manual=True)
    run = manager.scheduled_task_runs[task.last_run_id or ""]
    # attempts: parked(1), parked(2), parked(3), then the 4th drain fails.
    for expected in (1, 2, 3):
        assert run.status == ScheduledTaskRunStatus.WAITING
        assert run.dispatch_attempts == expected
        await manager._drain_scheduled_chat_runs(chat.id)
    assert run.status == ScheduledTaskRunStatus.FAILED
    assert "did not become ready" in (run.error or "")
    assert run.completed_at is not None
    assert dispatched == []


async def test_chat_turn_cold_runtime_redrives_via_self_scheduled_drain(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    """The WAITING run is redelivered automatically by the bounded redrain."""
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)
    manager._SCHEDULED_CHAT_COLD_REDRAIN_DELAY = timedelta(milliseconds=20)
    is_ready = False
    dispatched: list[str] = []

    async def ready(_tab_id: str) -> bool:
        return is_ready

    async def dispatch(run: Any, _task: ScheduledTask) -> None:
        dispatched.append(run.id)

    manager.configure_scheduled_chat_readiness(ready)
    manager.configure_scheduled_chat_dispatch(dispatch)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="self redrive",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="self redrive",
        )
    )

    await manager._fire_scheduled_task(task, datetime.now(), manual=True)
    run = manager.scheduled_task_runs[task.last_run_id or ""]
    assert run.status == ScheduledTaskRunStatus.WAITING

    is_ready = True
    # The parked run scheduled one redrain; after the short delay it must drain
    # itself and deliver without anyone calling the drain manually.
    deadline = asyncio.get_event_loop().time() + 2.0
    while asyncio.get_event_loop().time() < deadline and not dispatched:
        await asyncio.sleep(0.02)
    assert dispatched == [run.id]
    assert run.status == ScheduledTaskRunStatus.RUNNING
    # Exactly one redrain task exists per tab, never a pile-up.
    assert len(manager._sched_chat_redrain_tasks) <= 1


async def test_chat_turn_cold_runtime_gate_skipped_while_goal_active(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    """An active Goal parks the run BEFORE the cold-runtime gate is consulted."""
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    goal_state = _stub_goal(monkeypatch)
    goal_state["goal"] = SimpleNamespace(
        status=SimpleNamespace(value="active"),
        dispatch_state=SimpleNamespace(value="dispatched"),
    )
    readiness_calls = 0

    async def ready(_tab_id: str) -> bool:
        nonlocal readiness_calls
        readiness_calls += 1
        return True

    async def dispatch(_run: Any, _task: ScheduledTask) -> None:
        raise AssertionError("must not deliver while a Goal is active")

    manager.configure_scheduled_chat_readiness(ready)
    manager.configure_scheduled_chat_dispatch(dispatch)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="goal guarded",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="wait for goal",
        )
    )

    await manager._fire_scheduled_task(task, datetime.now(), manual=True)
    run = manager.scheduled_task_runs[task.last_run_id or ""]
    assert run.status == ScheduledTaskRunStatus.WAITING
    assert run.waiting_reason == "waiting for the active Goal"
    assert readiness_calls == 0


async def test_chat_turn_supersedes_blocked_occurrences_and_delivers_latest(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)
    busy = True
    dispatched: list[str] = []

    async def dispatch(run: Any, _task: ScheduledTask) -> None:
        if busy:
            raise RuntimeError(
                "a turn is already in flight; wait for it to complete before sending another message"
            )
        dispatched.append(run.id)

    manager.configure_scheduled_chat_dispatch(dispatch)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="supersede stale occurrences",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="scheduled message",
        )
    )

    first_at = task.next_run_at
    assert first_at is not None
    await manager._fire_scheduled_task(task, first_at)
    second_at = task.next_run_at
    assert second_at is not None
    await manager._fire_scheduled_task(task, second_at)
    third_at = task.next_run_at
    assert third_at is not None
    await manager._fire_scheduled_task(task, third_at)

    runs = sorted(
        manager.list_scheduled_task_runs(task.id),
        key=lambda run: (run.scheduled_for, run.queued_at),
    )
    assert len(runs) == 3
    assert len({run.id for run in runs}) == 3
    assert task.run_count == 3
    # While the Chat is busy, only the newest occurrence survives as the
    # pending head; earlier undelivered occurrences are auditable SKIPPED
    # records instead of a burst waiting in the FIFO (latest-wins).
    assert [run.status for run in runs] == [
        ScheduledTaskRunStatus.SKIPPED,
        ScheduledTaskRunStatus.SKIPPED,
        ScheduledTaskRunStatus.WAITING,
    ]
    assert all("superseded" in (run.error or "") for run in runs[:2])
    assert task.last_run_id == runs[-1].id

    busy = False
    await manager._drain_scheduled_chat_runs(chat.id)

    # Only the newest occurrence is delivered; superseded occurrences never
    # replay as a burst.
    assert dispatched == [runs[2].id]
    assert runs[0].status == ScheduledTaskRunStatus.SKIPPED
    assert runs[1].status == ScheduledTaskRunStatus.SKIPPED
    assert runs[2].status == ScheduledTaskRunStatus.RUNNING
    assert task.last_run_id == runs[-1].id
    assert task.last_status == ScheduledTaskRunStatus.RUNNING.value


async def test_chat_turn_blocked_backlog_is_auditable_and_bounded(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)
    monkeypatch.setattr(manager, "_SCHEDULED_CHAT_ACTIVE_RUN_LIMIT", 2)

    async def busy(_run: Any, _task: ScheduledTask) -> None:
        raise RuntimeError(
            "a turn is already in flight; wait for it to complete before sending another message"
        )

    manager.configure_scheduled_chat_dispatch(busy)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="bounded backlog",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="scheduled message",
        )
    )

    for _ in range(3):
        fire_at = task.next_run_at
        assert fire_at is not None
        await manager._fire_scheduled_task(task, fire_at)

    runs = sorted(
        manager.list_scheduled_task_runs(task.id),
        key=lambda run: (run.scheduled_for, run.queued_at, run.id),
    )
    assert task.run_count == 3
    # One pending head (waiting) per blocked task: intermediate occurrences
    # are superseded, each with an explicit, auditable reason.
    assert [run.status for run in runs] == [
        ScheduledTaskRunStatus.SKIPPED,
        ScheduledTaskRunStatus.SKIPPED,
        ScheduledTaskRunStatus.WAITING,
    ]
    for run in runs[:-1]:
        assert run.completed_at is not None
        assert "superseded" in (run.error or "")
    active = [run for run in runs if manager._chat_run_active(run)]
    assert len(active) <= 2
    assert task.last_run_id == runs[-1].id
    assert task.last_status == ScheduledTaskRunStatus.WAITING.value


async def test_chat_turn_archived_deleted_and_backend_changed(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab(archived=True)
    tabs = _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)
    manager.configure_scheduled_chat_dispatch(_async_noop)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="target lifecycle",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="hello",
        )
    )

    await manager._fire_scheduled_task(task, datetime.now(), manual=True)
    run = manager.scheduled_task_runs[task.last_run_id or ""]
    assert run.status == ScheduledTaskRunStatus.WAITING
    assert run.waiting_reason == "target Chat is archived"

    tabs[chat.id] = _chat_tab(agent_type=AgentType.CODEX)
    await manager._drain_scheduled_chat_runs(chat.id)
    assert run.status == ScheduledTaskRunStatus.SKIPPED
    assert task.enabled is False
    assert "backend changed" in (run.error or "")
    reenabled = manager.update_scheduled_task(task.id, ScheduledTaskUpdate(enabled=True))
    assert reenabled.enabled is True
    assert reenabled.agent_type == AgentType.CODEX

    second = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="deleted target",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="hello again",
        )
    )
    tabs.clear()
    manager._disable_deleted_chat_targets(datetime.now())
    assert second.enabled is False
    assert second.last_status == ScheduledTaskRunStatus.SKIPPED.value


async def test_chat_turn_runs_persist_and_recover_from_stream(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch, state_root: Path
) -> None:
    from claude_hub.models import AgentStreamEvent, AgentStreamEventType
    from claude_hub.services.agent_stream.store import AgentStreamStore

    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)
    manager.configure_scheduled_chat_dispatch(_async_noop)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="durable",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="persist me",
        )
    )
    await manager._fire_scheduled_task(task, datetime.now(), manual=True)
    run = manager.scheduled_task_runs[task.last_run_id or ""]
    assert run.status == ScheduledTaskRunStatus.RUNNING

    store = AgentStreamStore("terminal-tabs", f"terminal-tab-{chat.id}")
    now = datetime.now()
    await store.append(
        AgentStreamEvent(
            stream_sequence=-1,
            session_id=f"terminal-tab-{chat.id}",
            tab_id=chat.id,
            agent_type=chat.agent_type,
            type=AgentStreamEventType.TURN_STARTED,
            turn_id=run.client_turn_id,
            created_at=now,
        )
    )
    await store.append(
        AgentStreamEvent(
            stream_sequence=-1,
            session_id=f"terminal-tab-{chat.id}",
            tab_id=chat.id,
            agent_type=chat.agent_type,
            type=AgentStreamEventType.TURN_COMPLETED,
            turn_id=run.client_turn_id,
            payload={"status": "completed"},
            created_at=now,
        )
    )

    reloaded = WorkspaceManager()
    assert reloaded._scheduled_chat_recovery_pending is True
    await reloaded._recover_scheduled_chat_runs()
    recovered = reloaded.scheduled_task_runs[run.id]
    assert recovered.status == ScheduledTaskRunStatus.COMPLETED
    assert reloaded.list_scheduled_task_runs(task.id)[0].id == run.id
    assert (state_root / "scheduled_tasks.json").exists()


async def test_chat_turn_recovery_queues_unstarted_and_marks_started_uncertain(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)
    manager.configure_scheduled_chat_dispatch(_async_noop)
    first_task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="not started",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="one",
        )
    )
    await manager._fire_scheduled_task(first_task, datetime.now(), manual=True)
    first = manager.scheduled_task_runs[first_task.last_run_id or ""]
    first.status = ScheduledTaskRunStatus.DISPATCHING

    second_task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="started",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="two",
        )
    )
    second = manager._queue_scheduled_chat_run(
        second_task, datetime.now() + timedelta(seconds=1), datetime.now()
    )
    second.status = ScheduledTaskRunStatus.RUNNING

    async def lifecycle(run: Any) -> tuple[bool, str | None]:
        return (run.id == second.id, None)

    monkeypatch.setattr(manager, "_scheduled_turn_lifecycle", lifecycle)
    await manager._recover_scheduled_chat_runs()

    assert first.status == ScheduledTaskRunStatus.QUEUED
    assert second.status == ScheduledTaskRunStatus.UNCERTAIN
    assert second.completed_at is not None


# ---------------------------------------------------------------------------
# Live stale-run reaper, cancel status, and queue controls
# ---------------------------------------------------------------------------


def _stale_run(
    manager: WorkspaceManager,
    task: ScheduledTask,
    scheduled_for: datetime,
    *,
    status: ScheduledTaskRunStatus = ScheduledTaskRunStatus.RUNNING,
    age: timedelta = timedelta(minutes=11),
) -> Any:
    now = _wm._now()
    run = manager._queue_scheduled_chat_run(task, scheduled_for, now)
    run.status = status
    run.dispatched_at = now - age
    return run


async def test_reaper_completes_stale_run_from_durable_completion_edge(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    from claude_hub.models import AgentStreamEvent, AgentStreamEventType
    from claude_hub.services.agent_stream.store import AgentStreamStore

    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="reaper transcript",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="x",
        )
    )
    now = _wm._now()
    run = _stale_run(manager, task, now - timedelta(minutes=12))

    store = AgentStreamStore("terminal-tabs", f"terminal-tab-{chat.id}")
    await store.append(
        AgentStreamEvent(
            stream_sequence=-1,
            session_id=f"terminal-tab-{chat.id}",
            tab_id=chat.id,
            agent_type=chat.agent_type,
            type=AgentStreamEventType.TURN_STARTED,
            turn_id=run.client_turn_id,
            created_at=now,
        )
    )
    await store.append(
        AgentStreamEvent(
            stream_sequence=-1,
            session_id=f"terminal-tab-{chat.id}",
            tab_id=chat.id,
            agent_type=chat.agent_type,
            type=AgentStreamEventType.TURN_COMPLETED,
            turn_id=run.client_turn_id,
            payload={"status": "cancelled"},
            created_at=now,
        )
    )

    probed: list[str] = []

    async def liveness(tab_id: str, turn_id: str) -> str:
        probed.append(turn_id)
        return "active"  # must not matter: transcript evidence wins

    manager.configure_scheduled_chat_liveness(liveness)
    await manager._reap_stale_scheduled_chat_runs(now)

    assert run.status == ScheduledTaskRunStatus.CANCELLED
    assert probed == []  # transcript terminal edge is authoritative


async def test_reaper_marks_dead_started_turn_uncertain_and_drains_queue(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)
    dispatched: list[str] = []

    async def dispatch(run: Any, _task: ScheduledTask) -> None:
        dispatched.append(run.id)

    manager.configure_scheduled_chat_dispatch(dispatch)

    async def lifecycle(run: Any) -> tuple[bool, str | None]:
        return (run.id == wedged.id, None)

    monkeypatch.setattr(manager, "_scheduled_turn_lifecycle", lifecycle)

    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="reaper dead",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="x",
        )
    )
    now = _wm._now()
    wedged = _stale_run(manager, task, now - timedelta(minutes=12))
    # A newer occurrence waiting behind the wedged head.
    queued = manager._queue_scheduled_chat_run(task, now - timedelta(minutes=5), now)

    async def liveness(tab_id: str, turn_id: str) -> str:
        return "dead" if turn_id == wedged.client_turn_id else "unknown"

    manager.configure_scheduled_chat_liveness(liveness)
    await manager._reap_stale_scheduled_chat_runs(now)

    assert wedged.status == ScheduledTaskRunStatus.UNCERTAIN
    # Drain released the FIFO and dispatched the queued occurrence.
    assert queued.id in dispatched


async def test_reaper_leaves_active_turn_and_fresh_run_alone(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    now = _wm._now()
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="reaper active",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="x",
        )
    )
    active = _stale_run(manager, task, now - timedelta(minutes=12))
    fresh = manager._queue_scheduled_chat_run(task, now - timedelta(minutes=1), now)
    fresh.status = ScheduledTaskRunStatus.RUNNING
    fresh.dispatched_at = now - timedelta(minutes=1)

    async def lifecycle(run: Any) -> tuple[bool, str | None]:
        return (True, None)

    async def liveness(tab_id: str, turn_id: str) -> str:
        return "active"

    monkeypatch.setattr(manager, "_scheduled_turn_lifecycle", lifecycle)
    manager.configure_scheduled_chat_liveness(liveness)
    await manager._reap_stale_scheduled_chat_runs(now)

    assert active.status == ScheduledTaskRunStatus.RUNNING
    assert fresh.status == ScheduledTaskRunStatus.RUNNING


async def test_cancelled_completion_edge_maps_to_cancelled_run(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    manager.configure_scheduled_chat_dispatch(_async_noop)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="cancel edge",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="x",
        )
    )
    await manager._fire_scheduled_task(task, datetime.now(), manual=True)
    run = manager.scheduled_task_runs[task.last_run_id or ""]
    assert run.status == ScheduledTaskRunStatus.RUNNING
    await manager.on_scheduled_chat_turn_completed(chat.id, run.client_turn_id, "cancelled")
    assert run.status == ScheduledTaskRunStatus.CANCELLED
    assert task.last_status == ScheduledTaskRunStatus.CANCELLED.value


async def test_disabling_task_cancels_its_queued_occurrences(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    manager.configure_scheduled_chat_dispatch(_async_noop)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="disable clears backlog",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="x",
        )
    )
    now = _wm._now()
    waiting = manager._queue_scheduled_chat_run(task, now, now)
    queued = manager._queue_scheduled_chat_run(task, now + timedelta(seconds=60), now)
    running = manager._queue_scheduled_chat_run(task, now + timedelta(seconds=120), now)
    waiting.status = ScheduledTaskRunStatus.WAITING
    waiting.waiting_reason = "target Chat is archived"
    queued.status = ScheduledTaskRunStatus.QUEUED
    running.status = ScheduledTaskRunStatus.RUNNING
    running.dispatched_at = now

    manager.update_scheduled_task(task.id, ScheduledTaskUpdate(enabled=False))

    assert waiting.status == ScheduledTaskRunStatus.CANCELLED
    assert "disabled" in (waiting.error or "")
    assert queued.status == ScheduledTaskRunStatus.CANCELLED
    # The run already executing is left to finish.
    assert running.status == ScheduledTaskRunStatus.RUNNING


async def test_manual_cancel_run_and_clear_backlog(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)
    dispatched: list[str] = []

    async def dispatch(run: Any, _task: ScheduledTask) -> None:
        dispatched.append(run.id)

    manager.configure_scheduled_chat_dispatch(dispatch)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="manual controls",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="x",
        )
    )
    now = _wm._now()
    wedged = manager._queue_scheduled_chat_run(task, now, now)
    wedged.status = ScheduledTaskRunStatus.RUNNING
    wedged.dispatched_at = now - timedelta(minutes=30)
    queued = manager._queue_scheduled_chat_run(task, now + timedelta(seconds=60), now)

    # Cancelling the wedged head releases the queue: queued dispatches.
    result = await manager.cancel_scheduled_task_run(wedged.id)
    assert result.status == ScheduledTaskRunStatus.CANCELLED
    assert queued.id in dispatched

    # clear-backlog on a fresh pile cancels queued/waiting but not a live run.
    extra1 = manager._queue_scheduled_chat_run(task, now + timedelta(seconds=120), now)
    extra2 = manager._queue_scheduled_chat_run(task, now + timedelta(seconds=180), now)
    count = await manager.cancel_pending_scheduled_task_runs(task.id)
    assert count == 1  # only extra2 remains queued (extra1 was superseded when extra2 queued)
    assert extra1.status == ScheduledTaskRunStatus.SKIPPED
    assert extra2.status == ScheduledTaskRunStatus.CANCELLED


async def test_scheduled_task_view_exposes_backlog_counts(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="view counts",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="x",
        )
    )
    now = _wm._now()
    running = manager._queue_scheduled_chat_run(task, now, now)
    running.status = ScheduledTaskRunStatus.RUNNING
    running.dispatched_at = now
    manager._queue_scheduled_chat_run(task, now + timedelta(seconds=60), now)

    view = manager.scheduled_task_view(task)
    assert view.in_flight_run_count == 1
    assert view.in_flight_run_id == running.id
    assert view.queued_run_count == 1
    assert view.active_run_count == 2

    with pytest.raises(KeyError):
        await manager.cancel_scheduled_task_run("missing-run-id")


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------


async def test_fire_tab_message_stamps_and_sends(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)

    sent: List[tuple[str, str]] = []

    async def fake_tmux_send(tmux_session: str, message: str) -> None:
        sent.append((tmux_session, message))

    monkeypatch.setattr(manager, "_send_tmux_message", fake_tmux_send)

    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="nudge",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            cron="*/5 * * * *",
            tab_id=session.tab_id,
            message="check in",
        )
    )
    assert task.run_count == 0

    now = datetime.now()
    await manager._fire_scheduled_task(task, now, manual=True)

    # The tmux session name is derived from the tab id (claude-hub-<tab_id[:8]>).
    assert sent == [("claude-hub-tab-sess", "check in")]
    assert task.last_run_at == now
    assert task.run_count == 1
    assert task.last_status == "ok"
    assert task.last_error is None
    # A cron task is re-armed for a future run, not disabled.
    assert task.enabled is True
    assert task.next_run_at is not None
    assert task.next_run_at > now


async def test_fire_send_failure_marks_error_but_keeps_stamp(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)

    async def fake_tmux_send(tmux_session: str, message: str) -> None:
        raise RuntimeError("tmux down")

    monkeypatch.setattr(manager, "_send_tmux_message", fake_tmux_send)

    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="nudge",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            cron="*/5 * * * *",
            tab_id=session.tab_id,
            message="check in",
        )
    )
    now = datetime.now()
    await manager._fire_scheduled_task(task, now, manual=True)

    # Stamps persisted BEFORE the failing side effect (crash-idempotent).
    assert task.last_run_at == now
    assert task.run_count == 1
    assert task.last_status == "error"
    assert "tmux down" in (task.last_error or "")
    # The cron task is still re-armed despite the error.
    assert task.enabled is True
    assert task.next_run_at is not None


async def test_fire_one_shot_disables_and_clears_next_run(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)

    async def fake_tmux_send(tmux_session: str, message: str) -> None:
        return None

    monkeypatch.setattr(manager, "_send_tmux_message", fake_tmux_send)

    future = datetime.now() + timedelta(hours=1)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="one-shot",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            run_at=future,
            tab_id=session.tab_id,
            message="go",
        )
    )
    assert task.next_run_at == future

    await manager._fire_scheduled_task(task, datetime.now(), manual=True)
    assert task.enabled is False
    assert task.next_run_at is None
    assert task.run_count == 1
    assert task.last_status == "ok"


async def test_fire_new_session_creates_ephemeral_and_sends(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    captured_req: List[EnsureWorkspaceAgentRequest] = []
    new_session = _make_session(workspace, session_id="new-1", caller_owned_ephemeral=True)

    async def fake_ensure(
        workspace_id: str, payload: EnsureWorkspaceAgentRequest
    ) -> ManagedSession:
        captured_req.append(payload)
        manager.sessions[new_session.id] = new_session
        return new_session

    sent: List[tuple[str, str]] = []

    async def fake_send(
        session_id: str, message: str, attachments: list | None = None, call_id: str | None = None
    ) -> None:
        sent.append((session_id, message))

    monkeypatch.setattr(manager, "ensure_workspace_agent", fake_ensure)
    monkeypatch.setattr(manager, "send_session_message", fake_send)

    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="fresh run",
            kind=ScheduledTaskKind.NEW_SESSION,
            run_at=datetime.now() + timedelta(hours=1),
            workspace_id=workspace.id,
            message="do a thing",
        )
    )
    await manager._fire_scheduled_task(task, datetime.now(), manual=True)

    assert len(captured_req) == 1
    req = captured_req[0]
    assert req.ephemeral is True
    assert req.caller_owned_ephemeral is True
    assert req.role == WorkspaceSessionRole.ORCHESTRATOR
    assert req.reuse_existing is False
    assert sent == [("new-1", "do a thing")]
    assert task.last_status == "ok"


async def test_fire_hub_task_publishes_internal_task_and_dispatches(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    new_session = _make_session(workspace, session_id="orch-1", caller_owned_ephemeral=True)

    async def fake_ensure(
        workspace_id: str, payload: EnsureWorkspaceAgentRequest
    ) -> ManagedSession:
        manager.sessions[new_session.id] = new_session
        return new_session

    dispatched: List[tuple[str, str]] = []

    async def fake_dispatch(task: Any, session: ManagedSession) -> None:
        dispatched.append((task.id, session.id))

    monkeypatch.setattr(manager, "ensure_workspace_agent", fake_ensure)
    monkeypatch.setattr(manager, "_dispatch_task_to_session", fake_dispatch)

    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="nightly task",
            kind=ScheduledTaskKind.HUB_TASK,
            cron="0 3 * * *",
            workspace_id=workspace.id,
            task_title="Nightly lint",
            message="Run the lint sweep",
        )
    )
    await manager._fire_scheduled_task(task, datetime.now(), manual=True)

    assert task.last_status == "ok"
    assert len(dispatched) == 1
    internal_task_id, session_id = dispatched[0]
    assert session_id == "orch-1"
    internal = manager.tasks[internal_task_id]
    assert internal.system_internal is True
    assert internal.internal_kind == "scheduled"
    assert internal.status == WorkspaceTaskStatus.QUEUED
    assert internal.dispatch_reason == "scheduled"
    assert internal.title == "Nightly lint"
    assert internal.prompt == "Run the lint sweep"
    assert internal.agent_type == AgentType.CLAUDE


async def test_run_scheduled_task_fires_immediately(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)

    sent: List[str] = []

    async def fake_tmux_send(tmux_session: str, message: str) -> None:
        sent.append(message)

    monkeypatch.setattr(manager, "_send_tmux_message", fake_tmux_send)

    # A far-future run_at is not otherwise due, but run-now fires it anyway.
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="manual",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            run_at=datetime.now() + timedelta(days=1),
            tab_id=session.tab_id,
            message="now",
        )
    )
    await manager.run_scheduled_task(task.id)
    assert sent == ["now"]
    assert manager.scheduled_tasks[task.id].run_count == 1


async def test_run_scheduled_task_raises_on_fire_failure(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)

    async def fake_tmux_send(tmux_session: str, message: str) -> None:
        raise RuntimeError("tmux down")

    monkeypatch.setattr(manager, "_send_tmux_message", fake_tmux_send)

    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="fail",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            run_at=datetime.now() + timedelta(hours=1),
            tab_id=session.tab_id,
            message="go",
        )
    )
    # The fire failure propagates so the API / CLI can surface a non-success
    # status; the task is still stamped and records the error.
    with pytest.raises(RuntimeError, match="tmux down"):
        await manager.run_scheduled_task(task.id)
    assert task.last_status == "error"
    assert "tmux down" in (task.last_error or "")
    # A one-shot is still disabled even though its fire failed.
    assert task.enabled is False
    assert task.run_count == 1


async def test_run_scheduled_chat_task_raises_on_immediate_dispatch_failure(
    manager: WorkspaceManager, monkeypatch: MonkeyPatch
) -> None:
    chat = _chat_tab()
    _stub_chat_target(monkeypatch, chat)
    _stub_goal(monkeypatch)

    async def fail_dispatch(_run: Any, _task: ScheduledTask) -> None:
        raise RuntimeError("provider transport failed")

    manager.configure_scheduled_chat_dispatch(fail_dispatch)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="fail native send",
            kind=ScheduledTaskKind.CHAT_TURN,
            interval_seconds=60,
            tab_id=chat.id,
            message="send now",
        )
    )

    with pytest.raises(RuntimeError, match="provider transport failed"):
        await manager.run_scheduled_task(task.id)

    run = manager.scheduled_task_runs[task.last_run_id or ""]
    assert run.status == ScheduledTaskRunStatus.FAILED
    assert run.error == "provider transport failed"
    assert task.last_status == ScheduledTaskRunStatus.FAILED.value
    assert task.run_count == 1


async def test_run_scheduled_task_on_disabled_raises(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="off",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            run_at=datetime.now() + timedelta(hours=1),
            tab_id=session.tab_id,
            message="go",
            enabled=False,
        )
    )
    with pytest.raises(ValueError, match="disabled"):
        await manager.run_scheduled_task(task.id)
    # A disabled task is never stamped by a manual run.
    assert task.run_count == 0


async def test_fire_lock_prevents_concurrent_double_fire(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)

    sent: List[str] = []

    async def fake_tmux_send(tmux_session: str, message: str) -> None:
        # Yield so a second concurrent fire can reach the lock before the first
        # one releases it.
        await asyncio.sleep(0)
        sent.append(message)

    monkeypatch.setattr(manager, "_send_tmux_message", fake_tmux_send)

    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="concurrent",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            interval_seconds=3600,
            tab_id=session.tab_id,
            message="once",
        )
    )
    task.next_run_at = datetime.now() - timedelta(seconds=1)  # due

    now = datetime.now()
    await asyncio.gather(
        manager._fire_scheduled_task(task, now),
        manager._fire_scheduled_task(task, now),
    )

    # The per-task lock + in-lock re-check serialize the two fires; only one
    # actually stamps and sends (the second sees the advanced next_run_at).
    assert task.run_count == 1
    assert sent == ["once"]


async def test_manual_run_now_double_fire_fires_once(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Two concurrent manual run-now calls on a recurring task fire once.

    The in-lock re-check compares the captured ``run_count``; a recurring
    task stays enabled after firing, so without the counter check the
    second manual call would re-fire (the ``next_run_at`` re-check is
    tick-only and does not apply to the manual path).
    """
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)

    sent: List[str] = []

    async def fake_tmux_send(tmux_session: str, message: str) -> None:
        # Yield so the second manual fire reaches the lock before the first
        # releases it.
        await asyncio.sleep(0)
        sent.append(message)

    monkeypatch.setattr(manager, "_send_tmux_message", fake_tmux_send)

    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="manual double",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            interval_seconds=3600,
            tab_id=session.tab_id,
            message="fire-me",
        )
    )

    now = datetime.now()
    await asyncio.gather(
        manager._fire_scheduled_task(task, now, manual=True),
        manager._fire_scheduled_task(task, now, manual=True),
    )

    assert task.run_count == 1
    assert sent == ["fire-me"]
    # A recurring task stays enabled after firing.
    assert task.enabled is True


def test_update_allows_toggle_when_tab_deleted(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A task whose tab was deleted can still be disabled via update.

    The tab-existence re-check is skipped when ``tab_id`` is unchanged, so
    the enable/disable toggle (and other non-tab edits) don't raise
    "Terminal tab not found". Changing the tab to a live one still
    validates.
    """
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)

    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="stuck",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            interval_seconds=3600,
            tab_id=session.tab_id,
            message="hi",
        )
    )

    # The target tab is gone now.
    _stub_known_tabs(monkeypatch)

    # Disabling via the toggle (tab_id unchanged) must succeed.
    updated = manager.update_scheduled_task(task.id, ScheduledTaskUpdate(enabled=False))
    assert updated.enabled is False

    # Re-pointing to a still-missing tab is rejected.
    with pytest.raises(ValueError, match="not found"):
        manager.update_scheduled_task(task.id, ScheduledTaskUpdate(tab_id="also-gone"))


def test_new_session_rejects_recurring_schedule(manager: WorkspaceManager, tmp_path: Path) -> None:
    workspace = _make_workspace(manager, tmp_path)
    # A recurring new_session task would leak one ephemeral session per fire;
    # only one-shot run_at is allowed.
    with pytest.raises(ValueError, match="one-shot"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="recurring new session",
                kind=ScheduledTaskKind.NEW_SESSION,
                cron="0 9 * * *",
                workspace_id=workspace.id,
                message="do a thing",
            )
        )
    with pytest.raises(ValueError, match="one-shot"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="recurring new session",
                kind=ScheduledTaskKind.NEW_SESSION,
                interval_seconds=3600,
                workspace_id=workspace.id,
                message="do a thing",
            )
        )


def test_create_rejects_whitespace_name(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)
    with pytest.raises(ValueError, match="name must not be empty"):
        manager.create_scheduled_task(
            ScheduledTaskCreate(
                name="   ",
                kind=ScheduledTaskKind.TAB_MESSAGE,
                run_at=datetime.now() + timedelta(hours=1),
                tab_id=session.tab_id,
                message="go",
            )
        )


def test_update_rejects_whitespace_name(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)
    task = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="keep",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            run_at=datetime.now() + timedelta(hours=1),
            tab_id=session.tab_id,
            message="go",
        )
    )
    with pytest.raises(ValueError, match="name must not be empty"):
        manager.update_scheduled_task(task.id, ScheduledTaskUpdate(name="  "))
    # A non-empty name is stripped, not rejected.
    updated = manager.update_scheduled_task(task.id, ScheduledTaskUpdate(name="  renamed  "))
    assert updated.name == "renamed"


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


async def test_tick_fires_only_due_enabled_tasks(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    session = _make_session(workspace)
    manager.sessions[session.id] = session
    _stub_known_tabs(monkeypatch, session.tab_id)

    sent: List[str] = []

    async def fake_tmux_send(tmux_session: str, message: str) -> None:
        sent.append(message)

    monkeypatch.setattr(manager, "_send_tmux_message", fake_tmux_send)

    past = datetime.now() - timedelta(minutes=1)
    future = datetime.now() + timedelta(hours=1)

    due = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="due",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            interval_seconds=60,
            tab_id=session.tab_id,
            message="due-msg",
        )
    )
    due.next_run_at = past
    not_due = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="not-due",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            interval_seconds=60,
            tab_id=session.tab_id,
            message="not-due-msg",
        )
    )
    not_due.next_run_at = future
    disabled = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="disabled",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            interval_seconds=60,
            tab_id=session.tab_id,
            message="disabled-msg",
            enabled=False,
        )
    )
    disabled.next_run_at = past

    await manager._tick_scheduled_tasks()

    assert sent == ["due-msg"]
    assert manager.scheduled_tasks[due.id].run_count == 1
    assert manager.scheduled_tasks[not_due.id].run_count == 0
    assert manager.scheduled_tasks[disabled.id].run_count == 0


async def test_tick_no_tasks_is_noop(manager: WorkspaceManager) -> None:
    # Should simply return without raising when there are no tasks.
    await manager._tick_scheduled_tasks()


# ---------------------------------------------------------------------------
# Env injection — CLAUDE_HUB_TAB_ID
# ---------------------------------------------------------------------------


def test_child_env_overlays_tab_id_without_mutating_base() -> None:
    """``_child_env`` exposes ``CLAUDE_HUB_TAB_ID`` but keeps ``self.env`` clean.

    The overlay is re-derived at every render point (not stored on ``self.env``)
    so it is never persisted as user config and survives ``switch_env``
    reassignments that rebuild ``self.env``.
    """
    tm = import_module("claude_hub.services.ttyd_manager")
    proc = tm.TTYDProcess.__new__(tm.TTYDProcess)
    proc.env = {"FOO": "bar"}
    proc.tab_id = "tab-123"

    child = proc._child_env()

    assert child["FOO"] == "bar"
    assert child["CLAUDE_HUB_TAB_ID"] == "tab-123"
    # The base env is copied, not mutated.
    assert "CLAUDE_HUB_TAB_ID" not in proc.env


# ---------------------------------------------------------------------------
# Auto-cleanup of the scheduled task's ephemeral session
# ---------------------------------------------------------------------------


def _scheduled_internal_task(manager: WorkspaceManager, workspace: Workspace):
    return manager._create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Scheduled sweep", prompt="Do the sweep", task_mode=WorkspaceTaskMode.REVIEWED
        ),
        system_internal=True,
        internal_kind="scheduled",
    )


async def test_auto_cleanup_deletes_only_caller_owned_ephemeral(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    deleted: List[str] = []

    async def fake_delete(session_id: str) -> None:
        deleted.append(session_id)

    monkeypatch.setattr(manager, "delete_session", fake_delete)

    ephemeral = _make_session(workspace, session_id="eph", caller_owned_ephemeral=True)
    persistent = _make_session(workspace, session_id="persist", caller_owned_ephemeral=False)
    manager.sessions[ephemeral.id] = ephemeral
    manager.sessions[persistent.id] = persistent

    task = _scheduled_internal_task(manager, workspace)

    await manager._auto_cleanup_scheduled_session(task, ephemeral)
    assert deleted == ["eph"]

    # A non-ephemeral session is never torn down here.
    await manager._auto_cleanup_scheduled_session(task, persistent)
    assert deleted == ["eph"]

    # A session already removed from the registry is a no-op.
    manager.sessions.pop("eph", None)
    await manager._auto_cleanup_scheduled_session(task, ephemeral)
    assert deleted == ["eph"]


async def test_internal_report_scheduled_task_marks_done_and_cleans_ephemeral(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    task = _scheduled_internal_task(manager, workspace)
    session = _make_session(workspace, caller_owned_ephemeral=True)
    manager.sessions[session.id] = session

    deleted: List[str] = []

    async def fake_delete(session_id: str) -> None:
        deleted.append(session_id)
        manager.sessions.pop(session_id, None)

    async def fake_dispatch(workspace_id: str) -> None:
        return None

    monkeypatch.setattr(manager, "delete_session", fake_delete)
    monkeypatch.setattr(manager, "dispatch_workspace", fake_dispatch)
    monkeypatch.setattr(manager, "_record_system_task_audit", _noop)
    monkeypatch.setattr(manager, "_write_task_record", _noop)
    monkeypatch.setattr(manager, "_release_task_session", _noop)

    report = AgentReport(
        id="rep-1",
        workspace_id=workspace.id,
        task_id=task.id,
        session_id=session.id,
        state=AgentReportState.COMPLETED,
        message="done",
        created_at=datetime.now(),
    )
    await manager._handle_internal_task_report(manager.tasks[task.id], session, report)

    done = manager.tasks[task.id]
    assert done.status == WorkspaceTaskStatus.DONE
    assert done.review_skip_reason is not None
    assert deleted == [session.id]
    assert session.id not in manager.sessions


async def test_internal_report_scheduled_task_keeps_non_ephemeral_session(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    task = _scheduled_internal_task(manager, workspace)
    session = _make_session(workspace, caller_owned_ephemeral=False)
    manager.sessions[session.id] = session

    deleted: List[str] = []

    async def fake_delete(session_id: str) -> None:
        deleted.append(session_id)

    async def fake_dispatch(workspace_id: str) -> None:
        return None

    monkeypatch.setattr(manager, "delete_session", fake_delete)
    monkeypatch.setattr(manager, "dispatch_workspace", fake_dispatch)
    monkeypatch.setattr(manager, "_record_system_task_audit", _noop)
    monkeypatch.setattr(manager, "_write_task_record", _noop)
    monkeypatch.setattr(manager, "_release_task_session", _noop)

    report = AgentReport(
        id="rep-2",
        workspace_id=workspace.id,
        task_id=task.id,
        session_id=session.id,
        state=AgentReportState.READY_FOR_REVIEW,
        message="ready",
        created_at=datetime.now(),
    )
    await manager._handle_internal_task_report(manager.tasks[task.id], session, report)

    assert manager.tasks[task.id].status == WorkspaceTaskStatus.DONE
    assert deleted == []  # non-ephemeral session is left alone
    assert session.id in manager.sessions


# ---------------------------------------------------------------------------
# End-to-end: real fire -> real dispatch binding -> REAL report intake ->
# DONE -> ephemeral cleanup, plus dead-worker orphan convergence. These tests
# deliberately do NOT stub _fire_hub_task, _dispatch_task_to_session,
# create_report, delete_session, or ensure_workspace_agent — only the tmux /
# ttyd terminal side effects are faked, exactly as production would see them.
# ---------------------------------------------------------------------------


def _install_live_terminal_fakes(monkeypatch: MonkeyPatch, tmp_path: Path) -> Dict[str, List[str]]:
    """Fake the terminal control plane but keep session/dispatch/report real.

    Returns a dict recording ``deleted_tabs`` and ``sent`` tmux writes so tests
    can assert on lifecycle side effects.
    """
    import claude_hub.services.agent_stream as agent_stream

    recorded: Dict[str, List[str]] = {"deleted_tabs": [], "sent": []}
    tab_seq = {"n": 0}

    async def fake_create_tab(**kwargs: Any) -> SimpleNamespace:
        tab_seq["n"] += 1
        tab_id = f"tab-e2e-{tab_seq['n']}"
        return SimpleNamespace(
            id=tab_id,
            name=kwargs.get("name"),
            agent_type=kwargs.get("agent_type", AgentType.CLAUDE),
            agent_session_id=None,
            cursor_transport="terminal",
            cursor_data_dir=None,
            cursor_cli_version=None,
            cursor_transcript_path=None,
            cursor_transcript_schema=None,
        )

    async def fake_update_tab(tab_id: str, name: Optional[str] = None, **_: Any) -> Any:
        return SimpleNamespace(id=tab_id, name=name)

    async def fake_delete_tab(tab_id: str) -> None:
        recorded["deleted_tabs"].append(tab_id)

    async def fake_send_with_receipt(
        _self: Any, tmux_session: str, message: str, call_id: str
    ) -> None:
        recorded["sent"].append(call_id)

    async def fake_send_plain(_self: Any, tmux_session: str, message: str) -> None:
        # Fire-and-forget path (e.g. the no-call_id bootstrap prompt).
        recorded["sent"].append("bootstrap")

    async def fake_query_receipt(_self: Any, tmux_session: str, call_id: str) -> bool:
        # Receipt present: the paste is durably recorded, so the monitor never
        # re-pastes (mirrors a healthy tmux server).
        return True

    async def fake_discard_stream(workspace_id: str, session_id: str) -> None:
        return None

    async def fake_ready(_self: Any, _session: Any) -> None:
        return None

    async def fake_wait_prompt(_self: Any, _session: Any) -> None:
        return None

    monkeypatch.setattr(ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(ttyd_manager, "update_tab", fake_update_tab)
    monkeypatch.setattr(ttyd_manager, "delete_tab", fake_delete_tab)
    monkeypatch.setattr(
        _wm.WorkspaceManager, "_send_tmux_message_with_receipt", fake_send_with_receipt
    )
    monkeypatch.setattr(_wm.WorkspaceManager, "_send_tmux_message", fake_send_plain)
    monkeypatch.setattr(_wm.WorkspaceManager, "_query_tmux_receipt", fake_query_receipt)
    monkeypatch.setattr(_wm.WorkspaceManager, "_ensure_session_ready_for_send", fake_ready)
    monkeypatch.setattr(_wm.WorkspaceManager, "_wait_for_agent_prompt", fake_wait_prompt)
    monkeypatch.setattr(_wm.WorkspaceManager, "_capture_tmux_output", AsyncNoop.return_empty)
    monkeypatch.setattr(agent_stream, "discard_session_stream", fake_discard_stream)
    return recorded


class AsyncNoop:
    @staticmethod
    async def return_empty(*_args: Any, **_kwargs: Any) -> str:
        return ""

    @staticmethod
    async def dispatch_noop(*_args: Any, **_kwargs: Any) -> None:
        return None


def _create_hub_schedule(manager: WorkspaceManager, workspace: Workspace) -> ScheduledTask:
    return manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="nightly sweep",
            kind=ScheduledTaskKind.HUB_TASK,
            cron="0 3 * * *",
            workspace_id=workspace.id,
            task_title="Nightly lint",
            message="Run the lint sweep",
        )
    )


def _only_internal_task(manager: WorkspaceManager):
    internal = [t for t in manager.tasks.values() if t.internal_kind == "scheduled"]
    assert len(internal) == 1, f"expected one internal task, got {len(internal)}"
    return internal[0]


async def test_e2e_hub_task_fire_binds_session_and_real_report_completes_and_cleans(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    recorded = _install_live_terminal_fakes(monkeypatch, tmp_path)

    async def fake_dispatch_workspace(workspace_id: str, **_: Any) -> None:
        return None

    # The post-completion rebalance tick is orthogonal to the intake chain under
    # test; stub it so the e2e stays deterministic without a status refresh.
    monkeypatch.setattr(manager, "dispatch_workspace", fake_dispatch_workspace)

    schedule = _create_hub_schedule(manager, workspace)

    # REAL fire: creates the internal task, spawns a real ephemeral session, and
    # runs the real _dispatch_task_to_session (no stub of dispatch or spawn).
    await manager._fire_scheduled_task(schedule, datetime.now(), manual=True)

    task = _only_internal_task(manager)
    assert task.status == WorkspaceTaskStatus.WORKING
    # E1 regression guard: the canonical task -> session binding now exists.
    assert task.session_id is not None
    worker = manager.sessions[task.session_id]
    assert worker.caller_owned_ephemeral is True
    assert worker.task_id == task.id
    assert worker.current_task_id == task.id

    # Worker submits through the REAL report intake endpoint (not the internal
    # handler directly). E1 used to 400 here with "Task has no assigned worker".
    report = await manager.create_report(
        worker.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            message="lint sweep done",
            call_id=f"{task.id}-completed-cycle-1-attempt-1",
            changed_files=[],
        ),
    )
    assert report.task_id == task.id

    done = manager.tasks[task.id]
    assert done.status == WorkspaceTaskStatus.DONE
    assert done.completed_at is not None
    # Ephemeral auto-cleanup: the throwaway worker tab/session is gone.
    assert worker.id not in manager.sessions
    assert worker.tab_id in recorded["deleted_tabs"]


async def test_e2e_hub_task_worker_dies_early_redispatches_then_completes(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    recorded = _install_live_terminal_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(manager, "dispatch_workspace", AsyncNoop.dispatch_noop)

    schedule = _create_hub_schedule(manager, workspace)
    await manager._fire_scheduled_task(schedule, datetime.now(), manual=True)

    task = _only_internal_task(manager)
    first_worker_id = task.session_id
    first_attempt = task.dispatch_attempt
    assert first_attempt == 1
    # Snapshot the original fire's sends (it legitimately sent dispatch:1 once).
    sent_at_fire = list(recorded["sent"])
    dead_call = f"dispatch:{task.id}:{first_attempt}"
    assert dead_call in sent_at_fire

    # The first worker's tmux dies minutes before it ever ACKs/reports: the
    # session is observed STOPPED and the dispatch call is fail-closed uncertain.
    first = manager.sessions[first_worker_id]
    manager.sessions[first_worker_id] = first.model_copy(
        update={
            "status": ManagedSessionStatus.STOPPED,
            "runtime_status": AgentRuntimeStatus.OFFLINE,
            "processing_call_ids": [],
            "uncertain_call_ids": [dead_call],
        }
    )
    # Age past the orphan grace.
    backdated = manager.tasks[task.id].model_copy(
        update={"updated_at": datetime.now() - timedelta(seconds=120)}
    )
    manager.tasks[task.id] = backdated

    # Convergence sweep: bounded redispatch to a FRESH healthy ephemeral.
    await manager._converge_orphaned_hub_tasks(datetime.now())

    redispatched = manager.tasks[task.id]
    assert redispatched.status == WorkspaceTaskStatus.WORKING
    assert redispatched.session_id is not None
    assert redispatched.session_id != first_worker_id
    assert redispatched.dispatch_attempt == first_attempt + 1
    second_worker = manager.sessions[redispatched.session_id]
    assert second_worker.status == ManagedSessionStatus.WORKING
    # The dead first session was torn down; no stopped session is left behind.
    assert first_worker_id not in manager.sessions
    assert first.tab_id in recorded["deleted_tabs"]
    # The fresh dispatch used a brand-new call_id; the uncertain call on the
    # dead session was never auto-resent (its send count did not increase),
    # preserving the fail-closed contract.
    fresh_call = f"dispatch:{task.id}:{redispatched.dispatch_attempt}"
    sent_after = recorded["sent"]
    assert fresh_call in sent_after
    assert sent_after.count(dead_call) == sent_at_fire.count(dead_call)

    # No double-fire: another sweep while the replacement is healthy is a no-op
    # (same binding, same attempt, no extra session spawned).
    sessions_at = set(manager.sessions)
    await manager._converge_orphaned_hub_tasks(datetime.now())
    assert set(manager.sessions) == sessions_at
    again = manager.tasks[task.id]
    assert again.session_id == second_worker.id
    assert again.dispatch_attempt == redispatched.dispatch_attempt

    # The replacement worker completes through real report intake.
    await manager.create_report(
        second_worker.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            message="done on retry",
            call_id=f"{task.id}-completed-cycle-1-attempt-2",
        ),
    )
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.DONE
    assert second_worker.id not in manager.sessions


async def test_e2e_hub_task_dead_worker_exhausts_retries_fails_and_cleans_session(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    recorded = _install_live_terminal_fakes(monkeypatch, tmp_path)

    task = _scheduled_internal_task(manager, workspace)
    dead = _make_session(workspace, session_id="dead-eph", caller_owned_ephemeral=True)
    manager.sessions[dead.id] = dead
    now = datetime.now()
    manager.tasks[task.id] = task.model_copy(
        update={
            "status": WorkspaceTaskStatus.WORKING,
            "session_id": dead.id,
            "dispatch_attempt": _wm._scheduling.HUBTASK_ORPHAN_MAX_ATTEMPTS,
            "started_at": now - timedelta(seconds=300),
            "updated_at": now - timedelta(seconds=120),
        }
    )
    manager.sessions[dead.id] = dead.model_copy(
        update={
            "task_id": task.id,
            "current_task_id": task.id,
            "status": ManagedSessionStatus.STOPPED,
            "runtime_status": AgentRuntimeStatus.OFFLINE,
        }
    )

    await manager._converge_orphaned_hub_tasks(now)

    failed = manager.tasks[task.id]
    assert failed.status == WorkspaceTaskStatus.FAILED
    assert failed.failure_reason is not None
    assert failed.failed_at is not None
    assert dead.id not in manager.sessions
    assert dead.tab_id in recorded["deleted_tabs"]

    # A second sweep is a no-op on the now-terminal task (no respawn storm).
    sessions_before = set(manager.sessions)
    await manager._converge_orphaned_hub_tasks(now)
    assert set(manager.sessions) == sessions_before
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.FAILED


async def test_hub_task_orphan_convergence_leaves_healthy_and_non_scheduled_tasks_alone(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    _install_live_terminal_fakes(monkeypatch, tmp_path)

    # 1) A healthy scheduled hub_task, even very old, is never reaped.
    healthy_task = _scheduled_internal_task(manager, workspace)
    healthy = _make_session(workspace, session_id="healthy-eph", caller_owned_ephemeral=True)
    manager.sessions[healthy.id] = healthy
    old = datetime.now() - timedelta(hours=6)
    manager.tasks[healthy_task.id] = healthy_task.model_copy(
        update={
            "status": WorkspaceTaskStatus.WORKING,
            "session_id": healthy.id,
            "started_at": old,
            "updated_at": old,
        }
    )
    manager.sessions[healthy.id] = healthy.model_copy(
        update={
            "task_id": healthy_task.id,
            "current_task_id": healthy_task.id,
            "status": ManagedSessionStatus.WORKING,
            "runtime_status": AgentRuntimeStatus.WORKING,
        }
    )

    # 2) A normal (human, non-system) reviewed task with a dead worker keeps the
    # pre-existing reviewed-task recovery contract — the hub_task sweep must not
    # touch it (independent reviewer / long-task guarantees).
    human_task = manager._create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Human task", prompt="do it", task_mode=WorkspaceTaskMode.REVIEWED
        ),
    )
    human_session = _make_session(workspace, session_id="human-eph")
    manager.sessions[human_session.id] = human_session
    manager.tasks[human_task.id] = human_task.model_copy(
        update={
            "status": WorkspaceTaskStatus.WORKING,
            "session_id": human_session.id,
            "started_at": old,
            "updated_at": old,
        }
    )
    manager.sessions[human_session.id] = human_session.model_copy(
        update={
            "task_id": human_task.id,
            "current_task_id": human_task.id,
            "status": ManagedSessionStatus.STOPPED,
            "runtime_status": AgentRuntimeStatus.OFFLINE,
        }
    )

    sessions_before = set(manager.sessions)
    await manager._converge_orphaned_hub_tasks(datetime.now())

    assert set(manager.sessions) == sessions_before
    assert manager.tasks[healthy_task.id].status == WorkspaceTaskStatus.WORKING
    assert manager.tasks[healthy_task.id].session_id == healthy.id
    assert manager.tasks[human_task.id].status == WorkspaceTaskStatus.WORKING
    assert manager.tasks[human_task.id].session_id == human_session.id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _patch_get_client(
    monkeypatch: MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> List[httpx.Request]:
    captured: List[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    def fake_get_client(ctx: click.Context) -> HubClient:
        return HubClient(
            base_url="http://testserver", transport=httpx.MockTransport(recording_handler)
        )

    monkeypatch.setattr(cli_main, "get_client", fake_get_client)
    return captured


def test_cli_schedule_help_registers_commands() -> None:
    result = CliRunner().invoke(cli, ["schedule", "--help"])
    assert result.exit_code == 0, result.output
    for name in ("list", "get", "create", "update", "delete", "enable", "disable", "run"):
        assert name in result.output


def test_cli_schedule_body_rejects_multiple_schedules() -> None:
    with pytest.raises(click.ClickException, match="Only one"):
        _schedule_body(
            None,
            name="x",
            kind="tab_message",
            enabled=True,
            run_at="2026-09-09T09:00:00",
            cron="* * * * *",
            interval=None,
            tab_id=None,
            workspace_id=None,
            agent_type=None,
            message=None,
            task_title=None,
        )


def test_cli_schedule_builds_interval_body() -> None:
    body = _schedule_body(
        None,
        name="x",
        kind="hub_task",
        enabled=True,
        run_at=None,
        cron=None,
        interval=300,
        tab_id=None,
        workspace_id="ws-1",
        agent_type=None,
        message="m",
        task_title="t",
    )
    assert body["interval_seconds"] == 300
    assert body["kind"] == "hub_task"
    assert body["workspace_id"] == "ws-1"
    assert body["task_title"] == "t"


def test_cli_schedule_create_and_list(monkeypatch: MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/scheduled-tasks":
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "id": "st-1",
                    **body,
                    "run_count": 0,
                    "created_at": "2026-09-08T00:00:00",
                    "updated_at": "2026-09-08T00:00:00",
                },
            )
        if request.method == "GET" and request.url.path == "/api/scheduled-tasks":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "st-1",
                        "name": "nightly",
                        "kind": "hub_task",
                        "enabled": True,
                        "next_run_at": "2026-09-09T03:00:00",
                        "last_run_at": None,
                        "last_status": None,
                        "run_count": 0,
                        "created_at": "2026-09-08T00:00:00",
                        "updated_at": "2026-09-08T00:00:00",
                    }
                ],
            )
        return httpx.Response(
            404, json={"detail": f"unmatched {request.method} {request.url.path}"}
        )

    captured = _patch_get_client(monkeypatch, handler)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "schedule",
            "create",
            "--name",
            "nightly",
            "--kind",
            "hub_task",
            "--cron",
            "0 3 * * *",
            "--workspace-id",
            "ws-1",
            "--task-title",
            "Nightly",
            "--message",
            "Run it",
        ],
    )
    assert result.exit_code == 0, result.output
    post = next(r for r in captured if r.method == "POST")
    body = json.loads(post.content)
    assert body["cron"] == "0 3 * * *"
    assert body["kind"] == "hub_task"
    assert body["workspace_id"] == "ws-1"
    assert body["task_title"] == "Nightly"

    result = runner.invoke(cli, ["schedule", "list"])
    assert result.exit_code == 0, result.output
    assert "st-1" in result.output


def test_cli_schedule_enable_disable_and_run(monkeypatch: MonkeyPatch) -> None:
    patched: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH" and request.url.path.startswith("/api/scheduled-tasks/st-1"):
            patched.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "st-1", "enabled": True})
        if request.method == "POST" and request.url.path == "/api/scheduled-tasks/st-1/run":
            return httpx.Response(
                200,
                json={
                    "id": "st-1",
                    "last_run_at": "2026-09-08T10:00:00",
                    "last_status": "ok",
                    "last_error": None,
                },
            )
        return httpx.Response(404, json={"detail": "unmatched"})

    _patch_get_client(monkeypatch, handler)
    runner = CliRunner()

    result = runner.invoke(cli, ["schedule", "enable", "st-1"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli, ["schedule", "disable", "st-1"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli, ["schedule", "run", "st-1"])
    assert result.exit_code == 0, result.output

    assert {"enabled": True} in patched
    assert {"enabled": False} in patched


def test_cli_schedule_run_exits_nonzero_on_dispatch_failure(monkeypatch: MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/scheduled-tasks/st-1/run"
        return httpx.Response(500, json={"detail": "provider transport failed"})

    _patch_get_client(monkeypatch, handler)
    result = CliRunner().invoke(cli, ["schedule", "run", "st-1"])

    assert result.exit_code != 0
    assert "provider transport failed" in result.output


def test_cli_schedule_get_and_delete(monkeypatch: MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/scheduled-tasks/st-1":
            return httpx.Response(200, json={"id": "st-1", "name": "n", "kind": "hub_task"})
        if request.method == "DELETE" and request.url.path == "/api/scheduled-tasks/st-1":
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "unmatched"})

    _patch_get_client(monkeypatch, handler)
    runner = CliRunner()

    result = runner.invoke(cli, ["schedule", "get", "st-1"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli, ["schedule", "delete", "st-1"])
    assert result.exit_code == 0, result.output
    assert "deleted st-1" in result.output
