"""Tests for the unified Agent Tree + Durable Mailbox coordination layer."""

from __future__ import annotations

import asyncio
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest import MonkeyPatch

from claude_hub.models.agent_tree import (
    AgentEventType,
    AgentRunStatus,
    ExecutorKind,
    FollowupRequest,
    InterruptRequest,
    ListRunsRequest,
    SendRequest,
    SpawnRequest,
    WaitRequest,
)
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")


@pytest.fixture()
def state_root(monkeypatch: MonkeyPatch, tmp_path: Path) -> Generator[Path, None, None]:
    root = tmp_path / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    index_file = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)

    # Mock ttyd_manager / tmux so tests are hermetic regardless of whether
    # tmux is installed. We never exercise real terminal sessions in these
    # tests (managed-task runs are created directly with context_ref).
    fake_tab = MagicMock()
    fake_tab.id = "tab-mock"
    fake_tab.tmux_session = "tmux-mock"
    monkeypatch.setattr(_wm.ttyd_manager, "create_tab", AsyncMock(return_value=fake_tab))
    monkeypatch.setattr(_wm.ttyd_manager, "delete_tab", AsyncMock())
    monkeypatch.setattr(_wm.ttyd_manager, "update_tab", AsyncMock(return_value=fake_tab))
    monkeypatch.setattr(_wm.ttyd_manager, "rename_tab", MagicMock(return_value=fake_tab))
    monkeypatch.setattr(_wm.ttyd_manager, "get_tab", MagicMock(return_value=fake_tab))
    monkeypatch.setattr(_wm.ttyd_manager, "list_tabs", MagicMock(return_value=[]))
    monkeypatch.setattr(_wm.ttyd_manager, "list_tab_agent_statuses", AsyncMock(return_value={}))
    monkeypatch.setattr(
        _wm.ttyd_manager, "ensure_tab_tmux_session", AsyncMock(return_value=fake_tab)
    )
    monkeypatch.setattr(_wm.ttyd_manager, "set_tab_workspace_metadata", MagicMock())

    # Mock the workspace manager's tmux message sender.
    monkeypatch.setattr(_wm.WorkspaceManager, "_send_tmux_message", AsyncMock())

    yield root


@pytest.fixture()
def manager(state_root: Path) -> WorkspaceManager:
    return WorkspaceManager()


@pytest.fixture()
def ws_id(manager: WorkspaceManager, tmp_path: Path) -> str:
    """Create a real workspace and return its id.

    Agent tree actions validate that the workspace exists, so tests must
    operate against a workspace registered with the manager.
    """
    return _make_workspace(manager, tmp_path)


def _make_workspace(manager: WorkspaceManager, tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    from claude_hub.models import ExecutionTarget, WorkspaceCreate

    ws = manager.create_workspace(
        WorkspaceCreate(
            name="Agent Tree WS",
            path=str(repo),
            target=ExecutionTarget.LOCAL,
        )
    )
    return ws.id


# ---------------------------------------------------------------------------
# Root run + spawn
# ---------------------------------------------------------------------------


def test_create_root_run(manager: WorkspaceManager, ws_id: str) -> None:
    run = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.MANAGED_TASK,
        title="root",
        context_ref="sess-1",
    )
    assert run.parent_id is None
    assert run.path == run.id
    assert run.supervisor_id is None
    assert run.status == AgentRunStatus.RUNNING
    assert run.context_ref == "sess-1"


@pytest.mark.asyncio
async def test_spawn_creates_child_run(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            title="child",
            initial_message="do the thing",
            call_id="call-1",
        )
    )
    assert child.parent_id == root.id
    # Path includes the child's own id so subtree prefix-matching never
    # mixes in siblings.
    assert child.path == f"{root.path}/{child.id}"
    assert child.supervisor_id == root.id
    assert child.executor_kind == ExecutorKind.NATIVE_SUBAGENT
    assert child.last_task_message == "do the thing"
    # spawn emits dispatched + started events.
    events = manager.agent_tree.get_events(ws_id, child.id, subtree=False)
    types = [e.type for e in events]
    assert AgentEventType.DISPATCHED in types
    assert AgentEventType.STARTED in types


