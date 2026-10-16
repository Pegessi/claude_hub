"""Managed Workspace Chat admission and configuration contracts."""

from __future__ import annotations

from datetime import datetime

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from claude_hub.cli.client import HubError
from claude_hub.cli.main import cli
from claude_hub.models import (
    AgentType,
    EnsureWorkspaceAgentRequest,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SendSessionMessageRequest,
    SessionKind,
    WorkspaceSessionRole,
    WorkspaceTaskCreate,
    WorkspaceTaskUpdate,
)
from claude_hub.models.task_session import ManagedExecutorConfig
from claude_hub.services.task_session_executor import (
    config_from_session,
    managed_executor_capabilities,
    resolve_config_for_workspace,
    validate_session_matches_config,
)


def test_managed_session_kind_defaults_to_terminal() -> None:
    request = EnsureWorkspaceAgentRequest(agent_type=AgentType.CLAUDE)
    config = ManagedExecutorConfig(agent_type=AgentType.CLAUDE)

    assert request.session_kind == SessionKind.TERMINAL
    assert config.session_kind == SessionKind.TERMINAL


def test_task_session_kind_defaults_persists_and_validates_provider() -> None:
    terminal = WorkspaceTaskCreate(title="Terminal", prompt="work")
    chat = WorkspaceTaskCreate(
        title="Chat",
        prompt="work",
        agent_type=AgentType.MASO,
        session_kind=SessionKind.CHAT,
    )
    update = WorkspaceTaskUpdate(session_kind=SessionKind.CHAT)

    assert terminal.session_kind == SessionKind.TERMINAL
    assert chat.session_kind == SessionKind.CHAT
    assert update.session_kind == SessionKind.CHAT
    with pytest.raises(ValidationError, match="select a Chat session"):
        WorkspaceTaskCreate(title="bad", prompt="work", agent_type=AgentType.MASO)
    with pytest.raises(ValidationError, match="TraeX is not supported"):
        WorkspaceTaskCreate(
            title="bad",
            prompt="work",
            agent_type=AgentType.TRAEX,
            session_kind=SessionKind.CHAT,
        )


def test_plain_terminal_workspace_agent_remains_supported() -> None:
    request = EnsureWorkspaceAgentRequest(
        agent_type=AgentType.TERMINAL,
        session_kind=SessionKind.TERMINAL,
    )
    assert request.agent_type == AgentType.TERMINAL

    with pytest.raises(ValidationError, match="supports only"):
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.TERMINAL,
            session_kind=SessionKind.CHAT,
        )


@pytest.mark.parametrize(
    "agent_type",
    [AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR, AgentType.MASO],
)
def test_local_managed_chat_admits_supported_provider(agent_type: AgentType) -> None:
    request = EnsureWorkspaceAgentRequest(
        agent_type=agent_type,
        session_kind=SessionKind.CHAT,
        target=ExecutionTarget.LOCAL,
    )

    assert request.session_kind == SessionKind.CHAT


def test_managed_chat_rejects_remote_and_maso_terminal() -> None:
    with pytest.raises(ValidationError, match="Hub host"):
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            target=ExecutionTarget.REMOTE,
        )
    with pytest.raises(ValidationError, match="select a Chat session"):
        EnsureWorkspaceAgentRequest(agent_type=AgentType.MASO)

    with pytest.raises(ValueError, match="Hub host"):
        resolve_config_for_workspace(
            ManagedExecutorConfig(
                agent_type=AgentType.CODEX,
                session_kind=SessionKind.CHAT,
            ),
            workspace_id="ws",
            workspace_target=ExecutionTarget.REMOTE,
            remote_profile_id="remote",
            remote_cwd="/repo",
            remote_reconnect=True,
        )


def test_executor_config_preserves_surface_and_matches_it() -> None:
    now = datetime.now()
    session = ManagedSession(
        id="managed-chat",
        workspace_id="ws",
        tab_id="tab",
        role=WorkspaceSessionRole.REVIEWER,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        title="Reviewer",
        workspace_path="/repo",
        tmux_session="",
        env={"ANTHROPIC_MODEL": "sonnet", "KEEP": "1"},
        created_at=now,
        updated_at=now,
    )

    recovered = config_from_session(session)
    assert recovered.session_kind == SessionKind.CHAT
    assert recovered.model == "sonnet"
    assert recovered.env == {"KEEP": "1"}
    assert recovered.remote_reconnect is True
    resolved = resolve_config_for_workspace(
        recovered,
        workspace_id="ws",
        workspace_target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_cwd=None,
        remote_reconnect=True,
    )
    assert resolved.target == ExecutionTarget.LOCAL
    assert resolved.session_kind == SessionKind.CHAT
    validate_session_matches_config(session, resolved, workspace_id="ws")

    terminal = recovered.model_copy(update={"session_kind": SessionKind.TERMINAL})
    with pytest.raises(ValueError, match="session_kind"):
        validate_session_matches_config(session, terminal, workspace_id="ws")


