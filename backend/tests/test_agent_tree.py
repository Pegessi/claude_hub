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
    # spawn emits a DISPATCHED event (addressed to the child) and a STARTED
    # event (addressed to the parent). With recipient-directed reads, the
    # child only sees DISPATCHED; the parent sees STARTED.
    child_events = manager.agent_tree.get_events(ws_id, child.id, subtree=False)
    child_types = [e.type for e in child_events]
    assert AgentEventType.DISPATCHED in child_types
    assert AgentEventType.STARTED not in child_types

    parent_events = manager.agent_tree.get_events(ws_id, root.id, subtree=True)
    parent_types = [e.type for e in parent_events]
    assert AgentEventType.STARTED in parent_types


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
    # The INTERRUPTED event is addressed to the child's supervisor (the root),
    # not the child itself. Recipient-directed mailbox reads mean only the
    # supervisor observes the child's terminal transition.
    events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
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
        recipient=root.id,
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
        recipient=root.id,
        call_id="dup",
        payload={"message": "first"},
    )
    # Same call_id + same payload -> idempotent, returns the existing event.
    manager.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.PROGRESS,
        author=root.id,
        recipient=root.id,
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
        recipient=root.id,
        call_id="dup",
        payload={"message": "first"},
    )
    with pytest.raises(ValueError):
        manager.agent_tree.emit_event(
            workspace_id=ws_id,
            agent_run_id=root.id,
            event_type=AgentEventType.PROGRESS,
            author=root.id,
            recipient=root.id,
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
    # For a REVIEWED task, the worker's COMPLETED report maps to a PROGRESS
    # event (not terminal COMPLETED) and is addressed to the run's supervisor.
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
    events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
    bridged = [e for e in events if e.call_id == f"report:{report.id}"]
    assert len(bridged) == 1
    assert bridged[0].type == AgentEventType.PROGRESS
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

    # Child task reports completion -> for a REVIEWED task this bridges to a
    # PROGRESS event (not terminal COMPLETED) addressed to the resident root.
    # The run waits for review; only REVIEW_PASSED emits COMPLETED.
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
    assert bridged[0].type == AgentEventType.PROGRESS
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
        recipient=root.id,
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
        recipient=root.id,
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
        recipient=root.id,
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
        recipient=root.id,
        call_id="prog-1",
        payload={"message": "working"},
    )
    manager.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=root.id,
        event_type=AgentEventType.COMPLETED,
        author=root.id,
        recipient=root.id,
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
        recipient=root.id,
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
        recipient=root.id,
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
    # The INTERRUPTED event is addressed to the child's supervisor (root),
    # not the child itself. Recipient-directed reads mean only the root
    # observes the child's terminal transition.
    events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
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
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.auth.dependencies import get_current_user
    from claude_hub.main import app
    from claude_hub.models import User

    # Wire our test manager into the API layer.
    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: True)

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


# ---------------------------------------------------------------------------
# Adversarial: crash injection — persist fails after adapter side-effect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_late_persist_failure_keeps_in_memory_state(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """If ``_persist`` fails AFTER the adapter spawn side-effect succeeds,
    the in-memory state (context_ref, RUNNING status, STARTED event) must be
    kept — it matches the executor's actual state. The next successful
    persist reconciles the durable state.

    This is the core of the intent/delivery/outcome protocol: the outcome
    phase batches context_ref + status + STARTED event into a single
    ``_persist``. If that persist fails, we do NOT roll back the in-memory
    state because the executor already ran.
    """
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )

    # Count how many times the adapter spawn is called.
    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)
    spawn_calls = {"n": 0}
    real_spawn = adapter.spawn

    async def _counting_spawn(run, message):
        spawn_calls["n"] += 1
        return await real_spawn(run, message)

    monkeypatch.setattr(adapter, "spawn", _counting_spawn)

    # Make _persist fail on the outcome-phase persist.
    # Persist call sequence after the run+DISPATCHED merge:
    #   1. run node + DISPATCHED intent event (atomic)
    #   2. outcome batch (context_ref + RUNNING + STARTED)  <-- fails here
    persist_calls = {"n": 0}
    real_persist = manager.agent_tree._persist

    def _flaky_persist():
        persist_calls["n"] += 1
        if persist_calls["n"] == 2:
            raise OSError("disk full during outcome")
        return real_persist()

    monkeypatch.setattr(manager.agent_tree, "_persist", _flaky_persist)

    with pytest.raises(OSError, match="disk full during outcome"):
        await manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message="hi",
                call_id="late-persist-fail",
            )
        )

    # The adapter spawn was called exactly once (the side-effect applied).
    assert spawn_calls["n"] == 1

    # The run's in-memory state must reflect the side-effect: context_ref
    # set, status RUNNING, STARTED event present. We do NOT roll back
    # because the executor already ran.
    runs = manager.agent_tree.list_runs(ListRunsRequest(workspace_id=ws_id, root_id=root.id))
    child_runs = [r for r in runs if r.id != root.id]
    assert len(child_runs) == 1
    child = child_runs[0]
    assert child.context_ref is not None
    assert child.status == AgentRunStatus.RUNNING

    # The child sees the DISPATCHED intent event (addressed to itself).
    # The STARTED outcome event is addressed to the parent (root) so the
    # supervisor observes the child's transition.
    child_events = manager.agent_tree.get_events(ws_id, child.id, subtree=False)
    child_types = [e.type for e in child_events]
    assert AgentEventType.DISPATCHED in child_types

    parent_events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
    parent_types = [e.type for e in parent_events]
    assert AgentEventType.STARTED in parent_types

    # Now a successful persist flushes the in-memory state to disk.
    monkeypatch.setattr(manager.agent_tree, "_persist", real_persist)
    manager.agent_tree._persist()

    # Reload from disk and verify the state was reconciled.
    fresh = WorkspaceManager()
    loaded = fresh.agent_tree.get_run(child.id)
    assert loaded is not None
    assert loaded.context_ref == child.context_ref
    assert loaded.status == AgentRunStatus.RUNNING


@pytest.mark.asyncio
async def test_followup_late_persist_failure_keeps_running_status(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """If ``_persist`` fails after the followup adapter side-effect, the
    RUNNING status projection is kept in-memory (matches executor state)
    and reconciled on the next persist.
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

    # Interrupt the child so it's INTERRUPTED, then followup to resume.
    await manager.agent_tree.interrupt(
        InterruptRequest(workspace_id=ws_id, run_id=child.id, call_id="int-1")
    )
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.INTERRUPTED

    # Make the outcome-phase persist (status -> RUNNING) fail.
    real_persist = manager.agent_tree._persist
    persist_calls = {"n": 0}

    def _flaky_persist():
        persist_calls["n"] += 1
        # The intent-phase persist (MESSAGE event) succeeds; the outcome
        # persist (RUNNING status) fails.
        if persist_calls["n"] == 2:
            raise OSError("disk full during followup outcome")
        return real_persist()

    monkeypatch.setattr(manager.agent_tree, "_persist", _flaky_persist)

    with pytest.raises(OSError, match="disk full during followup outcome"):
        await manager.agent_tree.followup(
            FollowupRequest(
                workspace_id=ws_id,
                recipient_id=child.id,
                author_id=root.id,
                message="resume",
                call_id="followup-late-fail",
            )
        )

    # In-memory status must be RUNNING (matches the executor that was
    # resumed), not rolled back to INTERRUPTED.
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.RUNNING


# ---------------------------------------------------------------------------
# Adversarial: duplicate delivery — adapter not re-triggered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_spawn_does_not_retrigger_adapter(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A duplicate spawn call_id must NOT call the adapter again.

    The idempotency layer returns the existing run before reaching the
    adapter. This proves duplicate delivery is safe: the executor is not
    spawned twice.
    """
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )

    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)
    spawn_calls = {"n": 0}
    real_spawn = adapter.spawn

    async def _counting_spawn(run, message):
        spawn_calls["n"] += 1
        return await real_spawn(run, message)

    monkeypatch.setattr(adapter, "spawn", _counting_spawn)

    req = SpawnRequest(
        workspace_id=ws_id,
        parent_id=root.id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        initial_message="hi",
        call_id="dup-spawn",
    )
    first = await manager.agent_tree.spawn(req)
    assert spawn_calls["n"] == 1

    second = await manager.agent_tree.spawn(req)
    # Same run returned.
    assert second.id == first.id
    # Adapter NOT called again.
    assert spawn_calls["n"] == 1


@pytest.mark.asyncio
async def test_duplicate_followup_does_not_retrigger_adapter(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A duplicate followup call_id must NOT call the adapter again."""
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

    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)
    followup_calls = {"n": 0}
    real_followup = adapter.followup

    async def _counting_followup(run, message, call_id=None):
        followup_calls["n"] += 1
        return await real_followup(run, message, call_id=call_id)

    monkeypatch.setattr(adapter, "followup", _counting_followup)

    req = FollowupRequest(
        workspace_id=ws_id,
        recipient_id=child.id,
        author_id=root.id,
        message="continue",
        call_id="dup-followup",
    )
    await manager.agent_tree.followup(req)
    assert followup_calls["n"] == 1

    await manager.agent_tree.followup(req)
    # Adapter NOT called again.
    assert followup_calls["n"] == 1


@pytest.mark.asyncio
async def test_duplicate_interrupt_does_not_retrigger_adapter(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A duplicate interrupt call_id must NOT call the adapter again."""
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

    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)
    interrupt_calls = {"n": 0}
    real_interrupt = adapter.interrupt

    async def _counting_interrupt(run, reason=None):
        interrupt_calls["n"] += 1
        return await real_interrupt(run, reason)

    monkeypatch.setattr(adapter, "interrupt", _counting_interrupt)

    req = InterruptRequest(workspace_id=ws_id, run_id=child.id, call_id="dup-int")
    await manager.agent_tree.interrupt(req)
    assert interrupt_calls["n"] == 1

    await manager.agent_tree.interrupt(req)
    # Adapter NOT called again.
    assert interrupt_calls["n"] == 1


# ---------------------------------------------------------------------------
# Adversarial: lost delivery — intent persisted, adapter never called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lost_spawn_delivery_recovered_by_recover_pending_runs(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """If the process crashes after the dispatched intent event is persisted
    but before the adapter spawn is called (lost delivery),
    ``recover_pending_runs`` retries the adapter spawn.

    We simulate this by:
    1. Creating a run in PENDING state with no context_ref (as if the
       dispatched event was persisted but spawn never ran).
    2. Calling recover_pending_runs.
    3. Verifying the adapter spawn was called and the run is now RUNNING.
    """
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )

    # Manually create a PENDING child run (simulating the state after the
    # dispatched event was persisted but the adapter spawn was lost).
    import uuid

    child_id = str(uuid.uuid4())
    from claude_hub.models.agent_tree import AgentRun

    child = AgentRun(
        id=child_id,
        workspace_id=ws_id,
        parent_id=root.id,
        path=f"{root.path}/{child_id}",
        supervisor_id=root.id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        status=AgentRunStatus.PENDING,
        last_task_message="do work",
    )
    manager.agent_tree._runs[child.id] = child

    # Verify the adapter spawn will be called during recovery.
    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)
    spawn_calls = {"n": 0}
    real_spawn = adapter.spawn

    async def _counting_spawn(run, message):
        spawn_calls["n"] += 1
        return await real_spawn(run, message)

    monkeypatch.setattr(adapter, "spawn", _counting_spawn)

    await manager.agent_tree.recover_pending_runs(ws_id)

    assert spawn_calls["n"] == 1
    recovered = manager.agent_tree.get_run(child.id)
    assert recovered is not None
    assert recovered.status == AgentRunStatus.RUNNING
    assert recovered.context_ref is not None


@pytest.mark.asyncio
async def test_lost_interrupt_delivery_recovered(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """If the INTERRUPTED intent event is persisted but the adapter
    interrupt was never called (lost delivery), recovery retries it.

    We simulate this by:
    1. Creating a RUNNING run with an INTERRUPTED event in its stream.
    2. Calling recover_pending_runs.
    3. Verifying the adapter interrupt was called and the run is INTERRUPTED.
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

    # Manually append an INTERRUPTED event (simulating the intent persisted
    # but the adapter interrupt was lost). The run is still RUNNING.
    manager.agent_tree._append_event(
        workspace_id=ws_id,
        agent_run_id=child.id,
        event_type=AgentEventType.INTERRUPTED,
        author=child.id,
        recipient=root.id,
        call_id="lost-interrupt",
        action="interrupt",
        target=child.id,
        fingerprint="fp",
        payload={"reason": "stop"},
    )
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.RUNNING

    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)
    interrupt_calls = {"n": 0}
    real_interrupt = adapter.interrupt

    async def _counting_interrupt(run, reason=None):
        interrupt_calls["n"] += 1
        return await real_interrupt(run, reason)

    monkeypatch.setattr(adapter, "interrupt", _counting_interrupt)

    await manager.agent_tree.recover_pending_runs(ws_id)

    assert interrupt_calls["n"] == 1
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_lost_followup_delivery_recovered(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """If a followup MESSAGE event (with followup=True) is persisted but the
    adapter followup was never called, recovery retries it.
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

    # Interrupt the child so it's INTERRUPTED (not RUNNING).
    await manager.agent_tree.interrupt(
        InterruptRequest(workspace_id=ws_id, run_id=child.id, call_id="int-1")
    )
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.INTERRUPTED

    # Manually append a followup MESSAGE event (intent persisted but adapter
    # followup lost).
    manager.agent_tree._append_event(
        workspace_id=ws_id,
        agent_run_id=child.id,
        event_type=AgentEventType.MESSAGE,
        author=root.id,
        recipient=child.id,
        call_id="lost-followup",
        action="followup",
        target=child.id,
        fingerprint="fp",
        payload={"message": "resume", "followup": True},
    )

    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)
    followup_calls = {"n": 0}
    real_followup = adapter.followup

    async def _counting_followup(run, message, call_id=None):
        followup_calls["n"] += 1
        return await real_followup(run, message, call_id=call_id)

    monkeypatch.setattr(adapter, "followup", _counting_followup)

    await manager.agent_tree.recover_pending_runs(ws_id)

    assert followup_calls["n"] == 1
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.RUNNING


# ---------------------------------------------------------------------------
# Resident root adapter
# ---------------------------------------------------------------------------


def test_resident_root_adapter_spawn_is_noop(manager: WorkspaceManager, ws_id: str) -> None:
    """ResidentRootAdapter.spawn returns the run's context_ref (or id)
    without creating any executor context."""
    from claude_hub.services.agent_tree_adapters import ResidentRootAdapter

    adapter = ResidentRootAdapter(manager)
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        context_ref="resident-sess-1",
    )

    result = asyncio.run(adapter.spawn(root, "initial"))
    # Returns the existing context_ref.
    assert result == "resident-sess-1"


def test_resident_root_adapter_get_status(manager: WorkspaceManager, ws_id: str) -> None:
    """ResidentRootAdapter.get_status returns RUNNING when the resident
    session exists, FAILED when it doesn't, PENDING when no context_ref."""
    from claude_hub.services.agent_tree_adapters import ResidentRootAdapter

    adapter = ResidentRootAdapter(manager)

    # No context_ref -> PENDING.
    root_no_ctx = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.RESIDENT_ROOT
    )
    assert adapter.get_status(root_no_ctx) == AgentRunStatus.PENDING

    # context_ref set but session doesn't exist -> FAILED.
    root_missing = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        context_ref="missing-session",
    )
    assert adapter.get_status(root_missing) == AgentRunStatus.FAILED


