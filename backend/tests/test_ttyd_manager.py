import importlib

from pytest import MonkeyPatch

from claude_hub.models import AgentRuntimeStatus, AgentType, ExecutionTarget, RemoteProfile
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
    assert "ssh -tt devbox" in launcher
    assert "$HOME/.nvm/versions/node" in launcher
    assert "cd ~/repo" in launcher
    assert "tmux new-session -A -s claude-hub-tab-remo" in launcher
    assert "Remote tmux not found in PATH; starting without remote tmux persistence" in launcher
    assert "codex --ask-for-approval never --sandbox danger-full-access" in launcher


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
        agent_type=AgentType.CURSOR,
        target=ExecutionTarget.REMOTE,
        remote_profile_id="devbox",
        remote_cwd="/opt/tiger/app",
        remote_reconnect=False,
    )

    launcher = process._build_ttyd_command(session_exists=False)[-1]

    assert launcher.startswith("exec ")
    assert "ssh -tt -p 2222 tiger@devbox" in launcher
    assert "cd /opt/tiger/app" in launcher
    assert "${SHELL:-/bin/bash} -l" in launcher


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
