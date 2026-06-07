import importlib

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    RemoteProfile,
    WorkspaceSessionRole,
)
from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

ttyd_manager_module = importlib.import_module("claude_hub.services.ttyd_manager")


def test_codex_tab_uses_codex_command() -> None:
    process = TTYDProcess(
        tab_id="tab-codex-normal",
        port=12345,
        name="Codex",
        agent_type=AgentType.CODEX,
    )

    assert process.shell == "codex"
    assert process._build_ttyd_command(session_exists=False)[-1] == "codex"


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

    assert cmd[-1].startswith("env HTTP_PROXY=http://127.0.0.1:7890 NO_PROXY=localhost,127.0.0.1 ")
    assert data["env"] == {"HTTP_PROXY": "http://127.0.0.1:7890", "NO_PROXY": "localhost,127.0.0.1"}
    assert schema.env == {"HTTP_PROXY": "http://127.0.0.1:7890", "NO_PROXY": "localhost,127.0.0.1"}


def test_custom_env_rejects_invalid_names() -> None:
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        TTYDProcess(
            tab_id="tab-invalid-env",
            port=12348,
            name="Invalid Env Tab",
            agent_type=AgentType.TERMINAL,
            env={"BAD-NAME": "value"},
        )


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

    monkeypatch.setattr(ttyd_manager_module, "_is_local_port_listening", lambda _port: False)
    monkeypatch.setattr(TTYDProcess, "start", fake_start)

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
