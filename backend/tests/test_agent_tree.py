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
    # Same call_id + same payload -> idempotent, returns the existing event.
    manager.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.PROGRESS,
        author=root.id,
        recipient=None,
        call_id="dup",
        payload={"message": "first"},
    )
    events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
    progress = [e for e in events if e.call_id == "dup"]
    assert len(progress) == 1
    assert progress[0].payload["message"] == "first"


def test_emit_event_rejects_call_id_with_different_payload(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """Reusing a call_id with a different payload must raise ValueError."""
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
    with pytest.raises(ValueError):
        manager.agent_tree.emit_event(
            workspace_id=ws_id,
            agent_run_id=root.id,
            event_type=AgentEventType.PROGRESS,
            author=root.id,
            recipient=None,
            call_id="dup",
            payload={"message": "second"},
        )


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
        payload={"message": "working"},
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
        payload={"message": "working"},
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


# ---------------------------------------------------------------------------
# Idempotency namespacing: call_id scoped by workspace + action + target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_id_reused_with_different_action_raises(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """Reusing a call_id for a different action must raise ValueError.

    call_id idempotency is namespaced by (workspace, action, target), not
    just workspace. The same call_id cannot be reused for a different
    action even within the same workspace.
    """
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    # First use of call_id "c1" as a spawn.
    await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="c1",
        )
    )
    # Reusing "c1" for a followup (different action) must fail.
    child = manager.agent_tree.list_runs(ListRunsRequest(workspace_id=ws_id, root_id=root.id))[-1]
    with pytest.raises(ValueError, match="already used"):
        await manager.agent_tree.followup(
            FollowupRequest(
                workspace_id=ws_id,
                recipient_id=child.id,
                author_id=root.id,
                message="again",
                call_id="c1",
            )
        )


@pytest.mark.asyncio
async def test_call_id_reused_with_different_target_raises(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """Reusing a call_id for a different target run must raise ValueError.

    Even for the same action (spawn), reusing the call_id against a
    different parent/target is rejected.
    """
    root_a = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    root_b = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root_a.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="c1",
        )
    )
    with pytest.raises(ValueError, match="already used"):
        await manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root_b.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message="hi",
                call_id="c1",
            )
        )


@pytest.mark.asyncio
async def test_call_id_same_action_and_target_is_idempotent(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """Reusing a call_id for the same action+target returns the existing run."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child1 = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="c1",
        )
    )
    child2 = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="c1",
        )
    )
    assert child1.id == child2.id


# ---------------------------------------------------------------------------
# ACK cursor validation
# ---------------------------------------------------------------------------


def test_ack_sequence_exceeds_max_raises(manager: WorkspaceManager, ws_id: str) -> None:
    """ACKing a sequence beyond the workspace's max sequence must raise."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    # No events yet -> max sequence is 0.
    with pytest.raises(ValueError, match="exceeds workspace max sequence"):
        manager.agent_tree.ack(ws_id, root.id, sequence=1)


def test_ack_advances_cursor(manager: WorkspaceManager, ws_id: str) -> None:
    """A valid ACK advances the run's ack_sequence cursor."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    manager.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.PROGRESS,
        author=root.id,
        recipient=root.id,
        call_id="e1",
        payload={"message": "step"},
    )
    events = manager.agent_tree.get_events(ws_id, root.id, since_sequence=0, subtree=False)
    max_seq = max(e.sequence for e in events)
    run = manager.agent_tree.ack(ws_id, root.id, sequence=max_seq)
    assert run.ack_sequence == max_seq


# ---------------------------------------------------------------------------
# Atomic persistence
# ---------------------------------------------------------------------------


def test_state_written_atomically_via_temp_file(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """State writes go through a temp file + os.replace, never a direct write.

    We verify the atomic-write contract by patching ``os.replace`` to
    confirm it is invoked (i.e. the write path uses the temp-file
    rename pattern rather than truncating the target in place).
    """
    import os

    real_replace = os.replace
    calls: list[tuple] = []

    def _spy_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy_replace)

    manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        title="root",
    )
    # _persist -> _save_state -> _atomic_write_text -> os.replace
    assert len(calls) > 0
    for src, dst in calls:
        # The source is a temp file in the same directory as the target.
        assert os.path.dirname(src) == os.path.dirname(str(dst))


