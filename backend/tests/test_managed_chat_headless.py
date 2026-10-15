"""Headless lifecycle guarantees for managed Workspace Chat tabs."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    SessionKind,
    WorkspaceSessionRole,
)
from claude_hub.services.session_seat import validate_session_seat
from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

ttyd_module = import_module("claude_hub.services.ttyd_manager")


def _empty_manager(monkeypatch: pytest.MonkeyPatch) -> TTYDManager:
    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {}
    manager._next_port = 23000
    manager._tab_order = []
    manager._status_snapshots = {}
    manager._status_cache = {}
    manager._start_locks = {}
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(manager, "_save_order", lambda: None)
    return manager


@pytest.mark.asyncio
async def test_managed_chat_create_is_headless(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = _empty_manager(monkeypatch)

    async def forbidden(*_args: object, **_kwargs: object) -> bool:
        pytest.fail("managed Chat creation must not touch tmux or ttyd")

    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", forbidden)
    monkeypatch.setattr(TTYDProcess, "start", forbidden)

    tab = await manager.create_tab(
        name="Chat worker",
        cwd=str(tmp_path),
        solo_mode=True,
        agent_type=AgentType.CODEX,
        session_kind=SessionKind.CHAT,
        target=ExecutionTarget.LOCAL,
        workspace_id="ws",
        workspace_name="Workspace",
        workspace_role=WorkspaceSessionRole.WORKER,
        managed_session_id="managed-worker",
        env={"CODEX_MODEL": "gpt-test"},
    )

    process = manager.processes[tab.id]
    assert tab.session_kind == SessionKind.CHAT
    assert tab.managed_session_id == "managed-worker"
    assert tab.agent_session_id != tab.managed_session_id
    assert tab.is_active is False
    assert process.process is None
    assert process.is_managed_chat is True


@pytest.mark.asyncio
async def test_managed_maso_chat_rejects_non_loopback_before_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = _empty_manager(monkeypatch)

    async def forbidden_probe(_env: dict[str, str]) -> object:
        pytest.fail("non-loopback managed MASO must be rejected before network probing")

    monkeypatch.setattr("claude_hub.services.maso_discovery.discover_maso_service", forbidden_probe)
    with pytest.raises(ValueError, match="loopback-local"):
        await manager.create_tab(
            name="Remote MASO",
            cwd=str(tmp_path),
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
            workspace_id="ws",
            workspace_role=WorkspaceSessionRole.WORKER,
            env={"MASO_BASE_URL": "https://example.com"},
        )


def test_managed_tab_identity_survives_restore_without_provider_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = TTYDProcess(
        tab_id="maso-tab",
        port=23003,
        name="MASO Chat",
        agent_type=AgentType.MASO,
        session_kind=SessionKind.CHAT,
        workspace_id="ws",
        workspace_role=WorkspaceSessionRole.WORKER,
        managed_session_id="managed-maso",
    )
    assert process.agent_session_id is None

    state_file = tmp_path / "tabs.json"
    order_file = tmp_path / "order.json"
    state_file.write_text(json.dumps([process.to_dict()]))
    order_file.write_text(json.dumps([process.tab_id]))
    monkeypatch.setattr(ttyd_module, "STATE_FILE", state_file)
    monkeypatch.setattr(ttyd_module, "ORDER_FILE", order_file)

    restored = TTYDManager().processes[process.tab_id].to_schema()
    assert restored.managed_session_id == "managed-maso"
    assert restored.agent_session_id is None


@pytest.mark.asyncio
async def test_managed_chat_restore_does_not_initialize_tmux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = _empty_manager(monkeypatch)
    process = TTYDProcess(
        tab_id="headless-tab",
        port=23001,
        name="Persisted Chat",
        cwd=str(tmp_path),
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        workspace_id="ws",
        workspace_name="Workspace",
        workspace_role=WorkspaceSessionRole.REVIEWER,
        from_persisted_state=True,
    )
    manager.processes[process.tab_id] = process

    def forbidden_tmux() -> bool:
        pytest.fail("managed Chat restoration must not initialize tmux")

    monkeypatch.setattr(ttyd_module, "_ensure_tmux_server", forbidden_tmux)
    await manager.start_all_tabs()

    assert process.process is None
    assert process.is_active is False


@pytest.mark.asyncio
async def test_managed_chat_delete_safely_handles_headless_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = _empty_manager(monkeypatch)
    tab = await manager.create_tab(
        name="Disposable Chat",
        cwd=str(tmp_path),
        agent_type=AgentType.CURSOR,
        session_kind=SessionKind.CHAT,
        workspace_id="ws",
        workspace_name="Workspace",
        workspace_role=WorkspaceSessionRole.ORCHESTRATOR,
    )

    async def forbidden_kill(_name: str) -> None:
        pytest.fail("headless managed Chat deletion must not kill tmux")

    async def discard(_workspace_id: str, _session_id: str) -> None:
        return None

    monkeypatch.setattr(ttyd_module, "_tmux_kill_session", forbidden_kill)
    monkeypatch.setattr("claude_hub.services.agent_stream.tailer.discard_session_stream", discard)

    assert await manager.delete_tab(tab.id) is True
    assert tab.id not in manager.processes


def test_managed_chat_seat_is_tab_identity_not_tmux(tmp_path: Path) -> None:
    process = TTYDProcess(
        tab_id="chat-tab",
        port=23002,
        name="Chat",
        cwd=str(tmp_path),
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        workspace_id="ws",
        workspace_role=WorkspaceSessionRole.WORKER,
    )
    session = type(
        "Session",
        (),
        {
            "id": "session",
            "tab_id": process.tab_id,
            "workspace_id": "ws",
            "session_kind": SessionKind.CHAT,
            "tmux_session": "",
        },
    )()

    assert validate_session_seat(session, processes={process.tab_id: process}) == ""