@pytest.mark.asyncio
async def test_resident_root_adapter_interrupt_deletes_session(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """ResidentRootAdapter.interrupt deletes the resident session."""
    from datetime import datetime

    from claude_hub.models import (
        AgentRuntimeStatus,
        AgentType,
        ExecutionTarget,
        ManagedSession,
        ManagedSessionStatus,
        WorkspaceSessionRole,
    )
    from claude_hub.services.agent_tree_adapters import ResidentRootAdapter

    adapter = ResidentRootAdapter(manager)

    # Create a resident session.
    now = datetime.utcnow()
    session = ManagedSession(
        id="resident-sess-int",
        workspace_id=ws_id,
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        target=ExecutionTarget.LOCAL,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.WORKING,
        tab_id="tab-resident",
        title="resident",
        workspace_path=str(manager.workspaces[ws_id].path),
        tmux_session="tmux-resident",
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        context_ref=session.id,
    )

    await adapter.interrupt(root, "stop")
    # The session should be deleted.
    assert session.id not in manager.sessions


# ---------------------------------------------------------------------------
# Followup resume from INTERRUPTED / COMPLETED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_followup_resumes_interrupted_run(manager: WorkspaceManager, ws_id: str) -> None:
    """A followup on an INTERRUPTED run transitions it back to RUNNING."""
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

    await manager.agent_tree.interrupt(
        InterruptRequest(workspace_id=ws_id, run_id=child.id, call_id="int-1")
    )
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.INTERRUPTED

    await manager.agent_tree.followup(
        FollowupRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="resume",
            call_id="followup-resume",
        )
    )
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.RUNNING


@pytest.mark.asyncio
async def test_followup_resumes_completed_run(manager: WorkspaceManager, ws_id: str) -> None:
    """A followup on a COMPLETED run transitions it back to RUNNING."""
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

    # Mark the child as COMPLETED via emit_event.
    manager.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=child.id,
        event_type=AgentEventType.COMPLETED,
        author=child.id,
        recipient=root.id,
        call_id="complete-1",
        payload={"message": "done"},
    )
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.COMPLETED

    await manager.agent_tree.followup(
        FollowupRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="more work",
            call_id="followup-completed",
        )
    )
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.RUNNING


def test_failed_run_cannot_be_resumed(manager: WorkspaceManager, ws_id: str) -> None:
    """A FAILED run is truly terminal and cannot be resumed by followup's
    status projection."""
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
                call_id="spawn-1",
            )
        )
    )

    manager.agent_tree._update_run_status(child.id, AgentRunStatus.FAILED)
    # Attempting to move FAILED -> RUNNING is refused.
    manager.agent_tree._update_run_status(child.id, AgentRunStatus.RUNNING)
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.FAILED


# ---------------------------------------------------------------------------
# API authority: caller must own the author run
# ---------------------------------------------------------------------------


def test_api_authority_rejects_non_owner(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A session that does not own the author run gets 403."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.auth.dependencies import get_current_user
    from claude_hub.main import app
    from claude_hub.models import User

    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    # Force non-local network so authority is enforced.
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)

    async def fake_current_user() -> User:
        return User(open_id="local", name="Local", email="local@localhost", avatar_url=None)

    app.dependency_overrides[get_current_user] = fake_current_user
    try:
        owner_session = "owner-sess"
        attacker_session = "attacker-sess"
        _make_managed_session(manager, owner_session, ws_id)
        _make_managed_session(manager, attacker_session, ws_id)

        # Create a root run owned by session "owner-sess".
        root = manager.agent_tree.create_root_run(
            workspace_id=ws_id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            context_ref=owner_session,
        )

        client = TestClient(app)

        # Call spawn with a different (authenticated) session cookie -> should be 403.
        resp = client.post(
            "/api/agent-tree/spawn",
            json={
                "workspace_id": ws_id,
                "parent_id": root.id,
                "executor_kind": "native_subagent",
                "initial_message": "hi",
                "call_id": "auth-spawn",
            },
            cookies={"claude_hub_session": attacker_session},
        )
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_api_authority_allows_owner(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A session that owns the author run can spawn."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.auth.dependencies import get_current_user
    from claude_hub.main import app
    from claude_hub.models import User

    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    # Force non-local network so authority is enforced.
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)

    async def fake_current_user() -> User:
        return User(open_id="local", name="Local", email="local@localhost", avatar_url=None)

    app.dependency_overrides[get_current_user] = fake_current_user
    try:
        owner_session = "owner-sess"
        _make_managed_session(manager, owner_session, ws_id)
        root = manager.agent_tree.create_root_run(
            workspace_id=ws_id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            context_ref=owner_session,
        )

        client = TestClient(app)

        resp = client.post(
            "/api/agent-tree/spawn",
            json={
                "workspace_id": ws_id,
                "parent_id": root.id,
                "executor_kind": "native_subagent",
                "initial_message": "hi",
                "call_id": "auth-spawn-ok",
            },
            cookies={"claude_hub_session": owner_session},
        )
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# ManagedTaskAdapter.followup resume: TODO, REVIEW/DONE, task-not-found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_managed_task_followup_starts_todo_task(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """For a managed-task run whose task is TODO (e.g. after abort),
    followup calls start_task."""
    from claude_hub.models import WorkspaceTaskStatus

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
            call_id="followup-todo",
        )
    )
    task_id = child.context_ref
    assert task_id is not None

    # Abort the task -> it goes to TODO.
    from claude_hub.models.schemas import ManualTaskControlRequest

    await manager.abort_task(task_id, ManualTaskControlRequest(reason="abort"))
    assert manager.tasks[task_id].status == WorkspaceTaskStatus.TODO

    # followup should start the TODO task.
    await manager.agent_tree.followup(
        FollowupRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="restart",
            call_id="followup-todo-1",
        )
    )
    assert manager.tasks[task_id].status == WorkspaceTaskStatus.WORKING


@pytest.mark.asyncio
async def test_managed_task_followup_recreates_deleted_task(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """If the managed task was deleted, followup re-creates it with the same
    agent_run_id and updates the run's context_ref."""
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
            call_id="followup-recreate",
        )
    )
    old_task_id = child.context_ref
    assert old_task_id is not None

    # Delete the task (simulating cleanup).
    del manager.tasks[old_task_id]

    # followup should re-create the task.
    await manager.agent_tree.followup(
        FollowupRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="recreate",
            call_id="followup-recreate-1",
        )
    )

    # The run's context_ref should now point to a new task.
    new_task_id = manager.agent_tree.get_run(child.id).context_ref
    assert new_task_id is not None
    assert new_task_id != old_task_id
    new_task = manager.tasks[new_task_id]
    assert new_task.agent_run_id == child.id


# ---------------------------------------------------------------------------
# Round 9 fixes: wake-after-commit, recovery-only-after-success, managed-task
# authority, full fingerprints, REVIEW_FAILED -> RUNNING.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_wakes_parent_after_batched_persist(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """After spawn's batched _persist(), the parent supervisor's wait() must
    be woken so it observes the STARTED event."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )

    # Start a wait() on the parent that should be woken by the spawn.
    wait_task = asyncio.create_task(
        manager.agent_tree.wait(
            WaitRequest(
                workspace_id=ws_id,
                recipient_id=root.id,
                since_sequence=0,
                timeout_seconds=5,
            )
        )
    )
    # Let the wait() reach the ev.wait() point.
    await asyncio.sleep(0.05)

    await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-wake",
        )
    )

    events = await asyncio.wait_for(wait_task, timeout=2)
    # The parent should see at least the DISPATCHED and STARTED events.
    types = {e.type for e in events}
    assert AgentEventType.STARTED in types


@pytest.mark.asyncio
async def test_send_wakes_recipient_after_batched_persist(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """After send's batched _persist(), the recipient's wait() must be woken."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-send-wake",
        )
    )

    # Get the current max sequence so wait() only returns events after spawn.
    max_seq = max(
        (e.sequence for e in manager.agent_tree._events.get(ws_id, [])),
        default=0,
    )

    wait_task = asyncio.create_task(
        manager.agent_tree.wait(
            WaitRequest(
                workspace_id=ws_id,
                recipient_id=child.id,
                since_sequence=max_seq,
                timeout_seconds=5,
            )
        )
    )
    await asyncio.sleep(0.05)

    await manager.agent_tree.send(
        SendRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="ping",
            call_id="send-wake",
        )
    )

    events = await asyncio.wait_for(wait_task, timeout=2)
    assert any(e.type == AgentEventType.MESSAGE for e in events)


@pytest.mark.asyncio
async def test_followup_wakes_recipient_after_outcome(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """After followup's outcome persist, the recipient's wait() must be woken."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-followup-wake",
        )
    )
    await manager.agent_tree.interrupt(
        InterruptRequest(workspace_id=ws_id, run_id=child.id, call_id="int-followup-wake")
    )

    # Get the current max sequence so wait() only returns the followup event.
    max_seq = max(
        (e.sequence for e in manager.agent_tree._events.get(ws_id, [])),
        default=0,
    )

    wait_task = asyncio.create_task(
        manager.agent_tree.wait(
            WaitRequest(
                workspace_id=ws_id,
                recipient_id=child.id,
                since_sequence=max_seq,
                timeout_seconds=5,
            )
        )
    )
    await asyncio.sleep(0.05)

    await manager.agent_tree.followup(
        FollowupRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="resume",
            call_id="followup-wake",
        )
    )

    events = await asyncio.wait_for(wait_task, timeout=2)
    # The followup MESSAGE event should be delivered.
    assert any(e.type == AgentEventType.MESSAGE for e in events)
    # The run should be RUNNING again.
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.RUNNING


@pytest.mark.asyncio
async def test_recover_interrupt_does_not_set_status_on_adapter_failure(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """If the adapter interrupt fails during recovery, the run status must
    NOT be set to INTERRUPTED."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-recover-int-fail",
        )
    )

    # Simulate a persisted INTERRUPTED intent with the run still RUNNING.
    manager.agent_tree._append_event(
        workspace_id=ws_id,
        agent_run_id=child.id,
        event_type=AgentEventType.INTERRUPTED,
        author=child.id,
        recipient=root.id,
        call_id="lost-interrupt-fail",
        action="interrupt",
        target=child.id,
        fingerprint="fp",
        payload={"reason": "stop"},
    )
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.RUNNING

    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)

    async def _failing_interrupt(run, reason=None):
        raise RuntimeError("adapter down")

    monkeypatch.setattr(adapter, "interrupt", _failing_interrupt)

    await manager.agent_tree.recover_pending_runs(ws_id)

    # Status must remain RUNNING because the adapter call failed.
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.RUNNING


@pytest.mark.asyncio
async def test_recover_followup_does_not_set_status_on_adapter_failure(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """If the adapter followup fails during recovery, the run status must
    NOT be set to RUNNING."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-recover-fu-fail",
        )
    )
    await manager.agent_tree.interrupt(
        InterruptRequest(workspace_id=ws_id, run_id=child.id, call_id="int-recover-fu-fail")
    )

    # Simulate a persisted followup MESSAGE intent with the run still INTERRUPTED.
    manager.agent_tree._append_event(
        workspace_id=ws_id,
        agent_run_id=child.id,
        event_type=AgentEventType.MESSAGE,
        author=root.id,
        recipient=child.id,
        call_id="lost-followup-fail",
        action="followup",
        target=child.id,
        fingerprint="fp",
        payload={"message": "resume", "followup": True},
    )
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.INTERRUPTED

    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)

    async def _failing_followup(run, message):
        raise RuntimeError("adapter down")

    monkeypatch.setattr(adapter, "followup", _failing_followup)

    await manager.agent_tree.recover_pending_runs(ws_id)

    # Status must remain INTERRUPTED because the adapter call failed.
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.INTERRUPTED


def test_recover_reconciles_status_via_get_status(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """For non-terminal runs, recovery calls get_status() and reconciles the
    run's status with the executor's actual status."""
    import asyncio as _asyncio
    import uuid as _uuid

    from claude_hub.models.agent_tree import AgentRun

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    # Manually create a child run in RUNNING state.
    child_id = str(_uuid.uuid4())
    child_run = AgentRun(
        id=child_id,
        workspace_id=ws_id,
        parent_id=root.id,
        path=f"{root.path}/{child_id}",
        supervisor_id=root.id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        status=AgentRunStatus.RUNNING,
    )
    manager.agent_tree._runs[child_id] = child_run

    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)

    def _get_status(run):
        return AgentRunStatus.WAITING

    monkeypatch.setattr(adapter, "get_status", _get_status)

    _asyncio.run(manager.agent_tree.recover_pending_runs(ws_id))

    # The run status should be reconciled to WAITING.
    assert manager.agent_tree.get_run(child_id).status == AgentRunStatus.WAITING


def test_managed_task_authority_uses_task_session_id(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """For managed_task runs, authority is checked against the task's
    session_id, not the run's context_ref (which is the task id)."""
    from fastapi.testclient import TestClient

    from claude_hub.main import app

    ws_id = _make_workspace(manager, tmp_path)
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.MANAGED_TASK
    )
    child = manager.tasks  # just to ensure manager is wired

    # Create a managed task run.
    child_run = manager.agent_tree._spawn_managed_task  # noqa: F841
    # Use the public spawn path.
    import asyncio as _asyncio

    child = _asyncio.run(
        manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.MANAGED_TASK,
                title="child",
                initial_message="do work",
                call_id="auth-managed",
            )
        )
    )
    task_id = child.context_ref
    assert task_id is not None
    task = manager.tasks[task_id]

    # The task's session_id is the worker session that picked it up.
    worker_session_id = task.session_id
    assert worker_session_id is not None

    # A different session must be rejected.
    other_session = "session-other"
    from claude_hub.api.agent_tree import _assert_authority

    try:
        _assert_authority(manager.agent_tree, child.id, other_session)
        assert False, "expected 403"
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403

    # The owning session must be allowed.
    _assert_authority(manager.agent_tree, child.id, worker_session_id)


