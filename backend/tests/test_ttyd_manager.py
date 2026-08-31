import asyncio
import errno
import importlib
import json
import os
import shlex
import stat
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    RemoteProfile,
    SessionKind,
    TerminalTabCreate,
    WorkspaceSessionRole,
)
from claude_hub.services.ttyd_manager import (
    DEFAULT_CLAUDE_LAUNCH_ENV,
    TTYDManager,
    TTYDProcess,
)

ttyd_manager_module = importlib.import_module("claude_hub.services.ttyd_manager")
_REAL_IS_LOCAL_PORT_AVAILABLE = ttyd_manager_module._is_local_port_available


@pytest.fixture(autouse=True)
def _stub_available_ttyd_port(monkeypatch: MonkeyPatch) -> None:
    """Keep unit tests independent of host/sandbox socket permissions."""
    monkeypatch.setattr(ttyd_manager_module, "_is_local_port_available", lambda port: True)


def _run_coro_in_isolated_thread(coro) -> None:
    """Run an async assertion outside pytest-asyncio's shared-loop state."""
    errors = []

    def _runner() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "isolated async assertion timed out"
    if errors:
        raise errors[0]


def _wrapper_script(command: str) -> str:
    wrapper_path = _wrapper_path(command)
    return open(wrapper_path, encoding="utf-8").read()


def _wrapper_path(command: str) -> str:
    parts = shlex.split(command)
    return parts[1] if parts[:1] == ["/bin/sh"] else parts[0]


def _claude_settings_path(command: str) -> str:
    parts = shlex.split(command)
    if parts[:1] == ["/bin/sh"]:
        parts = shlex.split(parts[2])
    settings_index = parts.index("--settings")
    return parts[settings_index + 1]