def test_executor_capabilities_advertise_chat_additively() -> None:
    capabilities = managed_executor_capabilities()

    assert capabilities.supported_session_kinds == [SessionKind.TERMINAL, SessionKind.CHAT]
    assert capabilities.chat_supported_agent_types == [
        AgentType.CLAUDE,
        AgentType.CODEX,
        AgentType.CURSOR,
        AgentType.MASO,
    ]


def test_agent_create_cli_forwards_session_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Client:
        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def ensure_agent(self, workspace_id: str, body: dict[str, object]) -> dict[str, object]:
            captured.update({"workspace_id": workspace_id, "body": body})
            return {"id": "session", "session_kind": body["session_kind"]}

    monkeypatch.setattr("claude_hub.cli.main.get_client", lambda _ctx: Client())
    result = CliRunner().invoke(
        cli,
        [
            "agent",
            "create",
            "ws",
            "--agent-type",
            "codex",
            "--session-kind",
            "chat",
            "--env-preset",
            "none",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["workspace_id"] == "ws"
    assert captured["body"]["session_kind"] == "chat"  # type: ignore[index]


def test_task_run_cli_forwards_surface_to_agent_and_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, dict[str, object]] = {}

    class Client:
        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def ensure_agent(self, _workspace_id: str, body: dict[str, object]) -> dict[str, object]:
            captured["agent"] = body
            return {"id": "session"}

        def create_task(self, _workspace_id: str, body: dict[str, object]) -> dict[str, object]:
            captured["task"] = body
            return {"id": "task"}

        def start_task(self, _task_id: str, body: dict[str, object]) -> dict[str, object]:
            captured["start"] = body
            return {"status": "working"}

    monkeypatch.setattr("claude_hub.cli.main.get_client", lambda _ctx: Client())
    result = CliRunner().invoke(
        cli,
        [
            "task",
            "run",
            "ws",
            "--title",
            "Chat task",
            "--prompt",
            "work",
            "--agent-type",
            "codex",
            "--session-kind",
            "chat",
            "--env-preset",
            "none",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["agent"]["session_kind"] == "chat"
    assert captured["task"]["session_kind"] == "chat"


def test_session_send_cli_generates_and_forwards_stable_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {"fail": False}

    class Client:
        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def send_session(self, session_id: str, body: dict[str, object]) -> None:
            captured["session_id"] = session_id
            captured["body"] = body
            if captured["fail"]:
                raise HubError("network failed")

    monkeypatch.setattr("claude_hub.cli.main.get_client", lambda _ctx: Client())
    result = CliRunner().invoke(cli, ["session", "send", "session", "--message", "hello"])

    assert result.exit_code == 0, result.output
    call_id = captured["body"]["call_id"]  # type: ignore[index]
    assert isinstance(call_id, str)
    assert result.stdout.strip() == "sent"
    assert result.stderr.strip() == f"call_id={call_id}"
    assert SendSessionMessageRequest(message="hello", call_id=call_id).call_id == call_id
    with pytest.raises(ValidationError, match="must not be blank"):
        SendSessionMessageRequest(message="hello", call_id="   ")

    captured["fail"] = True
    failed = CliRunner().invoke(cli, ["session", "send", "session", "--message", "again"])
    failed_call_id = captured["body"]["call_id"]  # type: ignore[index]
    assert failed.exit_code != 0
    assert f"call_id={failed_call_id}" in failed.stderr
    assert "network failed" in failed.stderr

    captured["fail"] = False
    retried = CliRunner().invoke(
        cli,
        [
            "session",
            "send",
            "session",
            "--message",
            "hello",
            "--call-id",
            str(call_id),
        ],
    )
    assert retried.exit_code == 0, retried.output
    assert captured["body"]["call_id"] == call_id  # type: ignore[index]