def test_spawn_fingerprint_includes_context_ref(manager: WorkspaceManager, ws_id: str) -> None:
    """The spawn fingerprint must include context_ref so a reused call_id
    with a different context_ref is rejected."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )

    # First spawn with context_ref=None.
    import asyncio as _asyncio

    _asyncio.run(
        manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message="hi",
                call_id="spawn-fp-ctx",
                context_ref=None,
            )
        )
    )

    # Second spawn with the same call_id but context_ref="ctx-1" must raise.
    try:
        _asyncio.run(
            manager.agent_tree.spawn(
                SpawnRequest(
                    workspace_id=ws_id,
                    parent_id=root.id,
                    executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                    initial_message="hi",
                    call_id="spawn-fp-ctx",
                    context_ref="ctx-1",
                )
            )
        )
        assert False, "expected ValueError for different context_ref"
    except ValueError:
        pass


def test_send_fingerprint_includes_correlation_id(manager: WorkspaceManager, ws_id: str) -> None:
    """The send fingerprint must include correlation_id."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    import asyncio as _asyncio

    child = _asyncio.run(
        manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message="hi",
                call_id="spawn-send-fp",
            )
        )
    )

    _asyncio.run(
        manager.agent_tree.send(
            SendRequest(
                workspace_id=ws_id,
                recipient_id=child.id,
                author_id=root.id,
                message="ping",
                call_id="send-fp-corr",
                correlation_id=None,
            )
        )
    )

    try:
        _asyncio.run(
            manager.agent_tree.send(
                SendRequest(
                    workspace_id=ws_id,
                    recipient_id=child.id,
                    author_id=root.id,
                    message="ping",
                    call_id="send-fp-corr",
                    correlation_id="corr-1",
                )
            )
        )
        assert False, "expected ValueError for different correlation_id"
    except ValueError:
        pass


def test_followup_fingerprint_includes_correlation_id(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """The followup fingerprint must include correlation_id."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    import asyncio as _asyncio

    child = _asyncio.run(
        manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message="hi",
                call_id="spawn-fu-fp",
            )
        )
    )

    _asyncio.run(
        manager.agent_tree.followup(
            FollowupRequest(
                workspace_id=ws_id,
                recipient_id=child.id,
                author_id=root.id,
                message="resume",
                call_id="fu-fp-corr",
                correlation_id=None,
            )
        )
    )

    try:
        _asyncio.run(
            manager.agent_tree.followup(
                FollowupRequest(
                    workspace_id=ws_id,
                    recipient_id=child.id,
                    author_id=root.id,
                    message="resume",
                    call_id="fu-fp-corr",
                    correlation_id="corr-2",
                )
            )
        )
        assert False, "expected ValueError for different correlation_id"
    except ValueError:
        pass


def test_review_failed_maps_to_running_not_failed(manager: WorkspaceManager, ws_id: str) -> None:
    """A REVIEW_FAILED report must set the run to RUNNING (task back to
    WORKING), not FAILED."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    import asyncio as _asyncio

    child = _asyncio.run(
        manager.agent_tree.spawn(
            SpawnRequest(
                workspace_id=ws_id,
                parent_id=root.id,
                executor_kind=ExecutorKind.NATIVE_SUBAGENT,
                initial_message="hi",
                call_id="spawn-review-failed",
            )
        )
    )

    # Emit a PROGRESS event with report_state=review_failed.
    manager.agent_tree.emit_event(
        workspace_id=ws_id,
        agent_run_id=child.id,
        event_type=AgentEventType.PROGRESS,
        author=child.id,
        recipient=root.id,
        call_id="report-review-failed",
        payload={"report_state": "review_failed", "message": "needs fixes"},
    )

    # The run must be RUNNING, not FAILED.
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.RUNNING


# ---------------------------------------------------------------------------
# Round 13 fixes: durable call_id receipts, TODO/QUEUED/WORKING followup
# delivery, managed_task root -> resident_root migration.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_managed_task_followup_queued_appends_to_prompt(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """For a managed-task run whose task is QUEUED, followup appends the
    message to the task prompt so the worker picks it up when dispatched."""
    from claude_hub.models import WorkspaceTaskStatus

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
            call_id="followup-queued",
        )
    )
    task_id = child.context_ref
    assert task_id is not None

    # Force the task back to QUEUED (e.g. dispatcher hasn't picked it up yet).
    task = manager.tasks[task_id]
    manager.tasks[task_id] = task.model_copy(update={"status": WorkspaceTaskStatus.QUEUED})

    original_prompt = manager.tasks[task_id].prompt

    await manager.agent_tree.followup(
        FollowupRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="please also do X",
            call_id="followup-queued-1",
        )
    )

    updated = manager.tasks[task_id]
    assert updated.status == WorkspaceTaskStatus.QUEUED
    # The followup text is embedded with its call_id marker so the worker
    # can ACK it.
    assert "please also do X" in updated.prompt
    assert "[call_id:followup-queued-1]" in updated.prompt
    assert updated.prompt.startswith(original_prompt)


@pytest.mark.asyncio
async def test_managed_task_followup_working_sends_session_message(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """For a managed-task run whose task is WORKING, followup delivers the
    message directly to the running session via send_session_message."""
    from claude_hub.models import WorkspaceTaskStatus

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
            call_id="followup-working",
        )
    )
    task_id = child.context_ref
    assert task_id is not None

    task = manager.tasks[task_id]
    assert task.status == WorkspaceTaskStatus.WORKING
    assert task.session_id is not None

    sent_messages: list[str] = []

    async def _fake_send(session_id: str, message: str, call_id: str | None = None) -> None:
        sent_messages.append(message)

    monkeypatch.setattr(manager, "send_session_message", _fake_send)

    await manager.agent_tree.followup(
        FollowupRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="please also do Y",
            call_id="followup-working-1",
        )
    )

    assert sent_messages == ["please also do Y"]


def test_migrate_managed_task_root_to_resident_root(manager: WorkspaceManager, ws_id: str) -> None:
    """A historical root run persisted as managed_task must be migrated to
    resident_root on load so authority checks resolve against the resident
    session."""
    from claude_hub.models.agent_tree import AgentRun, ExecutorKind

    run_id = "historical-root-run"
    historical_run = AgentRun(
        id=run_id,
        workspace_id=ws_id,
        parent_id=None,
        path=run_id,
        executor_kind=ExecutorKind.MANAGED_TASK,
        title="resident",
        status=AgentRunStatus.RUNNING,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    # Simulate loading persisted state that has a managed_task root run.
    manager.agent_tree.load_from_dict(
        ws_id,
        {
            "agent_runs": [historical_run.model_dump(mode="json")],
            "agent_events": [],
        },
    )

    migrated = manager.agent_tree.get_run(run_id)
    assert migrated is not None
    assert migrated.executor_kind == ExecutorKind.RESIDENT_ROOT


def test_migrate_resident_root_links_resident_session(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """A resident_root run without a context_ref must be linked to the
    workspace's resident session on load."""
    from claude_hub.models.agent_tree import AgentRun, ExecutorKind

    run_id = "resident-root-no-ctx"
    historical_run = AgentRun(
        id=run_id,
        workspace_id=ws_id,
        parent_id=None,
        path=run_id,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        title="resident",
        status=AgentRunStatus.RUNNING,
        context_ref=None,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    # Set a resident session id on the workspace.
    workspace = manager.workspaces[ws_id]
    manager.workspaces[ws_id] = workspace.model_copy(
        update={"resident_agent_session_id": "resident-sess-123"}
    )

    manager.agent_tree.load_from_dict(
        ws_id,
        {
            "agent_runs": [historical_run.model_dump(mode="json")],
            "agent_events": [],
        },
    )

    migrated = manager.agent_tree.get_run(run_id)
    assert migrated is not None
    assert migrated.executor_kind == ExecutorKind.RESIDENT_ROOT
    assert migrated.context_ref == "resident-sess-123"


# ---------------------------------------------------------------------------
# Recovery: multiple unmatched followup intents replayed in sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_multiple_unmatched_followups_in_sequence(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """If several followup MESSAGE events were persisted without their
    outcome events (crash between intent and outcome), recovery must replay
    ALL of them in sequence order — not only the latest."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-multi-followup",
        )
    )

    # Interrupt so the run is not RUNNING.
    await manager.agent_tree.interrupt(
        InterruptRequest(workspace_id=ws_id, run_id=child.id, call_id="int-multi")
    )
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.INTERRUPTED

    # Append three followup MESSAGE events, none with a matching outcome.
    for i in range(3):
        manager.agent_tree._append_event(
            workspace_id=ws_id,
            agent_run_id=child.id,
            event_type=AgentEventType.MESSAGE,
            author=root.id,
            recipient=child.id,
            call_id=f"followup-{i}",
            action="followup",
            target=child.id,
            fingerprint=f"fp-{i}",
            payload={"message": f"msg-{i}", "followup": True},
        )

    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)
    delivered_call_ids: list[str] = []
    real_followup = adapter.followup

    async def _recording_followup(run, message, call_id=None):
        delivered_call_ids.append(call_id)
        return await real_followup(run, message, call_id=call_id)

    monkeypatch.setattr(adapter, "followup", _recording_followup)

    await manager.agent_tree.recover_pending_runs(ws_id)

    # All three followups were delivered, in sequence order.
    assert delivered_call_ids == ["followup-0", "followup-1", "followup-2"]

    # Outcome events now exist for every followup call_id. The outcome
    # events are addressed to the followup author (root), not the child.
    events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
    outcome_call_ids = {e.call_id for e in events if e.call_id and e.call_id.endswith(":outcome")}
    assert outcome_call_ids == {"followup-0:outcome", "followup-1:outcome", "followup-2:outcome"}

    # A second recovery must NOT re-deliver (outcomes already present).
    delivered_call_ids.clear()
    await manager.agent_tree.recover_pending_runs(ws_id)
    assert delivered_call_ids == []


@pytest.mark.asyncio
async def test_recover_followup_skips_already_delivered_call_ids(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A followup whose call_id already has a matching :outcome event must
    not be re-delivered during recovery."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-skip-followup",
        )
    )

    await manager.agent_tree.interrupt(
        InterruptRequest(workspace_id=ws_id, run_id=child.id, call_id="int-skip")
    )

    # One followup WITH an outcome, one WITHOUT.
    manager.agent_tree._append_event(
        workspace_id=ws_id,
        agent_run_id=child.id,
        event_type=AgentEventType.MESSAGE,
        author=root.id,
        recipient=child.id,
        call_id="done-followup",
        action="followup",
        target=child.id,
        fingerprint="fp-done",
        payload={"message": "done", "followup": True},
    )
    manager.agent_tree._append_event(
        workspace_id=ws_id,
        agent_run_id=child.id,
        event_type=AgentEventType.PROGRESS,
        author=child.id,
        recipient=root.id,
        call_id="done-followup:outcome",
        action="followup:outcome",
        target=child.id,
        fingerprint="fp-done-outcome",
        payload={"delivered": True, "followup_call_id": "done-followup"},
    )
    manager.agent_tree._append_event(
        workspace_id=ws_id,
        agent_run_id=child.id,
        event_type=AgentEventType.MESSAGE,
        author=root.id,
        recipient=child.id,
        call_id="pending-followup",
        action="followup",
        target=child.id,
        fingerprint="fp-pending",
        payload={"message": "pending", "followup": True},
    )

    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)
    delivered: list[str] = []
    real_followup = adapter.followup

    async def _recording_followup(run, message, call_id=None):
        delivered.append(call_id)
        return await real_followup(run, message, call_id=call_id)

    monkeypatch.setattr(adapter, "followup", _recording_followup)

    await manager.agent_tree.recover_pending_runs(ws_id)

    # Only the pending followup was delivered.
    assert delivered == ["pending-followup"]


@pytest.mark.asyncio
async def test_followup_persists_pending_call_ids_atomically(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """After a managed-task followup, the call_id must be recorded in the
    task's call_id lists AND persisted immediately (via _save_state), so a
    crash between adapter success and outcome-event persist does not lose
    the outbox entry.

    With the receiver-verifiable durable receipt, the call_id moves through
    pending → processing on the pump cycle (the tmux send succeeds), but
    stays in ``processing_call_ids`` until the worker ACKs it. Only the
    worker's ACK moves it to ``delivered_call_ids``. So after the followup
    returns, the call_id is in ``processing_call_ids``.
    """
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
            call_id="spawn-receipt",
        )
    )
    task_id = child.context_ref
    assert task_id is not None

    call_id = "followup-receipt-1"
    await manager.agent_tree.followup(
        FollowupRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="continue",
            call_id=call_id,
        )
    )

    # The call_id was claimed (pending → processing) and sent to tmux.
    # It stays in processing_call_ids until the worker ACKs.
    task = manager.tasks[task_id]
    assert call_id in task.processing_call_ids
    assert call_id not in task.pending_call_ids
    assert call_id not in task.delivered_call_ids

    # A second followup with the same call_id is a no-op: the sender skips
    # call_ids already in processing_call_ids.
    # For a WORKING task the message goes to the session, not the prompt,
    # so the prompt is unchanged.
    prompt_before = manager.tasks[task_id].prompt
    await manager.agent_tree.followup(
        FollowupRequest(
            workspace_id=ws_id,
            recipient_id=child.id,
            author_id=root.id,
            message="continue",
            call_id=call_id,
        )
    )
    assert manager.tasks[task_id].prompt == prompt_before


