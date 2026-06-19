"""Tests for orphan managed-tab reconciliation.

Terminal tabs (ttyd_manager) and managed sessions (workspace_manager) persist
to separate state files. A session removed without its tab leaves an orphan
terminal tab: visible in the tab bar yet absent from the "Manage Agents" board
(which lists sessions), so it cannot respond to dispatch nor be deleted there.

``_prune_orphan_workspace_tabs`` cleans these up. These tests verify it:

* prunes a managed tab for the workspace that has no backing session,
* preserves manual tabs (no ``workspace_id``),
* preserves tabs that back a live session,
* preserves freshly-created tabs within the grace window,
* never prunes tabs belonging to a *different* workspace.
"""

from datetime import datetime, timedelta
from importlib import import_module
from typing import Generator

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    TerminalTab,
    WorkspaceSessionRole,
)
from claude_hub.services.workspace_manager import (
    ORPHAN_TAB_PRUNE_GRACE_SECONDS,
    workspace_manager,
)

workspace_module = import_module("claude_hub.services.workspace_manager")


@pytest.fixture(autouse=True)
def isolated_manager(monkeypatch: MonkeyPatch) -> Generator[None, None, None]:
    workspace_manager.sessions.clear()
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)
    yield
    workspace_manager.sessions.clear()


def _tab(
    tab_id: str,
    *,
    workspace_id: str | None,
    role: WorkspaceSessionRole | None = WorkspaceSessionRole.REVIEWER,
    age_seconds: float = 3600.0,
) -> TerminalTab:
    return TerminalTab(
        id=tab_id,
        name=f"tab-{tab_id}",
        shell="claude",
        cwd="/tmp",
        solo_mode=True,
        agent_type=AgentType.CLAUDE,
        target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_cwd=None,
        remote_reconnect=True,
        port=10000,
        created_at=datetime.now() - timedelta(seconds=age_seconds),
        is_active=True,
        workspace_id=workspace_id,
        workspace_name="WS" if workspace_id else None,
        workspace_role=role,
    )


def _session(session_id: str, workspace_id: str, tab_id: str) -> ManagedSession:
    now = datetime.now()
    session = ManagedSession(
        id=session_id,
        workspace_id=workspace_id,
        task_id=None,
        tab_id=tab_id,
        role=WorkspaceSessionRole.REVIEWER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Reviewer",
        branch=None,
        workspace_path="/tmp",
        tmux_session=f"claude-hub-{tab_id}",
        target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_cwd=None,
        remote_reconnect=True,
        solo_mode=True,
        remote_forward_port=None,
        created_at=now,
        updated_at=now,
    )
    workspace_manager.sessions[session_id] = session
    return session


def _patch_tabs(monkeypatch: MonkeyPatch, tabs: list[TerminalTab], deleted: list[str]) -> None:
    monkeypatch.setattr(workspace_module.ttyd_manager, "list_tabs", lambda: list(tabs))

    async def fake_delete_tab(tab_id: str) -> bool:
        deleted.append(tab_id)
        return True

    monkeypatch.setattr(workspace_module.ttyd_manager, "delete_tab", fake_delete_tab)


@pytest.mark.asyncio
async def test_prunes_orphan_managed_tab(monkeypatch: MonkeyPatch) -> None:
    deleted: list[str] = []
    tabs = [_tab("orphan-1", workspace_id="ws-1")]
    _patch_tabs(monkeypatch, tabs, deleted)

    pruned = await workspace_manager._prune_orphan_workspace_tabs("ws-1")

    assert pruned == 1
    assert deleted == ["orphan-1"]


@pytest.mark.asyncio
async def test_preserves_manual_tab(monkeypatch: MonkeyPatch) -> None:
    deleted: list[str] = []
    # workspace_id=None => manual terminal tab, never managed.
    tabs = [_tab("manual-1", workspace_id=None, role=None)]
    _patch_tabs(monkeypatch, tabs, deleted)

    pruned = await workspace_manager._prune_orphan_workspace_tabs("ws-1")

    assert pruned == 0
    assert deleted == []


@pytest.mark.asyncio
async def test_preserves_tab_with_live_session(monkeypatch: MonkeyPatch) -> None:
    deleted: list[str] = []
    _session("cb-reviewer-1", "ws-1", "tab-live")
    tabs = [_tab("tab-live", workspace_id="ws-1")]
    _patch_tabs(monkeypatch, tabs, deleted)

    pruned = await workspace_manager._prune_orphan_workspace_tabs("ws-1")

    assert pruned == 0
    assert deleted == []


@pytest.mark.asyncio
async def test_preserves_freshly_created_tab(monkeypatch: MonkeyPatch) -> None:
    deleted: list[str] = []
    # Created just now — within the grace window, even though no session yet.
    tabs = [
        _tab(
            "tab-new",
            workspace_id="ws-1",
            age_seconds=ORPHAN_TAB_PRUNE_GRACE_SECONDS / 2,
        )
    ]
    _patch_tabs(monkeypatch, tabs, deleted)

    pruned = await workspace_manager._prune_orphan_workspace_tabs("ws-1")

    assert pruned == 0
    assert deleted == []


@pytest.mark.asyncio
async def test_ignores_other_workspace_tabs(monkeypatch: MonkeyPatch) -> None:
    deleted: list[str] = []
    tabs = [_tab("orphan-other", workspace_id="ws-2")]
    _patch_tabs(monkeypatch, tabs, deleted)

    pruned = await workspace_manager._prune_orphan_workspace_tabs("ws-1")

    assert pruned == 0
    assert deleted == []


@pytest.mark.asyncio
async def test_mixed_set_prunes_only_orphans(monkeypatch: MonkeyPatch) -> None:
    deleted: list[str] = []
    _session("cb-reviewer-1", "ws-1", "tab-live")
    tabs = [
        _tab("tab-live", workspace_id="ws-1"),  # live session -> keep
        _tab("orphan-1", workspace_id="ws-1"),  # orphan -> prune
        _tab("orphan-2", workspace_id="ws-1"),  # orphan -> prune
        _tab("manual-1", workspace_id=None, role=None),  # manual -> keep
        _tab("other-ws", workspace_id="ws-2"),  # other ws -> keep
        _tab(
            "tab-new",
            workspace_id="ws-1",
            age_seconds=1.0,
        ),  # within grace -> keep
    ]
    _patch_tabs(monkeypatch, tabs, deleted)

    pruned = await workspace_manager._prune_orphan_workspace_tabs("ws-1")

    assert pruned == 2
    assert sorted(deleted) == ["orphan-1", "orphan-2"]