@pytest.mark.asyncio
async def test_spawn_call_id_idempotent(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    req = SpawnRequest(
        workspace_id=ws_id,
        parent_id=root.id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        initial_message="x",
        call_id="same-call",
    )
    first = await manager.agent_tree.spawn(req)
    # A second spawn with the same call_id must return the SAME run and not
    # re-trigger the executor adapter.
    second = await manager.agent_tree.spawn(req)
    assert second.id == first.id
    # Only one run exists under root.
    runs = manager.agent_tree.list_runs(ListRunsRequest(workspace_id=ws_id, root_id=root.id))
    child_runs = [r for r in runs if r.id != root.id]
    assert len(child_runs) == 1
    # Only one dispatched event for this call_id.
    events = manager.agent_tree.get_events(ws_id, first.id, subtree=False)
    dispatched = [e for e in events if e.type == AgentEventType.DISPATCHED]
    assert len(dispatched) == 1
    assert dispatched[0].call_id == "same-call"


# ---------------------------------------------------------------------------
# send / followup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_appends_message_event(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-1",
        )
    )
    event = await manager.agent_tree.send(
        SendRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="a message",
            call_id="send-1",
        )
    )
    assert event.type == AgentEventType.MESSAGE
    assert event.payload["message"] == "a message"
    assert event.recipient == child.id
    # last_task_message updated.
    assert manager.agent_tree.get_run(child.id).last_task_message == "a message"


@pytest.mark.asyncio
async def test_followup_resumes_turn(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-1",
        )
    )
    event = await manager.agent_tree.followup(
        FollowupRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="continue",
            call_id="followup-1",
        )
    )
    assert event.type == AgentEventType.MESSAGE
    assert event.payload["followup"] is True
    # The native subagent adapter sets status to RUNNING on followup.
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.RUNNING


# ---------------------------------------------------------------------------
# wait (cursor-based blocking)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_returns_existing_events(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-1",
        )
    )
    # Since there are already events (dispatched, started), wait returns them
    # immediately without blocking.
    events = await manager.agent_tree.wait(
        WaitRequest(
            workspace_id=ws_id,
            recipient_id=root.id,
            since_sequence=0,
            subtree=True,
            timeout_seconds=1.0,
        )
    )
    assert len(events) > 0


@pytest.mark.asyncio
async def test_wait_blocks_until_event_arrives(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )

    async def _send_later():
        await asyncio.sleep(0.1)
        await manager.agent_tree.send(
            SendRequest(
                workspace_id=ws_id,
                recipient_id=root.id,
                author_id=root.id,
                message="wake up",
                call_id="wake-1",
            )
        )

    wait_task = asyncio.create_task(
        manager.agent_tree.wait(
            WaitRequest(
                workspace_id=ws_id,
                recipient_id=root.id,
                since_sequence=0,
                subtree=False,
                timeout_seconds=5.0,
            )
        )
    )
    send_task = asyncio.create_task(_send_later())
    events, _ = await asyncio.gather(wait_task, send_task)
    assert any(e.type == AgentEventType.MESSAGE for e in events)


@pytest.mark.asyncio
async def test_wait_times_out_with_empty_list(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    events = await manager.agent_tree.wait(
        WaitRequest(
            workspace_id=ws_id,
            recipient_id=root.id,
            since_sequence=999,
            subtree=False,
            timeout_seconds=0.1,
        )
    )
    assert events == []


# ---------------------------------------------------------------------------
# interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_sets_status(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-1",
        )
    )
    interrupted = await manager.agent_tree.interrupt(
        InterruptRequest(
            workspace_id=ws_id,
            run_id=child.id,
            call_id="int-1",
            reason="stop",
        )
    )
    assert interrupted.status == AgentRunStatus.INTERRUPTED
    events = manager.agent_tree.get_events(ws_id, child.id, subtree=False)
    assert any(e.type == AgentEventType.INTERRUPTED for e in events)


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