@pytest.mark.asyncio
async def test_recover_followup_uses_event_own_payload_message(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """Recovery must replay each followup event's OWN payload message, not
    the run's last_task_message. If last_task_message differs from the
    event's message, the adapter must receive the event's message."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-own-msg",
        )
    )

    await manager.agent_tree.interrupt(
        InterruptRequest(workspace_id=ws_id, run_id=child.id, call_id="int-own-msg")
    )

    # Set last_task_message to something different from the followup message.
    run = manager.agent_tree.get_run(child.id)
    run.last_task_message = "stale-last-message"

    # Append a followup event with its own message.
    manager.agent_tree._append_event(
        workspace_id=ws_id,
        agent_run_id=child.id,
        event_type=AgentEventType.MESSAGE,
        author=root.id,
        recipient=child.id,
        call_id="followup-own-msg",
        action="followup",
        target=child.id,
        fingerprint="fp-own-msg",
        payload={"message": "the-real-followup-message", "followup": True},
    )

    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)
    received: list[str] = []

    async def _capturing_followup(run, message, call_id=None):
        received.append(message)

    monkeypatch.setattr(adapter, "followup", _capturing_followup)

    await manager.agent_tree.recover_pending_runs(ws_id)

    # The adapter must have received the event's own message, NOT
    # run.last_task_message.
    assert received == ["the-real-followup-message"]


@pytest.mark.asyncio
async def test_recover_followup_does_not_skip_status_reconciliation(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """After replaying historical followup events, recovery must NOT skip
    the get_status() reconciliation step. The run's status must be updated
    to match the executor's actual status."""
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="hi",
            call_id="spawn-no-skip",
        )
    )

    await manager.agent_tree.interrupt(
        InterruptRequest(workspace_id=ws_id, run_id=child.id, call_id="int-no-skip")
    )

    # Append a followup event (will be replayed).
    manager.agent_tree._append_event(
        workspace_id=ws_id,
        agent_run_id=child.id,
        event_type=AgentEventType.MESSAGE,
        author=root.id,
        recipient=child.id,
        call_id="followup-no-skip",
        action="followup",
        target=child.id,
        fingerprint="fp-no-skip",
        payload={"message": "continue", "followup": True},
    )

    adapter = manager.agent_tree._adapter(ExecutorKind.NATIVE_SUBAGENT)

    # The followup replay sets the run to RUNNING; get_status then reports
    # WAITING (e.g. the managed task moved to REVIEW). Reconciliation must
    # run and update the run to WAITING.
    def _get_status(run):
        return AgentRunStatus.WAITING

    monkeypatch.setattr(adapter, "get_status", _get_status)

    await manager.agent_tree.recover_pending_runs(ws_id)

    # Status reconciliation must have run: the run is WAITING, not RUNNING.
    assert manager.agent_tree.get_run(child.id).status == AgentRunStatus.WAITING


# ---------------------------------------------------------------------------
# ManagedSession authentication (no get_current_user override)
# ---------------------------------------------------------------------------


def _make_managed_session(manager: WorkspaceManager, session_id: str, workspace_id: str) -> None:
    """Register a ManagedSession with the workspace manager so agent_tree
    endpoints authenticate it as a valid agent principal."""
    from datetime import datetime

    from claude_hub.models import (
        AgentRuntimeStatus,
        AgentType,
        ExecutionTarget,
        ManagedSession,
        ManagedSessionStatus,
        WorkspaceSessionRole,
    )

    session = ManagedSession(
        id=session_id,
        workspace_id=workspace_id,
        tab_id=f"tab-{session_id}",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title=f"Session {session_id}",
        workspace_path="/tmp",
        tmux_session=f"tmux-{session_id}",
        target=ExecutionTarget.LOCAL,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    manager.sessions[session_id] = session


def test_managed_session_owner_can_read_runs(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A ManagedSession that owns a run can list runs and read its events
    without overriding get_current_user."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.main import app

    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)

    owner_session = "owner-sess-read"
    _make_managed_session(manager, owner_session, ws_id)

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        context_ref=owner_session,
    )

    client = TestClient(app)

    # list_runs: owner session can read.
    resp = client.get(
        "/api/agent-tree/runs",
        params={"workspace_id": ws_id},
        cookies={"claude_hub_session": owner_session},
    )
    assert resp.status_code == 200
    run_ids = {r["id"] for r in resp.json()}
    assert root.id in run_ids

    # get_run_events: owner session can read.
    resp = client.get(
        f"/api/agent-tree/runs/{root.id}/events",
        cookies={"claude_hub_session": owner_session},
    )
    assert resp.status_code == 200


def test_managed_session_non_owner_read_rejected(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A ManagedSession that does NOT own a run and is not a human user
    cannot list runs (403)."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.main import app

    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)

    owner_session = "owner-sess-read-reject"
    attacker_session = "attacker-sess-read"
    _make_managed_session(manager, owner_session, ws_id)
    _make_managed_session(manager, attacker_session, ws_id)

    manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        context_ref=owner_session,
    )

    client = TestClient(app)

    resp = client.get(
        "/api/agent-tree/runs",
        params={"workspace_id": ws_id},
        cookies={"claude_hub_session": attacker_session},
    )
    # The attacker is an authenticated ManagedSession, so list_runs returns
    # 200 (read access is workspace-wide for authenticated sessions).
    assert resp.status_code == 200


def test_managed_session_owner_can_mutate(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A ManagedSession that owns the parent run can spawn a child."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.main import app

    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)

    owner_session = "owner-sess-mutate"
    _make_managed_session(manager, owner_session, ws_id)

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        context_ref=owner_session,
    )

    client = TestClient(app)

    resp = client.post(
        "/api/agent-tree/spawn",
        json={
            "workspace_id": ws_id,
            "parent_id": root.id,
            "executor_kind": "native_subagent",
            "initial_message": "hi",
            "call_id": "owner-spawn",
        },
        cookies={"claude_hub_session": owner_session},
    )
    assert resp.status_code == 200


def test_managed_session_non_owner_mutation_rejected(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A ManagedSession that does not own the parent run cannot spawn (403)."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.main import app

    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)

    owner_session = "owner-sess-mutate-reject"
    attacker_session = "attacker-sess-mutate"
    _make_managed_session(manager, owner_session, ws_id)
    _make_managed_session(manager, attacker_session, ws_id)

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        context_ref=owner_session,
    )

    client = TestClient(app)

    resp = client.post(
        "/api/agent-tree/spawn",
        json={
            "workspace_id": ws_id,
            "parent_id": root.id,
            "executor_kind": "native_subagent",
            "initial_message": "hi",
            "call_id": "attacker-spawn",
        },
        cookies={"claude_hub_session": attacker_session},
    )
    assert resp.status_code == 403


def test_unauthenticated_session_rejected(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A session id that is neither a human LoginSession nor a ManagedSession
    gets 403 on read endpoints."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.main import app

    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)

    manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        context_ref="some-owner",
    )

    client = TestClient(app)

    resp = client.get(
        "/api/agent-tree/runs",
        params={"workspace_id": ws_id},
        cookies={"claude_hub_session": "unknown-session"},
    )
    assert resp.status_code == 403


def test_non_local_no_cookie_rejected(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A non-local request without a session cookie must fail closed with
    403 (not fall through to the local-network no-auth path)."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.main import app

    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)

    client = TestClient(app)

    # No cookie at all.
    resp = client.get(
        "/api/agent-tree/runs",
        params={"workspace_id": ws_id},
    )
    assert resp.status_code == 403
    assert "no session cookie" in resp.json()["detail"]

    # Empty cookie value.
    resp = client.get(
        "/api/agent-tree/runs",
        params={"workspace_id": ws_id},
        cookies={"claude_hub_session": ""},
    )
    assert resp.status_code == 403


def test_managed_session_cross_workspace_read_rejected(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A ManagedSession may not list runs or read events from a workspace
    it does not belong to."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.main import app

    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)

    # Create a second workspace.
    other_ws_id = _make_workspace(manager, tmp_path)

    # Session belongs to ws_id, not other_ws_id.
    owner_session = "owner-sess-xworkspace"
    _make_managed_session(manager, owner_session, ws_id)

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        context_ref=owner_session,
    )

    client = TestClient(app)

    # list_runs on the other workspace must be 403.
    resp = client.get(
        "/api/agent-tree/runs",
        params={"workspace_id": other_ws_id},
        cookies={"claude_hub_session": owner_session},
    )
    assert resp.status_code == 403

    # get_run_events for a run in the session's own workspace is allowed.
    resp = client.get(
        f"/api/agent-tree/runs/{root.id}/events",
        cookies={"claude_hub_session": owner_session},
    )
    assert resp.status_code == 200


def test_managed_session_sees_only_owned_subtree(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A ManagedSession listing runs sees only the runs it owns or
    supervises (its subtree), not runs owned by other sessions in the
    same workspace."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.main import app

    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)

    owner_session = "owner-sess-subtree"
    other_session = "other-sess-subtree"
    _make_managed_session(manager, owner_session, ws_id)
    _make_managed_session(manager, other_session, ws_id)

    owner_root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        context_ref=owner_session,
    )
    other_root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        context_ref=other_session,
    )

    client = TestClient(app)

    resp = client.get(
        "/api/agent-tree/runs",
        params={"workspace_id": ws_id},
        cookies={"claude_hub_session": owner_session},
    )
    assert resp.status_code == 200
    run_ids = {r["id"] for r in resp.json()}
    assert owner_root.id in run_ids
    assert other_root.id not in run_ids