def test_persist_failure_fails_closed(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """If persistence fails, the action raises (fail closed)."""
    import os

    def _boom_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom_replace)

    with pytest.raises(OSError, match="disk full"):
        manager.agent_tree.create_root_run(
            workspace_id=ws_id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            title="root",
        )


# ---------------------------------------------------------------------------
# Resident -> spawn -> report -> directed wake -> ACK E2E
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resident_spawn_report_wake_ack_e2e(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """Full vertical slice: resident spawns a managed-task child, the child
    reports ready_for_review, the resident's wait returns the directed
    event, and the resident ACKs the cursor.

    This exercises:
    - ManagedTaskAdapter.spawn creates a task with agent_run_id = run.id
    - A report on that task bridges to an agent event addressed to the
      resident (the run's supervisor)
    - The resident's wait(subtree=True) returns only directed subtree events
    - The resident's ack advances its persisted cursor
    """
    from datetime import datetime

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
        WorkspaceTaskStatus,
    )

    ws_id = _make_workspace(manager, tmp_path)
    resident_session_id = "resident-sess-e2e"
    ws = manager.workspaces[ws_id]
    manager.workspaces[ws_id] = ws.model_copy(
        update={"resident_agent_session_id": resident_session_id}
    )

    # Resident root run.
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.MANAGED_TASK,
        title="Resident",
        context_ref=resident_session_id,
    )

    # Spawn a managed-task child. The adapter creates a workspace task
    # tagged with agent_run_id = child.id and starts it.
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.MANAGED_TASK,
            title="child",
            initial_message="do the work",
            call_id="spawn-child",
        )
    )
    assert child.context_ref is not None
    task = manager.tasks.get(child.context_ref)
    assert task is not None
    assert task.agent_run_id == child.id
    assert task.status == WorkspaceTaskStatus.WORKING

    # Worker session that owns the task.
    now = datetime.utcnow()
    session = ManagedSession(
        id="worker-sess-e2e",
        workspace_id=ws_id,
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        target=ExecutionTarget.LOCAL,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.WORKING,
        task_id=task.id,
        current_task_id=task.id,
        tab_id="tab-e2e",
        title="worker",
        workspace_path=str(ws.path),
        tmux_session="tmux-e2e",
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session

    # Child reports ready_for_review -> bridges to a PROGRESS event with
    # report_state=ready_for_review, addressed to the resident.
    report = await manager.create_report(
        session.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.READY_FOR_REVIEW,
            message="ready for review",
            message_en="ready for review",
            message_zh="待审核",
        ),
    )

    # The run's status should be WAITING (review gate).
    assert child.status == AgentRunStatus.WAITING

    # Resident's wait(subtree=True) returns the directed event.
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
    assert bridged[0].recipient == root.id
    assert bridged[0].author == child.id
    assert bridged[0].payload.get("report_state") == "ready_for_review"

    # Resident ACKs up to the event's sequence.
    max_seq = max(e.sequence for e in events)
    acked = manager.agent_tree.ack(ws_id, root.id, sequence=max_seq)
    assert acked.ack_sequence == max_seq

    # A second wait from the same cursor returns nothing new.
    after = await manager.agent_tree.wait(
        WaitRequest(
            workspace_id=ws_id,
            recipient_id=root.id,
            since_sequence=max_seq,
            subtree=True,
            timeout_seconds=0.5,
        )
    )
    assert after == []


