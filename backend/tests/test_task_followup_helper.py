"""3b-1a: ordinary Task routes followup by current task.session_id."""

from __future__ import annotations

from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest import MonkeyPatch

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
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.models.agent_tree import (
    AgentRun,
    AgentRunStatus,
    ExecutorCapabilities,
    ExecutorKind,
    FollowupRequest,
    ManagedExecutorConfig,
)
from claude_hub.models.task_mailbox import TaskActorRole, TaskEventType
from claude_hub.services.workspace_manager import WorkspaceManager
from claude_hub.services.workspace_manager._constants import DeliveryUncertain

_wm = import_module("claude_hub.services.workspace_manager")


@pytest.fixture()
def state_root(monkeypatch: MonkeyPatch, tmp_path: Path) -> Generator[Path, None, None]:
    root = tmp_path / "workspaces"
    root.mkdir(parents=True)
    index_file = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)

    fake_tab = MagicMock(id="tab-mock", tmux_session="tmux-mock")
    monkeypatch.setattr(_wm.ttyd_manager, "update_tab", AsyncMock(return_value=fake_tab))
    monkeypatch.setattr(
        _wm.ttyd_manager, "ensure_tab_tmux_session", AsyncMock(return_value=fake_tab)
    )
    monkeypatch.setattr(_wm.WorkspaceManager, "_send_tmux_message", AsyncMock())
    monkeypatch.setattr(_wm.WorkspaceManager, "_send_tmux_message_with_receipt", AsyncMock())
    monkeypatch.setattr(_wm.WorkspaceManager, "_ensure_session_ready_for_send", AsyncMock())
    yield root


@pytest.fixture()
def manager_and_workspace(state_root: Path, tmp_path: Path) -> tuple[WorkspaceManager, str]:
    manager = WorkspaceManager()
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = manager.create_workspace(
        WorkspaceCreate(name="followup helper", path=str(repo), target=ExecutionTarget.LOCAL)
    )
    return manager, workspace.id