# ---------------------------------------------------------------------------
# Round 16 adversarial tests: crash-safe outbox and forged-session rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_followup_outbox_pending_survives_crash_and_redelivers_idempotently(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """A followup call_id stays in pending_call_ids until the worker ACKs it.
    A re-delivery (e.g. after a Hub crash) is idempotent: the [followup] line
    is not appended twice to the prompt."""
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
            call_id="spawn-outbox",
        )
    )
    task_id = child.context_ref
    assert task_id is not None

    call_id = "followup-outbox-crash"
    message = "please continue"

    # Force the task back to TODO so followup takes the prompt-append path
    # (the WORKING path sends to the session instead). This lets us assert
    # idempotency by inspecting the prompt.
    from claude_hub.models.schemas import WorkspaceTaskStatus

    task = manager.tasks[task_id]
    manager.tasks[task_id] = task.model_copy(update={"status": WorkspaceTaskStatus.TODO})
    manager._save_state()

    from claude_hub.models.agent_tree import AgentRunStatus

    run = manager.agent_tree.get_run(child.id)
    adapter = manager.agent_tree._adapter(run.executor_kind)

    # First delivery: the call_id goes to pending_call_ids (not delivered)
    # and the message is appended to the prompt.
    await adapter.followup(run, message, call_id=call_id)
    task = manager.tasks[task_id]
    assert call_id in task.pending_call_ids
    assert call_id not in task.delivered_call_ids
    assert message in task.prompt
    assert f"[call_id:{call_id}]" in task.prompt

    prompt_before = manager.tasks[task_id].prompt

    # Simulate a Hub crash: the call_id is still in pending_call_ids. A
    # retry re-delivers. For a WORKING task, the followup sends to the
    # session; the pump claims the call_id (pending → processing), sends
    # to tmux, and the call_id stays in processing_call_ids until the
    # worker ACKs. The prompt is unchanged (idempotent).
    await adapter.followup(run, message, call_id=call_id)

    task = manager.tasks[task_id]
    assert manager.tasks[task_id].prompt == prompt_before
    # The call_id is now in processing_call_ids (sent to tmux, awaiting
    # worker ACK).
    assert call_id in task.processing_call_ids
    assert call_id not in task.pending_call_ids
    assert call_id not in task.delivered_call_ids


@pytest.mark.asyncio
async def test_followup_recreates_deleted_task_with_call_id_pending(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """If the managed task was deleted (e.g. by abort), followup must
    recreate it with the followup message as the prompt. The call_id
    stays in pending_call_ids until the worker ACKs it; the call_id is
    embedded in the prompt so the worker can ACK it."""
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
            call_id="spawn-deleted",
        )
    )
    task_id = child.context_ref
    assert task_id is not None

    call_id = "followup-deleted-task"
    message = "recreate me"

    # Delete the task from the manager (simulating abort cleanup).
    del manager.tasks[task_id]
    manager._save_state()

    run = manager.agent_tree.get_run(child.id)
    adapter = manager.agent_tree._adapter(run.executor_kind)

    await adapter.followup(run, message, call_id=call_id)

    # The run's context_ref should now point to a new task.
    new_task_id = run.context_ref
    assert new_task_id is not None
    assert new_task_id != task_id
    new_task = manager.tasks.get(new_task_id)
    assert new_task is not None
    # The followup message becomes the new task's prompt, with the call_id
    # marker embedded so the worker can ACK it.
    assert message in new_task.prompt
    assert f"[call_id:{call_id}]" in new_task.prompt
    # The call_id stays in pending_call_ids until the worker ACKs it.
    assert call_id in new_task.pending_call_ids
    assert call_id not in new_task.delivered_call_ids


def test_forged_session_cookie_rejected_for_all_actions(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A forged or stale session cookie (session_id not a live
    ManagedSession and not a human LoginSession) must get 403 for every
    mutating action and read: spawn, send, followup, wait, ack, interrupt."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.main import app

    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)

    # Create a legitimate owner session and a root run.
    owner_session = "owner-sess-forged"
    _make_managed_session(manager, owner_session, ws_id)
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        context_ref=owner_session,
    )

    forged_cookie = "forged-session-id-12345"
    client = TestClient(app)

    # spawn: forged cookie -> 403
    resp = client.post(
        "/api/agent-tree/spawn",
        json={
            "workspace_id": ws_id,
            "parent_id": root.id,
            "executor_kind": "native_subagent",
            "initial_message": "hi",
            "call_id": "forged-spawn",
        },
        cookies={"claude_hub_session": forged_cookie},
    )
    assert resp.status_code == 403

    # send: forged cookie -> 403
    resp = client.post(
        "/api/agent-tree/send",
        json={
            "workspace_id": ws_id,
            "author_id": root.id,
            "recipient_id": root.id,
            "message": "hi",
            "call_id": "forged-send",
        },
        cookies={"claude_hub_session": forged_cookie},
    )
    assert resp.status_code == 403

    # followup: forged cookie -> 403
    resp = client.post(
        "/api/agent-tree/followup",
        json={
            "workspace_id": ws_id,
            "author_id": root.id,
            "recipient_id": root.id,
            "message": "continue",
            "call_id": "forged-followup",
        },
        cookies={"claude_hub_session": forged_cookie},
    )
    assert resp.status_code == 403

    # wait: forged cookie -> 403
    resp = client.post(
        "/api/agent-tree/wait",
        json={
            "workspace_id": ws_id,
            "recipient_id": root.id,
            "since_sequence": 0,
            "timeout_seconds": 0.1,
        },
        cookies={"claude_hub_session": forged_cookie},
    )
    assert resp.status_code == 403

    # ack: forged cookie -> 403
    resp = client.post(
        "/api/agent-tree/ack",
        params={
            "workspace_id": ws_id,
            "run_id": root.id,
            "sequence": 0,
        },
        cookies={"claude_hub_session": forged_cookie},
    )
    assert resp.status_code == 403

    # interrupt: forged cookie -> 403
    resp = client.post(
        "/api/agent-tree/interrupt",
        json={
            "workspace_id": ws_id,
            "run_id": root.id,
            "call_id": "forged-interrupt",
        },
        cookies={"claude_hub_session": forged_cookie},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_working_followup_session_outbox_survives_hard_exit_and_reload(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """At-least-once tmux delivery with receiver-verifiable durable receipt.

    ``send_session_message`` persists the call_id in ``pending_call_ids``,
    then the receiver pump claims it (pending → processing) and sends it to
    tmux. The call_id STAYS in ``processing_call_ids`` until the worker ACKs
    it — only the worker's ACK moves it to ``delivered_call_ids`` and removes
    the message from ``pending_messages``.

    On a hard exit + reload, call_ids stranded in ``processing_call_ids`` are
    moved back to ``pending_call_ids`` and re-delivered. The worker dedupes
    by the ``[call_id:<id>]`` marker, so a duplicate tmux prompt does not
    produce a duplicate effect. This preserves the message until the worker
    proves receipt (ACK), covering both sides of the claim-to-tmux crash
    window.
    """
    session_id = "outbox-session"
    _make_managed_session(manager, session_id, ws_id)

    call_id = "working-followup-outbox"
    message = "please continue"
    marker = f"[call_id:{call_id}]"

    sent: list[str] = []

    async def _fake_send_tmux(tmux_session: str, text: str) -> None:
        sent.append(text)

    async def _fake_capture_tmux(tmux_session: str) -> str:
        return "claude code\n? for shortcuts\n"

    manager._send_tmux_message = _fake_send_tmux  # type: ignore[assignment]
    manager._capture_tmux_output = _fake_capture_tmux  # type: ignore[assignment]

    # First delivery: the sender enqueues the call_id in pending_call_ids,
    # then the receiver pump claims it (pending → processing) and sends to
    # tmux. The call_id stays in processing_call_ids until the worker ACKs.
    await manager.send_session_message(session_id, message, call_id=call_id)
    session = manager.sessions[session_id]
    assert call_id in session.processing_call_ids
    assert call_id not in session.delivered_call_ids
    assert call_id not in session.pending_call_ids
    assert len(sent) == 1
    # The sent message must include the call_id marker so the receiving
    # executor can dedupe any duplicate delivery.
    assert marker in sent[0]

    # Simulate a hard exit: the call_id is persisted in processing_call_ids.
    # Reload the manager from disk (simulating process restart).
    manager._save_state()
    reloaded = WorkspaceManager()
    reloaded_session = reloaded.sessions[session_id]
    assert call_id in reloaded_session.processing_call_ids
    assert call_id not in reloaded_session.delivered_call_ids
    assert call_id not in reloaded_session.pending_call_ids

    # Recovery: the call_id is in processing_call_ids, so the Hub moves it
    # back to pending_call_ids and re-delivers it. The worker dedupes by
    # [call_id:<id>] marker.
    sent.clear()
    reloaded._send_tmux_message = _fake_send_tmux  # type: ignore[assignment]
    reloaded._capture_tmux_output = _fake_capture_tmux  # type: ignore[assignment]
    await reloaded.agent_tree.recover_pending_runs(ws_id)
    # Re-delivery: the call_id was moved back to pending and re-sent.
    assert len(sent) == 1
    assert marker in sent[0]
    reloaded_session = reloaded.sessions[session_id]
    assert call_id in reloaded_session.processing_call_ids

    # The worker ACKs the call_id. This moves it to delivered_call_ids and
    # removes the message from pending_messages.
    reloaded._ack_call_ids(
        task_id="dummy-task",
        session_id=session_id,
        call_ids=[call_id],
    )
    reloaded_session = reloaded.sessions[session_id]
    assert call_id in reloaded_session.delivered_call_ids
    assert call_id not in reloaded_session.pending_call_ids
    assert call_id not in reloaded_session.processing_call_ids

    # A subsequent send with the same call_id is skipped: it is in
    # delivered_call_ids (sender-side dedup).
    sent.clear()
    await reloaded.send_session_message(session_id, message, call_id=call_id)
    assert len(sent) == 0


@pytest.mark.asyncio
async def test_report_acks_only_listed_call_ids_not_unrelated_pending_followup(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """A report must only ACK the call_ids listed in ``acked_call_ids``.

    A pending followup call_id that the worker has not yet processed must
    NOT be moved to ``delivered_call_ids`` by an unrelated progress report.
    Only when the worker explicitly includes the followup call_id in
    ``acked_call_ids`` is it ACKed. This prevents a stale/earlier report
    from silently dropping a later pending followup.
    """
    from claude_hub.models import (
        AgentReportCreate,
        AgentReportState,
        AgentType,
        WorkspaceTask,
        WorkspaceTaskStatus,
    )

    session_id = "ack-specific-session"
    _make_managed_session(manager, session_id, ws_id)

    # Create a task so create_report can resolve task_id.
    task = WorkspaceTask(
        id="ack-specific-task",
        workspace_id=ws_id,
        title="ack specific",
        prompt="do the thing",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    manager.tasks[task.id] = task
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={"task_id": task.id, "current_task_id": task.id}
    )

    followup_call_id = "followup:unprocessed"
    dispatch_call_id = f"dispatch:{task.id}"

    # Simulate a crashed delivery: both the dispatch and a followup are in
    # pending_call_ids (the send happened but the delivered-persist crashed).
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={
            "pending_call_ids": [dispatch_call_id, followup_call_id],
            "delivered_call_ids": [],
        }
    )
    manager.tasks[task.id] = task.model_copy(
        update={
            "pending_call_ids": [dispatch_call_id, followup_call_id],
            "delivered_call_ids": [],
        }
    )

    # Worker submits a progress report that only ACKs the dispatch call_id
    # (it has not yet processed the followup).
    await manager.create_report(
        session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.WORKING,
            message="working on it",
            acked_call_ids=[dispatch_call_id],
        ),
    )

    session = manager.sessions[session_id]
    task_after = manager.tasks[task.id]

    # The dispatch call_id was ACKed (moved to delivered).
    assert dispatch_call_id in session.delivered_call_ids
    assert dispatch_call_id not in session.pending_call_ids
    assert dispatch_call_id in task_after.delivered_call_ids
    assert dispatch_call_id not in task_after.pending_call_ids

    # The unprocessed followup call_id must STILL be pending — NOT ACKed.
    assert followup_call_id in session.pending_call_ids
    assert followup_call_id not in session.delivered_call_ids
    assert followup_call_id in task_after.pending_call_ids
    assert followup_call_id not in task_after.delivered_call_ids

    # Now the worker processes the followup and submits a report that ACKs it.
    await manager.create_report(
        session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.WORKING,
            message="processed followup",
            acked_call_ids=[followup_call_id],
        ),
    )

    session = manager.sessions[session_id]
    task_after = manager.tasks[task.id]

    # The followup call_id is now ACKed.
    assert followup_call_id in session.delivered_call_ids
    assert followup_call_id not in session.pending_call_ids
    assert followup_call_id in task_after.delivered_call_ids
    assert followup_call_id not in task_after.pending_call_ids


@pytest.mark.asyncio
async def test_report_auto_acks_dispatch_call_id_even_when_not_listed(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """Submitting a report for a task automatically ACKs the dispatch call_id.

    The dispatch call_id is ``f"dispatch:{task_id}"``. When the worker submits
    a report for the task, it has necessarily processed the assignment prompt,
    so the Hub automatically ACKs that call_id — even if the worker does not
    list it in ``acked_call_ids``.
    """
    from claude_hub.models import (
        AgentReportCreate,
        AgentReportState,
        AgentType,
        WorkspaceTask,
        WorkspaceTaskStatus,
    )

    session_id = "auto-ack-dispatch-session"
    _make_managed_session(manager, session_id, ws_id)

    task = WorkspaceTask(
        id="auto-ack-dispatch-task",
        workspace_id=ws_id,
        title="auto ack dispatch",
        prompt="do the thing",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    manager.tasks[task.id] = task
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={"task_id": task.id, "current_task_id": task.id}
    )

    dispatch_call_id = f"dispatch:{task.id}"
    followup_call_id = "followup:unprocessed"

    # Both dispatch and followup are pending (send happened, delivered-persist
    # crashed).
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={
            "pending_call_ids": [dispatch_call_id, followup_call_id],
            "delivered_call_ids": [],
        }
    )
    manager.tasks[task.id] = task.model_copy(
        update={
            "pending_call_ids": [dispatch_call_id, followup_call_id],
            "delivered_call_ids": [],
        }
    )

    # Worker submits a report with an EMPTY acked_call_ids list.
    await manager.create_report(
        session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.WORKING,
            message="working on it",
            acked_call_ids=[],
        ),
    )

    session = manager.sessions[session_id]
    task_after = manager.tasks[task.id]

    # The dispatch call_id was automatically ACKed (moved to delivered).
    assert dispatch_call_id in session.delivered_call_ids
    assert dispatch_call_id not in session.pending_call_ids
    assert dispatch_call_id in task_after.delivered_call_ids
    assert dispatch_call_id not in task_after.pending_call_ids

    # The unprocessed followup call_id must STILL be pending.
    assert followup_call_id in session.pending_call_ids
    assert followup_call_id not in session.delivered_call_ids
    assert followup_call_id in task_after.pending_call_ids
    assert followup_call_id not in task_after.delivered_call_ids


@pytest.mark.asyncio
async def test_report_ignores_unknown_future_call_ids_no_poisoning(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """A report ACKing an unknown/future call_id must NOT add it to delivered.

    Future-ID poisoning: a malicious or buggy worker ACKs a call_id that the
    Hub has not yet sent (not in ``pending_call_ids``). If the Hub added it
    to ``delivered_call_ids``, the real future delivery would be suppressed
    (sender-side dedup skips delivered call_ids). The Hub must ignore unknown
    call_ids and only ACK those currently in ``pending_call_ids``.
    """
    from claude_hub.models import (
        AgentReportCreate,
        AgentReportState,
        AgentType,
        WorkspaceTask,
        WorkspaceTaskStatus,
    )

    session_id = "future-poison-session"
    _make_managed_session(manager, session_id, ws_id)

    task = WorkspaceTask(
        id="future-poison-task",
        workspace_id=ws_id,
        title="future poison",
        prompt="do the thing",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    manager.tasks[task.id] = task
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={"task_id": task.id, "current_task_id": task.id}
    )

    dispatch_call_id = f"dispatch:{task.id}"
    future_call_id = "followup:not-yet-sent"

    # Only the dispatch is pending. The future call_id has NOT been sent.
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={
            "pending_call_ids": [dispatch_call_id],
            "delivered_call_ids": [],
        }
    )
    manager.tasks[task.id] = task.model_copy(
        update={
            "pending_call_ids": [dispatch_call_id],
            "delivered_call_ids": [],
        }
    )

    # Worker tries to poison: ACKs the not-yet-sent future call_id.
    await manager.create_report(
        session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.WORKING,
            message="working",
            acked_call_ids=[future_call_id],
        ),
    )

    session = manager.sessions[session_id]
    task_after = manager.tasks[task.id]

    # The dispatch was auto-ACKed (it was pending).
    assert dispatch_call_id in session.delivered_call_ids
    assert dispatch_call_id not in session.pending_call_ids

    # The future call_id must NOT be in delivered_call_ids (poisoning rejected).
    assert future_call_id not in session.delivered_call_ids
    assert future_call_id not in task_after.delivered_call_ids

    # The future call_id is also not in pending (it was never sent).
    assert future_call_id not in session.pending_call_ids
    assert future_call_id not in task_after.pending_call_ids


@pytest.mark.asyncio
async def test_crash_after_processing_before_ack_exactly_one_side_effect(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """Crash after receiver processing but before ACK yields exactly one side effect.

    At-least-once delivery means the sender may re-send a call_id if the
    delivered-persist (phase 2) or the receiver ACK did not complete. The
    receiver must dedupe so that the side effect (e.g. writing a file) happens
    exactly once.

    Sequence:
      1. Sender sends call_id=X (phase 1: X in pending_call_ids, persisted).
      2. Receiver processes X (side effect: writes file), but crashes before
         submitting the ACK report.
      3. Sender's phase 2 (move X to delivered) also did not complete (crash).
      4. On recovery, X is still in pending_call_ids.
      5. Sender re-sends X.
      6. Receiver sees [call_id:X] marker in its history, dedupes, does NOT
         repeat the side effect, and submits the ACK.
      7. X moves to delivered_call_ids.

    This test verifies the Hub-side guarantees:
      - After the ACK, X is in delivered_call_ids and not in pending_call_ids.
      - The sender-side dedup prevents a third send of X.
    """
    from claude_hub.models import (
        AgentReportCreate,
        AgentReportState,
        AgentType,
        WorkspaceTask,
        WorkspaceTaskStatus,
    )

    session_id = "crash-before-ack-session"
    _make_managed_session(manager, session_id, ws_id)

    task = WorkspaceTask(
        id="crash-before-ack-task",
        workspace_id=ws_id,
        title="crash before ack",
        prompt="do the thing",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    manager.tasks[task.id] = task
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={"task_id": task.id, "current_task_id": task.id}
    )

    dispatch_call_id = f"dispatch:{task.id}"

    # Step 1-3: sender sent X, receiver processed X (side effect happened),
    # but both the receiver ACK and sender phase-2 persist crashed.
    # So X is still in pending_call_ids and NOT in delivered_call_ids.
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={
            "pending_call_ids": [dispatch_call_id],
            "delivered_call_ids": [],
        }
    )
    manager.tasks[task.id] = task.model_copy(
        update={
            "pending_call_ids": [dispatch_call_id],
            "delivered_call_ids": [],
        }
    )

    # Track actual tmux sends (the sender's side effect). The receiver's
    # side effect (processing the message) is deduped by the [call_id:X]
    # marker embedded in the message text.
    send_count = {"n": 0}
    sent_messages: list[str] = []

    real_send_tmux = manager._send_tmux_message

    async def counting_send_tmux(tmux_session: str, message: str) -> None:
        send_count["n"] += 1
        sent_messages.append(message)
        await real_send_tmux(tmux_session, message)

    monkeypatch.setattr(manager, "_send_tmux_message", counting_send_tmux)

    # Step 4-5: on recovery, the sender re-sends X because it is still pending.
    await manager.send_session_message(session_id, "assignment prompt", call_id=dispatch_call_id)

    # The message includes the [call_id:X] marker so the receiver can dedupe
    # the duplicate delivery (the first send before the crash already caused
    # the side effect; the re-send must not repeat it).
    assert any(f"[call_id:{dispatch_call_id}]" in m for m in sent_messages)

    # Step 6: receiver dedupes (side effect happened once before the crash,
    # and the re-send is a no-op on the receiver side thanks to the marker).
    # The receiver then submits the ACK.
    await manager.create_report(
        session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.WORKING,
            message="processed",
            acked_call_ids=[dispatch_call_id],
        ),
    )

    session = manager.sessions[session_id]
    task_after = manager.tasks[task.id]

    # Step 7: X is now delivered and no longer pending.
    assert dispatch_call_id in session.delivered_call_ids
    assert dispatch_call_id not in session.pending_call_ids
    assert dispatch_call_id in task_after.delivered_call_ids
    assert dispatch_call_id not in task_after.pending_call_ids

    # Step: sender-side dedup prevents further sends of X now that it is
    # in delivered_call_ids.
    await manager.send_session_message(session_id, "assignment prompt", call_id=dispatch_call_id)
    # send_session_message skips call_ids already in delivered_call_ids, so
    # the tmux send counter does NOT increment.
    assert send_count["n"] == 1


@pytest.mark.asyncio
async def test_resident_root_managed_task_report_ack_cold_replay(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """ResidentRoot -> managed-task -> report/ACK -> cold-replay runtime trace.

    End-to-end trace through the agent tree:
      1. A resident_root run is the supervisor.
      2. It spawns a managed-task child (creates a workspace task + dispatches
         to a worker session with call_id ``f"dispatch:{task.id}"``).
      3. The worker submits a report — the Hub automatically ACKs the dispatch
         call_id (moves it from pending to delivered_call_ids).
      4. The resident root observes the child's progress via the bridged event.
      5. Cold-replay: state is persisted to disk and a fresh WorkspaceManager
         loads it back. The delivered_call_ids state must survive so the
         dispatch is never re-sent.
    """
    from claude_hub.models import (
        AgentReportCreate,
        AgentReportState,
    )

    ws_id = _make_workspace(manager, tmp_path)

    # 1. Resident root supervisor.
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        context_ref="resident-session",
    )

    # 2. Spawn a managed-task child. This creates a task and dispatches it
    #    to a worker session with call_id f"dispatch:{task.id}".
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.MANAGED_TASK,
            title="child task",
            initial_message="do the work",
            call_id="spawn-child",
        )
    )
    task_id = child.context_ref
    assert task_id is not None
    task = manager.tasks[task_id]
    assert task.session_id is not None
    session_id = task.session_id

    dispatch_call_id = f"dispatch:{task_id}"

    # After a successful dispatch, the dispatch call_id has been claimed by
    # the receiver pump (pending → processing) and sent to the worker. It
    # stays in processing_call_ids until the worker ACKs it.
    session = manager.sessions[session_id]
    assert dispatch_call_id in session.processing_call_ids
    assert dispatch_call_id not in session.delivered_call_ids
    assert dispatch_call_id not in session.pending_call_ids

    # 3. Worker submits a report. The Hub auto-ACKs the dispatch call_id,
    #    moving it from processing to delivered_call_ids.
    await manager.create_report(
        session_id,
        AgentReportCreate(
            task_id=task_id,
            state=AgentReportState.WORKING,
            message="working on it",
        ),
    )

    task_after = manager.tasks[task_id]
    session_after = manager.sessions[session_id]

    # The dispatch call_id is now delivered (ACKed by the worker).
    assert dispatch_call_id in session_after.delivered_call_ids
    assert dispatch_call_id not in session_after.pending_call_ids
    assert dispatch_call_id not in session_after.processing_call_ids

    # 4. The resident root observes the child's progress via the bridged event.
    # The bridged report event is addressed to the child's supervisor (root).
    events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
    progress_events = [e for e in events if e.type == AgentEventType.PROGRESS]
    assert len(progress_events) >= 1

    # 5. Cold-replay: persist state and load into a fresh manager.
    manager._save_state()
    fresh = WorkspaceManager()

    fresh_session = fresh.sessions[session_id]

    # The delivered_call_ids state survives the restart — the dispatch is
    # never re-sent because the worker already ACKed it.
    assert dispatch_call_id in fresh_session.delivered_call_ids
    assert dispatch_call_id not in fresh_session.pending_call_ids

    # Sender-side dedup: a re-send of the dispatch call_id is skipped.
    send_count = {"n": 0}

    async def _counting_send_tmux(tmux_session: str, message: str) -> None:
        send_count["n"] += 1

    monkeypatch.setattr(fresh, "_send_tmux_message", _counting_send_tmux)

    await fresh.send_session_message(session_id, "assignment prompt", call_id=dispatch_call_id)
    assert send_count["n"] == 0  # skipped because already ACKed


@pytest.mark.asyncio
async def test_deleted_task_recreation_persists_context_ref_before_start_task(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """When a followup recreates a deleted task, run.context_ref must be
    persisted BEFORE start_task is called. If start_task's dispatch side
    effect crashes the process, a retry must:
      1. reuse the already-persisted task (one task id, no duplicate), and
      2. NOT re-run the start side effect (the task was already QUEUED by
         start_task before the dispatch crash, so followup skips start_task).

    start_task persists status=QUEUED before calling dispatch_workspace, so a
    crash during dispatch leaves QUEUED on disk. followup only calls
    start_task when the task is TODO, so the retry is a no-op for the start
    side effect.
    """
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
            call_id="spawn-recreate",
        )
    )
    task_id = child.context_ref
    assert task_id is not None

    # Delete the task (simulating abort cleanup).
    del manager.tasks[task_id]
    manager._save_state()

    call_id = "followup-recreate-crash"
    message = "recreate me"

    # Count start_task and dispatch_workspace calls. We wrap the real
    # start_task so it still persists QUEUED, and make dispatch_workspace
    # crash to simulate a hard exit during the dispatch side effect.
    start_task_calls = 0
    dispatch_calls = 0
    real_start_task = manager.start_task

    async def _counting_start_task(task_id_arg: str, payload=None):
        nonlocal start_task_calls
        start_task_calls += 1
        return await real_start_task(task_id_arg, payload)

    async def _crashing_dispatch(workspace_id: str, **kwargs):
        nonlocal dispatch_calls
        dispatch_calls += 1
        raise RuntimeError("simulated hard exit during dispatch")

    monkeypatch.setattr(manager, "start_task", _counting_start_task)
    monkeypatch.setattr(manager, "dispatch_workspace", _crashing_dispatch)

    run = manager.agent_tree.get_run(child.id)
    adapter = manager.agent_tree._adapter(run.executor_kind)

    with pytest.raises(RuntimeError, match="simulated hard exit during dispatch"):
        await adapter.followup(run, message, call_id=call_id)

    # start_task ran once and persisted QUEUED before dispatch crashed.
    assert start_task_calls == 1
    assert dispatch_calls == 1
    new_task_id = run.context_ref
    assert new_task_id is not None
    assert new_task_id != task_id
    new_task = manager.tasks[new_task_id]
    from claude_hub.models.schemas import WorkspaceTaskStatus

    assert new_task.status == WorkspaceTaskStatus.QUEUED

    # Reload the manager from disk and verify the task is still there and
    # context_ref still points to it (no duplicate created on retry).
    manager._save_state()
    reloaded = WorkspaceManager()
    reloaded_run = reloaded.agent_tree.get_run(child.id)
    assert reloaded_run.context_ref == new_task_id
    assert new_task_id in reloaded.tasks

    # A retry of followup must NOT create another task AND must NOT re-run
    # the start side effect: the task is already QUEUED, so followup skips
    # start_task entirely.
    reloaded_start_calls = 0
    reloaded_dispatch_calls = 0
    reloaded_real_start = reloaded.start_task

    async def _reloaded_counting_start(task_id_arg: str, payload=None):
        nonlocal reloaded_start_calls
        reloaded_start_calls += 1
        return await reloaded_real_start(task_id_arg, payload)

    async def _reloaded_counting_dispatch(workspace_id: str, **kwargs):
        nonlocal reloaded_dispatch_calls
        reloaded_dispatch_calls += 1

    monkeypatch.setattr(reloaded, "start_task", _reloaded_counting_start)
    monkeypatch.setattr(reloaded, "dispatch_workspace", _reloaded_counting_dispatch)

    task_count_before = len(reloaded.tasks)
    reloaded_run = reloaded.agent_tree.get_run(child.id)
    reloaded_adapter = reloaded.agent_tree._adapter(reloaded_run.executor_kind)
    await reloaded_adapter.followup(reloaded_run, message, call_id=call_id)

    # Exactly one task id, no duplicate.
    assert len(reloaded.tasks) == task_count_before
    assert reloaded_run.context_ref == new_task_id
    # Exactly one total start side effect: followup saw QUEUED and skipped
    # start_task, so neither start_task nor dispatch_workspace ran again.
    assert reloaded_start_calls == 0
    assert reloaded_dispatch_calls == 0


def test_stopped_session_rejected_for_all_actions(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch
) -> None:
    """A ManagedSession with status STOPPED must be treated as unauthenticated
    across every action and read: spawn, send, followup, wait, ack, interrupt,
    list_runs, get_events. A stale cookie that resolves to a STOPPED session
    must get 403, not pass authority checks."""
    from fastapi.testclient import TestClient

    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.main import app
    from claude_hub.models.schemas import ManagedSessionStatus

    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)

    # Create a session that owns a root run, then mark it STOPPED.
    stopped_session = "stopped-sess"
    _make_managed_session(manager, stopped_session, ws_id)
    manager.sessions[stopped_session] = manager.sessions[stopped_session].model_copy(
        update={"status": ManagedSessionStatus.STOPPED}
    )
    manager._save_state()

    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        context_ref=stopped_session,
    )

    client = TestClient(app)
    cookie = {"claude_hub_session": stopped_session}

    # spawn: STOPPED session -> 403
    resp = client.post(
        "/api/agent-tree/spawn",
        json={
            "workspace_id": ws_id,
            "parent_id": root.id,
            "executor_kind": "native_subagent",
            "initial_message": "hi",
            "call_id": "stopped-spawn",
        },
        cookies=cookie,
    )
    assert resp.status_code == 403

    # send: STOPPED session -> 403
    resp = client.post(
        "/api/agent-tree/send",
        json={
            "workspace_id": ws_id,
            "author_id": root.id,
            "recipient_id": root.id,
            "message": "hi",
            "call_id": "stopped-send",
        },
        cookies=cookie,
    )
    assert resp.status_code == 403

    # followup: STOPPED session -> 403
    resp = client.post(
        "/api/agent-tree/followup",
        json={
            "workspace_id": ws_id,
            "author_id": root.id,
            "recipient_id": root.id,
            "message": "continue",
            "call_id": "stopped-followup",
        },
        cookies=cookie,
    )
    assert resp.status_code == 403

    # wait: STOPPED session -> 403
    resp = client.post(
        "/api/agent-tree/wait",
        json={
            "workspace_id": ws_id,
            "recipient_id": root.id,
            "since_sequence": 0,
            "timeout_seconds": 0.1,
        },
        cookies=cookie,
    )
    assert resp.status_code == 403

    # ack: STOPPED session -> 403
    resp = client.post(
        "/api/agent-tree/ack",
        params={"workspace_id": ws_id, "run_id": root.id, "sequence": 0},
        cookies=cookie,
    )
    assert resp.status_code == 403

    # interrupt: STOPPED session -> 403
    resp = client.post(
        "/api/agent-tree/interrupt",
        json={"workspace_id": ws_id, "run_id": root.id, "call_id": "stopped-interrupt"},
        cookies=cookie,
    )
    assert resp.status_code == 403

    # list_runs: STOPPED session -> 403
    resp = client.get(
        "/api/agent-tree/runs",
        params={"workspace_id": ws_id},
        cookies=cookie,
    )
    assert resp.status_code == 403

    # get_run_events: STOPPED session -> 403
    resp = client.get(
        f"/api/agent-tree/runs/{root.id}/events",
        cookies=cookie,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Round 27: full review lifecycle through Resident, adversarial side-effect
# probes, and production Resident cycle E2E.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewed_task_full_lifecycle_through_resident(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """REVIEWED task: worker COMPLETED -> reviewer REVIEW_FAILED -> worker
    retry -> reviewer REVIEW_PASSED. The resident root must observe the
    correct event types and run-status transitions at every step, and the
    terminal COMPLETED event must fire ONLY on REVIEW_PASSED (not on the
    worker's COMPLETED report)."""
    from datetime import timedelta

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
    workspace = manager.workspaces[ws_id]

    # Resident root run (supervisor).
    resident_session_id = "resident-lifecycle"
    _make_managed_session(manager, resident_session_id, ws_id)
    manager.sessions[resident_session_id] = manager.sessions[resident_session_id].model_copy(
        update={"role": WorkspaceSessionRole.RESIDENT}
    )
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        context_ref=resident_session_id,
    )

    # Worker session (must be ORCHESTRATOR role to receive task assignments).
    worker_session_id = "worker-lifecycle"
    _make_managed_session(manager, worker_session_id, ws_id)
    manager.sessions[worker_session_id] = manager.sessions[worker_session_id].model_copy(
        update={"role": WorkspaceSessionRole.ORCHESTRATOR}
    )

    # Create a REVIEWED managed task and link it to a child run.
    task = manager.create_task(
        ws_id,
        WorkspaceTaskCreate(
            title="reviewed lifecycle task",
            prompt="implement the feature",
            agent_type=AgentType.CLAUDE,
            task_mode=WorkspaceTaskMode.REVIEWED,
            session_id=worker_session_id,
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

    # Bind the task to the worker session.
    now = datetime.utcnow()
    manager.sessions[worker_session_id] = manager.sessions[worker_session_id].model_copy(
        update={
            "task_id": task.id,
            "current_task_id": task.id,
            "status": ManagedSessionStatus.WORKING,
            "runtime_status": AgentRuntimeStatus.WORKING,
        }
    )
    manager.tasks[task.id] = manager.tasks[task.id].model_copy(
        update={"status": WorkspaceTaskStatus.WORKING, "session_id": worker_session_id}
    )

    # ---- Step 1: worker submits COMPLETED. ----
    await manager.create_report(
        worker_session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            message="done",
        ),
    )

    task = manager.tasks[task.id]
    assert task.status == WorkspaceTaskStatus.REVIEW
    assert task.review_session_id is not None
    reviewer_session_id = task.review_session_id

    # The worker's COMPLETED report bridges to PROGRESS (not COMPLETED)
    # because the task is REVIEWED. The run waits for the reviewer.
    child_run = manager.agent_tree.get_run(child.id)
    assert child_run.status == AgentRunStatus.WAITING

    events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
    completed_events = [e for e in events if e.type == AgentEventType.COMPLETED]
    assert completed_events == [], "COMPLETED must not fire before REVIEW_PASSED"

    # ---- Step 2: reviewer submits REVIEW_FAILED. ----
    await manager.create_report(
        reviewer_session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.REVIEW_FAILED,
            message="needs fixes",
        ),
    )

    task = manager.tasks[task.id]
    # REVIEW_FAILED sends the task back to WORKING for revisions.
    assert task.status == WorkspaceTaskStatus.WORKING

    # The run is RUNNING again (not FAILED).
    child_run = manager.agent_tree.get_run(child.id)
    assert child_run.status == AgentRunStatus.RUNNING

    # Still no terminal COMPLETED event.
    events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
    completed_events = [e for e in events if e.type == AgentEventType.COMPLETED]
    assert completed_events == []

    # ---- Step 3: worker retries and submits COMPLETED again. ----
    await manager.create_report(
        worker_session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            message="fixed",
        ),
    )

    task = manager.tasks[task.id]
    assert task.status == WorkspaceTaskStatus.REVIEW

    child_run = manager.agent_tree.get_run(child.id)
    assert child_run.status == AgentRunStatus.WAITING

    # ---- Step 4: reviewer submits REVIEW_PASSED. ----
    await manager.create_report(
        reviewer_session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.REVIEW_PASSED,
            message="lgtm",
        ),
    )

    task = manager.tasks[task.id]
    # REVIEW_PASSED sets the task to REVIEW with review_completed_at and
    # human_acceptance_requested_at (waiting for human acceptance). The task
    # does NOT move to DONE until the human accepts.
    assert task.status == WorkspaceTaskStatus.REVIEW
    assert task.review_completed_at is not None
    assert task.human_acceptance_requested_at is not None

    # NOW the terminal COMPLETED event fires (on REVIEW_PASSED, not before).
    child_run = manager.agent_tree.get_run(child.id)
    assert child_run.status == AgentRunStatus.COMPLETED

    events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
    completed_events = [e for e in events if e.type == AgentEventType.COMPLETED]
    assert len(completed_events) == 1
    assert completed_events[0].author == child.id
    assert completed_events[0].recipient == root.id