# ---------------------------------------------------------------------------
# Adapter crash recovery: spawn reuses existing task by agent_run_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_recovers_existing_task_by_agent_run_id(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """If a previous spawn created a task (agent_run_id tagged) but the
    run's context_ref was never persisted (crash), a retry reuses the
    existing task instead of creating a duplicate.
    """
    from claude_hub.models import WorkspaceTaskStatus

    ws_id = _make_workspace(manager, tmp_path)
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.MANAGED_TASK
    )

    # First spawn: creates the run and a task tagged with agent_run_id.
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.MANAGED_TASK,
            title="child",
            initial_message="do work",
            call_id="recover-spawn",
        )
    )
    task_id = child.context_ref
    assert task_id is not None
    task = manager.tasks.get(task_id)
    assert task is not None
    assert task.agent_run_id == child.id

    # Simulate a crash: the task was created and started, but the run's
    # context_ref was not persisted (cleared). The call_id record survives
    # in the event stream.
    child.context_ref = None

    # Retry spawn with the same call_id. The idempotency path returns the
    # existing run, and _recover_context_ref links it back to the task
    # via agent_run_id.
    child2 = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.MANAGED_TASK,
            title="child",
            initial_message="do work",
            call_id="recover-spawn",
        )
    )
    assert child2.id == child.id
    assert child2.context_ref == task_id
    # No duplicate task was created.
    tasks_with_run_id = [
        t for t in manager.tasks.values() if getattr(t, "agent_run_id", None) == child.id
    ]
    assert len(tasks_with_run_id) == 1


# ---------------------------------------------------------------------------
# Crash recovery: recover_pending_runs retries lost adapter spawns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_pending_runs_retries_lost_spawn(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """After a crash, a PENDING run with no context_ref has its adapter spawn
    retried by recover_pending_runs. The managed-task adapter finds the
    existing task (tagged with agent_run_id) and reuses it.
    """
    from claude_hub.models import WorkspaceTaskStatus

    ws_id = _make_workspace(manager, tmp_path)
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.MANAGED_TASK
    )

    # Spawn a child: creates a run + a managed task tagged with agent_run_id.
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.MANAGED_TASK,
            title="child",
            initial_message="do work",
            call_id="recover-pending",
        )
    )
    task_id = child.context_ref
    assert task_id is not None

    # Simulate a crash mid-spawn: the run is persisted as PENDING with no
    # context_ref, but the task already exists on disk (tagged agent_run_id).
    child.context_ref = None
    child.status = AgentRunStatus.PENDING
    manager._save_state()

    # Recovery: retry the spawn for all PENDING runs without context_ref.
    await manager.agent_tree.recover_pending_runs(ws_id)

    # The run should now have its context_ref restored and be RUNNING.
    recovered = manager.agent_tree.get_run(child.id)
    assert recovered is not None
    assert recovered.context_ref == task_id
    assert recovered.status == AgentRunStatus.RUNNING
    # The task was reused, not duplicated.
    tasks_with_run_id = [
        t for t in manager.tasks.values() if getattr(t, "agent_run_id", None) == child.id
    ]
    assert len(tasks_with_run_id) == 1


@pytest.mark.asyncio
async def test_recover_pending_runs_marks_failed_when_adapter_fails(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """If the adapter spawn fails during recovery, the run is marked FAILED."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="recover-fail",
        )
    )

    # Simulate crash: PENDING, no context_ref.
    child.context_ref = None
    child.status = AgentRunStatus.PENDING

    # Make the adapter spawn fail.
    async def _fail_spawn(run, message):
        raise RuntimeError("adapter down")

    monkeypatch.setattr(
        manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT),
        "spawn",
        _fail_spawn,
    )

    await manager.agent_tree.recover_pending_runs(ws_id)

    recovered = manager.agent_tree.get_run(child.id)
    assert recovered is not None
    assert recovered.status == AgentRunStatus.FAILED


