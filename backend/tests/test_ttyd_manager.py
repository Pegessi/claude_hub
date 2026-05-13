from pytest import MonkeyPatch

from claude_hub.models import AgentType, WorkspaceSessionRole
from claude_hub.services.ttyd_manager import TTYDProcess


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