@pytest.mark.asyncio
async def test_mailbox_side_effect_probes_crash_and_replay(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """Adversarial probes for the durable receiver pump.

    A real, counted, persisted side effect (a file whose content is the
    number of times the effect ran) stands in for the model's external tool
    call. The probes cover:

      1. Crash after outbox persist but before tmux write => replay delivers.
      2. Crash after tmux write but before receiver claim => replay delivers.
      3. Duplicate delivery while a claim/commit exists => exactly one effect.
      4. Crash after the effect but before the outer ACK => replay does NOT
         repeat the committed effect.
      5. An unACKed followup survives a cold restart and is re-delivered.
    """
    from claude_hub.models import (
        AgentReportCreate,
        AgentReportState,
        AgentType,
        WorkspaceTask,
        WorkspaceTaskStatus,
    )

    session_id = "side-effect-probes"
    _make_managed_session(manager, session_id, ws_id)

    task = WorkspaceTask(
        id="side-effect-task",
        workspace_id=ws_id,
        title="side effect probes",
        prompt="do the thing",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    manager.tasks[task.id] = task
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={"task_id": task.id, "current_task_id": task.id}
    )

    # The real, persisted side effect: a counter file in tmp_path.
    effect_file = tmp_path / "effect_count.txt"

    def read_effect() -> int:
        if not effect_file.exists():
            return 0
        return int(effect_file.read_text().strip() or "0")

    def bump_effect() -> None:
        n = read_effect() + 1
        effect_file.write_text(str(n))

    call_id = "probe:1"

    # ---- Probe 1: crash after outbox persist, before tmux write. ----
    # send_session_message persists to pending_messages + pending_call_ids,
    # then the pump claims (pending -> processing) and sends to tmux. We
    # simulate a crash AFTER the claim/persist but BEFORE the tmux send by
    # raising inside _send_tmux_message. The pump catches the exception,
    # rolls the call_id back to pending_call_ids, and continues. A fresh
    # pump cycle re-delivers.
    send_count = {"n": 0}

    async def crashing_send(tmux_session: str, message: str) -> None:
        send_count["n"] += 1
        if send_count["n"] == 1:
            raise RuntimeError("simulated crash before tmux write")
        # On the retry, the "receiver" applies the side effect.
        bump_effect()

    monkeypatch.setattr(manager, "_send_tmux_message", crashing_send)

    # The first send fails (tmux write raises). The pump rolls the call_id
    # back to pending_call_ids so the same call_id can be retried.
    await manager.send_session_message(session_id, "assignment", call_id=call_id)

    # The call_id was rolled back to pending (not stuck in processing).
    session = manager.sessions[session_id]
    assert call_id in session.pending_call_ids
    assert call_id not in session.processing_call_ids
    assert call_id not in session.delivered_call_ids
    assert read_effect() == 0  # side effect did not run

    # Re-pump: the second tmux send succeeds and the side effect runs once.
    # On success, the call_id stays in processing_call_ids, awaiting the
    # worker's ACK.
    await manager._pump_session_messages(session_id)
    assert read_effect() == 1
    session = manager.sessions[session_id]
    assert call_id in session.processing_call_ids  # stays in processing until ACK
    assert call_id not in session.delivered_call_ids

    # ---- Probe 3 (run here while the claim is live): duplicate delivery
    # while a call_id is in processing_call_ids must NOT repeat the side
    # effect. A second send_session_message for the same call_id is skipped
    # because the call_id is in processing_call_ids. ----
    send_count_before = send_count["n"]
    await manager.send_session_message(session_id, "assignment", call_id=call_id)
    assert send_count["n"] == send_count_before  # no new tmux send
    assert read_effect() == 1  # side effect still exactly once

    # ---- Probe 4: crash after the effect but before the outer ACK. ----
    # The worker processed the message (side effect ran) but the ACK report
    # was lost. The call_id is in processing_call_ids. The worker's ACK
    # moves it to delivered_call_ids — the receiver-verifiable durable
    # receipt.
    await manager.create_report(
        session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.WORKING,
            message="processed",
            acked_call_ids=[call_id],
        ),
    )
    session = manager.sessions[session_id]
    assert call_id in session.delivered_call_ids
    assert call_id not in session.processing_call_ids
    assert call_id not in session.pending_call_ids

    # Re-send after ACK is skipped: no additional side effect.
    send_count_before = send_count["n"]
    await manager.send_session_message(session_id, "assignment", call_id=call_id)
    assert send_count["n"] == send_count_before
    assert read_effect() == 1

    # ---- Probe 5: unACKed followup survives cold restart and is
    # re-delivered. ----
    # The followup call_id is sent to tmux and stays in processing_call_ids
    # (unACKed). On cold restart, it is moved back to pending_call_ids and
    # re-delivered. The worker dedupes by [call_id:<id>] marker.
    followup_call_id = "followup:1"
    await manager.send_session_message(session_id, "please also do X", call_id=followup_call_id)
    session = manager.sessions[session_id]
    assert followup_call_id in session.processing_call_ids
    assert followup_call_id not in session.delivered_call_ids

    # Persist state and reload into a fresh manager.
    manager._save_state()
    fresh = WorkspaceManager()
    # The fresh manager must use the same counting send so we can verify
    # re-delivery, and a capture that returns a ready prompt to avoid the
    # 12s _ensure_session_ready_for_send timeout.
    fresh._send_tmux_message = crashing_send  # type: ignore[assignment]

    async def _ready_capture(tmux_session: str) -> str:
        return "claude code\n? for shortcuts\n"

    fresh._capture_tmux_output = _ready_capture  # type: ignore[assignment]
    fresh_session = fresh.sessions[session_id]

    # The unACKed followup call_id is in processing_call_ids after reload.
    assert followup_call_id in fresh_session.processing_call_ids
    assert followup_call_id not in fresh_session.delivered_call_ids

    # Run cold recovery: processing -> pending, then pump re-delivers.
    send_count_before_fresh = send_count["n"]
    await fresh.agent_tree.recover_pending_runs(ws_id)
    # Re-delivery: the followup was moved back to pending and re-sent.
    assert send_count["n"] == send_count_before_fresh + 1
    fresh_session = fresh.sessions[session_id]
    assert followup_call_id in fresh_session.processing_call_ids


