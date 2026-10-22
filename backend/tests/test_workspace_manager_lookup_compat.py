"""Behavior probes for historical call-time WorkspaceManager lookups."""

from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    EnsureWorkspaceAgentRequest,
    ExecutionTarget,
    ManagedSessionStatus,
    SessionKind,
    WorkspaceCreate,
    WorkspaceSessionRole,
)
from claude_hub.services.workspace_manager import workspace_manager


class LookupObserved(RuntimeError):
    """Raised when a patched public source lookup is reached."""


def _raise_lookup(*_args: Any, **_kwargs: Any) -> Any:
    raise LookupObserved


@pytest.mark.asyncio
async def test_tmux_transport_observes_runtime_command_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_isolation = importlib.import_module("claude_hub.services.runtime_isolation")
    transport_module = importlib.import_module(
        "claude_hub.services.workspace_manager.tmux.transport"
    )
    process = SimpleNamespace(
        communicate=AsyncMock(return_value=(b"captured", b"")),
        returncode=0,
    )
    create_subprocess = AsyncMock(return_value=process)
    monkeypatch.setattr(transport_module.asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(
        runtime_isolation,
        "tmux_command",
        lambda *args: ["patched-tmux", *args],
    )
    await workspace_manager._run_tmux("send-keys", "Enter")
    assert create_subprocess.await_args is not None
    assert create_subprocess.await_args.args[:2] == ("patched-tmux", "send-keys")

    create_subprocess.reset_mock()
    assert await workspace_manager._run_tmux_capture("show-options") == "captured"
    assert create_subprocess.await_args is not None
    assert create_subprocess.await_args.args[:2] == ("patched-tmux", "show-options")


def test_message_audits_observe_task_graph_and_mailbox_type_patches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox_models = importlib.import_module("claude_hub.models.task_mailbox")
    task_graph = importlib.import_module("claude_hub.services.task_graph")
    actor_roles = SimpleNamespace(
        REVIEWER="patched-reviewer", WORKER="patched-worker", HUMAN="patched-human"
    )
    event_types = SimpleNamespace(PROGRESS="patched-progress")
    monkeypatch.setattr(mailbox_models, "TaskActorRole", actor_roles)
    monkeypatch.setattr(mailbox_models, "TaskEventType", event_types)
    monkeypatch.setattr(task_graph, "make_task_consumer_key", lambda _task_id: "patched-consumer")
    monkeypatch.setattr(task_graph, "task_inbox_consumer_key", lambda _task: "patched-inbox")

    task = SimpleNamespace(
        id="task-1",
        workspace_id="workspace-1",
        session_id="session-1",
        review_session_id=None,
    )
    session = SimpleNamespace(
        id="session-1",
        workspace_id="workspace-1",
        current_task_id="task-1",
        task_id="task-1",
    )
    append_event = Mock()
    mailbox = SimpleNamespace(_events={}, append_event=append_event)
    monkeypatch.setattr(workspace_manager, "sessions", {session.id: session})
    monkeypatch.setattr(workspace_manager, "tasks", {task.id: task})
    monkeypatch.setattr(workspace_manager, "task_mailbox", mailbox)

    workspace_manager._emit_delivery_uncertain(task.workspace_id, session.id, "call-1")
    uncertain_event = append_event.call_args.kwargs
    assert uncertain_event["actor_role"] == "patched-worker"
    assert uncertain_event["event_type"] == "patched-progress"
    assert uncertain_event["consumer_key"] == "patched-consumer"

    append_event.reset_mock()
    monkeypatch.setattr(
        workspace_manager,
        "_audit_task_for_session",
        lambda _workspace_id, _session_id: task.id,
    )
    workspace_manager._emit_delivery_retry_requested(
        task.workspace_id,
        session.id,
        "call-1",
        "operator",
        "retry",
    )
    retry_event = append_event.call_args.kwargs
    assert retry_event["actor_role"] == "patched-human"
    assert retry_event["event_type"] == "patched-progress"
    assert retry_event["consumer_key"] == "patched-inbox"


@pytest.mark.asyncio
async def test_session_provisioning_observes_env_and_cwd_source_patches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_resolver = importlib.import_module("claude_hub.services.env_preset_resolver")
    workspace_identity = importlib.import_module("claude_hub.services.workspace_identity")
    workspace = SimpleNamespace(id="workspace-1", path=str(tmp_path), target=ExecutionTarget.LOCAL)
    monkeypatch.setattr(workspace_manager, "workspaces", {workspace.id: workspace})
    monkeypatch.setattr(env_resolver, "merge_env_with_preset", _raise_lookup)

    with pytest.raises(LookupObserved):
        await workspace_manager.ensure_workspace_agent(
            workspace.id,
            EnsureWorkspaceAgentRequest(agent_type=AgentType.CLAUDE, env_preset="patched"),
        )

    monkeypatch.setattr(env_resolver, "merge_env_with_preset", lambda **_kwargs: {})
    monkeypatch.setattr(
        workspace_identity,
        "validate_local_agent_cwd_for_workspace",
        _raise_lookup,
    )
    monkeypatch.setattr(
        workspace_manager,
        "_effective_agent_target",
        lambda _workspace, _payload: ExecutionTarget.LOCAL,
    )
    with pytest.raises(LookupObserved):
        await workspace_manager.ensure_workspace_agent(
            workspace.id,
            EnsureWorkspaceAgentRequest(agent_type=AgentType.CLAUDE),
        )

    monkeypatch.setattr(workspace_identity, "normalize_remote_cwd", lambda _value: "patched-cwd")
    assert workspace_manager._resolve_remote_cwd(None, "~/source", None) == "patched-cwd"


def test_workspace_crud_observes_identity_source_patches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_identity = importlib.import_module("claude_hub.services.workspace_identity")
    monkeypatch.setattr(workspace_identity, "reject_relative_local_path", _raise_lookup)
    with pytest.raises(LookupObserved):
        workspace_manager.create_workspace(
            WorkspaceCreate(name="patched", path=str(tmp_path)),
        )

    identity_for_fields = Mock(return_value=SimpleNamespace(key="patched-identity"))
    monkeypatch.setattr(workspace_identity, "workspace_identity_for_fields", identity_for_fields)
    monkeypatch.setattr(workspace_manager, "_workspaces_for_identity_key", lambda _key: [])
    workspace_manager._assert_no_duplicate_identity(
        WorkspaceCreate(name="patched", path=str(tmp_path)),
    )
    assert identity_for_fields.call_args.kwargs["path"] == str(tmp_path)


def test_session_inventory_and_reviewer_observe_identity_source_patches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_resolver = importlib.import_module("claude_hub.services.env_preset_resolver")
    workspace_identity = importlib.import_module("claude_hub.services.workspace_identity")
    workspace: Any = SimpleNamespace(
        id="workspace-1",
        path=str(tmp_path),
        remote_profile_id=None,
        remote_cwd=None,
    )
    payload: Any = SimpleNamespace(
        target=ExecutionTarget.LOCAL,
        cwd=str(tmp_path),
        remote_profile_id=None,
        remote_cwd=None,
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.TERMINAL,
        solo_mode=False,
        env={},
    )
    session = SimpleNamespace(
        id="session-1",
        role=payload.role,
        agent_type=payload.agent_type,
        session_kind=payload.session_kind,
        archived=False,
        chat_paused=False,
        solo_mode=False,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        ephemeral=False,
        caller_owned_ephemeral=False,
        target=ExecutionTarget.LOCAL,
        workspace_path=str(tmp_path),
        env={},
        created_at=datetime.min,
    )
    monkeypatch.setattr(
        workspace_identity,
        "validate_local_agent_cwd_for_workspace",
        lambda _workspace_path, requested: requested,
    )
    monkeypatch.setattr(workspace_identity, "effective_local_agent_cwd", lambda value: value)
    monkeypatch.setattr(
        workspace_identity,
        "local_agent_cwd_allowed_for_workspace",
        lambda _workspace_path, _session_path: True,
    )
    monkeypatch.setattr(env_resolver, "effective_launch_envs_match", _raise_lookup)
    monkeypatch.setattr(
        workspace_manager,
        "_cleanup_stale_orchestrator_assignments",
        lambda _workspace_id: False,
    )
    monkeypatch.setattr(
        workspace_manager,
        "_effective_agent_target",
        lambda _workspace, request: request.target,
    )
    monkeypatch.setattr(workspace_manager, "_resolve_remote_cwd", lambda **_kwargs: "~")
    monkeypatch.setattr(
        workspace_manager,
        "_sessions_for_workspace_raw",
        lambda _workspace_id: [session],
    )
    monkeypatch.setattr(workspace_manager, "_session_blocks_agent_reuse", lambda _session: False)
    with pytest.raises(LookupObserved):
        workspace_manager._find_compatible_workspace_agent(workspace, payload)

    reviewer = SimpleNamespace(
        role=WorkspaceSessionRole.REVIEWER,
        session_kind=SessionKind.TERMINAL,
        archived=False,
        chat_paused=False,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        task_id=None,
        current_task_id=None,
        target=ExecutionTarget.LOCAL,
        workspace_path=str(tmp_path),
        remote_profile_id=None,
        remote_cwd=None,
        ephemeral=False,
        created_at=datetime.min,
    )
    monkeypatch.setattr(workspace_identity, "resolve_path", _raise_lookup)
    monkeypatch.setattr(workspace_manager, "workspaces", {workspace.id: workspace})
    monkeypatch.setattr(
        workspace_manager,
        "_sessions_for_workspace_raw",
        lambda _workspace_id: [reviewer],
    )
    monkeypatch.setattr(
        workspace_manager,
        "_reviewer_has_active_task_binding",
        lambda _session: False,
    )
    with pytest.raises(LookupObserved):
        workspace_manager._first_available_reviewer(
            workspace.id,
            target=ExecutionTarget.LOCAL,
            cwd=str(tmp_path),
        )


@pytest.mark.asyncio
async def test_board_query_observes_pagination_source_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_pagination = importlib.import_module("claude_hub.services.board_pagination")
    monkeypatch.setattr(board_pagination, "paginate_board_tasks", _raise_lookup)
    workspace = SimpleNamespace(id="workspace-1")
    monkeypatch.setattr(workspace_manager, "workspaces", {workspace.id: workspace})
    monkeypatch.setattr(workspace_manager, "tasks", {})
    monkeypatch.setattr(workspace_manager, "_refresh_session_statuses", AsyncMock())
    monkeypatch.setattr(
        workspace_manager,
        "_reconcile_task_report_statuses",
        lambda _workspace_id: None,
    )
    monkeypatch.setattr(
        workspace_manager,
        "_reconcile_workspace_session_pointers",
        lambda _workspace_id: None,
    )
    monkeypatch.setattr(
        workspace_manager,
        "_cleanup_stale_orchestrator_assignments",
        lambda _workspace_id: False,
    )
    monkeypatch.setattr(
        workspace_manager,
        "_prune_orphan_workspace_tabs",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        workspace_manager,
        "_sync_workspace_tab_metadata",
        lambda _workspace_id: None,
    )
    with pytest.raises(LookupObserved):
        await workspace_manager.get_board(workspace.id)