def test_ensure_tmux_server_refreshes_launch_environment(monkeypatch: MonkeyPatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setenv("HOME", "/Users/example")
    monkeypatch.setenv("PATH", "/opt/example/bin:/usr/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "stale-test-owner")
    monkeypatch.setattr(ttyd_manager_module, "_tmux_server_running", lambda: True)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ttyd_manager_module.subprocess, "run", fake_run)

    assert ttyd_manager_module._ensure_tmux_server() is True
    assert (
        ttyd_manager_module.tmux_command("set-environment", "-g", "HOME", "/Users/example") in calls
    )
    assert (
        ttyd_manager_module.tmux_command(
            "set-environment", "-g", "PATH", "/opt/example/bin:/usr/bin"
        )
        in calls
    )
    assert ttyd_manager_module.tmux_command("set-environment", "-g", "SHELL", "/bin/zsh") in calls
    assert (
        ttyd_manager_module.tmux_command("set-environment", "-gu", "PYTEST_CURRENT_TEST") in calls
    )


def test_get_next_port_skips_existing_listener(monkeypatch: MonkeyPatch) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = 12000
    checked: list[int] = []

    def fake_is_available(port: int) -> bool:
        checked.append(port)
        return port not in {12000, 12001, 12002}

    monkeypatch.setattr(ttyd_manager_module, "_is_local_port_available", fake_is_available)

    assert manager._get_next_port() == 12003
    assert manager._next_port == 12004
    assert checked == [12000, 12001, 12002, 12003]


def test_manager_init_defers_tmux_probe_until_lifespan(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """Import-time manager construction must not block before the instance lock."""
    monkeypatch.setattr(ttyd_manager_module, "STATE_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(ttyd_manager_module, "ORDER_FILE", tmp_path / "order.json")

    def _unexpected_tmux_probe() -> bool:
        raise AssertionError("tmux must be probed only after backend ownership is acquired")

    monkeypatch.setattr(ttyd_manager_module, "_ensure_tmux_server", _unexpected_tmux_probe)

    manager = TTYDManager()

    assert manager.processes == {}


def test_get_next_port_raises_when_port_range_is_exhausted(
    monkeypatch: MonkeyPatch,
) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = ttyd_manager_module._MAX_TCP_PORT
    monkeypatch.setattr(ttyd_manager_module, "_is_local_port_available", lambda port: False)

    with pytest.raises(RuntimeError, match="No available ttyd ports remain"):
        manager._get_next_port()

    assert manager._next_port == ttyd_manager_module._MAX_TCP_PORT + 1


def test_get_next_port_can_allocate_max_tcp_port(monkeypatch: MonkeyPatch) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = ttyd_manager_module._MAX_TCP_PORT
    monkeypatch.setattr(ttyd_manager_module, "_is_local_port_available", lambda port: True)

    assert manager._get_next_port() == ttyd_manager_module._MAX_TCP_PORT
    assert manager._next_port == ttyd_manager_module._MAX_TCP_PORT + 1


def test_local_port_available_propagates_non_collision_bind_error(
    monkeypatch: MonkeyPatch,
) -> None:
    class DeniedSocket:
        def __enter__(self) -> "DeniedSocket":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def bind(self, address: tuple[str, int]) -> None:
            raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(
        ttyd_manager_module, "_is_local_port_available", _REAL_IS_LOCAL_PORT_AVAILABLE
    )
    monkeypatch.setattr(ttyd_manager_module.socket, "socket", lambda *args: DeniedSocket())

    with pytest.raises(OSError) as exc_info:
        ttyd_manager_module._is_local_port_available(12000)

    assert exc_info.value.errno == errno.EACCES


@pytest.mark.asyncio
@pytest.mark.parametrize("tmux_created", [False, True])
async def test_create_tab_start_failure_stops_unpersisted_process(
    monkeypatch: MonkeyPatch, tmp_path: Path, tmux_created: bool
) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = 12010
    manager.processes = {}
    manager._tab_order = []
    stopped: list[tuple[str, bool]] = []

    async def fake_start(self: TTYDProcess) -> None:
        raise RuntimeError("start interrupted")

    async def fake_ensure_tmux_session(self: TTYDProcess) -> bool:
        return tmux_created

    async def fake_stop(self: TTYDProcess, kill_tmux: bool = False) -> None:
        stopped.append((self.tab_id, kill_tmux))

    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", fake_ensure_tmux_session)
    monkeypatch.setattr(TTYDProcess, "start", fake_start)
    monkeypatch.setattr(TTYDProcess, "stop", fake_stop)

    with pytest.raises(RuntimeError, match="start interrupted"):
        await manager.create_tab(
            name="Cancelled tab",
            shell="/bin/zsh",
            cwd=str(tmp_path),
            agent_type=AgentType.TERMINAL,
        )

    assert [kill_tmux for _, kill_tmux in stopped] == ([False, True] if tmux_created else [False])
    assert manager.processes == {}
    assert manager._tab_order == []


@pytest.mark.asyncio
async def test_create_tab_cancellation_shields_unpersisted_process_cleanup(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = 12011
    manager.processes = {}
    manager._tab_order = []
    start_entered = asyncio.Event()
    cleanup_completed = asyncio.Event()
    stopped: list[tuple[str, bool]] = []

    async def fake_start(self: TTYDProcess) -> None:
        start_entered.set()
        await asyncio.Event().wait()

    async def fake_ensure_tmux_session(self: TTYDProcess) -> bool:
        return True

    async def fake_stop(self: TTYDProcess, kill_tmux: bool = False) -> None:
        await asyncio.sleep(0)
        stopped.append((self.tab_id, kill_tmux))
        cleanup_completed.set()

    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", fake_ensure_tmux_session)
    monkeypatch.setattr(TTYDProcess, "start", fake_start)
    monkeypatch.setattr(TTYDProcess, "stop", fake_stop)

    create_task = asyncio.create_task(
        manager.create_tab(
            name="Cancelled tab",
            shell="/bin/zsh",
            cwd=str(tmp_path),
            agent_type=AgentType.TERMINAL,
        )
    )
    await start_entered.wait()
    create_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await create_task

    assert cleanup_completed.is_set()
    assert len(stopped) == 1
    assert stopped[0][1] is True
    assert manager.processes == {}
    assert manager._tab_order == []


@pytest.mark.asyncio
async def test_create_tab_repeated_cancellation_waits_for_cleanup(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = 12012
    manager.processes = {}
    manager._tab_order = []
    start_entered = asyncio.Event()
    cleanup_entered = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_completed = asyncio.Event()

    async def fake_start(self: TTYDProcess) -> None:
        start_entered.set()
        await asyncio.Event().wait()

    async def fake_ensure_tmux_session(self: TTYDProcess) -> bool:
        return True

    async def fake_stop(self: TTYDProcess, kill_tmux: bool = False) -> None:
        assert kill_tmux is True
        cleanup_entered.set()
        await allow_cleanup.wait()
        cleanup_completed.set()

    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", fake_ensure_tmux_session)
    monkeypatch.setattr(TTYDProcess, "start", fake_start)
    monkeypatch.setattr(TTYDProcess, "stop", fake_stop)

    create_task = asyncio.create_task(
        manager.create_tab(
            name="Repeatedly cancelled tab",
            shell="/bin/zsh",
            cwd=str(tmp_path),
            agent_type=AgentType.TERMINAL,
        )
    )
    await start_entered.wait()
    create_task.cancel()
    await cleanup_entered.wait()
    create_task.cancel()
    await asyncio.sleep(0)

    assert not create_task.done()
    assert not cleanup_completed.is_set()

    allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await create_task
    assert cleanup_completed.is_set()


@pytest.mark.asyncio
async def test_create_tab_cancellation_during_tmux_creation_preserves_ownership(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = 12013
    manager.processes = {}
    manager._tab_order = []
    ensure_entered = asyncio.Event()
    allow_ensure = asyncio.Event()
    stopped: list[bool] = []

    async def fake_ensure_tmux_session(self: TTYDProcess) -> bool:
        ensure_entered.set()
        await allow_ensure.wait()
        return True

    async def fake_start(self: TTYDProcess) -> None:
        pytest.fail("ttyd must not start after cancellation during tmux creation")

    async def fake_stop(self: TTYDProcess, kill_tmux: bool = False) -> None:
        stopped.append(kill_tmux)

    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", fake_ensure_tmux_session)
    monkeypatch.setattr(TTYDProcess, "start", fake_start)
    monkeypatch.setattr(TTYDProcess, "stop", fake_stop)

    create_task = asyncio.create_task(
        manager.create_tab(
            name="Cancelled during tmux create",
            shell="/bin/zsh",
            cwd=str(tmp_path),
            agent_type=AgentType.TERMINAL,
        )
    )
    await ensure_entered.wait()
    create_task.cancel()
    await asyncio.sleep(0)
    assert not create_task.done()

    allow_ensure.set()
    with pytest.raises(asyncio.CancelledError):
        await create_task

    assert stopped == [True]


@pytest.mark.asyncio
async def test_create_tab_retries_when_allocated_port_is_claimed(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = 12020
    manager.processes = {}
    manager._tab_order = []
    attempted_ports: list[int] = []
    stopped: list[tuple[int, bool]] = []
    availability_checks: list[int] = []

    async def fake_ensure_tmux_session(self: TTYDProcess) -> bool:
        return True

    async def fake_start(self: TTYDProcess) -> None:
        attempted_ports.append(self.port)
        if len(attempted_ports) == 1:
            raise RuntimeError("ttyd failed to start")
        self.is_active = True

    async def fake_stop(self: TTYDProcess, kill_tmux: bool = False) -> None:
        stopped.append((self.port, kill_tmux))

    def fake_is_available(port: int) -> bool:
        availability_checks.append(port)
        return port != 12020 or availability_checks.count(12020) == 1

    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", fake_ensure_tmux_session)
    monkeypatch.setattr(TTYDProcess, "start", fake_start)
    monkeypatch.setattr(TTYDProcess, "stop", fake_stop)
    monkeypatch.setattr(ttyd_manager_module, "_is_local_port_available", fake_is_available)
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(manager, "_save_order", lambda: None)
    monkeypatch.setattr(manager, "_schedule_codex_discovery", lambda process: None)

    tab = await manager.create_tab(
        name="Raced port",
        shell="/bin/zsh",
        cwd=str(tmp_path),
        agent_type=AgentType.TERMINAL,
    )

    assert attempted_ports == [12020, 12021]
    assert availability_checks == [12020, 12020, 12021]
    assert stopped == [(12020, False)]
    assert tab.port == 12021
    assert manager.processes[tab.id].port == 12021


def test_codex_tab_uses_codex_command() -> None:
    process = TTYDProcess(
        tab_id="tab-codex-normal",
        port=12345,
        name="Codex",
        agent_type=AgentType.CODEX,
    )

    assert process.shell == "codex"
    assert process._build_ttyd_command(session_exists=False)[-1] == "codex"


def test_tab_session_kind_defaults_to_terminal_and_round_trips() -> None:
    legacy = TTYDProcess(
        tab_id="legacy-tab",
        port=12346,
        name="Legacy",
        agent_type=AgentType.CLAUDE,
    )
    agent = TTYDProcess(
        tab_id="agent-tab",
        port=12347,
        name="Agent",
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.AGENT,
    )

    assert legacy.session_kind == SessionKind.TERMINAL
    assert legacy.to_dict()["session_kind"] == "terminal"
    assert legacy.to_schema().session_kind == SessionKind.TERMINAL
    assert agent.session_kind == SessionKind.AGENT
    assert agent.to_dict()["session_kind"] == "agent"
    assert agent.to_schema().session_kind == SessionKind.AGENT


def test_structured_agent_session_rejects_plain_terminal_provider() -> None:
    with pytest.raises(ValueError, match="Agent sessions require"):
        TerminalTabCreate(
            name="Invalid Agent",
            agent_type=AgentType.TERMINAL,
            session_kind=SessionKind.AGENT,
        )


@pytest.mark.asyncio
async def test_direct_cursor_agent_pins_supported_transcript_transport(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = 12348
    manager.processes = {}
    manager._tab_order = []

    async def fake_ensure_tmux_session(self: TTYDProcess) -> bool:
        return True

    async def fake_start(self: TTYDProcess) -> None:
        self.is_active = True

    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", fake_ensure_tmux_session)
    monkeypatch.setattr(TTYDProcess, "start", fake_start)
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(manager, "_save_order", lambda: None)
    monkeypatch.setattr(manager, "_schedule_codex_discovery", lambda process: None)
    monkeypatch.setattr(
        ttyd_manager_module,
        "cursor_cli_version_from_executable",
        lambda: next(iter(ttyd_manager_module.SUPPORTED_CURSOR_TRANSCRIPT_VERSIONS)),
    )
    monkeypatch.setattr(
        ttyd_manager_module,
        "cursor_data_dir_for_env",
        lambda env: str(tmp_path / "cursor-data"),
    )

    tab = await manager.create_tab(
        name="Cursor Agent",
        cwd=str(tmp_path),
        agent_type=AgentType.CURSOR,
        session_kind=SessionKind.AGENT,
    )

    process = manager.processes[tab.id]
    assert process.cursor_transport == "terminal_transcript"
    assert process.cursor_transcript_schema == ttyd_manager_module.CURSOR_TRANSCRIPT_SCHEMA
    assert process.agent_session_id
    assert process.agent_session_id in (process.cursor_transcript_path or "")


@pytest.mark.asyncio
async def test_cursor_terminal_session_keeps_native_terminal_transport(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = 12349
    manager.processes = {}
    manager._tab_order = []

    async def fake_ensure_tmux_session(self: TTYDProcess) -> bool:
        return True

    async def fake_start(self: TTYDProcess) -> None:
        self.is_active = True

    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", fake_ensure_tmux_session)
    monkeypatch.setattr(TTYDProcess, "start", fake_start)
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(manager, "_save_order", lambda: None)
    monkeypatch.setattr(manager, "_schedule_codex_discovery", lambda process: None)

    tab = await manager.create_tab(
        name="Cursor Terminal",
        cwd=str(tmp_path),
        agent_type=AgentType.CURSOR,
        session_kind=SessionKind.TERMINAL,
    )

    assert manager.processes[tab.id].cursor_transport == "terminal"


@pytest.mark.asyncio
async def test_managed_nonterminal_tab_is_classified_as_agent(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._next_port = 12350
    manager.processes = {}
    manager._tab_order = []

    async def fake_ensure_tmux_session(self: TTYDProcess) -> bool:
        return True

    async def fake_start(self: TTYDProcess) -> None:
        self.is_active = True

    monkeypatch.setattr(TTYDProcess, "ensure_tmux_session", fake_ensure_tmux_session)
    monkeypatch.setattr(TTYDProcess, "start", fake_start)
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(manager, "_save_order", lambda: None)
    monkeypatch.setattr(manager, "_schedule_codex_discovery", lambda process: None)

    tab = await manager.create_tab(
        name="Managed Claude",
        cwd=str(tmp_path),
        agent_type=AgentType.CLAUDE,
        workspace_id="workspace-1",
    )

    assert tab.session_kind == SessionKind.AGENT


def test_non_solo_codex_initial_launch_translates_model_and_keeps_env_wrapper(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ttyd_manager_module, "LAUNCH_ENV_DIR", tmp_path)
    process = TTYDProcess(
        tab_id="tab-codex-model-fresh",
        port=12344,
        name="Codex Model Fresh",
        agent_type=AgentType.CODEX,
        solo_mode=False,
        env={"CODEX_MODEL": "gpt-5.6-codex", "FEATURE_FLAG": "1"},
    )

    launch = process._build_ttyd_command(session_exists=False)[-1]
    parts = shlex.split(launch)

    assert parts[:2] == ["/bin/sh", str(tmp_path / "tab-codex-model-fresh.sh")]
    assert parts[2] == "codex --model gpt-5.6-codex"
    wrapper = _wrapper_script(launch)
    assert "export CODEX_MODEL=gpt-5.6-codex" in wrapper
    assert "export FEATURE_FLAG=1" in wrapper
    assert "--ask-for-approval" not in parts[2]
    assert "--sandbox" not in parts[2]


def test_non_solo_codex_recover_translates_model_and_keeps_env_wrapper(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    pinned = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    monkeypatch.setattr(ttyd_manager_module, "LAUNCH_ENV_DIR", tmp_path)
    monkeypatch.setattr(
        ttyd_manager_module,
        "_codex_id_exists",
        lambda sid, cwd=None: sid == pinned and cwd == "/workspace",
    )
    process = TTYDProcess(
        tab_id="tab-codex-model-recover",
        port=12343,
        name="Codex Model Recover",
        agent_type=AgentType.CODEX,
        solo_mode=False,
        cwd="/workspace",
        from_persisted_state=True,
        agent_session_id=pinned,
        env={"CODEX_MODEL": "gpt-5.6-codex", "FEATURE_FLAG": "1"},
    )

    launch = process._build_ttyd_command(session_exists=False)[-1]
    parts = shlex.split(launch)

    assert parts[:2] == ["/bin/sh", str(tmp_path / "tab-codex-model-recover.sh")]
    assert parts[2] == (
        f"codex resume {pinned} --model gpt-5.6-codex " "|| codex --model gpt-5.6-codex"
    )
    wrapper = _wrapper_script(launch)
    assert "export CODEX_MODEL=gpt-5.6-codex" in wrapper
    assert "export FEATURE_FLAG=1" in wrapper
    assert "--ask-for-approval" not in parts[2]
    assert "--sandbox" not in parts[2]


def test_codex_solo_mode_command(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    process = TTYDProcess(
        tab_id="tab-codex-solo",
        port=12346,
        name="Codex Solo",
        solo_mode=True,
        agent_type=AgentType.CODEX,
    )

    cmd = process._build_ttyd_command(session_exists=False)

    assert cmd[-3:] == [
        "/bin/zsh",
        "-c",
        "codex --ask-for-approval never --sandbox danger-full-access; exec /bin/zsh",
    ]


def test_codex_solo_mode_reattaches_existing_tmux_session() -> None:
    process = TTYDProcess(
        tab_id="tab-codex-existing",
        port=12347,
        name="Codex Existing",
        solo_mode=True,
        agent_type=AgentType.CODEX,
    )

    assert process._build_ttyd_command(session_exists=True)[-1] == "codex"


def test_custom_env_is_injected_and_serialized() -> None:
    process = TTYDProcess(
        tab_id="tab-env",
        port=12348,
        name="Env Tab",
        agent_type=AgentType.TERMINAL,
        env={"HTTP_PROXY": "http://127.0.0.1:7890", "NO_PROXY": "localhost,127.0.0.1"},
    )

    cmd = process._build_ttyd_command(session_exists=False)
    data = process.to_dict()
    schema = process.to_schema()

    wrapper_path = _wrapper_path(cmd[-1])
    wrapper_mode = stat.S_IMODE(os.stat(wrapper_path).st_mode)
    wrapper = _wrapper_script(cmd[-1])

    assert "HTTP_PROXY=http://127.0.0.1:7890" not in cmd[-1]
    assert "NO_PROXY=localhost,127.0.0.1" not in cmd[-1]
    assert wrapper_mode == 0o600
    assert "export HTTP_PROXY=http://127.0.0.1:7890" in wrapper
    assert "export NO_PROXY=localhost,127.0.0.1" in wrapper
    assert data["env"] == {
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "NO_PROXY": "localhost,127.0.0.1",
        "http_proxy": "http://127.0.0.1:7890",
        "no_proxy": "localhost,127.0.0.1",
    }
    assert schema.env == {
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "NO_PROXY": "localhost,127.0.0.1",
        "http_proxy": "http://127.0.0.1:7890",
        "no_proxy": "localhost,127.0.0.1",
    }


def test_local_env_wrapper_command_executes_without_executable_bit() -> None:
    process = TTYDProcess(
        tab_id="tab-env-exec",
        port=12355,
        name="Env Exec Tab",
        agent_type=AgentType.TERMINAL,
        env={"CLAUDE_HUB_TEST_VALUE": "wrapper ok"},
    )

    command = process._with_env(
        'printf \'%s\' "$CLAUDE_HUB_TEST_VALUE"; test -n "$CLAUDE_HUB_TEST_VALUE"'
    )
    wrapper_path = _wrapper_path(command)
    wrapper_mode = stat.S_IMODE(os.stat(wrapper_path).st_mode)
    result = subprocess.run(
        shlex.split(command),
        check=True,
        capture_output=True,
        text=True,
    )

    assert command.startswith("/bin/sh ")
    assert wrapper_mode == 0o600
    assert result.stdout == "wrapper ok"


def test_custom_env_rejects_invalid_names() -> None:
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        TTYDProcess(
            tab_id="tab-invalid-env",
            port=12348,
            name="Invalid Env Tab",
            agent_type=AgentType.TERMINAL,
            env={"BAD-NAME": "value"},
        )


def test_claude_env_model_is_passed_as_startup_model_flag() -> None:
    process = TTYDProcess(
        tab_id="tab-claude-model-env",
        port=12349,
        name="Claude Model Env",
        agent_type=AgentType.CLAUDE,
        env={"ANTHROPIC_MODEL": "claude-opus-4-8"},
    )

    cmd = process._build_ttyd_command(session_exists=False)

    wrapper = _wrapper_script(cmd[-1])

    assert "ANTHROPIC_MODEL=claude-opus-4-8" not in cmd[-1]
    assert "--model claude-opus-4-8" in cmd[-1]
    assert "--settings " in cmd[-1]
    assert "export ANTHROPIC_MODEL=claude-opus-4-8" in wrapper


def test_claude_launch_defaults_to_volcengine_model_env() -> None:
    process = TTYDProcess(
        tab_id="tab-claude-default-model-env",
        port=12356,
        name="Claude Default Model Env",
        agent_type=AgentType.CLAUDE,
    )

    cmd = process._build_ttyd_command(session_exists=False)
    wrapper = _wrapper_script(cmd[-1])
    settings_path = _claude_settings_path(cmd[-1])
    settings_mode = stat.S_IMODE(os.stat(settings_path).st_mode)
    settings = json.load(open(settings_path, encoding="utf-8"))

    for key, value in DEFAULT_CLAUDE_LAUNCH_ENV.items():
        assert process.env[key] == value
        assert f"export {key}={shlex.quote(value)}" in wrapper
        assert settings["env"][key] == value
    assert "--model doubao-seed-2.0-code" in cmd[-1]
    assert settings_mode == 0o600
    assert "deepseek" not in json.dumps(settings)


def test_claude_explicit_env_overrides_default_model_env() -> None:
    process = TTYDProcess(
        tab_id="tab-claude-override-model-env",
        port=12357,
        name="Claude Override Model Env",
        agent_type=AgentType.CLAUDE,
        env={"ANTHROPIC_MODEL": "claude-opus-4-8"},
    )

    cmd = process._build_ttyd_command(session_exists=False)
    settings = json.load(open(_claude_settings_path(cmd[-1]), encoding="utf-8"))

    assert "ANTHROPIC_BASE_URL" not in process.env
    assert process.env["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    assert settings["env"] == {"ANTHROPIC_MODEL": "claude-opus-4-8"}
    assert "--model claude-opus-4-8" in cmd[-1]


def test_claude_solo_env_model_is_passed_as_startup_model_flag(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    process = TTYDProcess(
        tab_id="tab-claude-solo-model-env",
        port=12350,
        name="Claude Solo Model Env",
        solo_mode=True,
        agent_type=AgentType.CLAUDE,
        env={"ANTHROPIC_MODEL": "gateway/model with space"},
    )

    cmd = process._build_ttyd_command(session_exists=False)

    assert cmd[-3:-1] == ["/bin/zsh", "-c"]
    assert "tab-claude-solo-model-env.sh" in cmd[-1]
    assert "IS_SANDBOX=1 claude --dangerously-skip-permissions" in cmd[-1]
    assert "--model " in cmd[-1]
    wrapper = _wrapper_script(cmd[-1])

    assert "ANTHROPIC_MODEL='gateway/model with space'" not in cmd[-1]
    assert "ANTHROPIC_CUSTOM_MODEL_OPTION='gateway/model with space'" not in cmd[-1]
    assert "export ANTHROPIC_MODEL='gateway/model with space'" in wrapper
    assert "ANTHROPIC_CUSTOM_MODEL_OPTION" not in wrapper
    assert "ANTHROPIC_CUSTOM_MODEL_OPTION" not in process.env


def test_structured_claude_solo_preaccepts_bypass_disclaimer_as_flag_setting() -> None:
    process = TTYDProcess(
        tab_id="tab-structured-claude-solo",
        port=12358,
        name="Structured Claude Solo",
        solo_mode=True,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.AGENT,
        env={"ANTHROPIC_MODEL": "gateway/model"},
    )

    cmd = process._build_ttyd_command(session_exists=False)
    settings = json.load(open(_claude_settings_path(cmd[-1]), encoding="utf-8"))

    assert "bypassPermissionsModeAccepted" not in settings
    assert "bypassPermissionsModeAccepted" in cmd[-1]
    assert cmd[-1].count("--settings") == 2


def test_terminal_claude_solo_keeps_interactive_bypass_disclaimer() -> None:
    process = TTYDProcess(
        tab_id="tab-terminal-claude-solo",
        port=12359,
        name="Terminal Claude Solo",
        solo_mode=True,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.TERMINAL,
        env={"ANTHROPIC_MODEL": "gateway/model"},
    )

    cmd = process._build_ttyd_command(session_exists=False)
    settings = json.load(open(_claude_settings_path(cmd[-1]), encoding="utf-8"))

    assert "bypassPermissionsModeAccepted" not in settings
    assert cmd[-1].count("--settings") == 1


def test_claude_custom_model_env_option_is_not_overwritten() -> None:
    process = TTYDProcess(
        tab_id="tab-claude-explicit-custom-model",
        port=12351,
        name="Claude Explicit Custom Model",
        agent_type=AgentType.CLAUDE,
        env={
            "ANTHROPIC_MODEL": "ark/seed-code-0602[1m]",
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "ark/seed-code-0602",
        },
    )

    cmd = process._build_ttyd_command(session_exists=False)

    assert process.env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "ark/seed-code-0602"
    assert "--model " in cmd[-1]
    assert "export ANTHROPIC_MODEL='ark/seed-code-0602[1m]'" in _wrapper_script(cmd[-1])


def test_claude_volcengine_coding_plan_model_alias_is_normalized() -> None:
    process = TTYDProcess(
        tab_id="tab-claude-volcengine-model",
        port=12352,
        name="Claude Volcengine Model",
        agent_type=AgentType.CLAUDE,
        env={
            "ANTHROPIC_BASE_URL": "https://ark.cn-beijing.volces.com/api/coding",
            "ANTHROPIC_AUTH_TOKEN": "token",
            "ANTHROPIC_MODEL": "ark/seed-code-0602",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "ark/seed-code-0602",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "ark/seed-code-0602",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "ark/seed-code-0602",
            "CLAUDE_CODE_SUBAGENT_MODEL": "ark/seed-code-0602",
        },
    )

    cmd = process._build_ttyd_command(session_exists=False)

    assert process.env["ANTHROPIC_MODEL"] == "doubao-seed-2.0-code"
    assert process.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "doubao-seed-2.0-code"
    assert process.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "doubao-seed-2.0-code"
    assert process.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "doubao-seed-2.0-code"
    assert process.env["CLAUDE_CODE_SUBAGENT_MODEL"] == "doubao-seed-2.0-code"
    assert "--model doubao-seed-2.0-code" in cmd[-1]


def test_claude_volcengine_coding_plan_template_typo_alias_is_normalized() -> None:
    process = TTYDProcess(
        tab_id="tab-claude-volcengine-template-typo-model",
        port=12353,
        name="Claude Volcengine Template Typo Model",
        agent_type=AgentType.CLAUDE,
        env={
            "ANTHROPIC_BASE_URL": "https://ark.cn-beijing.volces.com/api/coding",
            "ANTHROPIC_MODEL": "ark/seed-code-6062",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "ark/seed-code-6062",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "ark/seed-code-6062",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "ark/seed-code-6062",
            "CLAUDE_CODE_SUBAGENT_MODEL": "ark/seed-code-6062",
        },
    )

    cmd = process._build_ttyd_command(session_exists=False)

    assert process.env["ANTHROPIC_MODEL"] == "doubao-seed-2.0-code"
    assert process.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "doubao-seed-2.0-code"
    assert process.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "doubao-seed-2.0-code"
    assert process.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "doubao-seed-2.0-code"
    assert process.env["CLAUDE_CODE_SUBAGENT_MODEL"] == "doubao-seed-2.0-code"
    assert "ark/seed-code-6062" not in cmd[-1]
    assert "--model doubao-seed-2.0-code" in cmd[-1]


def test_claude_super_relay_launch_env_is_preserved_with_proxy() -> None:
    process = TTYDProcess(
        tab_id="tab-claude-super-relay",
        port=12354,
        name="Claude Super Relay",
        agent_type=AgentType.CLAUDE,
        env={
            "ANTHROPIC_BASE_URL": "https://super-relay.byted.org",
            "ANTHROPIC_AUTH_TOKEN": "token",
            "ANTHROPIC_MODEL": "ark/seed-code-0602",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "ark/seed-code-0602",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "ark/seed-code-0602",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "ark/seed-code-0602",
            "CLAUDE_CODE_SUBAGENT_MODEL": "ark/seed-code-0602",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "140000",
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            "HTTP_PROXY": "http://127.0.0.1:23456",
            "HTTPS_PROXY": "http://127.0.0.1:23456",
        },
    )

    cmd = process._build_ttyd_command(session_exists=False)
    wrapper = _wrapper_script(cmd[-1])
    settings = json.load(open(_claude_settings_path(cmd[-1]), encoding="utf-8"))

    assert process.env["ANTHROPIC_BASE_URL"] == "https://super-relay.byted.org"
    assert process.env["ANTHROPIC_MODEL"] == "ark/seed-code-0602"
    assert process.env["HTTP_PROXY"] == "http://127.0.0.1:23456"
    assert process.env["HTTPS_PROXY"] == "http://127.0.0.1:23456"
    assert "ANTHROPIC_CUSTOM_MODEL_OPTION" not in process.env
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in process.env
    assert "https://127.0.0.1:" not in json.dumps(settings)
    assert "export ANTHROPIC_BASE_URL=https://super-relay.byted.org" in wrapper
    assert "export ANTHROPIC_MODEL=ark/seed-code-0602" in wrapper
    assert "export HTTP_PROXY=http://127.0.0.1:23456" in wrapper
    assert "export HTTPS_PROXY=http://127.0.0.1:23456" in wrapper
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://super-relay.byted.org"
    assert settings["env"]["ANTHROPIC_MODEL"] == "ark/seed-code-0602"
    assert "--model ark/seed-code-0602" in cmd[-1]


def test_workspace_metadata_is_serialized_to_tab_schema_and_state() -> None:
    process = TTYDProcess(
        tab_id="tab-workspace-agent",
        port=12348,
        name="Workspace Agent",
        cwd="/tmp/project",
        solo_mode=True,
        agent_type=AgentType.CODEX,
        workspace_id="workspace-1",
        workspace_name="Workspace One",
        workspace_role=WorkspaceSessionRole.ORCHESTRATOR,
    )

    data = process.to_dict()
    schema = process.to_schema()

    assert data["workspace_id"] == "workspace-1"
    assert data["workspace_name"] == "Workspace One"
    assert data["workspace_role"] == "orchestrator"
    assert schema.workspace_id == "workspace-1"
    assert schema.workspace_name == "Workspace One"
    assert schema.workspace_role == WorkspaceSessionRole.ORCHESTRATOR


def test_remote_codex_solo_mode_uses_reconnect_launcher(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(
        ttyd_manager_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: RemoteProfile(
            id=profile_id,
            name="DevBox",
            ssh_host="devbox",
            default_cwd="~/workspace",
        ),
    )
    process = TTYDProcess(
        tab_id="tab-remote-codex",
        port=12348,
        name="Remote Codex",
        solo_mode=True,
        agent_type=AgentType.CODEX,
        target=ExecutionTarget.REMOTE,
        remote_profile_id="devbox",
        remote_cwd="~/repo",
        remote_reconnect=True,
    )

    cmd = process._build_ttyd_command(session_exists=False)

    assert cmd[-3] == "/bin/zsh"
    assert cmd[-2] == "-lc"
    launcher = cmd[-1]
    assert "while true; do" in launcher
    assert "ssh -tt -o LogLevel=ERROR devbox" in launcher
    assert "$HOME/.nvm/versions/node" in launcher
    assert "cd ~/repo" in launcher
    assert "Remote cwd not found: %s; using home directory" in launcher
    assert "cd ~ ||" in launcher
    assert "tmux new-session -d -s claude-hub-tab-remo" in launcher
    assert "tmux set-option -t claude-hub-tab-remo status off" in launcher
    assert "tmux set-option -t claude-hub-tab-remo mouse off" in launcher
    assert "tmux set-option -t claude-hub-tab-remo focus-events on" in launcher
    assert "tmux set-window-option -t claude-hub-tab-remo mode-keys vi" in launcher
    assert "exec tmux attach-session -t claude-hub-tab-remo" in launcher
    assert "Remote tmux not found in PATH; starting without remote tmux persistence" in launcher
    assert "codex --ask-for-approval never --sandbox danger-full-access" in launcher


def test_remote_launcher_starts_agent_after_missing_cwd_fallback(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ttyd_manager_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: RemoteProfile(
            id=profile_id,
            name="DevBox",
            ssh_host="devbox",
        ),
    )
    process = TTYDProcess(
        tab_id="tab-remote-claude",
        port=12356,
        name="Remote Claude",
        solo_mode=True,
        agent_type=AgentType.CLAUDE,
        target=ExecutionTarget.REMOTE,
        remote_profile_id="devbox",
        remote_cwd="/Users/local/project",
    )

    launcher = process._build_ttyd_command(session_exists=False)[-1]

    assert "cd /Users/local/project ||" in launcher
    assert "Remote cwd not found: %s; using home directory" in launcher
    assert "cd ~ ||" in launcher
    assert launcher.index("cd /Users/local/project ||") < launcher.index(
        "tmux new-session -d -s claude-hub-tab-remo"
    )
    assert "IS_SANDBOX=1 claude --dangerously-skip-permissions" in launcher


def test_remote_terminal_can_disable_reconnect(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        ttyd_manager_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: RemoteProfile(
            id=profile_id,
            name="DevBox",
            ssh_host="devbox",
            user="tiger",
            port=2222,
        ),
    )
    process = TTYDProcess(
        tab_id="tab-remote-terminal",
        port=12349,
        name="Remote Terminal",
        agent_type=AgentType.TERMINAL,
        target=ExecutionTarget.REMOTE,
        remote_profile_id="devbox",
        remote_cwd="/opt/tiger/app",
        remote_reconnect=False,
    )

    launcher = process._build_ttyd_command(session_exists=False)[-1]

    assert launcher.startswith("exec ")
    assert "ssh -tt -o LogLevel=ERROR -p 2222 tiger@devbox" in launcher
    assert "cd /opt/tiger/app" in launcher
    assert "${SHELL:-/bin/bash} -l" in launcher


def test_remote_workspace_forwarding_adds_reverse_ssh_port(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        ttyd_manager_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: RemoteProfile(
            id=profile_id,
            name="DevBox",
            ssh_host="devbox",
            user="tiger",
        ),
    )
    process = TTYDProcess(
        tab_id="tab-remote-forward",
        port=12351,
        name="Remote Forward",
        agent_type=AgentType.CODEX,
        target=ExecutionTarget.REMOTE,
        remote_profile_id="devbox",
        remote_forward_port=18173,
    )

    launcher = process._build_ttyd_command(session_exists=False)[-1]

    assert "-o ExitOnForwardFailure=yes" in launcher
    assert "-R 127.0.0.1:18173:127.0.0.1:8173" in launcher
    assert "ssh -tt -o LogLevel=ERROR" in launcher
    assert "tiger@devbox" in launcher


def test_remote_capture_ssh_command_is_noninteractive(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        ttyd_manager_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: RemoteProfile(
            id=profile_id,
            name="DevBox",
            ssh_host="devbox",
            user="tiger",
            port=2222,
        ),
    )
    process = TTYDProcess(
        tab_id="tab-remote-capture",
        port=12352,
        name="Remote Capture",
        target=ExecutionTarget.REMOTE,
        remote_profile_id="devbox",
    )

    cmd = process._build_remote_ssh_command("tmux capture-pane")

    assert cmd == [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "LogLevel=ERROR",
        "-p",
        "2222",
        "tiger@devbox",
        "tmux capture-pane",
    ]


@pytest.mark.asyncio
async def test_remote_history_prefers_remote_tmux_capture(monkeypatch: MonkeyPatch) -> None:
    process = TTYDProcess(
        tab_id="tab-remote-history",
        port=12353,
        name="Remote History",
        target=ExecutionTarget.REMOTE,
        remote_profile_id="devbox",
    )
    commands: list[str] = []

    async def fake_remote(remote_command: str) -> str:
        commands.append(remote_command)
        return "remote scrollback\nremote visible\n"

    async def fail_local(_lines: int = 100000) -> str:
        raise AssertionError("local history should not be used when remote capture succeeds")

    monkeypatch.setattr(process, "_run_remote_capture_command", fake_remote)
    monkeypatch.setattr(process, "_capture_local_history", fail_local)

    history = await process.capture_history(lines=500, prefer_remote=True)

    assert history == "remote scrollback\nremote visible\n"
    assert len(commands) == 1
    assert "tmux capture-pane -p -e -S -500 -t claude-hub-tab-remo" in commands[0]


@pytest.mark.asyncio
async def test_remote_history_falls_back_to_local_tmux_on_capture_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    process = TTYDProcess(
        tab_id="tab-remote-fallback",
        port=12354,
        name="Remote Fallback",
        target=ExecutionTarget.REMOTE,
        remote_profile_id="devbox",
    )

    async def fail_remote(_lines: int = 100000) -> str:
        raise RuntimeError("ssh unavailable")

    async def fake_local(_lines: int = 100000) -> str:
        return "local visible\n"

    monkeypatch.setattr(process, "_capture_remote_history", fail_remote)
    monkeypatch.setattr(process, "_capture_local_history", fake_local)

    assert await process.capture_history(lines=500, prefer_remote=True) == "local visible\n"


@pytest.mark.asyncio
async def test_get_tab_history_requests_remote_preferred_capture(monkeypatch: MonkeyPatch) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    process = TTYDProcess(
        tab_id="tab-1",
        port=12355,
        name="History Tab",
    )
    calls: list[tuple[int, bool]] = []

    async def fake_capture_history(lines: int, prefer_remote: bool = False) -> str:
        calls.append((lines, prefer_remote))
        return "history"

    monkeypatch.setattr(process, "capture_history", fake_capture_history)
    manager.processes = {process.tab_id: process}

    assert await manager.get_tab_history("tab-1", lines=250) == "history"
    assert calls == [(250, True)]


def test_claude_spinner_status_classifies_as_working(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    process = TTYDProcess(
        tab_id="tab-claude-spinner",
        port=12350,
        name="Claude Spinner",
        agent_type=AgentType.CLAUDE,
    )

    status, status_text, detail, _last_changed_at = manager._classify_agent_status(
        process,
        "\n".join(
            [
                '⏺ Bash(ssh merlin_dev_ff45d_16 "grep -rn forward ...")',
                "  ⎿  ... +44 lines (ctrl+o to expand)",
                "✢ Gusting… (52s · ↑ 1.5k tokens · thought for 2s)",
                "  ⎿  Tip: Use /btw to ask a quick side question",
                "     without interrupting Claude's current work",
                "────────────────────────────────────────────────────",
                "❯ ",
                "────────────────────────────────────────────────────",
                "  ⏵⏵ bypass permissions on (shift+tab to cycle) ·",
            ]
        ),
        "hash",
        "zsh",
    )

    assert status == AgentRuntimeStatus.WORKING
    assert status_text == "Working"
    assert detail == "agent is processing"


def test_codex_running_tool_status_classifies_as_working(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    process = TTYDProcess(
        tab_id="tab-codex-running",
        port=12351,
        name="Codex Running",
        agent_type=AgentType.CODEX,
    )

    status, status_text, detail, _last_changed_at = manager._classify_agent_status(
        process,
        "\n".join(
            [
                '⏺ Bash(ssh merlin_dev "pytest tests")',
                "  ⎿  Running… (5s)",
                "     (ctrl+b ctrl+b (twice) to run in background)",
                "────────────────────────────────────────────────────",
                "❯ ",
                "────────────────────────────────────────────────────",
                "  ⏵⏵ bypass permissions on (shift+tab to cycle) ·",
            ]
        ),
        "hash",
        "zsh",
    )

    assert status == AgentRuntimeStatus.WORKING
    assert status_text == "Working"
    assert detail == "agent is processing"


def test_cursor_running_tokens_status_classifies_as_working(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    process = TTYDProcess(
        tab_id="tab-cursor-running",
        port=12354,
        name="Cursor Running",
        agent_type=AgentType.CURSOR,
    )

    status, status_text, detail, _last_changed_at = manager._classify_agent_status(
        process,
        "\n".join(
            [
                "  Read ...c634c238-083f-42d5-8dce-ec6929738d79/snapshot.md",
                "  $ curl -sS -X POST http://localhost:8173/api/workspaces/...",
                "  $ ssh merlin_dev_ff45d_16 'ls /opt/tiger/xperf_gpt_triton'",
                "",
                "  Running 662 tokens",
                "› Add a follow-up",
                "ctrl+c to stop",
                "Opus 4.7 1M High Thinking · MAX · 4.2%   Auto-run",
            ]
        ),
        "hash-cursor",
        "agent",
    )

    assert status == AgentRuntimeStatus.WORKING
    assert status_text == "Working"
    assert detail == "agent is processing"


_FROZEN_WORKING_FRAME = "\n".join(
    [
        "⏺ Now let me also add CSS for the attachment empty hint.",
        "✻ Implementing frontend task edit modal enhancements… (14m 59s · ↑ 16.9k tokens)",
        "  ⎿  ✔ Implement backend schema and update_task changes",
        "     ◼ Implement frontend task edit/create modal enhancements",
        "     ◻ Validate changes and update changelog",
        "────────────────────────────────────────────────────",
        "❯ ",
        "────────────────────────────────────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ctrl+t to hide tasks",
    ]
)


def test_frozen_working_frame_classifies_as_stuck(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    # Simulate a frame first observed well past the staleness window: the agent
    # left a working frame on screen but nothing has changed since.
    first_seen = datetime.now() - timedelta(
        seconds=ttyd_manager_module._WORKING_FRAME_STALE_SECONDS + 30
    )
    manager._status_snapshots = {
        "tab-frozen": {
            "hash": "frozen-hash",
            "last_changed_at": first_seen,
            "frame_first_seen_at": first_seen,
        }
    }
    process = TTYDProcess(
        tab_id="tab-frozen",
        port=12360,
        name="Frozen Working",
        agent_type=AgentType.CLAUDE,
    )

    status, status_text, _detail, _last_changed_at = manager._classify_agent_status(
        process,
        _FROZEN_WORKING_FRAME,
        "frozen-hash",
        "zsh",
    )

    assert status == AgentRuntimeStatus.ATTENTION
    assert status_text == "Agent may be stuck"


def test_ticking_working_frame_stays_working(monkeypatch: MonkeyPatch) -> None:
    """A live spinner repaints each sample, so a changed frame stays WORKING."""

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    # Previous sample had a different hash and is old; the new (changed) frame
    # resets frame_first_seen_at, so it must not be treated as stale.
    old = datetime.now() - timedelta(seconds=ttyd_manager_module._WORKING_FRAME_STALE_SECONDS + 120)
    manager._status_snapshots = {
        "tab-ticking": {
            "hash": "previous-hash",
            "last_changed_at": old,
            "frame_first_seen_at": old,
        }
    }
    process = TTYDProcess(
        tab_id="tab-ticking",
        port=12361,
        name="Ticking Working",
        agent_type=AgentType.CLAUDE,
    )

    status, status_text, detail, _last_changed_at = manager._classify_agent_status(
        process,
        _FROZEN_WORKING_FRAME,  # same content, but a NEW hash this sample
        "new-hash",
        "zsh",
    )

    assert status == AgentRuntimeStatus.WORKING
    assert status_text == "Working"
    assert detail == "agent is processing"


def test_frozen_working_frame_with_task_panel_classifies_as_stuck(
    monkeypatch: MonkeyPatch,
) -> None:
    """Persistent bottom task panel must not keep a stopped agent 'working'."""

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    first_seen = datetime.now() - timedelta(
        seconds=ttyd_manager_module._WORKING_FRAME_STALE_SECONDS + 5
    )
    manager._status_snapshots = {
        "tab-panel": {
            "hash": "panel-hash",
            "last_changed_at": first_seen,
            "frame_first_seen_at": first_seen,
        }
    }
    process = TTYDProcess(
        tab_id="tab-panel",
        port=12362,
        name="Frozen Panel",
        agent_type=AgentType.CLAUDE,
    )

    output = "\n".join(
        [
            "✻ Implementing frontend task edit modal enhancements… (8m 2s · ↑ 9k tokens)",
            "  3 tasks (0 done, 1 in progress, 2 open)",
            "  ◼ Implement frontend task edit/create modal enhancements",
            "  ◻ Validate changes and update changelog",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ctrl+t to hide tasks",
        ]
    )

    status, status_text, _detail, _last_changed_at = manager._classify_agent_status(
        process,
        output,
        "panel-hash",
        "zsh",
    )

    assert status == AgentRuntimeStatus.ATTENTION
    assert status_text == "Agent may be stuck"


def test_codex_selection_prompt_classifies_as_attention(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    process = TTYDProcess(
        tab_id="tab-codex-selection",
        port=12352,
        name="Codex Selection",
        agent_type=AgentType.CODEX,
    )

    status, status_text, detail, _last_changed_at = manager._classify_agent_status(
        process,
        "\n".join(
            [
                "待你决定:",
                "- 这是 feature branch, 要不要我 git push?",
                "› please continue",
                "Push 合并 commit a8f4909e 到 origin/feat/vidpool_adapter?",
                "❯ 1. Push",
                "2. 不 push, 留本地",
                "3. Type something.",
                "4. Chat about this",
                "",
                "Enter to select · Tab/Arrow keys to navigate · Esc to cancel",
            ]
        ),
        "hash",
        "codex",
    )

    assert status == AgentRuntimeStatus.ATTENTION
    assert status_text == "Agent waiting for input"
    assert detail == "needs your response"


# Codex (GPT-5.5) renders its working indicator ABOVE a tall persistent bottom
# chrome: the ›/❯ composer, a "Queued follow-up inputs" panel that grows one
# line per queued item, and a model footer. That chrome exceeds the bottom-10
# window the generic scan inspects, so these frames exercise the codex-specific
# wider scan. Frame text is modeled on real captures in backend.log.
_CODEX_BRAILLE_WORKING_FRAME = "\n".join(
    [
        "  → gc.collect() 和 call_malloc_trim() 耗时怎么样？",
        "",
        " ⠀⠞ Working  4.03k tokens",
        "",
        "",
        "› Add a follow-up",
        "",
        "  Queued follow-up inputs (3):",
        "    1. 继续",
        "    2. 继续",
        "    3. 继续",
        "",
        "  GPT-5.5 272K Extra High · MAX · 30.7% · 16 files edited      Auto-run",
        "  ~/Projects/codex_workspace · main",
    ]
)

_CODEX_BULLET_WORKING_FRAME = "\n".join(
    [
        "  The task is back in working state. Report progress with the same task_id.",
        "",
        "• Working (3s • esc to interrupt)",
        "",
        "",
        "› Find and fix a bug in @filename",
        "",
        "  Queued follow-up inputs (10):",
        "    1. 继续",
        "    2. 继续",
        "    3. 继续",
        "    4. 继续",
        "    5. 继续",
        "    6. 继续",
        "    7. 继续",
        "    8. 继续",
        "    9. 继续",
        "    10. 继续",
        "",
        "  gpt-5.5 medium · ~/claude_hub",
    ]
)

_CODEX_IDLE_FRAME = "\n".join(
    [
        "  → previous answer text from the agent is shown here",
        "",
        "› ",
        "",
        "  gpt-5.5 medium · ~/claude_hub",
        "  ? for shortcuts",
    ]
)


def test_codex_braille_working_above_chrome_classifies_as_working(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    process = TTYDProcess(
        tab_id="tab-codex-braille",
        port=12362,
        name="Codex Braille",
        agent_type=AgentType.CODEX,
    )

    status, status_text, detail, _last_changed_at = manager._classify_agent_status(
        process,
        _CODEX_BRAILLE_WORKING_FRAME,
        "hash-codex-braille",
        "codex",
    )

    assert status == AgentRuntimeStatus.WORKING
    assert status_text == "Working"
    assert detail == "agent is processing"


def test_codex_bullet_working_above_chrome_classifies_as_working(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    process = TTYDProcess(
        tab_id="tab-codex-bullet",
        port=12363,
        name="Codex Bullet",
        agent_type=AgentType.CODEX,
    )

    status, status_text, detail, _last_changed_at = manager._classify_agent_status(
        process,
        _CODEX_BULLET_WORKING_FRAME,
        "hash-codex-bullet",
        "codex",
    )

    assert status == AgentRuntimeStatus.WORKING
    assert status_text == "Working"
    assert detail == "agent is processing"


def test_codex_idle_frame_classifies_as_idle(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    process = TTYDProcess(
        tab_id="tab-codex-idle",
        port=12364,
        name="Codex Idle",
        agent_type=AgentType.CODEX,
    )

    status, status_text, _detail, _last_changed_at = manager._classify_agent_status(
        process,
        _CODEX_IDLE_FRAME,
        "hash-codex-idle",
        "codex",
    )

    assert status == AgentRuntimeStatus.IDLE
    assert status_text == "Idle"


def test_codex_working_marker_ignored_for_non_codex_agent(
    monkeypatch: MonkeyPatch,
) -> None:
    # The codex wider-scan path is keyed on agent_type. A Claude session showing
    # an above-chrome codex-style line must not be classified WORKING by it —
    # only the agent's own markers (absent here) drive its status.
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    process = TTYDProcess(
        tab_id="tab-claude-not-codex",
        port=12365,
        name="Claude Not Codex",
        agent_type=AgentType.CLAUDE,
    )

    status, _status_text, _detail, _last_changed_at = manager._classify_agent_status(
        process,
        _CODEX_BRAILLE_WORKING_FRAME,
        "hash-claude-not-codex",
        "claude",
    )

    assert status != AgentRuntimeStatus.WORKING


def test_codex_frozen_working_frame_classifies_as_stuck(monkeypatch: MonkeyPatch) -> None:
    # A busy codex frame that has not repainted past the staleness window is a
    # stopped agent behind a lingering "working" frame — flag ATTENTION, not
    # working-forever. Confirms the codex path routes through working_or_stale().
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    first_seen = datetime.now() - timedelta(
        seconds=ttyd_manager_module._WORKING_FRAME_STALE_SECONDS + 30
    )
    manager._status_snapshots = {
        "tab-codex-frozen": {
            "hash": "codex-frozen-hash",
            "last_changed_at": first_seen,
            "frame_first_seen_at": first_seen,
        }
    }
    process = TTYDProcess(
        tab_id="tab-codex-frozen",
        port=12366,
        name="Codex Frozen",
        agent_type=AgentType.CODEX,
    )

    status, status_text, _detail, _last_changed_at = manager._classify_agent_status(
        process,
        _CODEX_BRAILLE_WORKING_FRAME,
        "codex-frozen-hash",
        "codex",
    )

    assert status == AgentRuntimeStatus.ATTENTION
    assert status_text == "Agent may be stuck"


def test_shell_prompt_takes_priority_over_working_markers(
    monkeypatch: MonkeyPatch,
) -> None:
    """A bare shell prompt on the last line means the agent has finished and
    returned to a prompt. This must take priority over working markers
    (e.g. "esc to interrupt") lingering in the bottom-10 scrollback, so the
    agent is promptly classified idle instead of stuck in "working"."""

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    process = TTYDProcess(
        tab_id="tab-prompt-priority",
        port=12370,
        name="Prompt Priority",
        agent_type=AgentType.CLAUDE,
    )

    # Frame: the agent has finished; the shell prompt is on the last line, but
    # "esc to interrupt" (a working marker) is still visible a few lines up.
    output = "\n".join(
        [
            "✻ Implementing frontend task edit modal enhancements… (8m 2s · ↑ 9k tokens)",
            "  ⎿  ✔ Implement backend schema and update_task changes",
            "     ◼ Implement frontend task edit/create modal enhancements",
            "────────────────────────────────────────────────────",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ctrl+t to hide tasks",
            "❯ ",
        ]
    )

    status, status_text, detail, _last_changed_at = manager._classify_agent_status(
        process,
        output,
        "prompt-priority-hash",
        "zsh",
    )

    assert status == AgentRuntimeStatus.IDLE
    assert status_text == "Idle"
    assert detail == "shell prompt visible"


def test_idle_hints_take_priority_over_working_markers(
    monkeypatch: MonkeyPatch,
) -> None:
    """Claude Code idle hints ("? for shortcuts") on the bottom lines must
    take priority over working markers higher in the scrollback."""

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    manager._status_snapshots = {}
    process = TTYDProcess(
        tab_id="tab-idle-hint-priority",
        port=12371,
        name="Idle Hint Priority",
        agent_type=AgentType.CLAUDE,
    )

    # Frame: idle hints are visible on the bottom lines, but a working marker
    # ("esc to interrupt") is still in the bottom-10 scrollback.
    output = "\n".join(
        [
            "✻ Implementing frontend task edit modal enhancements… (8m 2s · ↑ 9k tokens)",
            "  ⎿  ✔ Implement backend schema and update_task changes",
            "     ◼ Implement frontend task edit/create modal enhancements",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ctrl+t to hide tasks",
            "  ? for shortcuts",
            "  / for commands",
        ]
    )

    status, status_text, detail, _last_changed_at = manager._classify_agent_status(
        process,
        output,
        "idle-hint-priority-hash",
        "zsh",
    )

    assert status == AgentRuntimeStatus.IDLE
    assert status_text == "Idle"
    assert detail == "agent prompt visible"


def test_frozen_working_frame_45s_window_classifies_as_stuck(
    monkeypatch: MonkeyPatch,
) -> None:
    """A working frame that has not changed for longer than the (45s) staleness
    window must be classified as ATTENTION, not linger in WORKING."""

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    first_seen = datetime.now() - timedelta(
        seconds=ttyd_manager_module._WORKING_FRAME_STALE_SECONDS + 5
    )
    manager._status_snapshots = {
        "tab-frozen-45s": {
            "hash": "frozen-45s-hash",
            "last_changed_at": first_seen,
            "frame_first_seen_at": first_seen,
        }
    }
    process = TTYDProcess(
        tab_id="tab-frozen-45s",
        port=12372,
        name="Frozen 45s",
        agent_type=AgentType.CLAUDE,
    )

    status, status_text, _detail, _last_changed_at = manager._classify_agent_status(
        process,
        _FROZEN_WORKING_FRAME,
        "frozen-45s-hash",
        "zsh",
    )

    assert status == AgentRuntimeStatus.ATTENTION
    assert status_text == "Agent may be stuck"


def test_working_frame_within_45s_window_stays_working(
    monkeypatch: MonkeyPatch,
) -> None:
    """A working frame that has not changed but is still within the (45s)
    staleness window must stay WORKING (e.g. a slow tool call that briefly
    stops repainting)."""

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists", lambda _session: True)
    manager = TTYDManager.__new__(TTYDManager)
    first_seen = datetime.now() - timedelta(
        seconds=ttyd_manager_module._WORKING_FRAME_STALE_SECONDS - 5
    )
    manager._status_snapshots = {
        "tab-within-45s": {
            "hash": "within-45s-hash",
            "last_changed_at": first_seen,
            "frame_first_seen_at": first_seen,
        }
    }
    process = TTYDProcess(
        tab_id="tab-within-45s",
        port=12373,
        name="Within 45s",
        agent_type=AgentType.CLAUDE,
    )

    status, status_text, detail, _last_changed_at = manager._classify_agent_status(
        process,
        _FROZEN_WORKING_FRAME,
        "within-45s-hash",
        "zsh",
    )

    assert status == AgentRuntimeStatus.WORKING
    assert status_text == "Working"
    assert detail == "agent is processing"


@pytest.mark.asyncio
async def test_ensure_tab_running_starts_missing_ttyd_listener(monkeypatch: MonkeyPatch) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._start_locks = {}
    process = TTYDProcess(
        tab_id="tab-missing-ttyd",
        port=12353,
        name="Missing TTYD",
        agent_type=AgentType.CODEX,
    )
    manager.processes = {process.tab_id: process}

    started: list[str] = []

    async def fake_start(self: TTYDProcess) -> None:
        started.append(self.tab_id)
        self.is_active = True

    async def existing_tmux(_session_name: str) -> bool:
        return True

    monkeypatch.setattr(ttyd_manager_module, "_is_local_port_listening", lambda _port: False)
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", existing_tmux)
    monkeypatch.setattr(TTYDProcess, "start", fake_start)
    monkeypatch.setattr(manager, "_save_state", lambda: None)

    tab = await manager.ensure_tab_running(process.tab_id)

    assert tab is not None
    assert tab.id == process.tab_id
    assert tab.is_active is True
    assert started == [process.tab_id]


@pytest.mark.asyncio
async def test_ensure_tab_running_reuses_existing_listener(monkeypatch: MonkeyPatch) -> None:
    manager = TTYDManager.__new__(TTYDManager)
    manager._start_locks = {}
    process = TTYDProcess(
        tab_id="tab-existing-ttyd",
        port=12354,
        name="Existing TTYD",
        agent_type=AgentType.CODEX,
    )
    manager.processes = {process.tab_id: process}

    async def fail_start(self: TTYDProcess) -> None:
        raise AssertionError("start should not be called when the port already listens")

    monkeypatch.setattr(ttyd_manager_module, "_is_local_port_listening", lambda _port: True)
    monkeypatch.setattr(TTYDProcess, "start", fail_start)

    tab = await manager.ensure_tab_running(process.tab_id)

    assert tab is not None
    assert tab.id == process.tab_id
    assert tab.is_active is True


# --- Reboot recovery (resume prior conversation on machine restart) ---------


def test_claude_fresh_launch_pins_stable_session_id() -> None:
    process = TTYDProcess(
        tab_id="tab-claude-fresh-session",
        port=12360,
        name="Claude Fresh",
        agent_type=AgentType.CLAUDE,
    )

    # A fresh claude tab gets a stable id and pins it via --session-id so a
    # later reboot can resume exactly this conversation.
    assert process.agent_session_id
    cmd = process._build_ttyd_command(session_exists=False)
    assert f"--session-id {process.agent_session_id}" in cmd[-1]
    assert "--resume" not in cmd[-1]


def test_codex_tabs_get_placeholder_session_id() -> None:
    # Codex now gets a placeholder uuid at construction time (overwritten by
    # the real codex-assigned id via post-launch discovery). This placeholder
    # lets tabs.json round-trip a value for agent_session_id before discovery
    # completes.
    process = TTYDProcess(
        tab_id="tab-codex-placeholder",
        port=12361,
        name="Codex Placeholder",
        agent_type=AgentType.CODEX,
    )
    assert process.agent_session_id is not None
    # Looks like a uuid.
    import re as _re

    assert _re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        process.agent_session_id,
    )


def test_cursor_pins_session_id_terminal_does_not() -> None:
    # Cursor is a constructive pin — always gets a uuid4 even without an
    # explicit id, so it can --resume <uuid> on recovery (and fall through to
    # fresh if the store doesn't exist yet).
    cursor = TTYDProcess(
        tab_id="tab-cursor-no-explicit",
        port=12361,
        name="No Session",
        agent_type=AgentType.CURSOR,
    )
    assert cursor.agent_session_id is not None
    assert "-" in cursor.agent_session_id and len(cursor.agent_session_id) == 36
    # Plain terminal still has no agent session id.
    terminal = TTYDProcess(
        tab_id="tab-terminal-no-session",
        port=12362,
        name="No Session",
        agent_type=AgentType.TERMINAL,
    )
    assert terminal.agent_session_id is None


def test_cursor_transcript_data_dir_is_pinned_to_child_environment(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A custom child HOME must not split Cursor's writer from its observer."""
    monkeypatch.setattr(
        ttyd_manager_module,
        "cursor_cli_version_from_executable",
        lambda: "2026.08.25-3e8eec8",
    )
    child_home = tmp_path / "isolated-home"
    sid = str(uuid.uuid4())
    data_dir = child_home / ".cursor"
    transcript = ttyd_manager_module.cursor_terminal_transcript_path(
        "/workspace", sid, data_dir=str(data_dir)
    )

    process = TTYDProcess(
        tab_id="tab-cursor-transcript-env",
        port=12363,
        name="Cursor Transcript",
        agent_type=AgentType.CURSOR,
        cwd="/workspace",
        env={"HOME": str(child_home)},
        agent_session_id=sid,
        cursor_transport="terminal_transcript",
        cursor_data_dir=str(data_dir),
        cursor_cli_version="2026.08.25-3e8eec8",
        cursor_transcript_path=str(transcript),
        cursor_transcript_schema="cli-transcript-v1",
    )

    assert process.cursor_transport == "terminal_transcript"
    assert process.cursor_data_dir == str(data_dir)
    assert process.env["CURSOR_DATA_DIR"] == str(data_dir)


def test_should_recover_only_when_persisted_and_session_gone() -> None:
    process = TTYDProcess(
        tab_id="tab-recover-flag",
        port=12362,
        name="Recover Flag",
        agent_type=AgentType.CLAUDE,
        from_persisted_state=True,
    )

    # Reboot: persisted tab whose tmux session is gone -> recover.
    assert process._should_recover(session_exists=False) is True
    # Backend-only restart: tmux session still alive -> reattach, no resume.
    assert process._should_recover(session_exists=True) is False


def test_fresh_tab_never_recovers() -> None:
    # A brand new tab (not from persisted state) must never resume.
    process = TTYDProcess(
        tab_id="tab-fresh-no-recover",
        port=12363,
        name="Fresh No Recover",
        agent_type=AgentType.CLAUDE,
        from_persisted_state=False,
    )
    assert process._should_recover(session_exists=False) is False


def test_fresh_tab_with_explicit_session_id_recovers() -> None:
    # A fresh tab created with an explicit agent_session_id (e.g. the user
    # picked a specific Codex session to resume) must recover so the launch
    # command runs codex resume <id> / claude --resume <id>.
    process = TTYDProcess(
        tab_id="tab-explicit-session",
        port=12370,
        name="Resume Session",
        agent_type=AgentType.CODEX,
        from_persisted_state=False,
        agent_session_id="019f40d9-c6e4-7bd0-82ca-5d88babf205f",
    )
    assert process._has_explicit_session_id is True
    assert process._should_recover(session_exists=False) is True
    # Live tmux session still wins: reattach, no resume.
    assert process._should_recover(session_exists=True) is False


def test_generated_session_id_does_not_recover() -> None:
    # A generated uuid4 placeholder (no explicit session_id) is for pinning
    # only and must NOT trigger recovery of a non-existent session.
    process = TTYDProcess(
        tab_id="tab-generated-session",
        port=12371,
        name="Generated Session",
        agent_type=AgentType.CODEX,
        from_persisted_state=False,
    )
    assert process._has_explicit_session_id is False
    assert process.agent_session_id  # a generated uuid exists for pinning
    assert process._should_recover(session_exists=False) is False


def test_terminal_and_remote_tabs_never_recover() -> None:
    terminal = TTYDProcess(
        tab_id="tab-terminal-no-recover",
        port=12364,
        name="Terminal",
        agent_type=AgentType.TERMINAL,
        from_persisted_state=True,
    )
    assert terminal._should_recover(session_exists=False) is False

    remote = TTYDProcess(
        tab_id="tab-remote-no-recover",
        port=12365,
        name="Remote",
        agent_type=AgentType.CLAUDE,
        target=ExecutionTarget.REMOTE,
        remote_profile_id="profile-x",
        from_persisted_state=True,
    )
    assert remote._should_recover(session_exists=False) is False


def test_claude_recovery_uses_resume_with_fresh_fallback() -> None:
    process = TTYDProcess(
        tab_id="tab-claude-recover",
        port=12366,
        name="Claude Recover",
        agent_type=AgentType.CLAUDE,
        agent_session_id="11111111-2222-3333-4444-555555555555",
        from_persisted_state=True,
    )

    cmd = process._build_ttyd_command(session_exists=False)
    launch = cmd[-1]
    # Resume the exact prior session, falling back to a fresh pinned session.
    assert "--resume 11111111-2222-3333-4444-555555555555" in launch
    assert "||" in launch
    assert "--session-id 11111111-2222-3333-4444-555555555555" in launch


def test_claude_live_session_reattaches_without_resume() -> None:
    process = TTYDProcess(
        tab_id="tab-claude-reattach",
        port=12367,
        name="Claude Reattach",
        agent_type=AgentType.CLAUDE,
        agent_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        from_persisted_state=True,
    )

    # tmux session alive (backend restart): tmux new-session -A reattaches and
    # no fresh agent command is appended, so nothing to resume.
    cmd = process._build_ttyd_command(session_exists=True)
    joined = " ".join(cmd)
    assert "--resume" not in joined
    assert "--session-id" not in joined


def test_codex_recovery_starts_fresh_without_verified_id() -> None:
    """BUG-3 fix: an unverified codex tab (freshly-generated placeholder id,
    not yet discovered in any rollout) must NOT use `codex resume --last`
    (which is cwd-scoped and cross-wires same-cwd tabs). Instead start
    fresh; Phase 1C will pin the new sid after launch."""
    process = TTYDProcess(
        tab_id="tab-codex-recover",
        port=12368,
        name="Codex Recover",
        agent_type=AgentType.CODEX,
        solo_mode=True,
        from_persisted_state=True,
    )

    cmd = process._build_ttyd_command(session_exists=False)
    launch = cmd[-1]
    # No resume attempt when the sid is unverified.
    assert "codex resume" not in launch
    assert "resume --last" not in launch
    # Solo flags still apply.
    assert "--ask-for-approval never" in launch
    assert "--sandbox danger-full-access" in launch


def test_codex_recovery_with_pinned_id_resumes_exact_uuid(monkeypatch: MonkeyPatch) -> None:
    """When agent_session_id is a verified codex session, resume by uuid with
    a fresh-codex fallback (no --last cwd-scoped fallback)."""

    # Monkeypatch the index check to treat our test id as verified.
    def _fake_id_in_index(sid: str, cwd=None) -> bool:
        return sid == "019f8e90-79d1-7a92-a165-8075ab17f552"

    monkeypatch.setattr(ttyd_manager_module, "_codex_id_exists", _fake_id_in_index)

    process = TTYDProcess(
        tab_id="tab-codex-pinned",
        port=12369,
        name="Codex Pinned",
        agent_type=AgentType.CODEX,
        solo_mode=True,
        from_persisted_state=True,
        agent_session_id="019f8e90-79d1-7a92-a165-8075ab17f552",
    )

    cmd = process._build_ttyd_command(session_exists=False)
    launch = cmd[-1]
    assert "codex resume 019f8e90-79d1-7a92-a165-8075ab17f552" in launch
    assert "resume --last" not in launch
    assert "||" in launch
    # Solo flags must ride on BOTH branches (resume and fresh fallback).
    assert (
        "codex resume 019f8e90-79d1-7a92-a165-8075ab17f552 --ask-for-approval never --sandbox danger-full-access"
        in launch
    )
    assert "|| codex --ask-for-approval never --sandbox danger-full-access" in launch


def test_codex_recovery_rejects_sid_owned_by_another_cwd(monkeypatch: MonkeyPatch) -> None:
    """A globally existing SID is not resumable when its rollout cwd differs."""
    pinned = "019f8e90-79d1-7a92-a165-8075ab17f552"
    calls = []

    def _fake_id_exists(sid: str, cwd=None) -> bool:
        calls.append((sid, cwd))
        return sid == pinned and cwd == "/owner-workspace"

    monkeypatch.setattr(ttyd_manager_module, "_codex_id_exists", _fake_id_exists)
    process = TTYDProcess(
        tab_id="tab-codex-wrong-cwd",
        port=12380,
        name="Codex Wrong Cwd",
        agent_type=AgentType.CODEX,
        cwd="/different-workspace",
        from_persisted_state=True,
        agent_session_id=pinned,
    )

    launch = process._build_ttyd_command(session_exists=False)[-1]

    assert calls == [(pinned, "/different-workspace")]
    assert "codex resume" not in launch
    assert pinned not in launch


def test_non_solo_codex_recovery_starts_fresh_when_unpinned() -> None:
    """Non-solo codex without a verified sid starts fresh (no --last)."""
    process = TTYDProcess(
        tab_id="tab-codex-nonsolo-recover",
        port=12378,
        name="Codex NonSolo Recover",
        agent_type=AgentType.CODEX,
        solo_mode=False,
        from_persisted_state=True,
    )

    recover_cmd = process._build_ttyd_command(session_exists=False)
    assert "codex resume" not in recover_cmd[-1]
    assert "resume --last" not in recover_cmd[-1]
    # Non-solo must NOT carry solo flags.
    assert "--ask-for-approval" not in recover_cmd[-1]
    assert "--sandbox" not in recover_cmd[-1]
    assert "codex" in recover_cmd[-1]

    # A live session (backend-only restart) must NOT resume — bare reattach.
    live_cmd = process._build_ttyd_command(session_exists=True)
    assert "resume" not in live_cmd[-1]


def test_non_solo_codex_recovery_with_pinned_id_resumes_uuid(monkeypatch: MonkeyPatch) -> None:
    pinned = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def _fake_id_exists(sid: str, cwd=None) -> bool:
        return sid == pinned

    monkeypatch.setattr(ttyd_manager_module, "_codex_id_exists", _fake_id_exists)

    process = TTYDProcess(
        tab_id="tab-codex-nonsolo-pinned",
        port=12379,
        name="Codex NonSolo Pinned",
        agent_type=AgentType.CODEX,
        solo_mode=False,
        cwd="/workspace",
        from_persisted_state=True,
        agent_session_id=pinned,
    )

    recover_cmd = process._build_ttyd_command(session_exists=False)
    assert f"codex resume {pinned}" in recover_cmd[-1]
    assert "--last" not in recover_cmd[-1]
    assert "--ask-for-approval" not in recover_cmd[-1]
    assert "|| codex" in recover_cmd[-1]


def test_cursor_recovery_uses_resume_uuid() -> None:
    """Cursor CLI is a constructive pin: `agent --resume <uuid>` creates the
    chat store immediately if it doesn't exist, so we ALWAYS pin a stable
    sid and always resume it (with fallback to fresh on failure). Legacy
    cursor `--continue` (BUG-3 class) is gone."""
    process = TTYDProcess(
        tab_id="tab-cursor-recover",
        port=12369,
        name="Cursor Recover",
        agent_type=AgentType.CURSOR,
        from_persisted_state=True,
    )
    # Process should have been assigned a uuid4 sid in __init__
    assert process.agent_session_id

    cmd = process._build_ttyd_command(session_exists=False)
    launch = cmd[-1]
    assert "agent --resume" in launch
    assert "--continue" not in launch
    assert process.agent_session_id in launch


def test_cursor_recovery_rotates_sid_not_verified_for_cwd(monkeypatch: MonkeyPatch) -> None:
    """A Cursor SID from another cwd is replaced with a constructive fresh pin."""
    stale_sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    monkeypatch.setattr(ttyd_manager_module, "_cursor_id_exists", lambda sid, cwd: False)
    process = TTYDProcess(
        tab_id="tab-cursor-wrong-cwd",
        port=12371,
        name="Cursor Wrong Cwd",
        agent_type=AgentType.CURSOR,
        cwd="/different-workspace",
        from_persisted_state=True,
        agent_session_id=stale_sid,
    )

    launch = process._build_ttyd_command(session_exists=False)[-1]

    assert process.agent_session_id != stale_sid
    assert stale_sid not in launch
    assert f"agent --resume {process.agent_session_id}" in launch


def test_cursor_launch_no_double_resume_when_live() -> None:
    """Hot reattach for cursor: no resume flags."""
    process = TTYDProcess(
        tab_id="tab-cursor-hot",
        port=12370,
        name="Cursor Hot",
        agent_type=AgentType.CURSOR,
        from_persisted_state=True,
    )
    cmd = process._build_ttyd_command(session_exists=True)
    joined = " ".join(cmd)
    assert "--resume" not in joined
    assert "--continue" not in joined


def test_agent_session_id_round_trips_through_state(monkeypatch: MonkeyPatch, tmp_path) -> None:
    state_file = tmp_path / "tabs.json"
    monkeypatch.setattr(ttyd_manager_module, "STATE_FILE", state_file)

    process = TTYDProcess(
        tab_id="tab-roundtrip",
        port=12370,
        name="Roundtrip",
        agent_type=AgentType.CLAUDE,
    )
    original_id = process.agent_session_id
    assert original_id

    # Serialize and persist exactly as TTYDManager._save_state does.
    state_file.write_text(json.dumps([process.to_dict()]), encoding="utf-8")

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {}
    manager._next_port = 10000
    manager._load_state()

    restored = manager.processes["tab-roundtrip"]
    assert restored.agent_session_id == original_id
    # A tab restored from disk is marked as persisted so it can recover.
    assert restored.from_persisted_state is True
    assert restored._should_recover(session_exists=False) is True


# --- Conservative agent_session_id backfill -----------------------------------


def _touch_jsonl(directory, session_id: str, start_epoch: float, mtime: float) -> str:
    """Write a minimal Claude conversation jsonl and set its mtime."""
    path = directory / f"{session_id}.jsonl"
    ts = datetime.utcfromtimestamp(start_epoch).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    path.write_text(json.dumps({"type": "user", "timestamp": ts}) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return str(path)


def test_pick_backfill_single_candidate_within_window(tmp_path) -> None:
    created = 1_000_000.0
    f = _touch_jsonl(tmp_path, "sid-a", created + 10, created + 10)
    chosen = ttyd_manager_module._pick_backfill_session(created, [(10.0, "sid-a", f)])
    assert chosen == "sid-a"


def test_pick_backfill_two_close_candidates_is_ambiguous(tmp_path) -> None:
    created = 1_000_000.0
    f1 = _touch_jsonl(tmp_path, "sid-a", created + 10, created + 10)
    f2 = _touch_jsonl(tmp_path, "sid-b", created + 20, created + 20)
    chosen = ttyd_manager_module._pick_backfill_session(
        created, [(10.0, "sid-a", f1), (20.0, "sid-b", f2)]
    )
    assert chosen is None


def test_pick_backfill_second_far_away_is_picked(tmp_path) -> None:
    created = 1_000_000.0
    f1 = _touch_jsonl(tmp_path, "sid-a", created + 10, created + 10)
    f2 = _touch_jsonl(tmp_path, "sid-b", created + 5000, created + 5000)
    chosen = ttyd_manager_module._pick_backfill_session(
        created, [(10.0, "sid-a", f1), (5000.0, "sid-b", f2)]
    )
    assert chosen == "sid-a"


def test_pick_backfill_best_delta_too_large(tmp_path) -> None:
    created = 1_000_000.0
    f = _touch_jsonl(tmp_path, "sid-a", created + 200, created + 200)
    chosen = ttyd_manager_module._pick_backfill_session(created, [(200.0, "sid-a", f)])
    assert chosen is None


def test_pick_backfill_stale_mtime_is_rejected(tmp_path) -> None:
    created = 1_000_000.0
    # Conversation start is close, but file last modified well before session.
    f = _touch_jsonl(tmp_path, "sid-a", created + 10, created - 100)
    chosen = ttyd_manager_module._pick_backfill_session(created, [(10.0, "sid-a", f)])
    assert chosen is None


def _backfill_manager(monkeypatch: MonkeyPatch, tmp_path, process: TTYDProcess) -> TTYDManager:
    state_file = tmp_path / "tabs.json"
    monkeypatch.setattr(ttyd_manager_module, "STATE_FILE", state_file)
    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {process.tab_id: process}
    return manager


def test_backfill_pins_unambiguous_session(monkeypatch: MonkeyPatch, tmp_path) -> None:
    cwd = "/Users/tester/proj"
    projects = tmp_path / ".claude" / "projects" / "-Users-tester-proj"
    projects.mkdir(parents=True)
    created = 1_700_000_000.0
    _touch_jsonl(projects, "real-convo", created + 15, created + 60)

    monkeypatch.setattr(ttyd_manager_module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_created", lambda _session: created)

    process = TTYDProcess(
        tab_id="tab-old",
        port=12380,
        name="Old Claude",
        cwd=cwd,
        agent_type=AgentType.CLAUDE,
        from_persisted_state=True,
    )
    process.agent_session_id = None

    manager = _backfill_manager(monkeypatch, tmp_path, process)
    manager._backfill_agent_session_ids()

    assert process.agent_session_id == "real-convo"
    # Pinned id persisted so a subsequent reboot can resume.
    saved = json.loads((tmp_path / "tabs.json").read_text(encoding="utf-8"))
    assert saved[0]["agent_session_id"] == "real-convo"


def test_backfill_skips_when_no_live_session(monkeypatch: MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(ttyd_manager_module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_created", lambda _session: None)

    process = TTYDProcess(
        tab_id="tab-dead",
        port=12381,
        name="Dead Claude",
        cwd="/Users/tester/proj",
        agent_type=AgentType.CLAUDE,
        from_persisted_state=True,
    )
    process.agent_session_id = None

    manager = _backfill_manager(monkeypatch, tmp_path, process)
    manager._backfill_agent_session_ids()

    assert process.agent_session_id is None
    assert not (tmp_path / "tabs.json").exists()


def test_backfill_skips_ambiguous_dir(monkeypatch: MonkeyPatch, tmp_path) -> None:
    cwd = "/Users/tester/proj"
    projects = tmp_path / ".claude" / "projects" / "-Users-tester-proj"
    projects.mkdir(parents=True)
    created = 1_700_000_000.0
    _touch_jsonl(projects, "convo-a", created + 10, created + 30)
    _touch_jsonl(projects, "convo-b", created + 25, created + 40)

    monkeypatch.setattr(ttyd_manager_module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_created", lambda _session: created)

    process = TTYDProcess(
        tab_id="tab-amb",
        port=12382,
        name="Ambiguous Claude",
        cwd=cwd,
        agent_type=AgentType.CLAUDE,
        from_persisted_state=True,
    )
    process.agent_session_id = None

    manager = _backfill_manager(monkeypatch, tmp_path, process)
    manager._backfill_agent_session_ids()

    assert process.agent_session_id is None
    assert not (tmp_path / "tabs.json").exists()


# --- switch_env (hot-swap env / model for live Claude tabs) ---------------------


class _FakeProcess:
    """Minimal asyncio.subprocess.Process stand-in for tmux respawn calls."""

    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return (b"", self._stderr)


def _make_claude_process(
    monkeypatch: MonkeyPatch, solo_mode: bool = False, tmp_path=None
) -> TTYDProcess:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    if tmp_path is not None:
        monkeypatch.setattr(ttyd_manager_module, "LAUNCH_ENV_DIR", tmp_path)
    process = TTYDProcess(
        tab_id="tab-switch-env",
        port=12390,
        name="Switch Env Test",
        solo_mode=solo_mode,
        agent_type=AgentType.CLAUDE,
        env=dict(DEFAULT_CLAUDE_LAUNCH_ENV),
    )
    # Pin a deterministic session id so the command is easy to assert on.
    process.agent_session_id = "test-session-id-abc"
    return process


@pytest.mark.asyncio
async def test_switch_env_rejects_unsupported_agent_types(monkeypatch: MonkeyPatch) -> None:
    # Codex is now supported; cursor and terminal are not.
    for agent_type in (AgentType.CURSOR, AgentType.TERMINAL):
        process = TTYDProcess(
            tab_id=f"tab-{agent_type.value}",
            port=12391,
            name=agent_type.value,
            agent_type=agent_type,
        )
        with pytest.raises(ValueError, match="Claude and Codex tabs"):
            await process.switch_env({"FOO": "bar"})


@pytest.mark.asyncio
async def test_switch_env_rejects_remote_tabs(monkeypatch: MonkeyPatch) -> None:
    process = TTYDProcess(
        tab_id="tab-remote",
        port=12392,
        name="Remote",
        agent_type=AgentType.CLAUDE,
        target=ExecutionTarget.REMOTE,
        remote_profile_id="prof",
    )
    with pytest.raises(ValueError, match="local tabs"):
        await process.switch_env({"ANTHROPIC_MODEL": "x"})


@pytest.mark.asyncio
async def test_switch_env_rejects_stopped_tabs(monkeypatch: MonkeyPatch, tmp_path) -> None:
    process = _make_claude_process(monkeypatch, tmp_path=tmp_path)

    async def _fake_exists(_session: str) -> bool:
        return False

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _fake_exists)

    with pytest.raises(RuntimeError, match="tmux session is not running"):
        await process.switch_env({"ANTHROPIC_MODEL": "claude-sonnet-4-5"})


@pytest.mark.asyncio
async def test_switch_env_non_solo_respawn_command(monkeypatch: MonkeyPatch, tmp_path) -> None:
    process = _make_claude_process(monkeypatch, solo_mode=False, tmp_path=tmp_path)
    new_env = {
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "ANTHROPIC_MODEL": "claude-sonnet-4-5",
    }
    captured: dict = {}

    async def _fake_exists(_session: str) -> bool:
        return True

    async def _fake_spawn(*args, **kwargs):
        captured["args"] = args
        return _FakeProcess(0)

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _fake_exists)
    monkeypatch.setattr(
        ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_spawn, raising=False
    )

    await process.switch_env(new_env)

    args = captured["args"]
    assert args[0] == "tmux"
    assert "respawn-pane" in args
    assert "-k" in args
    lc_index = args.index("-lc")
    respawn_cmd = args[lc_index + 1]

    assert "IS_SANDBOX=1" not in respawn_cmd
    assert "--dangerously-skip-permissions" not in respawn_cmd
    assert "claude" in respawn_cmd
    assert "--resume test-session-id-abc" in respawn_cmd
    assert "--session-id test-session-id-abc" in respawn_cmd
    assert "||" in respawn_cmd
    assert "--model claude-sonnet-4-5" in respawn_cmd
    assert process.env["ANTHROPIC_MODEL"] == "claude-sonnet-4-5"
    assert process.solo_mode is False


@pytest.mark.asyncio
async def test_switch_env_solo_respawn_command(monkeypatch: MonkeyPatch, tmp_path) -> None:
    process = _make_claude_process(monkeypatch, solo_mode=True, tmp_path=tmp_path)
    new_env = dict(DEFAULT_CLAUDE_LAUNCH_ENV)
    new_env["ANTHROPIC_MODEL"] = "claude-opus-4-5"
    captured: dict = {}

    async def _fake_exists(_session: str) -> bool:
        return True

    async def _fake_spawn(*args, **kwargs):
        captured["args"] = args
        return _FakeProcess(0)

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _fake_exists)
    monkeypatch.setattr(
        ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_spawn, raising=False
    )

    await process.switch_env(new_env)

    args = captured["args"]
    lc_index = args.index("-lc")
    respawn_cmd = args[lc_index + 1]

    assert "IS_SANDBOX=1 claude --dangerously-skip-permissions" in respawn_cmd
    assert "--resume test-session-id-abc" in respawn_cmd
    assert "--model claude-opus-4-5" in respawn_cmd
    assert respawn_cmd.rstrip().endswith("; exec /bin/zsh")
    assert process.solo_mode is True


@pytest.mark.asyncio
async def test_switch_env_toggles_solo_mode(monkeypatch: MonkeyPatch, tmp_path) -> None:
    process = _make_claude_process(monkeypatch, solo_mode=False, tmp_path=tmp_path)
    captured: dict = {}

    async def _fake_exists(_session: str) -> bool:
        return True

    async def _fake_spawn(*args, **kwargs):
        captured["args"] = args
        return _FakeProcess(0)

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _fake_exists)
    monkeypatch.setattr(
        ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_spawn, raising=False
    )

    await process.switch_env(dict(DEFAULT_CLAUDE_LAUNCH_ENV), solo_mode=True)

    args = captured["args"]
    lc_index = args.index("-lc")
    respawn_cmd = args[lc_index + 1]
    assert "IS_SANDBOX=1 claude --dangerously-skip-permissions" in respawn_cmd
    assert process.solo_mode is True


@pytest.mark.asyncio
async def test_switch_env_propagates_tmux_failure(monkeypatch: MonkeyPatch, tmp_path) -> None:
    process = _make_claude_process(monkeypatch, tmp_path=tmp_path)

    async def _fake_exists(_session: str) -> bool:
        return True

    async def _fake_spawn(*args, **kwargs):
        return _FakeProcess(1, stderr=b"tmux: failed")

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _fake_exists)
    monkeypatch.setattr(
        ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_spawn, raising=False
    )

    with pytest.raises(RuntimeError, match="tmux: failed"):
        await process.switch_env(dict(DEFAULT_CLAUDE_LAUNCH_ENV))

    # Rollback must restore in-memory state to the previous env/solo so the
    # still-running pane matches what the Python object thinks is active.
    assert process.solo_mode is False
    assert process.env["ANTHROPIC_MODEL"] == DEFAULT_CLAUDE_LAUNCH_ENV["ANTHROPIC_MODEL"]


@pytest.mark.asyncio
async def test_switch_env_rolls_back_on_spawn_exception(monkeypatch: MonkeyPatch, tmp_path) -> None:
    process = _make_claude_process(monkeypatch, tmp_path=tmp_path)

    async def _fake_exists(_session: str) -> bool:
        return True

    async def _fake_spawn(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _fake_exists)
    monkeypatch.setattr(
        ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_spawn, raising=False
    )

    with pytest.raises(OSError, match="spawn failed"):
        await process.switch_env({"ANTHROPIC_MODEL": "should-not-stick"})

    assert process.env["ANTHROPIC_MODEL"] == DEFAULT_CLAUDE_LAUNCH_ENV["ANTHROPIC_MODEL"]


@pytest.mark.asyncio
async def test_switch_env_manager_persists_state(monkeypatch: MonkeyPatch, tmp_path) -> None:
    state_file = tmp_path / "tabs.json"
    monkeypatch.setattr(ttyd_manager_module, "STATE_FILE", state_file)
    # Also redirect launch-env writes to tmp_path so we don't leak files.
    monkeypatch.setattr(ttyd_manager_module, "LAUNCH_ENV_DIR", tmp_path / "launch_env")
    monkeypatch.setenv("SHELL", "/bin/zsh")

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {}
    manager._next_port = 12400
    manager._tab_order = []
    manager._status_snapshots = {}
    manager._status_cache = {}
    manager._start_locks = {}

    process = TTYDProcess(
        tab_id="tab-persist",
        port=12400,
        name="Persist Test",
        agent_type=AgentType.CLAUDE,
    )
    process.agent_session_id = "persist-sid"
    manager.processes[process.tab_id] = process
    manager._tab_order.append(process.tab_id)

    new_env = {"ANTHROPIC_MODEL": "new-model", "ANTHROPIC_BASE_URL": "https://example.com"}

    async def _fake_exists(_session: str) -> bool:
        return True

    async def _fake_spawn(*args, **kwargs):
        return _FakeProcess(0)

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _fake_exists)
    monkeypatch.setattr(
        ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_spawn, raising=False
    )

    result = await manager.switch_env(process.tab_id, new_env, solo_mode=True)

    assert result.solo_mode is True
    assert result.env["ANTHROPIC_MODEL"] == "new-model"
    assert state_file.exists()
    saved = json.loads(state_file.read_text())
    assert len(saved) == 1
    assert saved[0]["id"] == process.tab_id
    assert saved[0]["solo_mode"] is True
    assert saved[0]["env"]["ANTHROPIC_MODEL"] == "new-model"


def _make_codex_process(
    monkeypatch: MonkeyPatch, solo_mode: bool = False, tmp_path=None, env: dict | None = None
) -> TTYDProcess:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    if tmp_path is not None:
        monkeypatch.setattr(ttyd_manager_module, "LAUNCH_ENV_DIR", tmp_path)
    process = TTYDProcess(
        tab_id="tab-codex-switch",
        port=12490,
        name="Codex Switch Env Test",
        solo_mode=solo_mode,
        agent_type=AgentType.CODEX,
        env=env or {},
    )
    return process


@pytest.mark.asyncio
async def test_switch_env_codex_non_solo_respawn_command(
    monkeypatch: MonkeyPatch, tmp_path
) -> None:
    process = _make_codex_process(monkeypatch, solo_mode=False, tmp_path=tmp_path)
    new_env = {"OPENAI_API_KEY": "test-key"}
    captured: dict = {}

    async def _fake_exists(_session: str) -> bool:
        return True

    async def _fake_spawn(*args, **kwargs):
        captured["args"] = args
        return _FakeProcess(0)

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _fake_exists)
    monkeypatch.setattr(
        ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_spawn, raising=False
    )

    await process.switch_env(new_env)

    args = captured["args"]
    assert args[0] == "tmux"
    assert "respawn-pane" in args
    lc_index = args.index("-lc")
    respawn_cmd = args[lc_index + 1]

    assert "--ask-for-approval" not in respawn_cmd
    assert "--sandbox" not in respawn_cmd
    # Unpinned codex starts fresh (no --last, no --resume).
    assert "codex resume" not in respawn_cmd
    assert "resume --last" not in respawn_cmd
    assert "||" not in respawn_cmd
    assert respawn_cmd.rstrip().endswith("; exec /bin/zsh")
    assert process.solo_mode is False
    assert process.env["OPENAI_API_KEY"] == "test-key"


@pytest.mark.asyncio
async def test_switch_env_codex_solo_respawn_command(monkeypatch: MonkeyPatch, tmp_path) -> None:
    process = _make_codex_process(monkeypatch, solo_mode=True, tmp_path=tmp_path)
    new_env = {"OPENAI_API_KEY": "test-key"}
    captured: dict = {}

    async def _fake_exists(_session: str) -> bool:
        return True

    async def _fake_spawn(*args, **kwargs):
        captured["args"] = args
        return _FakeProcess(0)

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _fake_exists)
    monkeypatch.setattr(
        ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_spawn, raising=False
    )

    await process.switch_env(new_env)

    args = captured["args"]
    lc_index = args.index("-lc")
    respawn_cmd = args[lc_index + 1]

    assert "codex --ask-for-approval never --sandbox danger-full-access" in respawn_cmd
    # Unpinned solo codex: starts fresh (no --last, no resume attempt).
    assert "codex resume" not in respawn_cmd
    assert "resume --last" not in respawn_cmd
    assert "||" not in respawn_cmd
    assert respawn_cmd.rstrip().endswith("; exec /bin/zsh")
    assert process.solo_mode is True


@pytest.mark.asyncio
async def test_switch_env_codex_toggles_solo_mode(monkeypatch: MonkeyPatch, tmp_path) -> None:
    process = _make_codex_process(monkeypatch, solo_mode=False, tmp_path=tmp_path)
    captured: dict = {}

    async def _fake_exists(_session: str) -> bool:
        return True

    async def _fake_spawn(*args, **kwargs):
        captured["args"] = args
        return _FakeProcess(0)

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _fake_exists)
    monkeypatch.setattr(
        ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_spawn, raising=False
    )

    await process.switch_env({"FOO": "bar"}, solo_mode=True)

    args = captured["args"]
    lc_index = args.index("-lc")
    respawn_cmd = args[lc_index + 1]

    assert "--ask-for-approval never" in respawn_cmd
    assert "--sandbox danger-full-access" in respawn_cmd
    # Toggling into solo for unpinned codex: starts fresh with solo flags.
    assert "codex resume" not in respawn_cmd
    assert "resume --last" not in respawn_cmd
    assert process.solo_mode is True


@pytest.mark.asyncio
async def test_switch_env_codex_rolls_back_on_failure(monkeypatch: MonkeyPatch, tmp_path) -> None:
    process = _make_codex_process(monkeypatch, solo_mode=False, tmp_path=tmp_path)

    async def _fake_exists(_session: str) -> bool:
        return True

    async def _fake_spawn(*args, **kwargs):
        return _FakeProcess(1, stderr=b"tmux: failed")

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _fake_exists)
    monkeypatch.setattr(
        ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_spawn, raising=False
    )

    with pytest.raises(RuntimeError, match="tmux: failed"):
        await process.switch_env({"NEW_VAR": "new-value"}, solo_mode=True)

    # Rollback must restore state
    assert process.solo_mode is False
    assert "NEW_VAR" not in process.env


@pytest.mark.asyncio
async def test_switch_env_codex_with_pinned_id_resumes_uuid(
    monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """AC6: switch_env (hot-swap) for codex uses `codex resume <uuid>` when a
    verified session id is pinned, not `--last`."""
    pinned = "11111111-2222-3333-4444-555555555555"

    def _fake_id_exists(sid: str, cwd=None) -> bool:
        return sid == pinned

    monkeypatch.setattr(ttyd_manager_module, "_codex_id_exists", _fake_id_exists)

    process = TTYDProcess(
        tab_id="tab-codex-switch-pinned",
        port=12499,
        name="Codex Switch Pinned",
        solo_mode=True,
        agent_type=AgentType.CODEX,
        env={},
        agent_session_id=pinned,
    )
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(ttyd_manager_module, "LAUNCH_ENV_DIR", tmp_path)
    captured: dict = {}

    async def _fake_exists(_session: str) -> bool:
        return True

    async def _fake_spawn(*args, **kwargs):
        captured["args"] = args
        return _FakeProcess(0)

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _fake_exists)
    monkeypatch.setattr(
        ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_spawn, raising=False
    )

    await process.switch_env({"OPENAI_API_KEY": "test-key"})

    args = captured["args"]
    lc_index = args.index("-lc")
    respawn_cmd = args[lc_index + 1]

    assert f"codex resume {pinned}" in respawn_cmd
    assert "resume --last" not in respawn_cmd
    assert "|| codex" in respawn_cmd
    # Solo flags must ride on both branches, just like the launch path.
    assert (
        f"codex resume {pinned} --ask-for-approval never --sandbox danger-full-access"
        in respawn_cmd
    )
    assert "|| codex --ask-for-approval never --sandbox danger-full-access" in respawn_cmd


@pytest.mark.asyncio
async def test_switch_env_manager_missing_tab_raises(monkeypatch: MonkeyPatch, tmp_path) -> None:
    state_file = tmp_path / "tabs.json"
    monkeypatch.setattr(ttyd_manager_module, "STATE_FILE", state_file)
    monkeypatch.setattr(ttyd_manager_module, "LAUNCH_ENV_DIR", tmp_path / "launch_env")
    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {}
    manager._next_port = 12500
    manager._tab_order = []
    manager._status_snapshots = {}
    manager._status_cache = {}
    manager._start_locks = {}
    with pytest.raises(KeyError):
        await manager.switch_env("nonexistent", {"X": "Y"})


# --- Codex session-id pinning and discovery ----------------------------------


def test_parse_session_meta_new_payload_format() -> None:
    """_parse_session_meta: canonical format is {type:session_meta, payload:{id, cwd, timestamp}}.
    id (not session_id) is the canonical sid; session_id is parent thread on forks."""
    meta = {
        "timestamp": "2026-07-23T10:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "id": "019f8e90-79d1-7a92-a165-8075ab17f552",
            "session_id": "parent-thread-id-on-fork",
            "cwd": "/Users/bytedance/claude_hub",
            "timestamp": "2026-07-23T10:00:00.000Z",
        },
    }
    sid, epoch, cwd, thread_source = ttyd_manager_module._parse_session_meta(json.dumps(meta))
    assert sid == "019f8e90-79d1-7a92-a165-8075ab17f552"
    assert cwd == "/Users/bytedance/claude_hub"
    assert epoch is not None
    assert abs(epoch - datetime(2026, 7, 23, 10, 0, 0, tzinfo=timezone.utc).timestamp()) < 1.0
    assert thread_source == ""


def test_parse_session_meta_legacy_flat_format() -> None:
    """Legacy flat format (no payload wrapper) is still parsed defensively."""
    meta = {
        "session_id": "legacy-sid",
        "cwd": "/legacy",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    sid, epoch, cwd, thread_source = ttyd_manager_module._parse_session_meta(json.dumps(meta))
    assert sid == "legacy-sid"
    assert cwd == "/legacy"
    assert epoch is not None
    assert thread_source == ""


def test_parse_session_meta_bad_line_returns_none() -> None:
    assert ttyd_manager_module._parse_session_meta("not json") == (None, None, None, "")
    assert ttyd_manager_module._parse_session_meta("") == (None, None, None, "")
    assert ttyd_manager_module._parse_session_meta("{}") == (None, None, None, "")


def test_codex_scan_sessions_walks_active_and_archived(monkeypatch: MonkeyPatch, tmp_path) -> None:
    """_codex_scan_sessions walks sessions/** and archived_sessions/** (flat),
    returning a dict sid -> ScanEntry."""
    codex_home = tmp_path / "codex_home"
    active_dir = codex_home / "sessions" / "2026" / "07" / "23"
    archived_dir = codex_home / "archived_sessions"
    active_dir.mkdir(parents=True)
    archived_dir.mkdir(parents=True)
    monkeypatch.setattr(ttyd_manager_module, "_codex_home_dir", lambda: codex_home)

    def _rollout(sid: str, cwd: str, dir_path: Path, ts: str, content_suffix: str = "") -> None:
        ts_safe = ts.replace(":", "-")
        path = dir_path / f"rollout-{ts_safe}-{sid}.jsonl"
        meta = {
            "timestamp": ts,
            "type": "session_meta",
            "payload": {"session_id": sid, "id": sid, "timestamp": ts, "cwd": cwd},
        }
        path.write_text(json.dumps(meta) + "\n" + content_suffix, encoding="utf-8")

    sid_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    sid_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    _rollout(sid_a, "/a", active_dir, "2026-07-23T10:00:00Z")
    _rollout(sid_b, "/b", archived_dir, "2026-06-01T10:00:00Z")

    scan = ttyd_manager_module._codex_scan_sessions()
    assert set(scan.keys()) == {sid_a, sid_b}
    assert scan[sid_a].cwd == "/a"
    assert scan[sid_b].cwd == "/b"
    assert scan[sid_a].is_archived is False
    assert scan[sid_b].is_archived is True
    assert scan[sid_a].size > 0


def test_diff_scans_classifies_new_appended_attr() -> None:
    """_diff_scans returns 'new' for unseen sids, 'appended' for grew-size+mtime,
    'attr_changed' otherwise."""
    import time as _time

    def _entry(
        sid: str, size: int, mtime_ns: int, cwd: str = "/x"
    ) -> "ttyd_manager_module.ScanEntry":
        return ttyd_manager_module.ScanEntry(
            path=f"/{sid}.jsonl",
            mtime_ns=mtime_ns,
            size=size,
            cwd=cwd,
            ts=_time.time(),
            is_archived=False,
        )

    pre = {
        "a": _entry("a", 100, 1000),
        "b": _entry("b", 200, 2000),
        "c": _entry("c", 300, 3000),
    }
    post = {
        # a: appended (size+mtime both grew)
        "a": _entry("a", 200, 2000),
        # b: attr_changed (mtime changed but size same)
        "b": _entry("b", 200, 2100),
        # c: unchanged -> absent from diff
        "c": _entry("c", 300, 3000),
        # d: new
        "d": _entry("d", 50, 4000),
    }
    diff = ttyd_manager_module._diff_scans(pre, post)
    assert diff == {"a": "appended", "b": "attr_changed", "d": "new"}


def test_claude_defensive_session_id_when_missing() -> None:
    """_claude_session_arg generates an id if agent_session_id is somehow None."""
    process = TTYDProcess(
        tab_id="tab-claude-noid",
        port=12400,
        name="Claude NoId",
        agent_type=AgentType.CLAUDE,
    )
    # Force None to simulate a corrupted/legacy tab
    process.agent_session_id = None
    arg = process._claude_session_arg()
    assert arg.startswith(" --session-id ")
    assert process.agent_session_id is not None


def test_codex_agent_type_change_resets_session_id() -> None:
    """Switching agent_type resets agent_session_id appropriately."""
    process = TTYDProcess(
        tab_id="tab-type-switch",
        port=12401,
        name="Type Switch",
        agent_type=AgentType.CLAUDE,
        agent_session_id="my-claude-session",
    )
    assert process.agent_session_id == "my-claude-session"

    # Simulate what update_tab does: detect type change, reset id
    new_type = AgentType.CODEX
    if process.agent_type != new_type:
        process.agent_session_id = None
        if new_type in (AgentType.CLAUDE, AgentType.CODEX):
            process.agent_session_id = str(uuid.uuid4())
        process.agent_type = new_type
    assert process.agent_type == AgentType.CODEX
    assert process.agent_session_id is not None
    assert process.agent_session_id != "my-claude-session"


def test_codex_id_exists_finds_session_by_rollout(monkeypatch: MonkeyPatch, tmp_path) -> None:
    """_codex_id_exists is rollout-based (not index-based), because
    session_index.jsonl only records interactive/VSCode sessions and never
    CLI-launched sessions (source=cli, which is what Claude Hub spawns)."""
    codex_home = tmp_path / "codex_home"
    active_dir = codex_home / "sessions" / "2026" / "07" / "23"
    archived_dir = codex_home / "archived_sessions"
    active_dir.mkdir(parents=True)
    archived_dir.mkdir(parents=True)
    monkeypatch.setattr(ttyd_manager_module, "_codex_home_dir", lambda: codex_home)

    assert ttyd_manager_module._codex_id_exists("00000000-0000-0000-0000-000000000000") is False
    assert ttyd_manager_module._codex_id_in_index is ttyd_manager_module._codex_id_exists

    def _rollout(sid: str, cwd: str, dir_path: Path, ts: str) -> None:
        ts_safe = ts.replace(":", "-")
        path = dir_path / f"rollout-{ts_safe}-{sid}.jsonl"
        meta = {
            "timestamp": ts,
            "type": "session_meta",
            "payload": {"session_id": sid, "id": sid, "timestamp": ts, "cwd": cwd},
        }
        path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

    real_sid = "019f8e90-79d1-7a92-a165-8075ab17f552"
    other_cwd_sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    archived_sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    _rollout(real_sid, "/workspace", active_dir, "2026-07-23T10:00:00Z")
    _rollout(other_cwd_sid, "/other", active_dir, "2026-07-23T10:01:00Z")
    _rollout(archived_sid, "/workspace", archived_dir, "2026-06-01T10:00:00Z")

    assert ttyd_manager_module._codex_id_exists(real_sid, cwd="/workspace") is True
    assert ttyd_manager_module._codex_id_exists(real_sid, cwd="/other") is False
    assert ttyd_manager_module._codex_id_exists(real_sid) is True
    assert ttyd_manager_module._codex_id_exists(archived_sid, cwd="/workspace") is True
    assert ttyd_manager_module._codex_id_exists("deadbeef-dead-dead-dead-deaddeaaaaad") is False


def test_resume_quarantined_round_trips_through_state(monkeypatch: MonkeyPatch, tmp_path) -> None:
    """resume_quarantined persists through tabs.json and defaults False for
    legacy state files that pre-date the field."""
    state_file = tmp_path / "tabs.json"
    monkeypatch.setattr(ttyd_manager_module, "STATE_FILE", state_file)

    quarantined = TTYDProcess(
        tab_id="tab-q",
        port=12403,
        name="Quarantined",
        agent_type=AgentType.CODEX,
        agent_session_id="quarantined-sid",
        resume_quarantined=True,
    )
    assert quarantined.resume_quarantined is True

    fresh = TTYDProcess(tab_id="tab-f", port=12404, name="Fresh", agent_type=AgentType.CODEX)
    assert fresh.resume_quarantined is False

    state_file.write_text(json.dumps([quarantined.to_dict(), fresh.to_dict()]), encoding="utf-8")

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {}
    manager._next_port = 10000
    manager._load_state()

    assert manager.processes["tab-q"].resume_quarantined is True
    assert manager.processes["tab-f"].resume_quarantined is False

    # Legacy state (no resume_quarantined key) must default to False.
    legacy_state = [
        {
            "id": "tab-legacy",
            "name": "Legacy",
            "shell": "codex",
            "cwd": "/x",
            "solo_mode": False,
            "agent_type": "codex",
            "target": "local",
            "port": 12405,
            "created_at": "2026-01-01T00:00:00",
            "agent_session_id": "legacy-sid",
        }
    ]
    state_file.write_text(json.dumps(legacy_state), encoding="utf-8")
    manager2 = TTYDManager.__new__(TTYDManager)
    manager2.processes = {}
    manager2._next_port = 10000
    manager2._load_state()
    assert manager2.processes["tab-legacy"].resume_quarantined is False


def test_codex_quarantined_does_not_issue_resume() -> None:
    """A quarantined codex tab starts fresh (no resume at all)."""
    process = TTYDProcess(
        tab_id="tab-q",
        port=12406,
        name="Quarantined",
        agent_type=AgentType.CODEX,
        from_persisted_state=True,
        agent_session_id="old-bad-sid",
        resume_quarantined=True,
    )
    cmd = process._build_ttyd_command(session_exists=False)
    launch = cmd[-1]
    assert "codex resume" not in launch
    assert "old-bad-sid" not in launch


def test_tmux_kill_session_raises_when_session_survives(monkeypatch: MonkeyPatch) -> None:
    """Ownership quarantine must not claim success while tmux is still live."""

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _fake_exec(*args, **kwargs):
        return _FakeProc()

    async def _still_exists(session_name: str) -> bool:
        return session_name == "claude-hub-live"

    monkeypatch.setattr(ttyd_manager_module.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", _still_exists)

    async def _exercise() -> None:
        with pytest.raises(RuntimeError, match="still exists after kill-session"):
            await ttyd_manager_module._tmux_kill_session("claude-hub-live")

    _run_coro_in_isolated_thread(_exercise())


def test_delete_tab_keeps_process_registered_when_teardown_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    """A failed tmux teardown must remain visible so reconciliation can retry it."""
    manager = TTYDManager.__new__(TTYDManager)
    process = SimpleNamespace(tmux_session="claude-hub-deadbeef")

    async def _failed_stop(*, kill_tmux: bool = False) -> None:
        assert kill_tmux is True
        raise RuntimeError("tmux still alive")

    process.stop = _failed_stop
    manager.processes = {"deadbeef-tab": process}
    manager._tab_order = ["deadbeef-tab"]

    async def _exercise() -> None:
        with pytest.raises(RuntimeError, match="tmux still alive"):
            await manager.delete_tab("deadbeef-tab")

    _run_coro_in_isolated_thread(_exercise())

    assert manager.processes["deadbeef-tab"] is process
    assert manager._tab_order == ["deadbeef-tab"]


def test_startup_prunes_only_old_unpersisted_managed_tmux(
    monkeypatch: MonkeyPatch,
) -> None:
    """Cold start removes old managed-prefix tmux sessions with no tabs.json owner."""
    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {
        "owned-tab": SimpleNamespace(tmux_session="claude-hub-a1b2c3d4"),
    }
    now = 10_000.0

    async def _fake_list() -> dict[str, float]:
        return {
            "claude-hub-a1b2c3d4": now - 500,  # persisted owner: keep
            "claude-hub-deadbeef": now - 500,  # old orphan: prune
            "claude-hub-feedface": now - 10,  # fresh create race: keep
            "claude-hub-nothexzz": now - 500,  # not our exact namespace: keep
            "unrelated-session": now - 500,  # unrelated tmux: keep
        }

    killed: list[str] = []

    async def _fake_kill(session_name: str) -> None:
        killed.append(session_name)

    monkeypatch.setattr(ttyd_manager_module, "_tmux_list_session_created", _fake_list)
    monkeypatch.setattr(ttyd_manager_module, "_tmux_kill_session", _fake_kill)
    monkeypatch.setattr(ttyd_manager_module.time, "time", lambda: now)

    async def _exercise() -> None:
        pruned = await manager._prune_orphan_tmux_sessions(grace_seconds=60.0)
        assert pruned == ["claude-hub-deadbeef"]

    _run_coro_in_isolated_thread(_exercise())
    assert killed == ["claude-hub-deadbeef"]


def test_hot_restart_reattaches_only_tabs_with_surviving_tmux(
    monkeypatch: MonkeyPatch,
) -> None:
    """A backend-only reload must not resurrect every historical saved tab."""
    manager = TTYDManager.__new__(TTYDManager)
    started: list[str] = []
    ensured: list[str] = []

    class FakeProcess:
        def __init__(
            self,
            tab_id: str,
            agent_type: AgentType,
            target: ExecutionTarget = ExecutionTarget.LOCAL,
        ) -> None:
            self.tab_id = tab_id
            self.tmux_session = f"claude-hub-{tab_id}"
            self.agent_type = agent_type
            self.target = target
            self.agent_session_id = "pinned-session"
            self.resume_quarantined = False
            self.cwd = "/tmp"
            self.is_active = False

        async def start(self) -> None:
            started.append(self.tab_id)
            self.is_active = True

        async def ensure_tmux_session(self) -> bool:
            ensured.append(self.tab_id)
            return True

    live = FakeProcess("live0001", AgentType.CLAUDE)
    stopped_cursor = FakeProcess("cold0001", AgentType.CURSOR)
    stopped_codex = FakeProcess("cold0002", AgentType.CODEX)
    stopped_terminal = FakeProcess("cold0003", AgentType.TERMINAL)
    stopped_remote = FakeProcess("cold0004", AgentType.CLAUDE, ExecutionTarget.REMOTE)
    manager.processes = {
        p.tab_id: p for p in (live, stopped_cursor, stopped_codex, stopped_terminal, stopped_remote)
    }

    async def fake_prune() -> list[str]:
        return []

    async def fake_session_exists(session_name: str) -> bool:
        return session_name == live.tmux_session

    monkeypatch.setattr(manager, "_prune_orphan_tmux_sessions", fake_prune)
    monkeypatch.setattr(manager, "_backfill_agent_session_ids", lambda: None)
    monkeypatch.setattr(manager, "_backfill_codex_session_ids", lambda: None)
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(ttyd_manager_module, "_ensure_tmux_server", lambda: None)
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", fake_session_exists)
    monkeypatch.setattr(ttyd_manager_module, "_codex_scan_sessions", lambda: {})
    monkeypatch.setattr(
        ttyd_manager_module.os,
        "popen",
        lambda command: SimpleNamespace(read=lambda: "one live managed session"),
    )

    _run_coro_in_isolated_thread(manager.start_all_tabs())

    assert started == ["live0001"]
    assert ensured == []
    assert live.is_active is True
    assert stopped_cursor.is_active is False
    assert stopped_codex.is_active is False
    assert stopped_terminal.is_active is False
    assert stopped_remote.is_active is False


def test_cold_restart_recovers_all_saved_non_codex_tabs(monkeypatch: MonkeyPatch) -> None:
    """With no surviving managed tmux, preserve full machine-restart recovery."""
    manager = TTYDManager.__new__(TTYDManager)
    started: list[str] = []
    ensured: list[str] = []

    class FakeProcess:
        def __init__(self, tab_id: str, agent_type: AgentType) -> None:
            self.tab_id = tab_id
            self.tmux_session = f"claude-hub-{tab_id}"
            self.agent_type = agent_type
            self.target = ExecutionTarget.LOCAL
            self.agent_session_id = None
            self.resume_quarantined = False
            self.cwd = "/tmp"
            self.is_active = False

        async def start(self) -> None:
            started.append(self.tab_id)
            self.is_active = True

        async def ensure_tmux_session(self) -> bool:
            ensured.append(self.tab_id)
            return True

    claude = FakeProcess("cold1001", AgentType.CLAUDE)
    terminal = FakeProcess("cold1002", AgentType.TERMINAL)
    manager.processes = {p.tab_id: p for p in (claude, terminal)}

    async def fake_prune() -> list[str]:
        return []

    async def no_session_exists(session_name: str) -> bool:
        return False

    monkeypatch.setattr(manager, "_prune_orphan_tmux_sessions", fake_prune)
    monkeypatch.setattr(manager, "_backfill_agent_session_ids", lambda: None)
    monkeypatch.setattr(manager, "_backfill_codex_session_ids", lambda: None)
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(ttyd_manager_module, "_ensure_tmux_server", lambda: None)
    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", no_session_exists)
    monkeypatch.setattr(ttyd_manager_module, "_codex_scan_sessions", lambda: {})
    monkeypatch.setattr(
        ttyd_manager_module.os,
        "popen",
        lambda command: SimpleNamespace(read=lambda: "no server running"),
    )

    _run_coro_in_isolated_thread(manager.start_all_tabs())

    assert sorted(started) == ["cold1001", "cold1002"]
    assert sorted(ensured) == ["cold1001", "cold1002"]
    assert claude.is_active is True
    assert terminal.is_active is True


@pytest.mark.asyncio
async def test_explicit_cursor_recovery_persists_rotated_sid_across_reload(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Opening a skipped Cursor tab must durably pin its constructive resume id."""
    state_file = tmp_path / "tabs.json"
    order_file = tmp_path / "order.json"
    monkeypatch.setattr(ttyd_manager_module, "STATE_FILE", state_file)
    monkeypatch.setattr(ttyd_manager_module, "ORDER_FILE", order_file)
    monkeypatch.setattr(ttyd_manager_module, "_is_local_port_listening", lambda port: False)
    monkeypatch.setattr(ttyd_manager_module, "_cursor_id_exists", lambda sid, cwd: False)

    process = TTYDProcess(
        tab_id="cursor-explicit-recovery",
        port=12501,
        name="Stopped Cursor",
        cwd=str(tmp_path),
        agent_type=AgentType.CURSOR,
        agent_session_id="stale-cursor-sid",
        from_persisted_state=True,
    )
    process.is_active = False

    async def no_tmux(session_name: str) -> bool:
        return False

    async def fake_start() -> None:
        process._build_ttyd_command(session_exists=False)
        process.is_active = True

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", no_tmux)
    monkeypatch.setattr(process, "start", fake_start)

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {process.tab_id: process}
    manager._tab_order = [process.tab_id]
    manager._start_locks = {}

    tab = await manager.ensure_tab_running(process.tab_id)

    assert tab is not None
    assert tab.is_active is True
    assert tab.agent_session_id not in {None, "stale-cursor-sid"}
    assert state_file.exists()

    reloaded = TTYDManager()
    assert reloaded.processes[process.tab_id].agent_session_id == tab.agent_session_id


@pytest.mark.asyncio
async def test_explicit_codex_fallback_is_attributed_and_persisted_across_reload(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Opening a skipped Codex tab must commit the fallback rollout identity."""
    state_file = tmp_path / "tabs.json"
    order_file = tmp_path / "order.json"
    monkeypatch.setattr(ttyd_manager_module, "STATE_FILE", state_file)
    monkeypatch.setattr(ttyd_manager_module, "ORDER_FILE", order_file)
    monkeypatch.setattr(ttyd_manager_module, "_is_local_port_listening", lambda port: False)
    monkeypatch.setattr(ttyd_manager_module, "_codex_scan_sessions", lambda: {})

    process = TTYDProcess(
        tab_id="codex-explicit-recovery",
        port=12502,
        name="Stopped Codex",
        cwd=str(tmp_path),
        agent_type=AgentType.CODEX,
        agent_session_id="stale-codex-sid",
        from_persisted_state=True,
    )
    process.is_active = False
    started: list[str] = []
    reconciled: list[list[str]] = []

    async def no_tmux(session_name: str) -> bool:
        return False

    async def fake_launch(tab: TTYDProcess) -> tuple[str, str]:
        assert tab is process
        tab.agent_session_id = "attributed-fallback-sid"
        tab._is_new_pin = True
        tab.resume_quarantined = False
        return "FALLBACK", "attributed-fallback-sid"

    async def fake_reconcile(tabs: list[TTYDProcess], pre_scan: dict) -> None:
        reconciled.append([tab.tab_id for tab in tabs])

    async def fake_start() -> None:
        started.append(process.tab_id)
        process.is_active = True

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", no_tmux)
    monkeypatch.setattr(process, "start", fake_start)

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {process.tab_id: process}
    manager._tab_order = [process.tab_id]
    manager._start_locks = {}
    monkeypatch.setattr(manager, "_launch_one_cold_codex_locked", fake_launch)
    monkeypatch.setattr(manager, "_reconcile_codex_phase_r", fake_reconcile)

    tab = await manager.ensure_tab_running(process.tab_id)

    assert tab is not None
    assert tab.is_active is True
    assert tab.agent_session_id == "attributed-fallback-sid"
    assert reconciled == [[process.tab_id]]
    assert started == [process.tab_id]

    reloaded = TTYDManager()
    restored = reloaded.processes[process.tab_id]
    assert restored.agent_session_id == "attributed-fallback-sid"
    assert restored.resume_quarantined is False


@pytest.mark.asyncio
async def test_explicit_prequarantined_codex_launch_failure_still_kills_writer(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An incoming quarantine flag must not suppress this attempt's rollback."""
    process = TTYDProcess(
        tab_id="prequarantined-codex",
        port=12503,
        name="Prequarantined Codex",
        cwd=str(tmp_path),
        agent_type=AgentType.CODEX,
        agent_session_id="untrusted-old-sid",
        from_persisted_state=True,
        resume_quarantined=True,
    )
    process.is_active = True
    stopped: list[bool] = []
    saves: list[str] = []

    async def no_tmux(_session_name: str) -> bool:
        return False

    async def fail_after_create(_tab: TTYDProcess) -> tuple[str, str]:
        process.is_active = True
        raise RuntimeError("launch failed after tmux create")

    async def fake_stop(kill_tmux: bool = False) -> None:
        stopped.append(kill_tmux)
        process.is_active = False

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", no_tmux)
    monkeypatch.setattr(ttyd_manager_module, "_codex_scan_sessions", lambda: {})
    monkeypatch.setattr(process, "stop", fake_stop)

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {process.tab_id: process}
    monkeypatch.setattr(manager, "_launch_one_cold_codex_locked", fail_after_create)
    monkeypatch.setattr(manager, "_save_state", lambda: saves.append("saved"))

    with pytest.raises(RuntimeError, match="launch failed after tmux create"):
        await manager._start_missing_tab_identity_safe(process)

    assert stopped == [True]
    assert process.is_active is False
    assert process.resume_quarantined is True
    assert process.agent_session_id is None
    assert saves == ["saved"]


@pytest.mark.asyncio
async def test_explicit_codex_recovery_cancellation_shields_writer_teardown(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation after tmux creation must wait for kill and durable quarantine."""
    process = TTYDProcess(
        tab_id="cancelled-codex-recovery",
        port=12504,
        name="Cancelled Codex",
        cwd=str(tmp_path),
        agent_type=AgentType.CODEX,
        agent_session_id="old-codex-sid",
        from_persisted_state=True,
    )
    process.is_active = False
    launch_created = asyncio.Event()
    keep_launch_open = asyncio.Event()
    stopped: list[bool] = []
    saves: list[str] = []

    async def no_tmux(_session_name: str) -> bool:
        return False

    async def wait_after_create(_tab: TTYDProcess) -> tuple[str, str]:
        process.is_active = True
        launch_created.set()
        await keep_launch_open.wait()
        raise AssertionError("cancelled launch should not resume")

    async def fake_stop(kill_tmux: bool = False) -> None:
        await asyncio.sleep(0)
        stopped.append(kill_tmux)
        process.is_active = False

    monkeypatch.setattr(ttyd_manager_module, "_tmux_session_exists_async", no_tmux)
    monkeypatch.setattr(ttyd_manager_module, "_codex_scan_sessions", lambda: {})
    monkeypatch.setattr(process, "stop", fake_stop)

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {process.tab_id: process}
    monkeypatch.setattr(manager, "_launch_one_cold_codex_locked", wait_after_create)
    monkeypatch.setattr(manager, "_save_state", lambda: saves.append("saved"))

    recovery = asyncio.create_task(manager._start_missing_tab_identity_safe(process))
    await launch_created.wait()
    recovery.cancel()

    with pytest.raises(asyncio.CancelledError):
        await recovery

    assert stopped == [True]
    assert process.is_active is False
    assert process.resume_quarantined is True
    assert process.agent_session_id is None
    assert saves == ["saved"]


@pytest.mark.asyncio
async def test_explicit_codex_cleanup_failure_persists_quarantine_evidence(
    monkeypatch: MonkeyPatch,
) -> None:
    """Repeated tmux kill failure must still durably retain its owner evidence."""
    process = TTYDProcess(
        tab_id="codex-cleanup-failure",
        port=12505,
        name="Codex Cleanup Failure",
        agent_type=AgentType.CODEX,
        agent_session_id="last-known-owner-sid",
        from_persisted_state=True,
    )
    stop_attempts: list[bool] = []
    saved: list[tuple[str | None, bool, str | None]] = []

    async def failed_stop(kill_tmux: bool = False) -> None:
        stop_attempts.append(kill_tmux)
        raise RuntimeError("tmux still alive")

    monkeypatch.setattr(process, "stop", failed_stop)

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {process.tab_id: process}
    monkeypatch.setattr(
        manager,
        "_save_state",
        lambda: saved.append(
            (
                process.agent_session_id,
                process.resume_quarantined,
                process._pending_quarantine_reason,
            )
        ),
    )

    with pytest.raises(RuntimeError, match="failed to stop owned tmux/ttyd"):
        await manager._finish_explicit_codex_recovery_rollback(process, "test rollback")

    assert stop_attempts == [True, True]
    assert process.agent_session_id == "last-known-owner-sid"
    assert process.resume_quarantined is True
    assert "cleanup failed" in (process._pending_quarantine_reason or "")
    assert saved == [
        (
            "last-known-owner-sid",
            True,
            process._pending_quarantine_reason,
        )
    ]


def test_quarantine_preserves_sid_when_teardown_cannot_be_proven(
    monkeypatch: MonkeyPatch,
) -> None:
    """Do not erase the last owner SID when its process may still be writing."""
    manager = TTYDManager.__new__(TTYDManager)
    tab = TTYDProcess(
        tab_id="tab-live-writer",
        port=12407,
        name="Live Writer",
        agent_type=AgentType.CODEX,
        agent_session_id="owned-sid",
    )

    async def _failed_stop(*, kill_tmux: bool = False) -> None:
        assert kill_tmux is True
        raise RuntimeError("tmux survived")

    monkeypatch.setattr(tab, "stop", _failed_stop)

    async def _exercise() -> None:
        with pytest.raises(RuntimeError, match="failed to stop owned tmux/ttyd"):
            await manager._quarantine_codex_tab(tab, "ambiguous attribution")

    _run_coro_in_isolated_thread(_exercise())

    assert tab.agent_session_id == "owned-sid"
    assert tab.resume_quarantined is True
    assert "cleanup failed" in (tab._pending_quarantine_reason or "")


def test_legacy_state_without_shell_explicitly_provided_uses_agent_cli(
    monkeypatch: MonkeyPatch, tmp_path
) -> None:
    """Legacy tabs.json (no shell_explicitly_provided key) must not bypass the
    agent CLI/resume path for Claude/Codex/Cursor tabs.

    Before the fix, the fallback ``bool(shell)`` was always True because
    ``self.shell`` is always non-None, so every agent tab was treated as an
    explicit-shell tab and launched the shell directly instead of the agent
    CLI with resume/session-id semantics.
    """
    state_file = tmp_path / "tabs.json"
    monkeypatch.setattr(ttyd_manager_module, "STATE_FILE", state_file)

    # Build persisted rows the way a pre-shell_explicitly_provided backend
    # would have: include "shell" but omit "shell_explicitly_provided".
    def _legacy_row(tab_id: str, agent_type: AgentType, shell: str) -> dict:
        proc = TTYDProcess(
            tab_id=tab_id,
            port=12380 + hash(tab_id) % 100,
            name=tab_id,
            agent_type=agent_type,
            shell=shell,
        )
        row = proc.to_dict()
        row.pop("shell_explicitly_provided", None)
        return row

    rows = [
        # Agent tabs whose shell equals the default agent command: these must
        # NOT be treated as explicit-shell overrides.
        _legacy_row("legacy-claude", AgentType.CLAUDE, "claude"),
        _legacy_row("legacy-codex", AgentType.CODEX, "codex"),
        _legacy_row("legacy-cursor", AgentType.CURSOR, "agent"),
        # A terminal tab: flag has no behavioral effect but must default False.
        _legacy_row("legacy-terminal", AgentType.TERMINAL, "/bin/zsh"),
        # An agent tab whose shell differs from the agent command: this WAS an
        # explicit override and must keep running the shell directly.
        _legacy_row("legacy-claude-override", AgentType.CLAUDE, "/bin/bash"),
    ]
    state_file.write_text(json.dumps(rows), encoding="utf-8")

    manager = TTYDManager.__new__(TTYDManager)
    manager.processes = {}
    manager._next_port = 10000
    manager._load_state()

    # Claude/Codex/Cursor with default agent command shell: NOT explicit, so
    # they must use the agent CLI/resume path (not the shell-direct branch).
    for tab_id, agent_type in [
        ("legacy-claude", AgentType.CLAUDE),
        ("legacy-codex", AgentType.CODEX),
        ("legacy-cursor", AgentType.CURSOR),
    ]:
        proc = manager.processes[tab_id]
        assert (
            proc._shell_explicitly_provided is False
        ), f"{tab_id}: default agent shell must not be marked explicit"
        cmd = proc._build_ttyd_command(session_exists=False)
        # The explicit-shell branch appends self.shell directly; the agent CLI
        # branch wraps the agent command with resume/session flags. For a fresh
        # (non-recover) launch the tail must contain the agent command, not a
        # bare shell path.
        tail = cmd[-1]
        agent_cmd = ttyd_manager_module.get_agent_command(agent_type)
        assert agent_cmd in tail, f"{tab_id}: agent CLI path must be used"

    # Terminal tab defaults to non-explicit (no behavioral difference).
    term = manager.processes["legacy-terminal"]
    assert term._shell_explicitly_provided is False

    # Agent tab with a non-default shell WAS an explicit override: keep it.
    override = manager.processes["legacy-claude-override"]
    assert override._shell_explicitly_provided is True
    cmd = override._build_ttyd_command(session_exists=False)
    assert "/bin/bash" in cmd[-1]

    # The next save must persist the now-derived key deterministically.
    # Exercise the real manager save boundary (not just to_dict()) and verify
    # the persisted raw JSON contains the expected boolean for every row.
    manager._save_state()
    saved_rows = json.loads(state_file.read_text(encoding="utf-8"))
    saved_by_id = {row["id"]: row for row in saved_rows}

    expected_explicit = {
        "legacy-claude": False,
        "legacy-codex": False,
        "legacy-cursor": False,
        "legacy-terminal": False,
        "legacy-claude-override": True,
    }
    for tab_id, expected in expected_explicit.items():
        row = saved_by_id[tab_id]
        assert (
            "shell_explicitly_provided" in row
        ), f"{tab_id}: saved row must include shell_explicitly_provided"
        assert row["shell_explicitly_provided"] is expected, (
            f"{tab_id}: expected shell_explicitly_provided={expected}, "
            f"got {row['shell_explicitly_provided']}"
        )

    # Reload from the persisted (migrated) state to prove the representation
    # is stable: the boolean round-trips unchanged through save+load.
    manager2 = TTYDManager.__new__(TTYDManager)
    manager2.processes = {}
    manager2._next_port = 10000
    manager2._load_state()
    for tab_id, expected in expected_explicit.items():
        proc = manager2.processes[tab_id]
        assert (
            proc._shell_explicitly_provided is expected
        ), f"{tab_id}: stable reload must preserve shell_explicitly_provided={expected}"