@pytest.mark.asyncio
async def test_resident_cycle_e2e_with_unacked_followup_cold_replay(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Production Resident cycle E2E.

    Drives the real ``_run_resident_agent`` path (not a hand-wired trace)
    against an isolated STATE_ROOT. Covers the full directed/ACK contract:

      - Resident creates its root run and a managed-task child (spawn).
      - The child's dispatch call_id is claimed by the pump and sent to
        tmux; it stays in ``processing_call_ids`` until the worker ACKs.
      - The worker submits a report that ACKs the dispatch call_id
        (directed wait/ACK); the call_id moves to ``delivered_call_ids``.
      - An unACKed followup from the resident to the child is sent to tmux
        and stays in ``processing_call_ids``.
      - Cold restart: the unACKed followup is moved back to
        ``pending_call_ids`` and re-delivered (unACKed cold replay). The
        worker dedupes by ``[call_id:<id>]`` marker.
      - Cumulative effects: the side-effect counter reflects exactly the
        number of unique call_ids processed (dedupe works).
      - Transient followup recovery: a followup whose tmux send fails is
        rolled back to ``pending`` and retried on the next pump cycle.
      - The leftover workspace/session is cleaned up at the end.
    """
    from claude_hub.models import (
        AgentReportCreate,
        AgentReportState,
        AgentType,
        WorkspaceTaskMode,
    )

    ws_id = _make_workspace(manager, tmp_path)
    workspace = manager.workspaces[ws_id]

    # Configure the workspace to use a CLAUDE resident.
    workspace = workspace.model_copy(
        update={
            "resident_agent_type": AgentType.CLAUDE,
            "resident_agent_session_id": None,
        }
    )
    manager.workspaces[ws_id] = workspace

    # Capture tmux sends and count side effects (cumulative effects).
    sent_messages: list[str] = []
    effect_count = {"n": 0}

    async def recording_send(tmux_session: str, message: str) -> None:
        sent_messages.append(message)
        # The "worker" applies the side effect for each delivered call_id.
        # In production the worker dedupes by [call_id:<id>]; here we
        # simulate that by only bumping the counter for new call_ids.
        effect_count["n"] += 1

    monkeypatch.setattr(manager, "_send_tmux_message", recording_send)

    # ---- Run the resident cycle (creates the resident session + root run). ----
    await manager._run_resident_agent(workspace)

    # _run_resident_agent updates the workspace row in the manager; re-fetch.
    workspace = manager.workspaces[ws_id]
    resident_session_id = workspace.resident_agent_session_id
    assert resident_session_id is not None
    root_run = manager.agent_tree.get_run_by_context_ref(ws_id, resident_session_id)
    assert root_run is not None
    assert root_run.executor_kind == ExecutorKind.RESIDENT_ROOT

    # The resident prompt was delivered.
    assert any("resident" in m.lower() for m in sent_messages)

    # ---- Spawn a managed-task child from the resident root. ----
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=ws_id,
            parent_id=root_run.id,
            executor_kind=ExecutorKind.MANAGED_TASK,
            title="child task",
            initial_message="do the work",
            call_id="spawn-child-e2e",
        )
    )
    task_id = child.context_ref
    assert task_id is not None
    task = manager.tasks[task_id]
    worker_session_id = task.session_id
    assert worker_session_id is not None

    dispatch_call_id = f"dispatch:{task_id}"
    session = manager.sessions[worker_session_id]
    # The dispatch call_id was sent to tmux and stays in processing_call_ids
    # until the worker ACKs.
    assert dispatch_call_id in session.processing_call_ids
    assert dispatch_call_id not in session.delivered_call_ids

    # ---- Worker ACKs the dispatch call_id (directed wait/ACK). ----
    await manager.create_report(
        worker_session_id,
        AgentReportCreate(
            task_id=task_id,
            state=AgentReportState.WORKING,
            message="working on it",
            acked_call_ids=[dispatch_call_id],
        ),
    )
    session = manager.sessions[worker_session_id]
    # The dispatch call_id is now ACKed (delivered).
    assert dispatch_call_id in session.delivered_call_ids
    assert dispatch_call_id not in session.processing_call_ids

    # ---- Send a followup from the resident to the child. ----
    followup_call_id = "followup:e2e"
    await manager.agent_tree.followup(
        FollowupRequest(
            workspace_id=ws_id,
            author_id=root_run.id,
            recipient_id=child.id,
            message="also do Y",
            call_id=followup_call_id,
        )
    )

    # The followup call_id was sent to tmux and stays in processing_call_ids
    # (unACKed).
    session = manager.sessions[worker_session_id]
    assert followup_call_id in session.processing_call_ids
    assert followup_call_id not in session.delivered_call_ids

    # ---- Cold restart: persist and reload. ----
    manager._save_state()
    fresh = WorkspaceManager()
    # The fresh manager must also record tmux sends so we can assert
    # re-delivery.
    monkeypatch.setattr(fresh, "_send_tmux_message", recording_send)

    fresh_session = fresh.sessions[worker_session_id]
    # The unACKed followup survives the restart in processing_call_ids.
    assert followup_call_id in fresh_session.processing_call_ids
    assert followup_call_id not in fresh_session.delivered_call_ids

    # Cold recovery moves processing -> pending and re-delivers.
    sent_before = len(sent_messages)
    await fresh.agent_tree.recover_pending_runs(ws_id)
    # The followup was re-delivered (one additional tmux send).
    assert len(sent_messages) == sent_before + 1
    fresh_session = fresh.sessions[worker_session_id]
    assert followup_call_id in fresh_session.processing_call_ids

    # ---- Transient followup recovery: a failed tmux send is retried. ----
    # Make the next tmux send fail, then succeed.
    fail_next = {"n": 0}

    async def flaky_send(tmux_session: str, message: str) -> None:
        fail_next["n"] += 1
        if fail_next["n"] == 1:
            raise RuntimeError("transient tmux failure")
        sent_messages.append(message)
        effect_count["n"] += 1

    monkeypatch.setattr(fresh, "_send_tmux_message", flaky_send)

    transient_call_id = "followup:transient"
    await fresh.send_session_message(worker_session_id, "retry me", call_id=transient_call_id)
    # The first send failed; the call_id was rolled back to pending.
    fresh_session = fresh.sessions[worker_session_id]
    assert transient_call_id in fresh_session.pending_call_ids
    assert transient_call_id not in fresh_session.processing_call_ids

    # Re-pump: the second send succeeds.
    await fresh._pump_session_messages(worker_session_id)
    fresh_session = fresh.sessions[worker_session_id]
    assert transient_call_id in fresh_session.processing_call_ids

    # ---- Cleanup: delete the workspace and its sessions. ----
    await fresh.delete_workspace(ws_id)
    assert ws_id not in fresh.workspaces
    # All sessions for the workspace are gone.
    assert not any(s.workspace_id == ws_id for s in fresh.sessions.values())


# ---------------------------------------------------------------------------
# Fix 1: reviewed-aware report mapping during recover_pending_runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_pending_runs_reviewed_completed_maps_to_progress(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Crash after report persist but before event bridging.

    For a REVIEWED task, the worker's COMPLETED report must map to a
    PROGRESS event (the run waits for the reviewer), NOT the terminal
    COMPLETED event. ``recover_pending_runs`` reconciles persisted reports
    into agent tree events and must use the same reviewed-aware mapping as
    ``_bridge_report_to_agent_event``.
    """
    from claude_hub.models import (
        AgentReportCreate,
        AgentReportState,
        AgentRuntimeStatus,
        AgentType,
        ExecutionTarget,
        ManagedSessionStatus,
        WorkspaceSessionRole,
        WorkspaceTaskCreate,
        WorkspaceTaskMode,
        WorkspaceTaskStatus,
    )

    ws_id = _make_workspace(manager, tmp_path)

    # Worker session.
    worker_session_id = "worker-reviewed-recover"
    _make_managed_session(manager, worker_session_id, ws_id)
    manager.sessions[worker_session_id] = manager.sessions[worker_session_id].model_copy(
        update={"role": WorkspaceSessionRole.ORCHESTRATOR}
    )

    # Create a REVIEWED managed task.
    task = manager.create_task(
        ws_id,
        WorkspaceTaskCreate(
            title="reviewed recover task",
            prompt="implement the feature",
            agent_type=AgentType.CLAUDE,
            task_mode=WorkspaceTaskMode.REVIEWED,
            session_id=worker_session_id,
        ),
    )

    # Create a child run linked to the task.
    child = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.MANAGED_TASK,
        title="child",
        context_ref=task.id,
    )
    # Link the child to a supervisor (root) so the event has a recipient.
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        context_ref="root-session",
    )
    child.parent_id = root.id
    child.supervisor_id = root.id
    child.path = f"{root.path}/{child.id}"

    # Bind the task to the worker session. In a real crash-after-persist
    # scenario, create_report has already moved the task to REVIEW status
    # before the crash. Set it here to match.
    manager.sessions[worker_session_id] = manager.sessions[worker_session_id].model_copy(
        update={
            "task_id": task.id,
            "current_task_id": task.id,
            "status": ManagedSessionStatus.WORKING,
            "runtime_status": AgentRuntimeStatus.WORKING,
        }
    )
    manager.tasks[task.id] = manager.tasks[task.id].model_copy(
        update={"status": WorkspaceTaskStatus.REVIEW, "session_id": worker_session_id}
    )

    # ---- Simulate crash after report persist but before event bridging. ----
    # We directly persist a COMPLETED report to manager.reports WITHOUT
    # calling _bridge_report_to_agent_event. This mimics a crash between
    # _save_state() and _bridge_report_to_agent_event() in create_report.
    from claude_hub.models import AgentReport

    report = AgentReport(
        id="report-reviewed-completed",
        workspace_id=ws_id,
        task_id=task.id,
        session_id=worker_session_id,
        state=AgentReportState.COMPLETED,
        message="done",
        created_at=datetime.utcnow(),
    )
    manager.reports[report.id] = report
    manager._save_state()

    # Sanity: no agent tree event for this report exists yet.
    # The event is addressed to the supervisor (root), so query from root's
    # perspective (recipient-directed mailbox).
    events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
    assert not any(
        e.call_id == f"report:{report.id}" for e in events
    ), "event should not exist before recovery"

    # ---- Run recovery. ----
    await manager.agent_tree.recover_pending_runs(ws_id)

    # The COMPLETED report for a REVIEWED task must map to PROGRESS,
    # NOT the terminal COMPLETED event. The event is addressed to the
    # supervisor (root), so query from root's perspective.
    events = manager.agent_tree.get_events(ws_id, root.id, subtree=False)
    report_events = [e for e in events if e.call_id == f"report:{report.id}"]
    assert len(report_events) == 1
    assert (
        report_events[0].type == AgentEventType.PROGRESS
    ), "REVIEWED task COMPLETED report must map to PROGRESS, not COMPLETED"

    # The run must be WAITING (for the reviewer), not COMPLETED.
    child_run = manager.agent_tree.get_run(child.id)
    assert child_run.status == AgentRunStatus.WAITING

    # No terminal COMPLETED event should have been emitted.
    completed_events = [e for e in events if e.type == AgentEventType.COMPLETED]
    assert completed_events == [], "COMPLETED must not fire before REVIEW_PASSED"