def test_list_runs_scoped_to_subtree(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    other = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    runs = manager.agent_tree.list_runs(ListRunsRequest(workspace_id=ws_id, root_id=root.id))
    ids = {r.id for r in runs}
    assert root.id in ids
    assert other.id not in ids


# ---------------------------------------------------------------------------
# Event ingestion from executor (emit_event)
# ---------------------------------------------------------------------------


def test_emit_event_updates_run_status(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    manager.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.COMPLETED,
        author=root.id,
        recipient=None,
        call_id="done-1",
        payload={"message": "finished"},
    )
    run = manager.agent_tree.get_run(root.id)
    assert run.status == AgentRunStatus.COMPLETED
    assert run.last_task_message == "finished"


def test_emit_event_idempotent_on_call_id(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    manager.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.PROGRESS,
        author=root.id,
        recipient=None,
        call_id="dup",
        payload={"message": "first"},
    )
    manager.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.PROGRESS,
        author=root.id,
        recipient=None,
        call_id="dup",
        payload={"message": "second"},
    )
    events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
    progress = [e for e in events if e.call_id == "dup"]
    assert len(progress) == 1
    assert progress[0].payload["message"] == "first"


# ---------------------------------------------------------------------------
# Report -> agent event bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_bridges_to_agent_event(manager: WorkspaceManager, tmp_path: Path) -> None:
    from datetime import datetime

    from claude_hub.models import (
        AgentReportCreate,
        AgentReportState,
        AgentRuntimeStatus,
        AgentType,
        ExecutionTarget,
        ManagedSession,
        ManagedSessionStatus,
        WorkspaceCreate,
        WorkspaceSessionRole,
        WorkspaceTaskCreate,
        WorkspaceTaskMode,
    )

    ws_id = _make_workspace(manager, tmp_path)
    # Create a root run and a managed-task child run WITHOUT going through
    # the full spawn (which would start a real ttyd session). We create the
    # task directly and set the run's context_ref manually.
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.MANAGED_TASK
    )
    task = manager.create_task(
        ws_id,
        WorkspaceTaskCreate(
            title="task",
            prompt="do work",
            agent_type=AgentType.CLAUDE,
            task_mode=WorkspaceTaskMode.REVIEWED,
        ),
    )
    # Create a child run whose context_ref is the task id.
    child = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.MANAGED_TASK,
        title="child",
        context_ref=task.id,
    )
    # Wire parent/supervisor so the bridge addresses the event to root.
    child.parent_id = root.id
    child.supervisor_id = root.id
    child.path = f"{root.path}/{child.id}"

    now = datetime.utcnow()
    session = ManagedSession(
        id="sess-1",
        workspace_id=ws_id,
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        target=ExecutionTarget.LOCAL,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.WORKING,
        task_id=task.id,
        current_task_id=task.id,
        tab_id="tab-1",
        title="worker",
        workspace_path=str(tmp_path / "repo"),
        tmux_session="tmux-1",
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session

    # Submit a report; it should be bridged into the agent event stream.
    report = await manager.create_report(
        session.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            message="done",
            message_en="done",
            message_zh="完成",
        ),
    )
    events = manager.agent_tree.get_events(ws_id, child.id, subtree=False)
    bridged = [e for e in events if e.call_id == f"report:{report.id}"]
    assert len(bridged) == 1
    assert bridged[0].type == AgentEventType.COMPLETED
    assert bridged[0].recipient == root.id


