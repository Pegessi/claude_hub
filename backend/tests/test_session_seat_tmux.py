"""Real-tmux seat checks on an isolated socket. Never the default server."""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from datetime import datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Generator

import pytest

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceCreate,
    WorkspaceSessionRole,
)
from claude_hub.services.runtime_isolation import tmux_command
from claude_hub.services.session_seat import SessionSeatMismatch
from claude_hub.services.ttyd_manager import ttyd_manager
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture()
def isolated_tmux_socket(monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    socket_name = f"ch-seat-{uuid.uuid4().hex[:10]}"
    monkeypatch.setenv("CLAUDE_HUB_TMUX_SOCKET", socket_name)
    try:
        yield socket_name
    finally:
        subprocess.run(
            ["tmux", "-L", socket_name, "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


@pytest.fixture()
def isolated_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> WorkspaceManager:
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", root / "index.json")
    return WorkspaceManager()


def _start_effect_session(name: str, effect_file: Path) -> None:
    effect_file.write_text("")
    subprocess.run(
        tmux_command(
            "new-session",
            "-d",
            "-s",
            name,
            "bash",
            "-c",
            f"exec cat >> {effect_file}",
        ),
        check=True,
    )
    subprocess.run(tmux_command("has-session", "-t", name), check=True)
    time.sleep(0.2)


def _session_exists(name: str) -> bool:
    return (
        subprocess.run(
            tmux_command("has-session", "-t", name),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


@pytest.mark.asyncio
async def test_clear_goes_to_matching_seat_not_victim(
    isolated_tmux_socket: str,
    isolated_manager: WorkspaceManager,
    tmp_path: Path,
) -> None:
    reviewer_tab = "aabbccdd-reviewer"
    reviewer_tmux = "claude-hub-aabbccdd"
    victim_tmux = "claude-hub-bea8f4d8"
    reviewer_effect = tmp_path / "reviewer.log"
    victim_effect = tmp_path / "victim.log"
    _start_effect_session(reviewer_tmux, reviewer_effect)
    _start_effect_session(victim_tmux, victim_effect)

    workspace = isolated_manager.create_workspace(
        WorkspaceCreate(name="seat-tmux", path=str(tmp_path), target=ExecutionTarget.LOCAL)
    )
    now = datetime.utcnow()
    session = ManagedSession(
        id="reviewer-seat",
        workspace_id=workspace.id,
        tab_id=reviewer_tab,
        role=WorkspaceSessionRole.REVIEWER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        title="isolated reviewer",
        workspace_path=str(tmp_path),
        tmux_session=reviewer_tmux,
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    isolated_manager.sessions[session.id] = session
    ttyd_manager.processes[reviewer_tab] = SimpleNamespace(tmux_session=reviewer_tmux)
    try:
        await isolated_manager.send_session_message(session.id, "/clear")
        time.sleep(0.3)
        assert "/clear" in reviewer_effect.read_text(errors="ignore")
        assert "/clear" not in victim_effect.read_text(errors="ignore")

        isolated_manager.sessions[session.id] = session.model_copy(
            update={"tmux_session": victim_tmux}
        )
        with pytest.raises(SessionSeatMismatch):
            await isolated_manager.send_session_message(session.id, "/clear")
        time.sleep(0.2)
        assert "/clear" not in victim_effect.read_text(errors="ignore")
    finally:
        ttyd_manager.processes.pop(reviewer_tab, None)


@pytest.mark.asyncio
async def test_missing_pane_is_not_recreated_before_clear(
    isolated_tmux_socket: str,
    isolated_manager: WorkspaceManager,
    tmp_path: Path,
) -> None:
    tab_id = "ccddeeff-gone"
    tmux_name = "claude-hub-ccddeeff"
    effect = tmp_path / "gone.log"
    _start_effect_session(tmux_name, effect)
    subprocess.run(tmux_command("kill-session", "-t", tmux_name), check=True)
    assert _session_exists(tmux_name) is False

    workspace = isolated_manager.create_workspace(
        WorkspaceCreate(name="seat-gone", path=str(tmp_path), target=ExecutionTarget.LOCAL)
    )
    now = datetime.utcnow()
    session = ManagedSession(
        id="reviewer-gone",
        workspace_id=workspace.id,
        tab_id=tab_id,
        role=WorkspaceSessionRole.REVIEWER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        title="gone reviewer",
        workspace_path=str(tmp_path),
        tmux_session=tmux_name,
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    isolated_manager.sessions[session.id] = session
    ttyd_manager.processes[tab_id] = SimpleNamespace(tmux_session=tmux_name)
    try:
        with pytest.raises(SessionSeatMismatch, match="gone"):
            await isolated_manager.send_session_message(session.id, "/clear")
        assert _session_exists(tmux_name) is False
    finally:
        ttyd_manager.processes.pop(tab_id, None)
