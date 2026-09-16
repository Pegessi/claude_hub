"""TraeX (``traex``) agent integration.

TraeX is a branded Codex fork, so the structured Chat transport reuses the
Codex app-server JSON-RPC engine and only swaps the launch command (the bare
``traex app-server`` — traex rejects Codex's ``--stdio`` flag). These tests pin
that wiring plus the terminal TUI launch command.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter
from claude_hub.services.agent_stream.native import (
    CodexNativeSession,
    TraexNativeSession,
    create_native_session,
    native_provider_binary,
)
from claude_hub.services.agent_stream.registry import (
    get_adapter,
    get_adapter_for_session,
    supports_structured,
)
from claude_hub.services.ttyd_manager import TTYDProcess, get_agent_command


def _managed_session(agent_type: AgentType = AgentType.TRAEX) -> ManagedSession:
    return ManagedSession(
        id="sess-traex",
        workspace_id="ws-1",
        tab_id="tab-traex",
        role=WorkspaceSessionRole.WORKER,
        agent_type=agent_type,
        status=ManagedSessionStatus.IDLE,
        title="traex",
        workspace_path="/tmp",
        tmux_session="tmux-traex",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


# ── binary mapping ──────────────────────────────────────────────────────────


def test_get_agent_command_traex() -> None:
    assert get_agent_command(AgentType.TRAEX) == "traex"


def test_native_provider_binary_traex() -> None:
    assert native_provider_binary(AgentType.TRAEX) == "traex"


# ── native Chat transport ───────────────────────────────────────────────────


def test_create_native_session_traex_is_codex_subclass() -> None:
    transport = create_native_session(_managed_session())
    assert isinstance(transport, TraexNativeSession)
    # It deliberately reuses the whole Codex JSON-RPC engine.
    assert isinstance(transport, CodexNativeSession)


def test_traex_native_build_command_has_no_stdio_flag() -> None:
    transport = TraexNativeSession(_managed_session())
    # traex's default listener is stdio:// and it rejects codex's --stdio flag.
    assert transport._build_command() == ["traex", "app-server"]
    assert transport.adapter_id == "traex-native"
    # A persistent app-server: EOF must remain fatal.
    assert transport.eof_is_fatal is True


def test_registry_traex_reuses_codex_adapter() -> None:
    assert supports_structured(AgentType.TRAEX) is True
    assert get_adapter(AgentType.TRAEX) is CodexJsonlAdapter
    adapter = get_adapter_for_session(_managed_session())
    assert isinstance(adapter, CodexJsonlAdapter)


# ── terminal TUI launch ─────────────────────────────────────────────────────


def _traex_process(solo_mode: bool = False) -> TTYDProcess:
    return TTYDProcess(
        tab_id="tab-traex-launch",
        port=13099,
        name="TraeX",
        agent_type=AgentType.TRAEX,
        solo_mode=solo_mode,
    )


def test_traex_launch_command_fresh_without_resume() -> None:
    assert _traex_process()._traex_launch_command() == "traex"


def test_traex_launch_command_solo_adds_codex_style_bypass_flags() -> None:
    assert _traex_process(solo_mode=True)._traex_launch_command() == (
        "traex --ask-for-approval never --sandbox danger-full-access"
    )


def test_traex_ttyd_command_wraps_shell_non_solo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    cmd = _traex_process()._build_ttyd_command(session_exists=False)

    assert cmd[-3:-1] == ["/bin/zsh", "-c"]
    assert "traex; exec /bin/zsh" in cmd[-1]
    assert "--ask-for-approval" not in cmd[-1]


def test_traex_ttyd_command_solo_keeps_flags_in_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    cmd = _traex_process(solo_mode=True)._build_ttyd_command(session_exists=False)

    assert cmd[-3:-1] == ["/bin/zsh", "-c"]
    assert "traex --ask-for-approval never --sandbox danger-full-access; exec /bin/zsh" in cmd[-1]


def test_traex_existing_session_reattaches_without_relaunch() -> None:
    # tmux new-session -A attaches the live session; the trailing command is
    # ignored, but must still be a benign shell/provider token, not a resume.
    last = _traex_process()._build_ttyd_command(session_exists=True)[-1]
    assert last.endswith("traex")
    assert "resume" not in last