# ---------------------------------------------------------------------------
# Resident + managed-task directed wakeup E2E
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resident_directed_wakeup_via_subtree_event(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """A managed-task child's report event wakes the resident supervisor.

    End-to-end: resident root run -> managed-task child run -> child task
    reports -> event bridged to agent tree addressed to resident ->
    resident's ``wait(subtree=True)`` returns the event AND
    ``_workspace_activity_since`` returns True so the resident fires.
    """
    from datetime import datetime, timedelta

    from claude_hub.models import (
        AgentReportCreate,
        AgentReportState,
        AgentRuntimeStatus,
        AgentType,
        ExecutionTarget,
        ManagedSession,
        ManagedSessionStatus,
        WorkspaceSessionRole,
        WorkspaceTaskCreate,
        WorkspaceTaskMode,
    )

    resident_session_id = "resident-sess-1"
    # Mark the workspace as having a resident session so
    # _workspace_activity_since scans the agent tree.
    ws = manager.workspaces[ws_id]
    manager.workspaces[ws_id] = ws.model_copy(
        update={"resident_agent_session_id": resident_session_id}
    )

    # Resident root run: context_ref is the resident session id.
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.MANAGED_TASK,
        title="Resident",
        context_ref=resident_session_id,
    )

    # Managed-task child run: context_ref is the task id.
    task = manager.create_task(
        ws_id,
        WorkspaceTaskCreate(
            title="child task",
            prompt="do work",
            agent_type=AgentType.CLAUDE,
            task_mode=WorkspaceTaskMode.REVIEWED,
        ),
    )
    child = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.MANAGED_TASK,
        title="child",
        context_ref=task.id,
    )
    child.parent_id = root.id
    child.supervisor_id = root.id
    child.path = f"{root.path}/{child.id}"

    # Worker session that owns the task.
    now = datetime.utcnow()
    session = ManagedSession(
        id="worker-sess-1",
        workspace_id=ws_id,
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        target=ExecutionTarget.LOCAL,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.WORKING,
        task_id=task.id,
        current_task_id=task.id,
        tab_id="tab-1",
        title="worker",
        workspace_path=str(ws.path),
        tmux_session="tmux-1",
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session

    last_run = now - timedelta(hours=1)

    # Before the report: no activity since last_run.
    assert manager._workspace_activity_since(ws_id, last_run) is False

    # Child task reports completion -> bridges to a COMPLETED event
    # addressed to the resident root.
    report = await manager.create_report(
        session.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            message="done",
            message_en="done",
            message_zh="完成",
        ),
    )

    # Directed wakeup #1: _workspace_activity_since now returns True.
    assert manager._workspace_activity_since(ws_id, last_run) is True

    # Directed wakeup #2: the resident's wait(subtree=True) returns the
    # child's event without blocking (it already arrived).
    events = await manager.agent_tree.wait(
        WaitRequest(
            workspace_id=ws_id,
            recipient_id=root.id,
            since_sequence=0,
            subtree=True,
            timeout_seconds=1.0,
        )
    )
    bridged = [e for e in events if e.call_id == f"report:{report.id}"]
    assert len(bridged) == 1
    assert bridged[0].type == AgentEventType.COMPLETED
    assert bridged[0].author == child.id
    assert bridged[0].recipient == root.id


@pytest.mark.asyncio
async def test_resident_wait_blocks_until_child_event(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """The resident's ``wait(subtree=True)`` blocks until a child emits.

    Proves the race-free directed wakeup: the resident calls wait first
    (no events yet), then a child run emits an event, and the wait
    returns that event.
    """
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-1",
        )
    )

    # Advance the cursor past the spawn's dispatched/started events so the
    # wait blocks until the child emits its own event.
    existing = manager.agent_tree.get_events(ws_id, root.id, since_sequence=0, subtree=True)
    since_seq = max(e.sequence for e in existing) if existing else 0

    async def _emit_later():
        await asyncio.sleep(0.1)
        manager.agent_tree.emit_event(
            workspace_id=ws_id,
            agent_run_id=child.id,
            event_type=AgentEventType.PROGRESS,
            author=child.id,
            recipient=root.id,
            call_id="prog-1",
            payload={"message": "working"},
        )

    wait_task = asyncio.create_task(
        manager.agent_tree.wait(
            WaitRequest(
                workspace_id=ws_id,
                recipient_id=root.id,
                since_sequence=since_seq,
                subtree=True,
                timeout_seconds=5.0,
            )
        )
    )
    emit_task = asyncio.create_task(_emit_later())
    events, _ = await asyncio.gather(wait_task, emit_task)
    assert any(e.type == AgentEventType.PROGRESS and e.author == child.id for e in events)