def _session(
    manager: WorkspaceManager,
    workspace_id: str,
    *,
    session_id: str,
    task_id: str,
    role: WorkspaceSessionRole = WorkspaceSessionRole.WORKER,
) -> ManagedSession:
    now = datetime.utcnow()
    session = ManagedSession(
        id=session_id,
        workspace_id=workspace_id,
        tab_id=f"tab-{session_id}",
        role=role,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="worker",
        workspace_path="/tmp",
        tmux_session=f"tmux-{session_id}",
        target=ExecutionTarget.LOCAL,
        task_id=task_id,
        current_task_id=task_id,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    return session


@pytest.mark.asyncio
async def test_followup_existing_task_routes_to_current_session_id(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    now = datetime.utcnow()
    task = WorkspaceTask(
        id="task-ordinary",
        workspace_id=workspace_id,
        title="ordinary followup",
        prompt="do the work",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id="session-old",
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    _session(manager, workspace_id, session_id="session-old", task_id=task.id)
    _session(manager, workspace_id, session_id="session-new", task_id=task.id)
    manager.tasks[task.id] = task.model_copy(update={"session_id": "session-new"})

    sent: list[tuple[str, str, str | None]] = []

    async def _fake_send(session_id: str, message: str, call_id: str | None = None) -> None:
        sent.append((session_id, message, call_id))

    monkeypatch.setattr(manager, "send_session_message", _fake_send)

    await manager._followup_existing_task(task.id, "please also do Y", call_id="fu-route-1")

    assert sent == [("session-new", "please also do Y", "fu-route-1")]
    assert manager.agent_tree._runs == {}
    assert manager.agent_tree._events.get(workspace_id, []) == []


def _ordinary_working_task(
    manager: WorkspaceManager,
    workspace_id: str,
    *,
    task_id: str = "task-ordinary-working",
    review_cycle: int = 2,
) -> WorkspaceTask:
    now = datetime.utcnow()
    task = WorkspaceTask(
        id=task_id,
        workspace_id=workspace_id,
        title="ordinary working followup",
        prompt="do the work",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id="session-old",
        review_cycle=review_cycle,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    _session(manager, workspace_id, session_id="session-old", task_id=task.id)
    _session(manager, workspace_id, session_id="session-new", task_id=task.id)
    manager.tasks[task.id] = task.model_copy(update={"session_id": "session-new"})
    return manager.tasks[task.id]


def _mailbox_events(manager: WorkspaceManager, workspace_id: str) -> list:
    return list(manager.task_mailbox._events.get(workspace_id, []))


@pytest.mark.asyncio
async def test_followup_task_working_writes_one_mailbox_event_and_routes(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _ordinary_working_task(manager, workspace_id)
    sent: list[tuple[str, str, str | None]] = []

    async def _fake_send(session_id: str, message: str, call_id: str | None = None) -> None:
        sent.append((session_id, message, call_id))

    monkeypatch.setattr(manager, "send_session_message", _fake_send)

    first = await manager.followup_task(
        workspace_id,
        task.id,
        "please also do Y",
        "fu-working-1",
        actor_session_id="session-supervisor",
        actor_role=TaskActorRole.SUPERVISOR,
    )
    events = _mailbox_events(manager, workspace_id)
    assert len(events) == 1
    event = events[0]
    assert event is first
    assert event.type in {TaskEventType.FOLLOWUP, TaskEventType.MESSAGE}
    assert event.task_id == task.id
    assert event.actor_session_id == "session-supervisor"
    assert event.actor_role == TaskActorRole.SUPERVISOR
    assert event.review_cycle == 2
    assert event.call_id == "fu-working-1"
    assert event.payload.get("message") == "please also do Y"
    assert sent == [("session-new", "please also do Y", "fu-working-1")]

    retry = await manager.followup_task(
        workspace_id,
        task.id,
        "please also do Y",
        "fu-working-1",
        actor_session_id="session-supervisor",
        actor_role=TaskActorRole.SUPERVISOR,
    )
    assert retry.sequence == first.sequence
    assert retry.call_id == first.call_id
    assert _mailbox_events(manager, workspace_id) == [first]


@pytest.mark.asyncio
async def test_followup_task_working_persist_rollback_and_conflict(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _ordinary_working_task(manager, workspace_id)
    mailbox = manager.task_mailbox
    prompt_before = task.prompt
    session_before = task.session_id
    status_before = task.status
    pending_before = list(task.pending_call_ids)
    processing_before = list(task.processing_call_ids)
    delivered_before = list(task.delivered_call_ids)
    events_before = list(mailbox._events.get(workspace_id, []))
    call_index_before = dict(mailbox._call_index.get(workspace_id, {}))
    next_seq_before = mailbox._next_seq.get(workspace_id)
    sent: list[tuple[str, str, str | None]] = []

    async def _fake_send(session_id: str, message: str, call_id: str | None = None) -> None:
        sent.append((session_id, message, call_id))

    monkeypatch.setattr(manager, "send_session_message", _fake_send)
    monkeypatch.setattr(
        manager,
        "_save_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        await manager.followup_task(
            workspace_id,
            task.id,
            "please also do Y",
            "fu-working-rollback",
            actor_session_id="session-supervisor",
            actor_role=TaskActorRole.SUPERVISOR,
        )

    assert mailbox._events.get(workspace_id, []) == events_before
    assert mailbox._call_index.get(workspace_id, {}) == call_index_before
    assert mailbox._next_seq.get(workspace_id) == next_seq_before
    rolled = manager.tasks[task.id]
    assert list(rolled.pending_call_ids) == pending_before
    assert list(rolled.processing_call_ids) == processing_before
    assert list(rolled.delivered_call_ids) == delivered_before
    assert rolled.prompt == prompt_before
    assert rolled.status == status_before
    assert rolled.session_id == session_before
    assert sent == []

    monkeypatch.undo()
    monkeypatch.setattr(manager, "send_session_message", _fake_send)

    await manager.followup_task(
        workspace_id,
        task.id,
        "please also do Y",
        "fu-working-conflict",
        actor_session_id="session-supervisor",
        actor_role=TaskActorRole.SUPERVISOR,
    )
    with pytest.raises(ValueError):
        await manager.followup_task(
            workspace_id,
            task.id,
            "a different followup",
            "fu-working-conflict",
            actor_session_id="session-supervisor",
            actor_role=TaskActorRole.SUPERVISOR,
        )


def _followup_kwargs() -> dict[str, object]:
    return {
        "actor_session_id": "session-supervisor",
        "actor_role": TaskActorRole.SUPERVISOR,
    }


@pytest.mark.asyncio
async def test_followup_task_first_save_has_event_and_task_pending(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _ordinary_working_task(manager, workspace_id)
    call_id = "fu-working-same-txn"
    observed: list[tuple[list[str], list[str]]] = []
    real_save = manager._save_state

    def _recording_save() -> None:
        observed.append(
            (
                [item.call_id for item in _mailbox_events(manager, workspace_id)],
                list(manager.tasks[task.id].pending_call_ids),
            )
        )
        real_save()

    async def _fake_send(session_id: str, message: str, call_id: str | None = None) -> None:
        return None

    monkeypatch.setattr(manager, "_save_state", _recording_save)
    monkeypatch.setattr(manager, "send_session_message", _fake_send)

    await manager.followup_task(
        workspace_id,
        task.id,
        "please also do Y",
        call_id,
        **_followup_kwargs(),
    )

    assert observed
    assert call_id in observed[0][0]
    assert call_id in observed[0][1]
    assert len(observed) == 1
    assert call_id in [item.call_id for item in _mailbox_events(manager, workspace_id)]
    assert call_id in manager.tasks[task.id].pending_call_ids


@pytest.mark.asyncio
async def test_followup_task_intent_save_failure_rolls_back_event_and_pending(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _ordinary_working_task(manager, workspace_id)
    call_id = "fu-working-same-txn-fail"
    observed: list[tuple[list[str], list[str]]] = []
    sent: list[tuple[str, str, str | None]] = []

    def _failing_save() -> None:
        observed.append(
            (
                [item.call_id for item in _mailbox_events(manager, workspace_id)],
                list(manager.tasks[task.id].pending_call_ids),
            )
        )
        raise OSError("disk full")

    async def _fake_send(session_id: str, message: str, call_id: str | None = None) -> None:
        sent.append((session_id, message, call_id))

    monkeypatch.setattr(manager, "_save_state", _failing_save)
    monkeypatch.setattr(manager, "send_session_message", _fake_send)

    with pytest.raises(OSError, match="disk full"):
        await manager.followup_task(
            workspace_id,
            task.id,
            "please also do Y",
            call_id,
            **_followup_kwargs(),
        )

    assert observed
    assert call_id in observed[0][0]
    assert call_id in observed[0][1]
    assert _mailbox_events(manager, workspace_id) == []
    assert workspace_id not in manager.task_mailbox._call_index
    assert workspace_id not in manager.task_mailbox._next_seq
    assert call_id not in manager.tasks[task.id].pending_call_ids
    assert sent == []


@pytest.mark.asyncio
async def test_followup_task_post_commit_transport_failure_keeps_intent(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _ordinary_working_task(manager, workspace_id)
    call_id = "fu-working-transport"
    message = "please also do Y"

    async def _boom_send(session_id: str, message: str, call_id: str | None = None) -> None:
        raise DeliveryUncertain("tmux ambiguous")

    monkeypatch.setattr(manager, "send_session_message", _boom_send)

    with pytest.raises(DeliveryUncertain, match="tmux ambiguous"):
        await manager.followup_task(
            workspace_id,
            task.id,
            message,
            call_id,
            **_followup_kwargs(),
        )

    events = _mailbox_events(manager, workspace_id)
    assert [item.call_id for item in events] == [call_id]
    assert call_id in manager.tasks[task.id].pending_call_ids
    assert call_id not in manager.tasks[task.id].processing_call_ids
    assert call_id not in manager.tasks[task.id].delivered_call_ids

    fresh = WorkspaceManager()
    cold_events = _mailbox_events(fresh, workspace_id)
    assert [item.call_id for item in cold_events] == [call_id]
    assert call_id in fresh.tasks[task.id].pending_call_ids
    sent: list[tuple[str, str, str | None]] = []

    async def _ok_send(session_id: str, text: str, call_id: str | None = None) -> None:
        sent.append((session_id, text, call_id))

    monkeypatch.setattr(fresh, "send_session_message", _ok_send)
    retry = await fresh.followup_task(
        workspace_id,
        task.id,
        message,
        call_id,
        **_followup_kwargs(),
    )
    assert retry.sequence == cold_events[0].sequence
    assert [item.call_id for item in _mailbox_events(fresh, workspace_id)] == [call_id]
    assert sent == [("session-new", message, call_id)]

    with pytest.raises(ValueError):
        await fresh.followup_task(
            workspace_id,
            task.id,
            "a different followup",
            call_id,
            **_followup_kwargs(),
        )
    assert [item.call_id for item in _mailbox_events(fresh, workspace_id)] == [call_id]


_RUN_STAMP = datetime(2026, 8, 22, 18, 0, 0)
_RUN_CAPS = ExecutorCapabilities(
    available=True,
    supports_spawn=True,
    supports_send=True,
    supports_followup=True,
    supports_interrupt=True,
    durable_status=True,
)
_RUN_CONFIG = ManagedExecutorConfig(agent_type=AgentType.CLAUDE, solo_mode=True)


def _seed_linked_managed_runs(
    manager: WorkspaceManager,
    workspace_id: str,
    task: WorkspaceTask,
) -> tuple[AgentRun, AgentRun]:
    resident = _session(
        manager,
        workspace_id,
        session_id="session-resident",
        task_id="",
        role=WorkspaceSessionRole.RESIDENT,
    )
    root = AgentRun(
        id="run-root",
        workspace_id=workspace_id,
        parent_id=None,
        path="run-root",
        supervisor_id=None,
        executor_kind=ExecutorKind.RESIDENT_ROOT,
        executor_config=_RUN_CONFIG,
        executor_capabilities=_RUN_CAPS,
        status=AgentRunStatus.RUNNING,
        context_ref=resident.id,
        ack_sequence=1,
        last_task_message="root-old",
        title="resident-root",
        created_at=_RUN_STAMP,
        updated_at=_RUN_STAMP,
    )
    child = AgentRun(
        id="run-child",
        workspace_id=workspace_id,
        parent_id=root.id,
        path="run-root/run-child",
        supervisor_id=root.id,
        executor_kind=ExecutorKind.MANAGED_TASK,
        executor_config=_RUN_CONFIG,
        executor_capabilities=_RUN_CAPS,
        status=AgentRunStatus.WAITING,
        context_ref=task.id,
        ack_sequence=4,
        last_task_message="child-old",
        title="managed-child",
        created_at=_RUN_STAMP,
        updated_at=_RUN_STAMP,
    )
    manager.agent_tree._runs[root.id] = root
    manager.agent_tree._runs[child.id] = child
    current = manager.tasks[task.id]
    manager.tasks[current.id] = current.model_copy(update={"agent_run_id": child.id})
    return manager.agent_tree._runs[root.id], manager.agent_tree._runs[child.id]


def _run_bytes(run: AgentRun) -> dict[str, object]:
    return run.model_dump(mode="json")


def _tree_bytes(manager: WorkspaceManager, workspace_id: str) -> dict[str, object]:
    tree = manager.agent_tree
    return {
        "events": [item.model_dump() for item in tree._events.get(workspace_id, [])],
        "call_index": {
            key: {
                "action": value.get("action"),
                "target": value.get("target"),
                "fingerprint": value.get("fingerprint"),
                "event_call_id": getattr(value.get("event"), "call_id", None),
            }
            for key, value in tree._call_index.get(workspace_id, {}).items()
        },
    }


def _followup_request(
    workspace_id: str,
    *,
    recipient_id: str,
    author_id: str,
    message: str,
    call_id: str,
    correlation_id: str | None = None,
) -> FollowupRequest:
    return FollowupRequest(
        workspace_id=workspace_id,
        recipient_id=recipient_id,
        author_id=author_id,
        message=message,
        call_id=call_id,
        correlation_id=correlation_id,
    )


@pytest.mark.asyncio
async def test_agent_tree_followup_managed_task_is_task_canonical_only(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _ordinary_working_task(manager, workspace_id)
    root, child = _seed_linked_managed_runs(manager, workspace_id, task)
    call_id = "fu-ac2-gate"
    message = "please also do Y"
    run_bytes = {
        root.id: _run_bytes(root),
        child.id: _run_bytes(child),
    }
    tree_bytes = _tree_bytes(manager, workspace_id)
    pending_before = list(manager.tasks[task.id].pending_call_ids)
    sent: list[tuple[str, str, str | None]] = []

    async def _fake_send(session_id: str, text: str, call_id: str | None = None) -> None:
        sent.append((session_id, text, call_id))

    real_save = manager._save_state
    monkeypatch.setattr(manager, "send_session_message", _fake_send)
    monkeypatch.setattr(
        manager,
        "_save_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        await manager.agent_tree.followup(
            _followup_request(
                workspace_id,
                recipient_id=child.id,
                author_id=root.id,
                message=message,
                call_id=call_id,
            )
        )

    assert _mailbox_events(manager, workspace_id) == []
    assert workspace_id not in manager.task_mailbox._call_index
    assert workspace_id not in manager.task_mailbox._next_seq
    assert list(manager.tasks[task.id].pending_call_ids) == pending_before
    assert _run_bytes(manager.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes
    assert sent == []

    monkeypatch.setattr(manager, "_save_state", real_save)

    projected = await manager.agent_tree.followup(
        _followup_request(
            workspace_id,
            recipient_id=child.id,
            author_id=root.id,
            message=message,
            call_id=call_id,
        )
    )
    events = _mailbox_events(manager, workspace_id)
    assert len(events) == 1
    event = events[0]
    assert event.type == TaskEventType.FOLLOWUP
    assert event.call_id == call_id
    assert event.task_id == task.id
    assert event.actor_role == TaskActorRole.SUPERVISOR
    assert event.actor_session_id == "session-resident"
    assert event.payload.get("message") == message
    assert event.payload.get("compat_author_run_id") == root.id
    assert event.payload.get("correlation_id") is None
    assert projected.call_id == call_id
    assert projected.payload.get("followup") is True
    assert call_id not in manager.agent_tree._call_index.get(workspace_id, {})
    assert all(item.call_id != call_id for item in manager.agent_tree._events.get(workspace_id, []))
    assert _run_bytes(manager.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes
    assert sent == [("session-new", message, call_id)]

    fresh = WorkspaceManager()
    assert _run_bytes(fresh.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(fresh.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(fresh, workspace_id) == tree_bytes
    cold_events = _mailbox_events(fresh, workspace_id)
    assert [item.sequence for item in cold_events] == [event.sequence]
    assert cold_events[0].type == TaskEventType.FOLLOWUP
    sent.clear()
    monkeypatch.setattr(fresh, "send_session_message", _fake_send)
    cold = await fresh.agent_tree.followup(
        _followup_request(
            workspace_id,
            recipient_id=child.id,
            author_id=root.id,
            message=message,
            call_id=call_id,
        )
    )
    assert cold.sequence == event.sequence
    assert [item.call_id for item in _mailbox_events(fresh, workspace_id)] == [call_id]
    assert _run_bytes(fresh.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(fresh.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(fresh, workspace_id) == tree_bytes
    assert sent == [("session-new", message, call_id)]

    deleted_run_bytes = {
        root.id: _run_bytes(fresh.agent_tree._runs[root.id]),
        child.id: _run_bytes(fresh.agent_tree._runs[child.id]),
    }
    deleted_tree_bytes = _tree_bytes(fresh, workspace_id)
    del fresh.tasks[task.id]
    with pytest.raises(KeyError):
        await fresh.agent_tree.followup(
            _followup_request(
                workspace_id,
                recipient_id=child.id,
                author_id=root.id,
                message="recreate must not write context_ref",
                call_id="fu-ac2-missing-task",
            )
        )
    assert task.id not in fresh.tasks
    assert _run_bytes(fresh.agent_tree._runs[root.id]) == deleted_run_bytes[root.id]
    assert _run_bytes(fresh.agent_tree._runs[child.id]) == deleted_run_bytes[child.id]
    assert _tree_bytes(fresh, workspace_id) == deleted_tree_bytes
    assert [item.call_id for item in _mailbox_events(fresh, workspace_id)] == [call_id]


@pytest.mark.asyncio
async def test_agent_tree_followup_managed_task_fingerprint_keeps_author_and_correlation(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    task = _ordinary_working_task(manager, workspace_id)
    root, child = _seed_linked_managed_runs(manager, workspace_id, task)
    call_id = "fu-ac2-fingerprint"
    message = "please also do Y"
    tree_bytes = _tree_bytes(manager, workspace_id)
    run_bytes = {
        root.id: _run_bytes(root),
        child.id: _run_bytes(child),
    }
    sent: list[tuple[str, str, str | None]] = []

    async def _fake_send(session_id: str, text: str, call_id: str | None = None) -> None:
        sent.append((session_id, text, call_id))

    monkeypatch.setattr(manager, "send_session_message", _fake_send)

    first = await manager.agent_tree.followup(
        _followup_request(
            workspace_id,
            recipient_id=child.id,
            author_id=root.id,
            message=message,
            call_id=call_id,
            correlation_id="corr-a",
        )
    )
    events = _mailbox_events(manager, workspace_id)
    assert len(events) == 1
    assert events[0].payload.get("compat_author_run_id") == root.id
    assert events[0].payload.get("correlation_id") == "corr-a"
    assert events[0].actor_session_id == "session-resident"
    assert _tree_bytes(manager, workspace_id) == tree_bytes
    assert _run_bytes(manager.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]

    with pytest.raises(ValueError):
        await manager.agent_tree.followup(
            _followup_request(
                workspace_id,
                recipient_id=child.id,
                author_id=root.id,
                message=message,
                call_id=call_id,
                correlation_id="corr-b",
            )
        )
    with pytest.raises(ValueError):
        await manager.agent_tree.followup(
            _followup_request(
                workspace_id,
                recipient_id=child.id,
                author_id=child.id,
                message=message,
                call_id=call_id,
                correlation_id="corr-a",
            )
        )
    assert [item.call_id for item in _mailbox_events(manager, workspace_id)] == [call_id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes
    assert _run_bytes(manager.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert manager.agent_tree._events.get(workspace_id, []) == []
    assert manager.agent_tree._call_index.get(workspace_id, {}) == {}

    fresh = WorkspaceManager()
    assert _run_bytes(fresh.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(fresh.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(fresh, workspace_id) == tree_bytes
    monkeypatch.setattr(fresh, "send_session_message", _fake_send)
    retry = await fresh.agent_tree.followup(
        _followup_request(
            workspace_id,
            recipient_id=child.id,
            author_id=root.id,
            message=message,
            call_id=call_id,
            correlation_id="corr-a",
        )
    )
    assert retry.sequence == first.sequence
    assert [item.call_id for item in _mailbox_events(fresh, workspace_id)] == [call_id]
    assert _tree_bytes(fresh, workspace_id) == tree_bytes
    assert _run_bytes(fresh.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(fresh.agent_tree._runs[child.id]) == run_bytes[child.id]


@pytest.mark.asyncio
async def test_agent_tree_followup_resolves_actor_session_from_author_task(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    child_task = _ordinary_working_task(manager, workspace_id, task_id="task-child")
    now = datetime.utcnow()
    author_task = WorkspaceTask(
        id="task-author",
        workspace_id=workspace_id,
        title="author task",
        prompt="supervise",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id="session-author",
        created_at=now,
        updated_at=now,
    )
    manager.tasks[author_task.id] = author_task
    _session(manager, workspace_id, session_id="session-author", task_id=author_task.id)
    author = AgentRun(
        id="run-author",
        workspace_id=workspace_id,
        parent_id="run-grand",
        path="run-grand/run-author",
        supervisor_id="run-grand",
        executor_kind=ExecutorKind.MANAGED_TASK,
        executor_config=_RUN_CONFIG,
        executor_capabilities=_RUN_CAPS,
        status=AgentRunStatus.RUNNING,
        context_ref=author_task.id,
        title="author",
        created_at=_RUN_STAMP,
        updated_at=_RUN_STAMP,
    )
    child = AgentRun(
        id="run-child-from-author-task",
        workspace_id=workspace_id,
        parent_id=author.id,
        path="run-grand/run-author/run-child-from-author-task",
        supervisor_id=author.id,
        executor_kind=ExecutorKind.MANAGED_TASK,
        executor_config=_RUN_CONFIG,
        executor_capabilities=_RUN_CAPS,
        status=AgentRunStatus.RUNNING,
        context_ref=child_task.id,
        title="child",
        created_at=_RUN_STAMP,
        updated_at=_RUN_STAMP,
    )
    manager.agent_tree._runs[author.id] = author
    manager.agent_tree._runs[child.id] = child
    manager.tasks[child_task.id] = child_task.model_copy(update={"agent_run_id": child.id})
    manager.tasks[author_task.id] = author_task.model_copy(update={"agent_run_id": author.id})
    monkeypatch.setattr(manager, "send_session_message", AsyncMock())

    await manager.agent_tree.followup(
        _followup_request(
            workspace_id,
            recipient_id=child.id,
            author_id=author.id,
            message="from author task session",
            call_id="fu-actor-from-task",
        )
    )
    events = _mailbox_events(manager, workspace_id)
    assert len(events) == 1
    assert events[0].actor_session_id == "session-author"
    assert manager.agent_tree._events.get(workspace_id, []) == []


@pytest.mark.asyncio
async def test_agent_tree_followup_ignores_stale_context_ref_when_task_is_linked(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    wrong = _ordinary_working_task(manager, workspace_id, task_id="task-wrong")
    linked = _ordinary_working_task(manager, workspace_id, task_id="task-linked")
    root, child = _seed_linked_managed_runs(manager, workspace_id, linked)
    manager.tasks[linked.id] = manager.tasks[linked.id].model_copy(
        update={"agent_run_id": child.id}
    )
    manager.agent_tree._runs[child.id] = child.model_copy(update={"context_ref": wrong.id})
    child = manager.agent_tree._runs[child.id]
    run_bytes = {
        root.id: _run_bytes(root),
        child.id: _run_bytes(child),
    }
    tree_bytes = _tree_bytes(manager, workspace_id)
    monkeypatch.setattr(manager, "send_session_message", AsyncMock())

    await manager.agent_tree.followup(
        _followup_request(
            workspace_id,
            recipient_id=child.id,
            author_id=root.id,
            message="must hit linked task",
            call_id="fu-stale-context-ref",
        )
    )
    events = _mailbox_events(manager, workspace_id)
    assert len(events) == 1
    assert events[0].task_id == linked.id
    assert events[0].task_id != wrong.id
    assert _run_bytes(manager.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes


@pytest.mark.asyncio
async def test_agent_tree_followup_rejects_conflict_context_ref_without_linked_task(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    wrong = _ordinary_working_task(manager, workspace_id, task_id="task-orphan-wrong")
    root, child = _seed_linked_managed_runs(manager, workspace_id, wrong)
    manager.tasks[wrong.id] = manager.tasks[wrong.id].model_copy(update={"agent_run_id": None})
    manager.agent_tree._runs[child.id] = child.model_copy(update={"context_ref": wrong.id})
    child = manager.agent_tree._runs[child.id]
    run_bytes = {
        root.id: _run_bytes(root),
        child.id: _run_bytes(child),
    }
    tree_bytes = _tree_bytes(manager, workspace_id)
    sent: list[tuple[str, str, str | None]] = []

    async def _fake_send(session_id: str, text: str, call_id: str | None = None) -> None:
        sent.append((session_id, text, call_id))

    monkeypatch.setattr(manager, "send_session_message", _fake_send)

    with pytest.raises(KeyError):
        await manager.agent_tree.followup(
            _followup_request(
                workspace_id,
                recipient_id=child.id,
                author_id=root.id,
                message="must not retarget ordinary task",
                call_id="fu-conflict-context-ref",
            )
        )

    assert _mailbox_events(manager, workspace_id) == []
    assert workspace_id not in manager.task_mailbox._call_index
    assert sent == []
    assert _run_bytes(manager.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes
    assert child.context_ref == wrong.id
    assert manager.tasks[wrong.id].agent_run_id is None


@pytest.mark.asyncio
async def test_followup_actor_session_ignores_stale_author_context_ref(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    child_task = _ordinary_working_task(manager, workspace_id, task_id="task-child-actor")
    now = datetime.utcnow()
    wrong_task = WorkspaceTask(
        id="task-author-wrong",
        workspace_id=workspace_id,
        title="wrong author task",
        prompt="not the author",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id="session-wrong",
        created_at=now,
        updated_at=now,
    )
    linked_author_task = WorkspaceTask(
        id="task-author-linked",
        workspace_id=workspace_id,
        title="linked author task",
        prompt="supervise",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id="session-linked-author",
        created_at=now,
        updated_at=now,
    )
    manager.tasks[wrong_task.id] = wrong_task
    manager.tasks[linked_author_task.id] = linked_author_task
    _session(manager, workspace_id, session_id="session-wrong", task_id=wrong_task.id)
    _session(
        manager, workspace_id, session_id="session-linked-author", task_id=linked_author_task.id
    )
    author = AgentRun(
        id="run-author-stale-ref",
        workspace_id=workspace_id,
        parent_id="run-grand",
        path="run-grand/run-author-stale-ref",
        supervisor_id="run-grand",
        executor_kind=ExecutorKind.MANAGED_TASK,
        executor_config=_RUN_CONFIG,
        executor_capabilities=_RUN_CAPS,
        status=AgentRunStatus.RUNNING,
        context_ref="session-wrong",
        title="author-stale",
        created_at=_RUN_STAMP,
        updated_at=_RUN_STAMP,
    )
    child = AgentRun(
        id="run-child-actor-stale",
        workspace_id=workspace_id,
        parent_id=author.id,
        path="run-grand/run-author-stale-ref/run-child-actor-stale",
        supervisor_id=author.id,
        executor_kind=ExecutorKind.MANAGED_TASK,
        executor_config=_RUN_CONFIG,
        executor_capabilities=_RUN_CAPS,
        status=AgentRunStatus.RUNNING,
        context_ref=child_task.id,
        title="child",
        created_at=_RUN_STAMP,
        updated_at=_RUN_STAMP,
    )
    manager.agent_tree._runs[author.id] = author
    manager.agent_tree._runs[child.id] = child
    manager.tasks[child_task.id] = child_task.model_copy(update={"agent_run_id": child.id})
    manager.tasks[linked_author_task.id] = linked_author_task.model_copy(
        update={"agent_run_id": author.id}
    )
    run_bytes = {
        author.id: _run_bytes(author),
        child.id: _run_bytes(child),
    }
    tree_bytes = _tree_bytes(manager, workspace_id)
    monkeypatch.setattr(manager, "send_session_message", AsyncMock())

    await manager.agent_tree.followup(
        _followup_request(
            workspace_id,
            recipient_id=child.id,
            author_id=author.id,
            message="actor session from linked author task",
            call_id="fu-actor-stale-ref",
        )
    )
    events = _mailbox_events(manager, workspace_id)
    assert len(events) == 1
    assert events[0].actor_session_id == "session-linked-author"
    assert events[0].actor_session_id != "session-wrong"
    assert _run_bytes(manager.agent_tree._runs[author.id]) == run_bytes[author.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes

    manager.agent_tree._runs[author.id] = author.model_copy(update={"context_ref": wrong_task.id})
    author = manager.agent_tree._runs[author.id]
    run_bytes[author.id] = _run_bytes(author)
    await manager.agent_tree.followup(
        _followup_request(
            workspace_id,
            recipient_id=child.id,
            author_id=author.id,
            message="actor session still from linked author task",
            call_id="fu-actor-stale-task-ref",
        )
    )
    second = [
        item
        for item in _mailbox_events(manager, workspace_id)
        if item.call_id == "fu-actor-stale-task-ref"
    ]
    assert len(second) == 1
    assert second[0].actor_session_id == "session-linked-author"
    assert _run_bytes(manager.agent_tree._runs[author.id]) == run_bytes[author.id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes


@pytest.mark.asyncio
async def test_followup_rejects_ambiguous_linked_tasks_even_when_context_ref_selects_one(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    first = _ordinary_working_task(manager, workspace_id, task_id="task-linked-a")
    second = _ordinary_working_task(manager, workspace_id, task_id="task-linked-b")
    root, child = _seed_linked_managed_runs(manager, workspace_id, first)
    manager.tasks[first.id] = manager.tasks[first.id].model_copy(update={"agent_run_id": child.id})
    manager.tasks[second.id] = manager.tasks[second.id].model_copy(
        update={"agent_run_id": child.id}
    )
    manager.agent_tree._runs[child.id] = child.model_copy(update={"context_ref": first.id})
    child = manager.agent_tree._runs[child.id]
    run_bytes = {
        root.id: _run_bytes(root),
        child.id: _run_bytes(child),
    }
    tree_bytes = _tree_bytes(manager, workspace_id)
    sent: list[tuple[str, str, str | None]] = []

    async def _fake_send(session_id: str, text: str, call_id: str | None = None) -> None:
        sent.append((session_id, text, call_id))

    monkeypatch.setattr(manager, "send_session_message", _fake_send)

    with pytest.raises(ValueError, match="canonical Task.agent_run_id linkage must be unique"):
        await manager.agent_tree.followup(
            _followup_request(
                workspace_id,
                recipient_id=child.id,
                author_id=root.id,
                message="ambiguous linked tasks",
                call_id="fu-ambiguous-linked",
            )
        )

    assert _mailbox_events(manager, workspace_id) == []
    assert workspace_id not in manager.task_mailbox._call_index
    assert sent == []
    assert _run_bytes(manager.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes


def _set_task_status(
    manager: WorkspaceManager,
    task_id: str,
    status: WorkspaceTaskStatus,
    **updates: object,
) -> WorkspaceTask:
    current = manager.tasks[task_id]
    manager.tasks[task_id] = current.model_copy(update={"status": status, **updates})
    return manager.tasks[task_id]


def _task_snapshot(manager: WorkspaceManager, task_id: str) -> dict[str, object]:
    task = manager.tasks[task_id]
    return {
        "dump": task.model_dump(mode="json"),
        "pending": list(task.pending_call_ids),
        "processing": list(task.processing_call_ids),
        "delivered": list(task.delivered_call_ids),
        "uncertain": list(task.uncertain_call_ids),
        "prompt": task.prompt,
        "status": task.status,
    }


def _continue_reports(manager: WorkspaceManager, task_id: str, message: str | None = None) -> list:
    reports = [item for item in manager.reports.values() if item.task_id == task_id]
    if message is not None:
        reports = [item for item in reports if item.message == message]
    return reports


@pytest.mark.asyncio
async def test_followup_task_todo_and_queued_marker_persists_before_retry(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    todo = _ordinary_working_task(manager, workspace_id, task_id="task-todo")
    queued = _ordinary_working_task(manager, workspace_id, task_id="task-queued")
    _set_task_status(manager, todo.id, WorkspaceTaskStatus.TODO, session_id=None)
    _set_task_status(manager, queued.id, WorkspaceTaskStatus.QUEUED, session_id=None)
    root, child = _seed_linked_managed_runs(manager, workspace_id, todo)
    run_bytes = {root.id: _run_bytes(root), child.id: _run_bytes(child)}
    tree_bytes = _tree_bytes(manager, workspace_id)
    started: list[str] = []
    first_save: list[tuple[str, list[str], list[str]]] = []
    real_save = manager._save_state

    def _recording_save() -> None:
        first_save.append(
            (
                manager.tasks[todo.id].prompt,
                list(manager.tasks[todo.id].pending_call_ids),
                [item.call_id for item in _mailbox_events(manager, workspace_id)],
            )
        )
        real_save()

    async def _fake_start(task_id: str, payload: object = None) -> None:
        started.append(task_id)

    monkeypatch.setattr(manager, "_save_state", _recording_save)
    monkeypatch.setattr(manager, "start_task", _fake_start)
    monkeypatch.setattr(manager, "send_session_message", AsyncMock())
    monkeypatch.setattr(manager, "continue_task", AsyncMock())

    await manager.followup_task(
        workspace_id,
        todo.id,
        "todo followup",
        "fu-todo",
        **_followup_kwargs(),
    )
    assert first_save
    assert "[followup]" in first_save[0][0]
    assert "[call_id:fu-todo]" in first_save[0][0]
    assert "fu-todo" in first_save[0][1]
    assert first_save[0][2] == ["fu-todo"]
    assert started == [todo.id]

    await manager.followup_task(
        workspace_id,
        queued.id,
        "queued followup",
        "fu-queued",
        **_followup_kwargs(),
    )
    assert started == [todo.id]
    assert manager.tasks[queued.id].status == WorkspaceTaskStatus.QUEUED

    fresh = WorkspaceManager()
    assert "[followup]" in fresh.tasks[todo.id].prompt
    assert "[call_id:fu-todo]" in fresh.tasks[todo.id].prompt
    assert fresh.tasks[todo.id].prompt.count("[followup]") == 1
    assert "fu-todo" in fresh.tasks[todo.id].pending_call_ids
    assert "[followup]" in fresh.tasks[queued.id].prompt
    assert "[call_id:fu-queued]" in fresh.tasks[queued.id].prompt
    assert fresh.tasks[queued.id].prompt.count("[followup]") == 1
    assert "fu-queued" in fresh.tasks[queued.id].pending_call_ids
    assert [item.call_id for item in _mailbox_events(fresh, workspace_id)] == [
        "fu-todo",
        "fu-queued",
    ]
    assert [item.sequence for item in _mailbox_events(fresh, workspace_id)] == [1, 2]
    assert _run_bytes(fresh.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(fresh.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(fresh, workspace_id) == tree_bytes


@pytest.mark.asyncio
async def test_followup_task_todo_precommit_save_fault_rolls_back_prompt(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    todo = _ordinary_working_task(manager, workspace_id, task_id="task-todo-rollback")
    _set_task_status(manager, todo.id, WorkspaceTaskStatus.TODO, session_id=None)
    root, child = _seed_linked_managed_runs(manager, workspace_id, todo)
    before = _task_snapshot(manager, todo.id)
    run_bytes = {root.id: _run_bytes(root), child.id: _run_bytes(child)}
    tree_bytes = _tree_bytes(manager, workspace_id)
    mailbox = manager.task_mailbox
    started: list[str] = []

    async def _fake_start(task_id: str, payload: object = None) -> None:
        started.append(task_id)

    monkeypatch.setattr(
        manager,
        "_save_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(manager, "start_task", _fake_start)
    monkeypatch.setattr(manager, "send_session_message", AsyncMock())
    monkeypatch.setattr(manager, "continue_task", AsyncMock())

    with pytest.raises(OSError, match="disk full"):
        await manager.followup_task(
            workspace_id,
            todo.id,
            "todo followup",
            "fu-todo-rollback",
            **_followup_kwargs(),
        )

    assert started == []
    assert _mailbox_events(manager, workspace_id) == []
    assert workspace_id not in mailbox._call_index
    assert workspace_id not in mailbox._next_seq
    assert _task_snapshot(manager, todo.id) == before
    assert _run_bytes(manager.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes


@pytest.mark.asyncio
async def test_followup_task_done_is_rejected_with_zero_writes(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    done = _ordinary_working_task(manager, workspace_id, task_id="task-done")
    _set_task_status(manager, done.id, WorkspaceTaskStatus.DONE)
    root, child = _seed_linked_managed_runs(manager, workspace_id, done)
    before = _task_snapshot(manager, done.id)
    run_bytes = {root.id: _run_bytes(root), child.id: _run_bytes(child)}
    tree_bytes = _tree_bytes(manager, workspace_id)
    mailbox = manager.task_mailbox
    sent: list[tuple[str, str, str | None]] = []
    started: list[str] = []
    continued: list[str] = []

    async def _fake_send(session_id: str, message: str, call_id: str | None = None) -> None:
        sent.append((session_id, message, call_id))

    async def _fake_start(task_id: str, payload: object = None) -> None:
        started.append(task_id)

    async def _fake_continue(
        task_id: str, payload: object = None, call_id: str | None = None
    ) -> None:
        continued.append(task_id)

    monkeypatch.setattr(manager, "send_session_message", _fake_send)
    monkeypatch.setattr(manager, "start_task", _fake_start)
    monkeypatch.setattr(manager, "continue_task", _fake_continue)

    with pytest.raises(RuntimeError, match="Done tasks cannot receive followup"):
        await manager.followup_task(
            workspace_id,
            done.id,
            "done followup",
            "fu-done",
            **_followup_kwargs(),
        )

    assert sent == []
    assert started == []
    assert continued == []
    assert _mailbox_events(manager, workspace_id) == []
    assert workspace_id not in mailbox._call_index
    assert workspace_id not in mailbox._next_seq
    assert _task_snapshot(manager, done.id) == before
    assert _run_bytes(manager.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes


@pytest.mark.asyncio
async def test_followup_task_review_real_continue_and_cold_retry(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    review = _ordinary_working_task(manager, workspace_id, task_id="task-review")
    _set_task_status(manager, review.id, WorkspaceTaskStatus.REVIEW)
    session = manager.sessions["session-new"]
    manager.sessions["session-new"] = session.model_copy(
        update={"role": WorkspaceSessionRole.ORCHESTRATOR, "task_id": review.id}
    )
    root, child = _seed_linked_managed_runs(manager, workspace_id, review)
    run_bytes = {root.id: _run_bytes(root), child.id: _run_bytes(child)}
    tree_bytes = _tree_bytes(manager, workspace_id)
    continue_calls: list[str] = []
    real_continue = manager.continue_task

    async def _spy_continue(
        task_id: str, payload: object = None, call_id: str | None = None
    ) -> object:
        continue_calls.append(task_id)
        return await real_continue(task_id, payload, call_id=call_id)

    monkeypatch.setattr(manager, "continue_task", _spy_continue)

    await manager.followup_task(
        workspace_id,
        review.id,
        "review followup",
        "fu-review",
        **_followup_kwargs(),
    )
    assert continue_calls == [review.id]
    assert manager.tasks[review.id].status == WorkspaceTaskStatus.WORKING
    continue_reports = _continue_reports(manager, review.id)
    assert len(continue_reports) == 1
    expected_ids = ["fu-review", f"report:{continue_reports[0].id}"]
    assert [item.call_id for item in _mailbox_events(manager, workspace_id)] == expected_ids
    assert _run_bytes(manager.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes

    fresh = WorkspaceManager()
    assert fresh.tasks[review.id].status == WorkspaceTaskStatus.WORKING
    assert [item.call_id for item in _mailbox_events(fresh, workspace_id)] == expected_ids
    assert len(_continue_reports(fresh, review.id)) == 1
    assert _continue_reports(fresh, review.id)[0].id == continue_reports[0].id
    continue_calls.clear()
    real_fresh_continue = fresh.continue_task

    async def _spy_fresh_continue(
        task_id: str, payload: object = None, call_id: str | None = None
    ) -> object:
        continue_calls.append(task_id)
        return await real_fresh_continue(task_id, payload, call_id=call_id)

    monkeypatch.setattr(fresh, "continue_task", _spy_fresh_continue)
    retry = await fresh.followup_task(
        workspace_id,
        review.id,
        "review followup",
        "fu-review",
        **_followup_kwargs(),
    )
    assert retry.call_id == "fu-review"
    assert continue_calls == []
    assert [item.call_id for item in _mailbox_events(fresh, workspace_id)] == expected_ids
    assert len(_continue_reports(fresh, review.id)) == 1
    assert _continue_reports(fresh, review.id)[0].id == continue_reports[0].id
    assert _run_bytes(fresh.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(fresh.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(fresh, workspace_id) == tree_bytes


@pytest.mark.asyncio
async def test_direct_continue_task_bridges_report_event_live_and_cold(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    review = _ordinary_working_task(manager, workspace_id, task_id="task-direct-continue")
    _set_task_status(manager, review.id, WorkspaceTaskStatus.REVIEW)
    session = manager.sessions["session-new"]
    manager.sessions["session-new"] = session.model_copy(
        update={"role": WorkspaceSessionRole.ORCHESTRATOR, "task_id": review.id}
    )
    root, child = _seed_linked_managed_runs(manager, workspace_id, review)
    run_bytes = {root.id: _run_bytes(root), child.id: _run_bytes(child)}
    tree_bytes = _tree_bytes(manager, workspace_id)
    monkeypatch.setattr(manager, "send_session_message", AsyncMock())

    await manager.continue_task(review.id)
    reports = _continue_reports(manager, review.id)
    assert len(reports) == 1
    report_call = f"report:{reports[0].id}"
    assert [item.call_id for item in _mailbox_events(manager, workspace_id)] == [report_call]
    assert manager.tasks[review.id].status == WorkspaceTaskStatus.WORKING
    assert _run_bytes(manager.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes

    fresh = WorkspaceManager()
    assert [item.call_id for item in _mailbox_events(fresh, workspace_id)] == [report_call]
    assert len(_continue_reports(fresh, review.id)) == 1
    assert _continue_reports(fresh, review.id)[0].id == reports[0].id
    assert _run_bytes(fresh.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(fresh.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(fresh, workspace_id) == tree_bytes

    with pytest.raises(RuntimeError, match="Only review tasks can continue"):
        await fresh.continue_task(review.id)
    assert [item.call_id for item in _mailbox_events(fresh, workspace_id)] == [report_call]
    assert len(_continue_reports(fresh, review.id)) == 1


@pytest.mark.asyncio
async def test_followup_task_outbox_states_same_process_and_cold_retry(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    pending = _ordinary_working_task(manager, workspace_id, task_id="task-pending")
    delivered = _ordinary_working_task(manager, workspace_id, task_id="task-delivered")
    processing = _ordinary_working_task(manager, workspace_id, task_id="task-processing")
    uncertain = _ordinary_working_task(manager, workspace_id, task_id="task-uncertain")
    _set_task_status(
        manager, delivered.id, WorkspaceTaskStatus.WORKING, delivered_call_ids=["fu-del"]
    )
    _set_task_status(
        manager, processing.id, WorkspaceTaskStatus.WORKING, processing_call_ids=["fu-proc"]
    )
    _set_task_status(
        manager, uncertain.id, WorkspaceTaskStatus.WORKING, uncertain_call_ids=["fu-unc"]
    )
    root, child = _seed_linked_managed_runs(manager, workspace_id, pending)
    run_bytes = {root.id: _run_bytes(root), child.id: _run_bytes(child)}
    tree_bytes = _tree_bytes(manager, workspace_id)
    transport: list[object] = []

    async def _count_receipt(*args: object, **kwargs: object) -> None:
        transport.append((args, kwargs))

    monkeypatch.setattr(manager, "_send_tmux_message_with_receipt", _count_receipt)

    await manager.followup_task(
        workspace_id,
        delivered.id,
        "already delivered",
        "fu-del",
        **_followup_kwargs(),
    )
    await manager.followup_task(
        workspace_id,
        processing.id,
        "already processing",
        "fu-proc",
        **_followup_kwargs(),
    )
    await manager.followup_task(
        workspace_id,
        uncertain.id,
        "still uncertain",
        "fu-unc",
        **_followup_kwargs(),
    )
    await manager.followup_task(
        workspace_id,
        pending.id,
        "pending followup",
        "fu-pend",
        **_followup_kwargs(),
    )
    first_transport = len(transport)
    assert first_transport == 1
    assert manager.tasks[delivered.id].delivered_call_ids == ["fu-del"]
    assert manager.tasks[processing.id].processing_call_ids == ["fu-proc"]
    assert manager.tasks[uncertain.id].uncertain_call_ids == ["fu-unc"]

    await manager.followup_task(
        workspace_id,
        pending.id,
        "pending followup",
        "fu-pend",
        **_followup_kwargs(),
    )
    assert len(transport) == first_transport
    assert [item.call_id for item in _mailbox_events(manager, workspace_id)] == [
        "fu-del",
        "fu-proc",
        "fu-unc",
        "fu-pend",
    ]
    assert _run_bytes(manager.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(manager.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(manager, workspace_id) == tree_bytes

    fresh = WorkspaceManager()
    transport.clear()
    monkeypatch.setattr(fresh, "_send_tmux_message_with_receipt", _count_receipt)
    await fresh.followup_task(
        workspace_id,
        delivered.id,
        "already delivered",
        "fu-del",
        **_followup_kwargs(),
    )
    await fresh.followup_task(
        workspace_id,
        processing.id,
        "already processing",
        "fu-proc",
        **_followup_kwargs(),
    )
    await fresh.followup_task(
        workspace_id,
        uncertain.id,
        "still uncertain",
        "fu-unc",
        **_followup_kwargs(),
    )
    await fresh.followup_task(
        workspace_id,
        pending.id,
        "pending followup",
        "fu-pend",
        **_followup_kwargs(),
    )
    assert transport == []
    assert [item.call_id for item in _mailbox_events(fresh, workspace_id)] == [
        "fu-del",
        "fu-proc",
        "fu-unc",
        "fu-pend",
    ]
    assert fresh.tasks[delivered.id].delivered_call_ids == ["fu-del"]
    assert fresh.tasks[processing.id].processing_call_ids == ["fu-proc"]
    assert fresh.tasks[uncertain.id].uncertain_call_ids == ["fu-unc"]
    assert _run_bytes(fresh.agent_tree._runs[root.id]) == run_bytes[root.id]
    assert _run_bytes(fresh.agent_tree._runs[child.id]) == run_bytes[child.id]
    assert _tree_bytes(fresh, workspace_id) == tree_bytes


def _assign_worker(
    manager: WorkspaceManager,
    workspace_id: str,
    task_id: str,
    session_id: str,
) -> ManagedSession:
    session = _session(manager, workspace_id, session_id=session_id, task_id=task_id)
    current = manager.tasks[task_id]
    manager.tasks[task_id] = current.model_copy(
        update={"session_id": session.id, "status": WorkspaceTaskStatus.WORKING}
    )
    return session


def _workspace_fact_snapshot(manager: WorkspaceManager, workspace_id: str) -> dict[str, object]:
    mailbox = manager.task_mailbox
    tree = manager.agent_tree
    return {
        "reports": {
            key: value.model_dump(mode="json")
            for key, value in manager.reports.items()
            if value.workspace_id == workspace_id
        },
        "sessions": {
            key: value.model_dump(mode="json")
            for key, value in manager.sessions.items()
            if value.workspace_id == workspace_id
        },
        "tasks": {
            key: value.model_dump(mode="json")
            for key, value in manager.tasks.items()
            if value.workspace_id == workspace_id
        },
        "mailbox_events": [
            item.model_dump(mode="json") for item in mailbox._events.get(workspace_id, [])
        ],
        "mailbox_call_ids": sorted(mailbox._call_index.get(workspace_id, {})),
        "mailbox_next": mailbox._next_seq.get(workspace_id),
        "tree": _tree_bytes(manager, workspace_id),
        "runs": {
            key: _run_bytes(value)
            for key, value in tree._runs.items()
            if value.workspace_id == workspace_id
        },
    }


@pytest.mark.asyncio
async def test_todo_followup_ack_is_task_first_after_worker_assignment(
    manager_and_workspace: tuple[WorkspaceManager, str],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, workspace_id = manager_and_workspace
    todo = _ordinary_working_task(manager, workspace_id, task_id="task-todo-ack")
    _set_task_status(manager, todo.id, WorkspaceTaskStatus.TODO, session_id=None)
    call_id = "fu-todo-ack"

    async def _fake_start(task_id: str, payload: object = None) -> None:
        return None

    monkeypatch.setattr(manager, "start_task", _fake_start)
    await manager.followup_task(
        workspace_id,
        todo.id,
        "todo followup ack",
        call_id,
        **_followup_kwargs(),
    )

    assigned = WorkspaceManager()
    worker = _assign_worker(assigned, workspace_id, todo.id, "session-worker")
    wrong = _session(assigned, workspace_id, session_id="session-wrong", task_id="task-other")
    assigned._save_state()
    before = _workspace_fact_snapshot(assigned, workspace_id)
    assert call_id in assigned.tasks[todo.id].pending_call_ids
    assert [item.call_id for item in _mailbox_events(assigned, workspace_id)] == [call_id]

    with pytest.raises(RuntimeError, match="not the current worker"):
        await assigned.create_report(
            wrong.id,
            AgentReportCreate(
                task_id=todo.id,
                state=AgentReportState.WORKING,
                message="wrong session must not ack",
                call_id=f"{todo.id}-working-progress-cycle-2-1",
                acked_call_ids=[call_id],
            ),
        )
    assert _workspace_fact_snapshot(assigned, workspace_id) == before

    assigned._ack_call_ids(todo.id, wrong.id, [call_id])
    assert _workspace_fact_snapshot(assigned, workspace_id) == before

    await assigned.create_report(
        worker.id,
        AgentReportCreate(
            task_id=todo.id,
            state=AgentReportState.WORKING,
            message="worker acks followup",
            call_id=f"{todo.id}-working-progress-cycle-2-2",
            acked_call_ids=[call_id],
        ),
    )
    assert call_id not in assigned.tasks[todo.id].pending_call_ids
    assert call_id in assigned.tasks[todo.id].delivered_call_ids
    assert [
        item.call_id for item in _mailbox_events(assigned, workspace_id) if item.call_id == call_id
    ] == [call_id]
    assert call_id not in assigned.sessions[worker.id].delivered_call_ids
    assert assigned.tasks[todo.id].session_id == worker.id
    assert assigned.sessions[wrong.id].task_id == "task-other"

    cold = WorkspaceManager()
    assert call_id not in cold.tasks[todo.id].pending_call_ids
    assert call_id in cold.tasks[todo.id].delivered_call_ids
    assert [
        item.call_id for item in _mailbox_events(cold, workspace_id) if item.call_id == call_id
    ] == [call_id]
    assert cold.sessions[wrong.id].task_id == "task-other"
