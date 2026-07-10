import importlib
import json
import os
import shlex
import stat
import subprocess
from datetime import datetime, timedelta

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    RemoteProfile,
    WorkspaceSessionRole,
)
from claude_hub.services.ttyd_manager import (
    DEFAULT_CLAUDE_LAUNCH_ENV,
    TTYDManager,
    TTYDProcess,
)

ttyd_manager_module = importlib.import_module("claude_hub.services.ttyd_manager")


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


def test_non_claude_agents_do_not_pin_session_id() -> None:
    for agent_type in (AgentType.CODEX, AgentType.CURSOR, AgentType.TERMINAL):
        process = TTYDProcess(
            tab_id=f"tab-no-session-{agent_type.value}",
            port=12361,
            name="No Session",
            agent_type=agent_type,
        )
        assert process.agent_session_id is None


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


def test_codex_recovery_resumes_last_with_fallback() -> None:
    # Solo codex (the workspace default) recovers via the solo-mode launch path.
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
    assert "codex resume --last" in launch
    assert "||" in launch
    # Solo flags must ride on the resume branch, not only the fresh fallback,
    # or a successful `resume --last` silently drops solo mode.
    assert "codex resume --last --ask-for-approval never --sandbox danger-full-access" in launch
    assert "|| codex --ask-for-approval never --sandbox danger-full-access" in launch


def test_non_solo_codex_recovery_resumes_last() -> None:
    # Non-solo codex normally launches via the bare "codex" shell; on recovery
    # it must still route through the agent command so `resume --last` runs.
    process = TTYDProcess(
        tab_id="tab-codex-nonsolo-recover",
        port=12378,
        name="Codex NonSolo Recover",
        agent_type=AgentType.CODEX,
        solo_mode=False,
        from_persisted_state=True,
    )

    recover_cmd = process._build_ttyd_command(session_exists=False)
    assert "codex resume --last" in recover_cmd[-1]
    assert "||" in recover_cmd[-1]
    # Non-solo must NOT carry solo flags on either branch.
    assert "--ask-for-approval" not in recover_cmd[-1]
    assert "--sandbox" not in recover_cmd[-1]

    # A live session (backend-only restart) must NOT resume — bare reattach.
    live_cmd = process._build_ttyd_command(session_exists=True)
    assert "resume" not in live_cmd[-1]


def test_cursor_recovery_continues_with_fallback() -> None:
    process = TTYDProcess(
        tab_id="tab-cursor-recover",
        port=12369,
        name="Cursor Recover",
        agent_type=AgentType.CURSOR,
        from_persisted_state=True,
    )

    cmd = process._build_ttyd_command(session_exists=False)
    launch = cmd[-1]
    assert "agent --continue" in launch
    assert "|| agent" in launch


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
    assert "codex resume --last" in respawn_cmd
    assert "|| codex" in respawn_cmd
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
    assert "codex resume --last" in respawn_cmd
    # Regression guard: solo flags must ride on the resume branch itself, not
    # only the fresh fallback — a successful `resume --last` would otherwise
    # relaunch codex without solo mode.
    assert (
        "codex resume --last --ask-for-approval never --sandbox danger-full-access" in respawn_cmd
    )
    assert "|| codex --ask-for-approval never --sandbox danger-full-access" in respawn_cmd
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
    # Toggling into solo must also flag the resume branch.
    assert (
        "codex resume --last --ask-for-approval never --sandbox danger-full-access" in respawn_cmd
    )
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