# ---------------------------------------------------------------------------
# Persistence / restart replay
# ---------------------------------------------------------------------------


def test_runs_and_events_survive_save_load(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    manager.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.PROGRESS,
        author=root.id,
        recipient=None,
        call_id="prog-1",
        payload={"message": "working"},
    )

    # Serialize and reload into a fresh manager.
    data = manager.agent_tree.to_dict(ws_id)
    assert len(data["agent_runs"]) == 1
    assert len(data["agent_events"]) == 1

    fresh = WorkspaceManager()
    fresh.agent_tree.load_from_dict(ws_id, data)

    loaded_run = fresh.agent_tree.get_run(root.id)
    assert loaded_run is not None
    assert loaded_run.id == root.id
    assert loaded_run.status == root.status

    loaded_events = fresh.agent_tree.get_events(ws_id, root.id, subtree=False)
    assert len(loaded_events) == 1
    assert loaded_events[0].call_id == "prog-1"
    assert loaded_events[0].payload["message"] == "working"

    # The next sequence number should be after the loaded events.
    new_event = fresh.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.PROGRESS,
        author=root.id,
        recipient=None,
        call_id="prog-2",
        payload={"message": "more"},
    )
    assert new_event.sequence == loaded_events[0].sequence + 1

    # call_id idempotency should still work after reload.
    dup = fresh.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.PROGRESS,
        author=root.id,
        recipient=None,
        call_id="prog-1",
        payload={"message": "duplicate"},
    )
    assert dup.sequence == loaded_events[0].sequence
    assert dup.payload["message"] == "working"


def test_restart_replay_from_disk(manager: WorkspaceManager, tmp_path: Path) -> None:
    """Full process-restart replay: state is written to disk via _save_state,
    then a fresh WorkspaceManager loads it back. Runs, events, sequence
    continuity, and call_id idempotency must all survive."""
    from claude_hub.models import ExecutionTarget, WorkspaceCreate

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    ws = manager.create_workspace(
        WorkspaceCreate(name="Restart WS", path=str(repo), target=ExecutionTarget.LOCAL)
    )
    ws_id = ws.id

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    manager.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.PROGRESS,
        author=root.id,
        recipient=None,
        call_id="prog-1",
        payload={"message": "working"},
    )
    manager.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.COMPLETED,
        author=root.id,
        recipient=None,
        call_id="done-1",
        payload={"message": "finished"},
    )

    # Persist to disk (this is what happens on every mutation).
    manager._save_state()

    # Simulate a process restart: a fresh manager loads from the same state root.
    fresh = WorkspaceManager()

    loaded_run = fresh.agent_tree.get_run(root.id)
    assert loaded_run is not None
    assert loaded_run.id == root.id
    assert loaded_run.status == AgentRunStatus.COMPLETED

    loaded_events = fresh.agent_tree.get_events(ws_id, root.id, subtree=False)
    assert len(loaded_events) == 2
    seqs = sorted(e.sequence for e in loaded_events)
    assert seqs == [seqs[0], seqs[0] + 1]

    # New events after restart continue the sequence.
    new_event = fresh.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.PROGRESS,
        author=root.id,
        recipient=None,
        call_id="prog-2",
        payload={"message": "post-restart"},
    )
    assert new_event.sequence == seqs[-1] + 1

    # call_id idempotency survives the restart.
    dup = fresh.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.PROGRESS,
        author=root.id,
        recipient=None,
        call_id="prog-1",
        payload={"message": "duplicate"},
    )
    assert dup.sequence == seqs[0]
    assert dup.payload["message"] == "working"


