"""Start overrides must keep the same provider/surface admission as task creation."""

from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    StartTaskRequest,
    Workspace,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskStatus,
)
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WorkspaceManager:
    monkeypatch.setattr(_wm, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(WorkspaceManager, "_load_state", lambda self: None)
    monkeypatch.setattr(WorkspaceManager, "_load_scheduled_tasks", lambda self: None)
    instance = WorkspaceManager()
    now = datetime.now()
    instance.workspaces["ws"] = Workspace(
        id="ws",
        name="Workspace",
        path=str(tmp_path),
        default_branch="main",
        session_prefix="ws",
        created_at=now,
        updated_at=now,
    )
    instance.sessions["worker"] = ManagedSession(
        id="worker",
        workspace_id="ws",
        tab_id="tab",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.TERMINAL,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="Worker",
        workspace_path=str(tmp_path),
        tmux_session="test-terminal",
        created_at=now,
        updated_at=now,
    )
    instance.tasks["task"] = WorkspaceTask(
        id="task",
        workspace_id="ws",
        title="Task",
        prompt="Do the bounded task.",
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.TERMINAL,
        status=WorkspaceTaskStatus.TODO,
        session_id="worker",
        created_at=now,
        updated_at=now,
    )
    monkeypatch.setattr(instance, "_refresh_session_statuses", AsyncMock())
    monkeypatch.setattr(instance, "dispatch_workspace", AsyncMock())
    monkeypatch.setattr(instance, "ensure_workspace_agent", AsyncMock())
    monkeypatch.setattr(instance, "_save_state", Mock())
    return instance


def _select_surface(manager: WorkspaceManager, kind: SessionKind) -> None:
    manager.tasks["task"] = manager.tasks["task"].model_copy(update={"session_kind": kind})
    manager.sessions["worker"] = manager.sessions["worker"].model_copy(
        update={"session_kind": kind, "tmux_session": "" if kind == SessionKind.CHAT else "tmux"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_target", [True, False])
@pytest.mark.parametrize(
    ("kind", "provider", "error"),
    [
        (SessionKind.TERMINAL, AgentType.MASO, "select a Chat session"),
        (SessionKind.TERMINAL, AgentType.ANTIGRAVITY, "select a Chat session"),
        (SessionKind.CHAT, AgentType.TERMINAL, "Managed Chat supports only"),
        (SessionKind.CHAT, AgentType.TRAEX, "TraeX is not supported"),
        (SessionKind.TERMINAL, AgentType.TRAEX, "TraeX is not supported"),
    ],
)
async def test_start_rejects_invalid_override_before_any_side_effect(
    manager: WorkspaceManager,
    kind: SessionKind,
    provider: AgentType,
    error: str,
    explicit_target: bool,
) -> None:
    _select_surface(manager, kind)
    if not explicit_target:
        manager.sessions.clear()
    before = manager.tasks["task"].model_dump()
    with pytest.raises(ValueError, match=error):
        WorkspaceTaskCreate(title="Task", prompt="Work", agent_type=provider, session_kind=kind)

    with pytest.raises(ValueError, match=error):
        await manager.start_task(
            "task",
            StartTaskRequest(
                agent_type=provider,
                target_session_id="worker" if explicit_target else None,
            ),
        )

    assert manager.tasks["task"].model_dump() == before
    cast(AsyncMock, manager._refresh_session_statuses).assert_not_awaited()
    cast(AsyncMock, manager.ensure_workspace_agent).assert_not_awaited()
    cast(AsyncMock, manager.dispatch_workspace).assert_not_awaited()
    cast(Mock, manager._save_state).assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "provider"),
    [
        (SessionKind.TERMINAL, AgentType.CODEX),
        (SessionKind.CHAT, AgentType.CODEX),
        (SessionKind.CHAT, AgentType.MASO),
        (SessionKind.CHAT, AgentType.ANTIGRAVITY),
    ],
)
async def test_start_materializes_a_supported_override(
    manager: WorkspaceManager, kind: SessionKind, provider: AgentType
) -> None:
    _select_surface(manager, kind)
    result = await manager.start_task(
        "task", StartTaskRequest(agent_type=provider, target_session_id="worker")
    )
    assert result.agent_type == provider
    assert result.session_kind == kind
    assert result.session_id == "worker"
    assert result.status == WorkspaceTaskStatus.QUEUED
    assert result.dispatch_attempt == 1
    cast(AsyncMock, manager.dispatch_workspace).assert_awaited_once_with("ws")
    cast(AsyncMock, manager.ensure_workspace_agent).assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_target", [True, False])
async def test_start_revalidates_surface_after_awaited_refresh(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch, explicit_target: bool
) -> None:
    _select_surface(manager, SessionKind.CHAT)
    if not explicit_target:
        manager.sessions.clear()

    async def concurrent_edit(_workspace_id: str) -> None:
        manager.tasks["task"] = manager.tasks["task"].model_copy(
            update={"session_kind": SessionKind.TERMINAL}
        )

    monkeypatch.setattr(manager, "_refresh_session_statuses", concurrent_edit)
    with pytest.raises(ValueError, match="select a Chat session"):
        await manager.start_task(
            "task",
            StartTaskRequest(
                agent_type=AgentType.MASO,
                target_session_id="worker" if explicit_target else None,
            ),
        )
    cast(AsyncMock, manager.ensure_workspace_agent).assert_not_awaited()
    task = manager.tasks["task"]
    assert task.status == WorkspaceTaskStatus.TODO
    assert task.session_kind == SessionKind.TERMINAL
    assert task.agent_type == AgentType.CLAUDE
    assert task.dispatch_attempt == 0
    cast(Mock, manager._save_state).assert_not_called()
    cast(AsyncMock, manager.dispatch_workspace).assert_not_awaited()


@pytest.mark.asyncio
async def test_start_revalidates_after_an_already_started_agent_creation(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _select_surface(manager, SessionKind.CHAT)
    worker = manager.sessions.pop("worker")

    async def concurrent_edit(_workspace_id, request):
        assert request.agent_type == AgentType.MASO
        assert request.session_kind == SessionKind.CHAT
        manager.tasks["task"] = manager.tasks["task"].model_copy(
            update={"session_kind": SessionKind.TERMINAL}
        )
        return worker

    create = AsyncMock(side_effect=concurrent_edit)
    monkeypatch.setattr(manager, "ensure_workspace_agent", create)
    with pytest.raises(ValueError, match="select a Chat session"):
        await manager.start_task("task", StartTaskRequest(agent_type=AgentType.MASO))
    create.assert_awaited_once()
    assert manager.tasks["task"].status == WorkspaceTaskStatus.TODO
    assert manager.tasks["task"].agent_type == AgentType.CLAUDE
    assert manager.tasks["task"].session_kind == SessionKind.TERMINAL
    cast(Mock, manager._save_state).assert_not_called()
    cast(AsyncMock, manager.dispatch_workspace).assert_not_awaited()


def test_restored_managed_chat_uses_task_mode_without_changing_identity(
    manager: WorkspaceManager,
) -> None:
    _select_surface(manager, SessionKind.CHAT)
    saved = manager.sessions["worker"].model_dump(mode="json")
    saved.update(chat_mode="plan", agent_session_id="provider-context", chat_paused=True)
    restored = ManagedSession.model_validate(manager._normalize_session_item(saved))
    assert restored.chat_mode.value == "default"
    assert restored.id == "worker"
    assert restored.tab_id == "tab"
    assert restored.agent_session_id == "provider-context"
    assert restored.chat_paused is True
    assert saved["chat_mode"] == "plan"