# ---------------------------------------------------------------------------
# Quota: MAX_CONCURRENT_CHILDREN enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_exceeds_concurrent_children_limit(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A parent may not have more than MAX_CONCURRENT_CHILDREN active
    (non-terminal) children. The 33rd spawn raises RuntimeError."""
    from claude_hub.services.agent_tree import MAX_CONCURRENT_CHILDREN

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )

    # Spawn up to the limit.
    for i in range(MAX_CONCURRENT_CHILDREN):
        await manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message=f"child {i}",
                call_id=f"spawn-quota-{i}",
            )
        )

    active = manager.agent_tree._active_children(root.id)
    assert len(active) == MAX_CONCURRENT_CHILDREN

    # The next spawn must be rejected.
    with pytest.raises(RuntimeError, match="active children"):
        await manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message="one too many",
                call_id="spawn-quota-over",
            )
        )


@pytest.mark.asyncio
async def test_completed_child_frees_quota_slot(manager: WorkspaceManager, ws_id: str) -> None:
    """Once a child reaches a terminal status, its slot is freed and a new
    child may be spawned."""
    from claude_hub.services.agent_tree import MAX_CONCURRENT_CHILDREN

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )

    # Fill the quota.
    children = []
    for i in range(MAX_CONCURRENT_CHILDREN):
        child = await manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message=f"child {i}",
                call_id=f"spawn-free-{i}",
            )
        )
        children.append(child)

    # Complete the first child (terminal status frees a slot).
    manager.agent_tree._update_run_status(children[0].id, AgentRunStatus.COMPLETED)

    # Now a new spawn should succeed.
    new_child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="replacement",
            call_id="spawn-free-new",
        )
    )
    assert new_child is not None


# ---------------------------------------------------------------------------
# Terminal status guards: no transitions out of terminal; interrupt no-op
# ---------------------------------------------------------------------------


def test_interrupt_noop_on_terminal_run(manager: WorkspaceManager, ws_id: str) -> None:
    """Interrupting a run that is already COMPLETED/FAILED/INTERRUPTED returns
    the run unchanged and does not call the adapter."""
    import asyncio

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = asyncio.run(
        manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message="hi",
                call_id="term-interrupt",
            )
        )
    )

    # Move to COMPLETED (terminal).
    manager.agent_tree._update_run_status(child.id, AgentRunStatus.COMPLETED)

    # Interrupt should be a no-op.
    interrupted = asyncio.run(
        manager.agent_tree.interrupt(
            InterruptRequest(
                workspace_id=ws_id,
                run_id=child.id,
                call_id="interrupt-terminal",
            )
        )
    )
    assert interrupted.status == AgentRunStatus.COMPLETED


def test_no_transition_out_of_terminal_status(manager: WorkspaceManager, ws_id: str) -> None:
    """_update_run_status refuses to move a terminal run to a non-terminal
    status."""
    import asyncio

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = asyncio.run(
        manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message="hi",
                call_id="term-transition",
            )
        )
    )

    manager.agent_tree._update_run_status(child.id, AgentRunStatus.FAILED)
    # Attempting to move FAILED -> RUNNING is refused.
    manager.agent_tree._update_run_status(child.id, AgentRunStatus.RUNNING)
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.FAILED


# ---------------------------------------------------------------------------
# ACK cursor: forward-only, bounded by workspace max sequence
# ---------------------------------------------------------------------------


def test_ack_cannot_go_backwards(manager: WorkspaceManager, ws_id: str) -> None:
    """ACK sequence must not be less than the current ack cursor."""
    import asyncio

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    # Spawn a child to append events and push the workspace max sequence up.
    asyncio.run(
        manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message="hi",
                call_id="ack-back-spawn",
            )
        )
    )
    max_seq = manager.agent_tree._next_seq.get(ws_id, 1) - 1
    assert max_seq >= 1

    # Advance the cursor to max_seq.
    manager.agent_tree.ack(ws_id, root.id, sequence=max_seq)
    # ACKing a lower sequence must raise.
    with pytest.raises(ValueError, match="behind current ack cursor"):
        manager.agent_tree.ack(ws_id, root.id, sequence=max_seq - 1)


def test_ack_cannot_exceed_max_sequence(manager: WorkspaceManager, ws_id: str) -> None:
    """ACK sequence must not exceed the workspace's current max sequence."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    max_seq = manager.agent_tree._next_seq.get(ws_id, 1) - 1
    with pytest.raises(ValueError, match="exceeds workspace max sequence"):
        manager.agent_tree.ack(ws_id, root.id, sequence=max_seq + 100)


