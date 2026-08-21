"""Focused reliability coverage for deep Agent Tree supervision and replay."""

from __future__ import annotations

import asyncio
from datetime import datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceCreate,
    WorkspaceSessionRole,
)
from claude_hub.models.agent_tree import (
    AgentEventType,
    AgentRun,
    ExecutorCapabilities,
    ExecutorKind,
    SpawnRequest,
    WaitRequest,
)
from claude_hub.services.agent_tree_adapters import NativeSubagentAdapter
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")


class _AvailableNativeAdapter(NativeSubagentAdapter):
    """Test-only runtime: explicitly available, deterministic, and in-memory."""

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            available=True,
            supports_send=True,
            supports_followup=True,
            supports_interrupt=True,
            durable_status=False,
        )


@pytest.fixture()
def manager(monkeypatch: MonkeyPatch, tmp_path: Path) -> WorkspaceManager:
    root = tmp_path / "workspaces"
    root.mkdir()
    index_file = root / "index.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)

    fake_tab = MagicMock(id="tab-mock", tmux_session="tmux-mock")
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
    monkeypatch.setattr(_wm.WorkspaceManager, "_send_tmux_message", AsyncMock())
    monkeypatch.setattr(_wm.WorkspaceManager, "_query_tmux_receipt", AsyncMock(return_value=False))
    workspace_manager = WorkspaceManager()
    workspace_manager.agent_tree._adapters[ExecutorKind.NATIVE_SUBAGENT] = _AvailableNativeAdapter()
    return workspace_manager


def _workspace(manager: WorkspaceManager, tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return manager.create_workspace(
        WorkspaceCreate(name="subtree", path=str(repo), target=ExecutionTarget.LOCAL)
    ).id


def _session(session_id: str, workspace_id: str, **updates: object) -> ManagedSession:
    session = ManagedSession(
        id=session_id,
        workspace_id=workspace_id,
        tab_id=f"tab-{session_id}",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title=session_id,
        workspace_path="/tmp",
        tmux_session=f"tmux-{session_id}",
        target=ExecutionTarget.LOCAL,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    return session.model_copy(update=updates)


async def _three_level_tree(
    manager: WorkspaceManager, workspace_id: str, owner_session: str | None = None
) -> tuple[AgentRun, AgentRun, AgentRun]:
    root = manager.agent_tree.create_root_run(
        workspace_id=workspace_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        context_ref=owner_session,
    )
    child = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=workspace_id,
            parent_id=root.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="child",
            call_id="spawn-child",
        )
    )
    grandchild = await manager.agent_tree.spawn(
        SpawnRequest(
            workspace_id=workspace_id,
            parent_id=child.id,
            executor_kind=ExecutorKind.NATIVE_SUBAGENT,
            initial_message="grandchild",
            call_id="spawn-grandchild",
        )
    )
    return root, child, grandchild