# ---------------------------------------------------------------------------
# Idempotency for followup / interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_followup_call_id_idempotent(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-1",
        )
    )
    req = FollowupRequest(
        workspace_id=ws_id,
        recipient_id=child.id,
        author_id=root.id,
        message="continue",
        call_id="followup-1",
    )
    first = await manager.agent_tree.followup(req)
    second = await manager.agent_tree.followup(req)
    # Same event returned; no duplicate message appended.
    assert second.sequence == first.sequence
    events = manager.agent_tree.get_events(ws_id, child.id, subtree=False)
    messages = [e for e in events if e.call_id == "followup-1"]
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_interrupt_call_id_idempotent(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-1",
        )
    )
    req = InterruptRequest(
        workspace_id=ws_id,
        run_id=child.id,
        call_id="int-1",
        reason="stop",
    )
    first = await manager.agent_tree.interrupt(req)
    assert first.status == AgentRunStatus.INTERRUPTED
    # Second interrupt with the same call_id returns the run without
    # re-emitting an interrupted event.
    second = await manager.agent_tree.interrupt(req)
    assert second.id == first.id
    events = manager.agent_tree.get_events(ws_id, child.id, subtree=False)
    interrupted = [e for e in events if e.call_id == "int-1"]
    assert len(interrupted) == 1


# ---------------------------------------------------------------------------
# Sibling path isolation
# ---------------------------------------------------------------------------


def test_sibling_paths_do_not_overlap(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    # Two children must have distinct paths that include their own id, so
    # subtree queries for one never return the other.
    child_a = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child_a.parent_id = root.id
    child_a.supervisor_id = root.id
    child_a.path = f"{root.path}/{child_a.id}"

    child_b = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child_b.parent_id = root.id
    child_b.supervisor_id = root.id
    child_b.path = f"{root.path}/{child_b.id}"

    assert child_a.path != child_b.path
    assert child_a.id in child_a.path
    assert child_b.id in child_b.path
    # Subtree of A must not include B.
    a_subtree = manager.agent_tree.list_runs(
        ListRunsRequest(workspace_id=ws_id, root_id=child_a.id)
    )
    a_ids = {r.id for r in a_subtree}
    assert child_b.id not in a_ids


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_spawns_get_distinct_ids(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )

    async def _spawn(i: int):
        return await manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message=f"task-{i}",
                call_id=f"spawn-{i}",
            )
        )

    runs = await asyncio.gather(*[_spawn(i) for i in range(5)])
    ids = {r.id for r in runs}
    assert len(ids) == 5
    # All children should have the same parent.
    assert all(r.parent_id == root.id for r in runs)


@pytest.mark.asyncio
async def test_concurrent_waits_wake_on_event(manager: WorkspaceManager, ws_id: str) -> None:
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )

    async def _wait():
        return await manager.agent_tree.wait(
            WaitRequest(
                workspace_id=ws_id,
                recipient_id=root.id,
                since_sequence=0,
                subtree=False,
                timeout_seconds=5.0,
            )
        )

    async def _send():
        await asyncio.sleep(0.1)
        await manager.agent_tree.send(
            SendRequest(
                workspace_id=ws_id,
                recipient_id=root.id,
                author_id=root.id,
                message="broadcast",
                call_id="broadcast-1",
            )
        )

    waiters = [asyncio.create_task(_wait()) for _ in range(3)]
    sender = asyncio.create_task(_send())
    results = await asyncio.gather(*waiters, sender)
    for events in results[:3]:
        assert any(e.type == AgentEventType.MESSAGE for e in events)