# ---------------------------------------------------------------------------
# Legacy continue: followup maps to continue_task for managed tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_followup_maps_to_continue_task(manager: WorkspaceManager, tmp_path: Path) -> None:
    """For a managed-task run in REVIEW status, followup calls continue_task
    to re-dispatch the work back to the original worker."""
    from datetime import datetime

    from claude_hub.models import (
        AgentType,
        ExecutionTarget,
        ManagedSession,
        ManagedSessionStatus,
        WorkspaceSessionRole,
        WorkspaceTaskStatus,
    )

    ws_id = _make_workspace(manager, tmp_path)
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.MANAGED_TASK
    )

    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.MANAGED_TASK,
            title="child",
            initial_message="do work",
            call_id="followup-continue",
        )
    )
    task_id = child.context_ref
    assert task_id is not None

    # Move the task to REVIEW status (review passed, awaiting acceptance).
    task = manager.tasks[task_id]
    now = datetime.utcnow()
    manager.tasks[task_id] = task.model_copy(
        update={
            "status": WorkspaceTaskStatus.REVIEW,
            "human_acceptance_requested_at": now,
            "updated_at": now,
        }
    )

    # followup should call continue_task, moving the task back to WORKING.
    await manager.agent_tree.followup(
        FollowupRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="needs more work",
            call_id="followup-1",
        )
    )

    updated_task = manager.tasks[task_id]
    assert updated_task.status == WorkspaceTaskStatus.WORKING


# ---------------------------------------------------------------------------
# API endpoint smoke tests (FastAPI TestClient)
# ---------------------------------------------------------------------------


def test_agent_tree_api_endpoints(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """Smoke-test the agent tree HTTP endpoints: spawn, wait, ack, interrupt,
    list_runs, get_run_events."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth.dependencies import get_current_user
    from claude_hub.main import app
    from claude_hub.models import User

    # Wire our test manager into the API layer.
    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)

    async def fake_current_user() -> User:
        return User(
            open_id="local",
            name="Local User",
            email="local@localhost",
            avatar_url=None,
        )

    app.dependency_overrides[get_current_user] = fake_current_user
    try:
        client = TestClient(app)

        # Create a root run directly (no API for that yet).
        root = manager.agent_tree.create_root_run(
            workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
        )

        # POST /agent-tree/spawn
        resp = client.post(
            "/api/agent-tree/spawn",
            json={
                "workspace_id": ws_id,
                "parent_id": root.id,
                "executor_kind": "native_subagent",
                "initial_message": "hi",
                "call_id": "api-spawn",
            },
        )
        assert resp.status_code == 200
        child_id = resp.json()["id"]

        # GET /agent-tree/runs
        resp = client.get("/api/agent-tree/runs", params={"workspace_id": ws_id})
        assert resp.status_code == 200
        run_ids = {r["id"] for r in resp.json()}
        assert root.id in run_ids
        assert child_id in run_ids

        # POST /agent-tree/wait
        resp = client.post(
            "/api/agent-tree/wait",
            json={
                "workspace_id": ws_id,
                "recipient_id": root.id,
                "since_sequence": 0,
                "subtree": True,
                "timeout_seconds": 1,
            },
        )
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) > 0

        # POST /agent-tree/ack
        max_seq = max(e["sequence"] for e in events)
        resp = client.post(
            "/api/agent-tree/ack",
            params={"workspace_id": ws_id, "run_id": root.id, "sequence": max_seq},
        )
        assert resp.status_code == 200
        assert resp.json()["ack_sequence"] == max_seq

        # GET /agent-tree/runs/{run_id}/events
        resp = client.get(f"/api/agent-tree/runs/{child_id}/events")
        assert resp.status_code == 200

        # POST /agent-tree/interrupt
        resp = client.post(
            "/api/agent-tree/interrupt",
            json={
                "workspace_id": ws_id,
                "run_id": child_id,
                "call_id": "api-interrupt",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "interrupted"

        # Idempotency: same call_id returns the same result.
        resp2 = client.post(
            "/api/agent-tree/interrupt",
            json={
                "workspace_id": ws_id,
                "run_id": child_id,
                "call_id": "api-interrupt",
            },
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "interrupted"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