def test_owner_can_wait_and_interrupt_grandchild_via_api(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from claude_hub.api import agent_tree as agent_tree_api
    from claude_hub.auth import dependencies as auth_deps
    from claude_hub.main import app

    workspace_id = _workspace(manager, tmp_path)
    owner_id = "owner-session"
    manager.sessions[owner_id] = _session(owner_id, workspace_id)
    _, _, grandchild = asyncio.run(_three_level_tree(manager, workspace_id, owner_id))
    monkeypatch.setattr(agent_tree_api, "workspace_manager", manager)
    monkeypatch.setattr(auth_deps, "is_local_network_request", lambda request: False)
    client = TestClient(app)

    wait_response = client.post(
        "/api/agent-tree/wait",
        json={
            "workspace_id": workspace_id,
            "recipient_id": grandchild.id,
            "since_sequence": 0,
            "timeout_seconds": 0,
            "subtree": False,
        },
        cookies={"claude_hub_session": owner_id},
    )
    assert wait_response.status_code == 200

    interrupt_response = client.post(
        "/api/agent-tree/interrupt",
        json={
            "workspace_id": workspace_id,
            "run_id": grandchild.id,
            "call_id": "interrupt-grandchild",
            "reason": "deep supervision",
        },
        cookies={"claude_hub_session": owner_id},
    )
    assert interrupt_response.status_code == 200
    assert interrupt_response.json()["status"] == "interrupted"


@pytest.mark.asyncio
async def test_subtree_replay_is_directed_and_wakes_only_opted_in_ancestors(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    workspace_id = _workspace(manager, tmp_path)
    root, child, grandchild = await _three_level_tree(manager, workspace_id)
    since = manager.agent_tree._next_seq[workspace_id] - 1

    manager.agent_tree.emit_event(
        workspace_id=workspace_id,
        agent_run_id=grandchild.id,
        event_type=AgentEventType.PROGRESS,
        author=grandchild.id,
        recipient=child.id,
        call_id="grandchild-progress",
        payload={"message": "deep progress"},
    )
    assert (
        manager.agent_tree.get_events(workspace_id, root.id, since_sequence=since, subtree=False)
        == []
    )
    assert [
        event.call_id
        for event in manager.agent_tree.get_events(
            workspace_id, root.id, since_sequence=since, subtree=True
        )
    ] == ["grandchild-progress"]

    cursor = manager.agent_tree._next_seq[workspace_id] - 1
    subtree_wait = asyncio.create_task(
        manager.agent_tree.wait(
            WaitRequest(
                workspace_id=workspace_id,
                recipient_id=root.id,
                since_sequence=cursor,
                timeout_seconds=1,
                subtree=True,
            )
        )
    )
    directed_wait = asyncio.create_task(
        manager.agent_tree.wait(
            WaitRequest(
                workspace_id=workspace_id,
                recipient_id=root.id,
                since_sequence=cursor,
                timeout_seconds=0.08,
                subtree=False,
            )
        )
    )
    await asyncio.sleep(0)
    manager.agent_tree.emit_event(
        workspace_id=workspace_id,
        agent_run_id=grandchild.id,
        event_type=AgentEventType.PROGRESS,
        author=grandchild.id,
        recipient=child.id,
        call_id="grandchild-wakeup",
    )
    assert [event.call_id for event in await subtree_wait] == ["grandchild-wakeup"]
    assert await directed_wait == []


@pytest.mark.asyncio
async def test_uncertain_emit_failure_is_replayed_once_after_restart(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace_id = _workspace(manager, tmp_path)
    session_id = "uncertain-session"
    call_id = "call-uncertain"
    manager.sessions[session_id] = _session(
        session_id,
        workspace_id,
        processing_call_ids=[call_id],
        pending_messages={call_id: "payload"},
    )
    manager.agent_tree.create_root_run(
        workspace_id=workspace_id,
        executor_kind=ExecutorKind.NATIVE_SUBAGENT,
        context_ref=session_id,
    )
    manager._save_state()

    original_persist = manager.agent_tree._persist

    def fail_event_persist() -> None:
        raise OSError("simulated event persist failure")

    monkeypatch.setattr(manager.agent_tree, "_persist", fail_event_persist)
    with pytest.raises(OSError, match="simulated event persist failure"):
        manager._mark_processing_as_uncertain(session_id, [call_id])
    assert call_id in manager.sessions[session_id].uncertain_call_ids
    monkeypatch.setattr(manager.agent_tree, "_persist", original_persist)

    restarted = WorkspaceManager()
    assert call_id in restarted.sessions[session_id].uncertain_call_ids
    await restarted.agent_tree.recover_pending_runs(workspace_id)
    await restarted.agent_tree.recover_pending_runs(workspace_id)
    events = [
        event
        for event in restarted.agent_tree._events[workspace_id]
        if event.call_id == f"delivery:uncertain:{call_id}"
    ]
    assert len(events) == 1


def test_legacy_session_without_root_keeps_uncertain_state(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    workspace_id = _workspace(manager, tmp_path)
    session_id = "legacy-session"
    call_id = "legacy-call"
    manager.sessions[session_id] = _session(
        session_id,
        workspace_id,
        processing_call_ids=[call_id],
        pending_messages={call_id: "payload"},
    )

    manager._mark_processing_as_uncertain(session_id, [call_id])

    assert call_id in manager.sessions[session_id].uncertain_call_ids


def test_batch_persistence_options_skip_inner_persist_and_wake(
    manager: WorkspaceManager, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace_id = _workspace(manager, tmp_path)
    root, child, _ = asyncio.run(_three_level_tree(manager, workspace_id))
    followup = manager.agent_tree._append_event(
        workspace_id=workspace_id,
        agent_run_id=child.id,
        event_type=AgentEventType.MESSAGE,
        author=root.id,
        recipient=child.id,
        call_id="followup-batch",
        action="followup",
        target=child.id,
        fingerprint="batch",
        payload={"message": "continue", "followup": True},
    )[0]
    persisted = MagicMock()
    woken = MagicMock()
    monkeypatch.setattr(manager.agent_tree, "_persist", persisted)
    monkeypatch.setattr(manager.agent_tree, "_wake_for_run", woken)

    manager.agent_tree.reconcile_followup_outcome(
        workspace_id=workspace_id,
        call_id=followup.call_id,
        persist=False,
        wake=False,
    )
    manager.agent_tree.ack(
        workspace_id,
        root.id,
        followup.sequence,
        persist=False,
    )
    persisted.assert_not_called()
    woken.assert_not_called()
    assert manager.agent_tree.get_run(root.id).ack_sequence == followup.sequence
    assert manager.agent_tree._call_record(workspace_id, "followup-batch:outcome") is not None