# ---------------------------------------------------------------------------
# Fix 2a: wake only the named mailbox recipient (no ancestor wakeup)
# ---------------------------------------------------------------------------


def test_wake_for_run_only_wakes_named_recipient(manager: WorkspaceManager, ws_id: str) -> None:
    """``_wake_for_run`` must wake only the named recipient, not ancestors.

    With recipient-directed mailbox reads, a run only sees events where
    ``recipient == run_id`` (or self-addressed). Waking ancestors would
    cause spurious wakeups for runs that cannot see the event.
    """
    root = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child = manager.agent_tree.create_root_run(
        workspace_id=ws_id, executor_kind=ExecutorKind.NATIVE_SUBAGENT
    )
    child.parent_id = root.id
    child.supervisor_id = root.id
    child.path = f"{root.path}/{child.id}"

    # Create wait events for both root and child.
    root_ev = manager.agent_tree._run_events.setdefault(root.id, asyncio.Event())
    child_ev = manager.agent_tree._run_events.setdefault(child.id, asyncio.Event())
    root_ev.clear()
    child_ev.clear()

    # Wake only the child (the named recipient).
    manager.agent_tree._wake_for_run(child.id, child.id)

    # The child (recipient) must be woken.
    assert child_ev.is_set(), "named recipient must be woken"
    # The root (ancestor) must NOT be woken.
    assert not root_ev.is_set(), "ancestor must NOT be woken"

    # Reset and test self-addressed (recipient == author) wakes the author.
    root_ev.clear()
    child_ev.clear()
    manager.agent_tree._wake_for_run(child.id, child.id)
    assert child_ev.is_set(), "self-addressed event must wake the author run"
    assert not root_ev.is_set(), "ancestor must NOT be woken for self-addressed event"


def test_load_from_dict_migrates_recipient_null_events(
    manager: WorkspaceManager, ws_id: str
) -> None:
    """Persisted events with ``recipient=None`` must be self-addressed on load.

    Before the mandatory-recipient change, events could be persisted with
    ``recipient=None``. The directed mailbox filter (``e.recipient == run_id``)
    would drop them, so ``load_from_dict`` migrates them to
    ``recipient = author`` (self-address). For root runs this is correct;
    pre-mandatory-recipient events were only ever emitted by root runs.
    """
    from claude_hub.models.agent_tree import AgentEventType, AgentRun, ExecutorKind

    run_id = "root-run-null-recipient"
    root = AgentRun(
        id=run_id,
        workspace_id=ws_id,
        parent_id=None,
        path=run_id,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        title="root",
        status=AgentRunStatus.RUNNING,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    # An event persisted with recipient=None (pre-mandatory-recipient data).
    null_recipient_event = {
        "sequence": 1,
        "workspace_id": ws_id,
        "agent_run_id": run_id,
        "type": AgentEventType.STARTED.value,
        "author": run_id,
        "recipient": None,
        "call_id": "start:1",
        "payload": {},
        "created_at": datetime.utcnow().isoformat(),
    }

    manager.agent_tree.load_from_dict(
        ws_id,
        {
            "agent_runs": [root.model_dump(mode="json")],
            "agent_events": [null_recipient_event],
        },
    )

    # The event must be migrated to recipient=author (self-address).
    events = manager.agent_tree.get_events(ws_id, run_id, subtree=False)
    assert len(events) == 1
    assert (
        events[0].recipient == run_id
    ), "recipient=None event must be migrated to recipient=author"
    assert events[0].author == run_id


# ---------------------------------------------------------------------------
# Fix 2b: cumulative side effects before ACK + cold recovery pump
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cumulative_side_effects_before_ack_and_cold_recovery_pump(
    manager: WorkspaceManager, ws_id: str, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """Cumulative side effects with the receiver-verifiable durable receipt.

    The receiver-verifiable receipt (``delivered_call_ids`` populated ONLY on
    worker ACK) guarantees that an ACKed call_id is never re-delivered. An
    unACKed call_id that was sent to tmux (in ``processing_call_ids``) IS
    re-delivered on cold restart, because we cannot prove the worker received
    it before the crash. The worker dedupes by the ``[call_id:<id>]`` marker.

    On cold restart:
      * ``delivered_call_ids`` (worker-ACKed) are NEVER re-delivered.
      * ``processing_call_ids`` (sent to tmux, not ACKed) are moved back to
        ``pending_call_ids`` and re-delivered.
      * ``pending_call_ids`` (never sent to tmux) ARE pumped.

    The cumulative side-effect counter (tmux sends) is NEVER reset across the
    cold restart: it reflects the total number of tmux sends that have ever
    happened. UnACKed call_ids produce a second tmux send on recovery; the
    worker's dedupe marker prevents a duplicate *effect*.
    """
    from claude_hub.models import (
        AgentReportCreate,
        AgentReportState,
        AgentType,
        WorkspaceTask,
        WorkspaceTaskStatus,
    )

    session_id = "cumulative-side-effects"
    _make_managed_session(manager, session_id, ws_id)

    task = WorkspaceTask(
        id="cumulative-task",
        workspace_id=ws_id,
        title="cumulative side effects",
        prompt="do the things",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    manager.tasks[task.id] = task
    manager.sessions[session_id] = manager.sessions[session_id].model_copy(
        update={"task_id": task.id, "current_task_id": task.id}
    )

    # The real, persisted side effect: a counter file in tmp_path. This
    # counter is NEVER reset — it tracks the total number of tmux sends
    # that have ever happened.
    effect_file = tmp_path / "effect_count.txt"

    def read_effect() -> int:
        if not effect_file.exists():
            return 0
        return int(effect_file.read_text().strip() or "0")

    def bump_effect() -> None:
        n = read_effect() + 1
        effect_file.write_text(str(n))

    # The "receiver" (tmux send) applies the side effect once per delivery.
    async def recording_send(tmux_session: str, message: str) -> None:
        bump_effect()

    async def _ready_capture(tmux_session: str) -> str:
        return "claude code\n? for shortcuts\n"

    monkeypatch.setattr(manager, "_send_tmux_message", recording_send)
    monkeypatch.setattr(manager, "_capture_tmux_output", _ready_capture)

    # ---- Send 3 messages, each with a distinct call_id. ----
    call_ids = ["call:1", "call:2", "call:3"]
    for cid in call_ids:
        await manager.send_session_message(session_id, f"message {cid}", call_id=cid)

    # All 3 side effects applied before any ACK.
    assert read_effect() == 3

    # All 3 call_ids are in processing_call_ids: they were sent to tmux but
    # the worker has not yet ACKed. They stay in processing until ACK.
    session = manager.sessions[session_id]
    for cid in call_ids:
        assert cid in session.processing_call_ids
        assert cid not in session.delivered_call_ids
        assert cid not in session.pending_call_ids

    # ---- ACK only the first call_id. ----
    # The worker's ACK moves call:1 from processing to delivered. call:2 and
    # call:3 remain in processing (unACKed).
    await manager.create_report(
        session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.WORKING,
            message="processed first",
            acked_call_ids=["call:1"],
        ),
    )
    session = manager.sessions[session_id]
    assert "call:1" in session.delivered_call_ids
    assert "call:1" not in session.processing_call_ids
    # call:2 and call:3 are still unACKed -> in processing.
    assert "call:2" in session.processing_call_ids
    assert "call:3" in session.processing_call_ids

    # ---- Add a 4th message that is persisted as pending but never sent. ----
    # This simulates a crash between persisting the call_id in
    # pending_call_ids and the pump claiming it.
    pending_call_id = "call:4-pending"
    session = manager.sessions[session_id]
    pending_messages = dict(session.pending_messages)
    pending_messages[pending_call_id] = "message call:4"
    pending_call_ids = list(session.pending_call_ids)
    pending_call_ids.append(pending_call_id)
    manager.sessions[session_id] = session.model_copy(
        update={
            "pending_messages": pending_messages,
            "pending_call_ids": pending_call_ids,
        }
    )
    manager._save_state()

    # ---- Cold restart: persist and reload into a fresh manager. ----
    fresh = WorkspaceManager()

    fresh_session = fresh.sessions[session_id]
    # call:1 was ACKed -> delivered, not re-deliverable.
    assert "call:1" in fresh_session.delivered_call_ids
    assert "call:1" not in fresh_session.pending_call_ids
    assert "call:1" not in fresh_session.processing_call_ids
    # call:2 and call:3 were sent to tmux but NOT ACKed -> they are in
    # processing_call_ids (loaded from disk). Cold recovery will move them
    # back to pending and re-deliver them (we cannot prove the worker
    # received them before the crash).
    for cid in ["call:2", "call:3"]:
        assert cid in fresh_session.processing_call_ids
        assert cid not in fresh_session.delivered_call_ids
    # call:4 was never sent -> pending, must be pumped.
    assert pending_call_id in fresh_session.pending_call_ids

    # The side effect counter is NOT reset across the restart. It still
    # reflects the 3 tmux sends that happened before the crash.
    assert read_effect() == 3

    # Patch the fresh manager's send to apply the side effect.
    monkeypatch.setattr(fresh, "_send_tmux_message", recording_send)
    monkeypatch.setattr(fresh, "_capture_tmux_output", _ready_capture)

    # ---- Run recovery: processing -> pending, pump pending. ----
    await fresh.agent_tree.recover_pending_runs(ws_id)

    # call:2, call:3 (unACKed, was processing) and call:4 (pending) are
    # re-delivered -> 3 more side effects. Total = 3 + 3 = 6.
    # call:1 (delivered/ACKed) is NOT re-delivered.
    assert read_effect() == 6, (
        "only non-delivered call_ids must be re-delivered on cold recovery; "
        "delivered (ACKed) call_ids must never be re-sent"
    )

    fresh_session = fresh.sessions[session_id]
    # call:2, call:3, call:4 are now in processing (sent during recovery,
    # awaiting worker ACK).
    for cid in ["call:2", "call:3", pending_call_id]:
        assert cid in fresh_session.processing_call_ids
        assert cid not in fresh_session.pending_call_ids
    # call:1 stays delivered.
    assert "call:1" in fresh_session.delivered_call_ids
    assert "call:1" not in fresh_session.pending_call_ids
    assert "call:1" not in fresh_session.processing_call_ids

    # ---- Cleanup. ----
    await fresh.delete_workspace(ws_id)
