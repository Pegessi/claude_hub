"""TraeX (``traex``) agent integration.

TraeX is a branded Codex fork, so the structured Chat transport reuses the
Codex app-server JSON-RPC engine and only swaps the launch command (the bare
``traex app-server`` — traex rejects Codex's ``--stdio`` flag). These tests pin
that wiring plus the terminal TUI launch command and the scope boundaries
(Chat-only structured transport; no workspace worker / transcript discovery).
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timezone

import pytest

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream.codex_jsonl import (
    CodexJsonlAdapter,
    TraexJsonlAdapter,
)
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
from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess, get_agent_command

# ``from claude_hub.services import ttyd_manager`` resolves to the manager
# singleton exported by the package __init__, not the submodule; import the
# module explicitly so monkeypatching hits the real globals.
ttyd_manager_module = importlib.import_module("claude_hub.services.ttyd_manager")


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


def test_registry_traex_uses_traex_adapter_that_reuses_codex_normalization() -> None:
    assert supports_structured(AgentType.TRAEX) is True
    adapter_cls = get_adapter(AgentType.TRAEX)
    assert adapter_cls is TraexJsonlAdapter
    # Live notification normalization is inherited unchanged from Codex.
    assert issubclass(TraexJsonlAdapter, CodexJsonlAdapter)

    adapter = get_adapter_for_session(_managed_session())
    assert isinstance(adapter, TraexJsonlAdapter)


def test_traex_adapter_disables_transcript_discovery() -> None:
    # Terminal tabs must not be matched to a Codex rollout under ~/.codex, and
    # edit-resend must fail closed with "transcript not located".
    adapter = TraexJsonlAdapter()
    assert adapter.supports_transcript_discovery is False
    assert adapter.discover_source(_managed_session()) is None


# ── terminal TUI launch ─────────────────────────────────────────────────────


def _traex_process(
    solo_mode: bool = False, session_kind: SessionKind = SessionKind.TERMINAL
) -> TTYDProcess:
    return TTYDProcess(
        tab_id="tab-traex-launch",
        port=13099,
        name="TraeX",
        agent_type=AgentType.TRAEX,
        solo_mode=solo_mode,
        session_kind=session_kind,
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


def test_traex_existing_session_reattaches_without_relaunch_or_flags() -> None:
    # tmux new-session -A attaches the live session; the trailing token is
    # ignored and must carry neither a resume nor solo bypass flags.
    last = _traex_process(solo_mode=True)._build_ttyd_command(session_exists=True)[-1]
    assert last.endswith("traex")
    assert "resume" not in last
    assert "danger-full-access" not in last
    assert "exec /bin" not in last


# ── Chat kind: tmux must own only an inert shell, never launch traex ─────────


def test_traex_chat_process_shell_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    chat = _traex_process(session_kind=SessionKind.CHAT)
    assert chat.shell == "/bin/zsh"
    assert chat.shell != "traex"


def test_traex_chat_ttyd_command_never_launches_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    for session_exists in (False, True):
        cmd = _traex_process(session_kind=SessionKind.CHAT)._build_ttyd_command(
            session_exists=session_exists
        )
        assert "traex app-server" not in cmd[-1]
        assert cmd[-1].rstrip().endswith("/bin/zsh") or "CLAUDE_HUB_TAB_ID" in cmd[-1]


async def test_traex_chat_ensure_tmux_does_not_launch_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    captured: dict[str, list[str]] = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", b"")

    async def _fake_exec(*args: object, **_kwargs: object) -> _FakeProc:
        captured["cmd"] = [str(a) for a in args]
        return _FakeProc()

    async def _session_absent(_session: str) -> bool:
        return False

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _session_absent)
    monkeypatch.setattr(ttyd_manager_module, "_ensure_tmux_server", lambda: None)
    monkeypatch.setattr(ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_exec)

    await _traex_process(session_kind=SessionKind.CHAT).ensure_tmux_session()

    assert "cmd" in captured
    assert "traex app-server" not in captured["cmd"][-1]
    assert "/bin/zsh" in captured["cmd"][-1]


async def test_traex_terminal_ensure_tmux_launches_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    captured: dict[str, list[str]] = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", b"")

    async def _fake_exec(*args: object, **_kwargs: object) -> _FakeProc:
        captured["cmd"] = [str(a) for a in args]
        return _FakeProc()

    async def _session_absent(_session: str) -> bool:
        return False

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _session_absent)
    monkeypatch.setattr(ttyd_manager_module, "_ensure_tmux_server", lambda: None)
    monkeypatch.setattr(ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_exec)

    await _traex_process(solo_mode=True).ensure_tmux_session()

    assert "traex --ask-for-approval never --sandbox danger-full-access" in captured["cmd"][-1]


# ── model/env switching: Chat yes, terminal no ──────────────────────────────


async def test_traex_chat_switch_env_updates_without_respawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    chat = _traex_process(session_kind=SessionKind.CHAT)
    await chat.switch_env({"CODEX_MODEL": "Seed-Code"})
    assert chat.env["CODEX_MODEL"] == "Seed-Code"


async def test_traex_terminal_switch_env_rejected() -> None:
    # Terminal-side tmux respawn is not wired for TraeX (fresh-only TUI).
    terminal = _traex_process(session_kind=SessionKind.TERMINAL)
    with pytest.raises(ValueError):
        await terminal.switch_env({"CODEX_MODEL": "Seed-Code"})


# ── working-status classifier shares the codex-family marker set ────────────


def test_traex_working_indicator_classifies_as_working(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    process = _traex_process()

    frame = "\n".join(
        [
            "  → some prior assistant line",
            "",
            " ⠀⠞ Working  4.03k tokens",
            "",
            "",
            "› Add a follow-up",
            "",
            "  Queued follow-up inputs (3):",
            "    1. continue",
            "    2. continue",
            "    3. continue",
            "",
            "  Seed-Evolving · ~/Downloads",
        ]
    )

    status, status_text, detail, _ = manager._classify_agent_status(
        process, frame, "hash-traex-working", "traex"
    )

    assert status == AgentRuntimeStatus.WORKING
    assert status_text == "Working"
    assert detail == "agent is processing"
