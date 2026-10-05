"""Review dispatch must inspect the checkout that actually produced the work."""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    Workspace,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.services.workspace_manager import WorkspaceManager


def _session(workspace: Workspace, session_id: str, cwd: str, *, reviewer: bool) -> ManagedSession:
    now = datetime.now()
    return ManagedSession(
        id=session_id,
        workspace_id=workspace.id,
        tab_id=session_id,
        role=WorkspaceSessionRole.REVIEWER if reviewer else WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        title=session_id,
        workspace_path=cwd,
        tmux_session=f"claude-hub-{session_id[:8]}",
        created_at=now,
        updated_at=now,
    )


def _context(tmp_path: Path) -> tuple[WorkspaceManager, Workspace, WorkspaceTask, ManagedSession]:
    now = datetime.now()
    root = tmp_path / "main"
    root.mkdir()
    feature = tmp_path / "feature"
    feature.mkdir()
    workspace = Workspace(
        id="ws",
        name="repo",
        default_branch="main",
        session_prefix="test",
        path=str(root),
        created_at=now,
        updated_at=now,
    )
    manager = WorkspaceManager()
    manager.workspaces = {workspace.id: workspace}
    worker = _session(workspace, "worker", str(feature), reviewer=False)
    manager.sessions = {worker.id: worker}
    task = WorkspaceTask(
        id="task",
        workspace_id=workspace.id,
        title="change",
        prompt="verify",
        status=WorkspaceTaskStatus.WORKING,
        agent_type=AgentType.CLAUDE,
        session_id=worker.id,
        created_at=now,
        updated_at=now,
    )
    manager.tasks = {task.id: task}
    return manager, workspace, task, worker


@pytest.mark.asyncio
@pytest.mark.parametrize("previously_assigned", [False, True])
async def test_review_dispatch_creates_reviewer_in_worker_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previously_assigned: bool
) -> None:
    manager, workspace, task, worker = _context(tmp_path)
    wrong = _session(workspace, "main-reviewer", workspace.path, reviewer=True)
    manager.sessions[wrong.id] = wrong
    if previously_assigned:
        task.review_session_id = wrong.id
    created = _session(workspace, "fresh-reviewer", worker.workspace_path, reviewer=True)
    ensure = AsyncMock(return_value=created)
    monkeypatch.setattr(manager, "ensure_workspace_agent", ensure)

    reviewer = await manager._select_or_create_reviewer(workspace, task)

    assert reviewer.id == created.id
    payload = ensure.call_args.args[1]
    assert payload.cwd == worker.workspace_path
    assert payload.reuse_existing is False


@pytest.mark.asyncio
@pytest.mark.parametrize("previously_assigned", [False, True])
async def test_review_dispatch_reuses_equivalent_local_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previously_assigned: bool
) -> None:
    manager, workspace, task, worker = _context(tmp_path)
    alias = tmp_path / "feature-alias"
    alias.symlink_to(worker.workspace_path, target_is_directory=True)
    wrong = _session(workspace, "main-reviewer", workspace.path, reviewer=True)
    correct = _session(workspace, "feature-reviewer", str(alias), reviewer=True)
    manager.sessions.update({wrong.id: wrong, correct.id: correct})
    if previously_assigned:
        task.review_session_id = correct.id
    ensure = AsyncMock(side_effect=AssertionError("same checkout reviewer should be reused"))
    monkeypatch.setattr(manager, "ensure_workspace_agent", ensure)

    assert (await manager._select_or_create_reviewer(workspace, task)).id == correct.id
    ensure.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("feature_worker", [False, True])
async def test_legacy_reviewer_without_cwd_only_matches_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feature_worker: bool
) -> None:
    manager, workspace, task, worker = _context(tmp_path)
    if not feature_worker:
        worker.workspace_path = workspace.path
    legacy = _session(workspace, "legacy-reviewer", "", reviewer=True)
    manager.sessions[legacy.id] = legacy
    created = _session(workspace, "fresh-reviewer", worker.workspace_path, reviewer=True)
    ensure = AsyncMock(return_value=created)
    monkeypatch.setattr(manager, "ensure_workspace_agent", ensure)

    reviewer = await manager._select_or_create_reviewer(workspace, task)

    assert reviewer.id == (created.id if feature_worker else legacy.id)
    assert ensure.await_count == int(feature_worker)
