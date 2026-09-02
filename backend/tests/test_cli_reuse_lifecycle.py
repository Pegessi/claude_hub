"""Tests for CLI workspace/agent reuse lifecycle policy."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from click.testing import CliRunner
from pytest import MonkeyPatch

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient
from claude_hub.cli.commands.common import LIFECYCLE_RECIPE
from claude_hub.cli.main import cli
from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    EnsureWorkspaceAgentRequest,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    TerminalTab,
    Workspace,
    WorkspaceCreate,
    WorkspaceEnsure,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
    WorkspaceUpdate,
)
from claude_hub.services.ttyd_manager import ttyd_manager
from claude_hub.services.workspace_identity import DuplicateWorkspaceError
from claude_hub.services.workspace_manager import WorkspaceManager, workspace_manager


def make_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> HubClient:
    transport = httpx.MockTransport(handler)
    return HubClient(base_url="http://testserver", transport=transport, **kwargs)


def patch_get_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    def fake_get_client(ctx: Any) -> HubClient:
        return make_client(handler)

    monkeypatch.setattr(cli_main, "get_client", fake_get_client)


LIFECYCLE_HELP_MARKERS = (
    "workspace ensure",
    "task cleanup",
    "--cwd .",
    "--env-preset NAME_OR_ID",
    "avoid extra agents",
    "built-in or saved custom env preset",
    "Never delete reused/shared agents",
)


def _normalize_help_text(text: str) -> str:
    return " ".join(text.split())


def _assert_lifecycle_help(output: str) -> None:
    assert "Lifecycle:" in output
    assert "git common-dir" in output
    normalized = _normalize_help_text(output)
    for marker in LIFECYCLE_HELP_MARKERS:
        assert marker in normalized, marker
    # Group help must render the shared recipe body, not summary-only hacks.
    assert _normalize_help_text(LIFECYCLE_RECIPE) in _normalize_help_text(output)


@pytest.mark.parametrize(
    ("args", "summary"),
    [
        (["--help"], "Claude Hub command-line interface."),
        (["workspace", "--help"], "Manage agent workspaces."),
        (["agent", "--help"], "Manage resident workspace agent sessions."),
        (["task", "--help"], "Manage workspace tasks."),
    ],
)
def test_cli_group_help_renders_lifecycle_recipe(args: list[str], summary: str) -> None:
    """Root and workspace/agent/task group --help must render the lifecycle recipe."""
    runner = CliRunner()
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert summary in result.output
    _assert_lifecycle_help(result.output)


@pytest.mark.parametrize("args", [["--help"], ["agent", "--help"]])
def test_cli_root_and_agent_help_include_feature_worktree_cwd_and_generic_preset(
    args: list[str],
) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    normalized = _normalize_help_text(result.output)
    assert "--cwd ." in normalized
    assert "--env-preset NAME_OR_ID" in normalized
    assert "day1 is only an example" in normalized
    assert "--agent-type claude" in normalized


def test_agent_entry_docs_feature_worktree_cwd_and_generic_preset_guidance() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    agents = (repo_root / "AGENTS.md").read_text(encoding="utf-8")
    claude = (repo_root / "CLAUDE.md").read_text(encoding="utf-8")
    assert agents == claude
    for text in (agents,):
        assert "--cwd ." in text
        assert "--env-preset NAME_OR_ID" in text
        assert "any built-in preset or saved custom preset" in text
        assert "not a concurrency/idempotency boundary" in text
        assert "--agent-type claude" in text
        assert "feature worktree" in text.lower()
        assert "Hub Workspace is shared" in text or "share the same" in text


def test_agent_create_defaults_reuse_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "s1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["agent", "create", "ws1", "--agent-type", "claude"])
    assert result.exit_code == 0, result.output
    assert bodies[0]["reuse_existing"] is True


def test_agent_create_ephemeral_conflicts_with_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_get_client(monkeypatch, lambda r: httpx.Response(200, json={}))
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["agent", "create", "ws1", "--ephemeral", "--reuse-existing"],
    )
    assert result.exit_code != 0
    assert "Cannot combine" in result.output


def test_agent_create_ephemeral_sets_caller_owned_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"id": "s1", "env": {"ANTHROPIC_AUTH_TOKEN": "secret"}},
        )

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "agent", "create", "ws1", "--ephemeral"])
    assert result.exit_code == 0, result.output
    assert bodies[0]["ephemeral"] is True
    assert bodies[0]["caller_owned_ephemeral"] is True
    payload = json.loads(result.output)
    assert payload["env"]["ANTHROPIC_AUTH_TOKEN"] == "[redacted]"


def test_agent_create_non_ephemeral_leaves_caller_owned_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "s1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["agent", "create", "ws1", "--agent-type", "claude"])
    assert result.exit_code == 0, result.output
    assert bodies[0].get("caller_owned_ephemeral") is False


def test_workspace_ensure_posts_to_api(monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"id": "ws1", "path": "/repo"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["workspace", "ensure", "--name", "Demo", "--path", "/repo"],
    )
    assert result.exit_code == 0, result.output
    assert paths[0] == "/api/workspaces/ensure"


def test_cli_workspace_ensure_accepts_path_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docs recipe: ``claude-hub workspace ensure --path …`` without --name."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "ws1", "name": "repo", "path": "/repo"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    help_result = runner.invoke(cli, ["workspace", "ensure", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "[required]" not in help_result.output.split("--name")[1].split("--path")[0]

    result = runner.invoke(cli, ["workspace", "ensure", "--path", "/repo"])
    assert result.exit_code == 0, result.output
    assert bodies[0]["path"] == "/repo"
    assert "name" not in bodies[0]


def test_cli_workspace_create_resolves_relative_path_in_request_body(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "ws1", "name": "A", "path": str(repo.resolve())})

    patch_get_client(monkeypatch, handler)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["workspace", "create", "--name", "A", "--path", "repo"])
    assert result.exit_code == 0, result.output
    assert bodies[0]["path"] == str(repo.resolve())
    assert Path(bodies[0]["path"]).is_absolute()


def test_cli_workspace_ensure_resolves_relative_path_in_request_body(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "ws1", "name": "repo", "path": str(repo.resolve())})

    patch_get_client(monkeypatch, handler)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["workspace", "ensure", "--path", "./repo"])
    assert result.exit_code == 0, result.output
    assert bodies[0]["path"] == str(repo.resolve())
    assert Path(bodies[0]["path"]).is_absolute()


def test_cli_agent_create_resolves_relative_cwd_and_preserves_remote_cwd(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "s1"})

    patch_get_client(monkeypatch, handler)
    monkeypatch.chdir(repo)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "agent",
            "create",
            "ws1",
            "--agent-type",
            "claude",
            "--cwd",
            ".",
            "--remote-cwd",
            "~/evals/ff45d",
        ],
    )
    assert result.exit_code == 0, result.output
    assert bodies[0]["cwd"] == str(repo.resolve())
    assert Path(bodies[0]["cwd"]).is_absolute()
    assert bodies[0]["remote_cwd"] == "~/evals/ff45d"


def test_cli_agent_create_omits_cwd_when_unset(
    monkeypatch: MonkeyPatch,
) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "s1"})

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["agent", "create", "ws1", "--agent-type", "claude"])
    assert result.exit_code == 0, result.output
    assert "cwd" not in bodies[0]


@pytest.mark.asyncio
async def test_ensure_workspace_create_derives_name_from_canonical_basename(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "my-repo"
    repo.mkdir()
    workspace = manager.ensure_workspace(WorkspaceEnsure(path=str(repo)))
    assert workspace.name == "my-repo"


@pytest.mark.asyncio
async def test_ensure_workspace_create_from_worktree_uses_primary_basename(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo, wt = _init_git_repo_with_worktree(tmp_path)
    workspace = manager.ensure_workspace(WorkspaceEnsure(path=str(wt)))
    assert workspace.name == repo.name
    assert workspace.path == str(repo.resolve())


@pytest.mark.asyncio
async def test_create_workspace_duplicate_refused(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    first = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    assert first.id
    with pytest.raises(DuplicateWorkspaceError):
        manager.create_workspace(WorkspaceCreate(name="B", path=str(repo)))


@pytest.mark.asyncio
async def test_ensure_workspace_reuses_existing(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    created = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    ensured = manager.ensure_workspace(WorkspaceEnsure(path=str(repo)))
    assert ensured.id == created.id


@pytest.mark.asyncio
async def test_compatible_agent_reuse_strict_match(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    idle_claude = ManagedSession(
        id="agent-1",
        workspace_id=ws.id,
        task_id=None,
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.SPAWNING,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Agent",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    busy_codex = idle_claude.model_copy(
        update={
            "id": "agent-2",
            "tab_id": "tab-2",
            "agent_type": AgentType.CODEX,
            "runtime_status": AgentRuntimeStatus.WORKING,
        }
    )
    manager.sessions[idle_claude.id] = idle_claude
    manager.sessions[busy_codex.id] = busy_codex

    match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(agent_type=AgentType.CLAUDE, reuse_existing=True),
    )
    assert match is not None
    assert match.id == "agent-1"

    no_match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(agent_type=AgentType.CODEX, reuse_existing=True),
    )
    assert no_match is None


def _init_git_repo(repo: Path) -> None:
    import subprocess

    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e.com",
        },
    )


def _init_git_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    import subprocess

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    wt = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "feat/wt"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo, wt


def test_find_compatible_same_repo_different_cwd_no_reuse(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from claude_hub.services.workspace_identity import effective_local_agent_cwd, same_local_repo

    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo, wt = _init_git_repo_with_worktree(tmp_path)
    assert same_local_repo(repo, wt)
    assert effective_local_agent_cwd(repo) != effective_local_agent_cwd(wt)

    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    main_agent = ManagedSession(
        id="agent-main",
        workspace_id=ws.id,
        task_id=None,
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Main",
        branch=None,
        workspace_path=str(repo.resolve()),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[main_agent.id] = main_agent

    same_cwd = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            reuse_existing=True,
            cwd=str(repo),
        ),
    )
    assert same_cwd is not None
    assert same_cwd.id == "agent-main"

    linked_cwd = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            reuse_existing=True,
            cwd=str(wt),
        ),
    )
    assert linked_cwd is None


@pytest.mark.asyncio
async def test_ensure_workspace_agent_rejects_foreign_repo_cwd(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo_a, _ = _init_git_repo_with_worktree(tmp_path / "a")
    repo_b = tmp_path / "b" / "repo"
    _init_git_repo(repo_b)
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo_a)))

    with pytest.raises(ValueError, match="different Git repository"):
        await manager.ensure_workspace_agent(
            ws.id,
            EnsureWorkspaceAgentRequest(
                agent_type=AgentType.CLAUDE,
                reuse_existing=False,
                cwd=str(repo_b),
            ),
        )


@pytest.mark.asyncio
async def test_ensure_workspace_agent_accepts_linked_worktree_cwd_without_reusing_main(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo, wt = _init_git_repo_with_worktree(tmp_path)
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    main_agent = ManagedSession(
        id="agent-main",
        workspace_id=ws.id,
        task_id=None,
        tab_id="tab-main",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Main",
        branch=None,
        workspace_path=str(repo.resolve()),
        tmux_session="tmux-main",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[main_agent.id] = main_agent

    created_cwds: list[str] = []

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        created_cwds.append(str(kwargs.get("cwd")))
        tab_id = f"tab-{len(created_cwds)}"
        return TerminalTab(
            id=tab_id,
            name=str(kwargs.get("name") or tab_id),
            cwd=str(kwargs.get("cwd")),
            solo_mode=True,
            agent_type=AgentType.CLAUDE,
            port=9100 + len(created_cwds),
            created_at=datetime.now(timezone.utc),
            is_active=True,
        )

    monkeypatch.setattr(ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", lambda *_a, **_k: None)

    session = await manager.ensure_workspace_agent(
        ws.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            reuse_existing=True,
            cwd=str(wt),
        ),
    )
    assert session.id != "agent-main"
    assert session.workspace_path == str(wt.resolve())
    assert created_cwds == [str(wt.resolve())]


@pytest.mark.asyncio
async def test_ensure_workspace_agent_accepts_non_git_descendant_cwd(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    plain = tmp_path / "plain"
    plain.mkdir()
    sub = plain / "sub"
    sub.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="Plain", path=str(plain)))

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        return TerminalTab(
            id="tab-sub",
            name="sub",
            cwd=str(kwargs.get("cwd")),
            solo_mode=True,
            agent_type=AgentType.CLAUDE,
            port=9100,
            created_at=datetime.now(timezone.utc),
            is_active=True,
        )

    monkeypatch.setattr(ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", lambda *_a, **_k: None)

    session = await manager.ensure_workspace_agent(
        ws.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            reuse_existing=False,
            cwd=str(sub),
        ),
    )
    assert session.workspace_path == str(sub.resolve())

    with pytest.raises(ValueError, match="subdirectory"):
        await manager.ensure_workspace_agent(
            ws.id,
            EnsureWorkspaceAgentRequest(
                agent_type=AgentType.CLAUDE,
                reuse_existing=False,
                cwd=str(tmp_path / "outside"),
            ),
        )


def test_find_compatible_dispatcher_requires_strict_compatibility(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    dispatcher = ManagedSession(
        id="dispatcher-1",
        workspace_id=ws.id,
        task_id=None,
        tab_id="tab-1",
        role=WorkspaceSessionRole.DISPATCHER,
        agent_type=AgentType.CODEX,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Dispatcher",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[dispatcher.id] = dispatcher
    manager.workspaces[ws.id] = ws.model_copy(update={"dispatcher_session_id": dispatcher.id})

    match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            role=WorkspaceSessionRole.DISPATCHER,
            reuse_existing=True,
            cwd=str(repo),
        ),
    )
    assert match is None


def test_find_compatible_reviewer_requires_strict_compatibility(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    reviewer = ManagedSession(
        id="reviewer-1",
        workspace_id=ws.id,
        task_id=None,
        tab_id="tab-1",
        role=WorkspaceSessionRole.REVIEWER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Reviewer",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[reviewer.id] = reviewer

    match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CODEX,
            role=WorkspaceSessionRole.REVIEWER,
            reuse_existing=True,
            cwd=str(repo),
        ),
    )
    assert match is None


def _cleanup_workspace(tmp_path: Path, manager: WorkspaceManager) -> tuple[Path, Workspace]:
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    return repo, ws


def _caller_owned_ephemeral_session(
    *,
    ws: Workspace,
    repo: Path,
    session_id: str = "ephemeral-1",
    tab_id: str = "tab-1",
    role: WorkspaceSessionRole = WorkspaceSessionRole.ORCHESTRATOR,
    runtime_status: AgentRuntimeStatus = AgentRuntimeStatus.IDLE,
    status: ManagedSessionStatus = ManagedSessionStatus.IDLE,
    task_id: str | None = None,
    current_task_id: str | None = None,
    workspace_id: str | None = None,
    now: datetime | None = None,
) -> ManagedSession:
    ts = now or datetime.now(timezone.utc)
    return ManagedSession(
        id=session_id,
        workspace_id=workspace_id or ws.id,
        task_id=task_id,
        tab_id=tab_id,
        role=role,
        agent_type=AgentType.CLAUDE,
        status=status,
        runtime_status=runtime_status,
        current_task_id=current_task_id,
        queued_count=0,
        title="Ephemeral",
        branch=None,
        workspace_path=str(repo),
        tmux_session=f"tmux-{session_id}",
        target=ExecutionTarget.LOCAL,
        ephemeral=True,
        caller_owned_ephemeral=True,
        created_at=ts,
        updated_at=ts,
    )


def _done_task_for_session(
    *,
    ws: Workspace,
    session_id: str,
    task_id: str = "task-done",
    workspace_id: str | None = None,
    now: datetime | None = None,
) -> WorkspaceTask:
    ts = now or datetime.now(timezone.utc)
    return WorkspaceTask(
        id=task_id,
        workspace_id=workspace_id or ws.id,
        title="done",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.DONE,
        session_id=session_id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=ts,
        updated_at=ts,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [WorkspaceSessionRole.ORCHESTRATOR, WorkspaceSessionRole.WORKER])
async def test_task_cleanup_deletes_caller_owned_ephemeral_session(
    tmp_path: Path, monkeypatch: MonkeyPatch, role: WorkspaceSessionRole
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo, ws = _cleanup_workspace(tmp_path, manager)
    session = _caller_owned_ephemeral_session(ws=ws, repo=repo, role=role)
    task = _done_task_for_session(ws=ws, session_id=session.id)
    manager.sessions[session.id] = session
    manager.tasks[task.id] = task
    deleted_tabs: list[str] = []

    async def fake_delete_tab(tab_id: str) -> None:
        deleted_tabs.append(tab_id)

    monkeypatch.setattr(ttyd_manager, "delete_tab", fake_delete_tab)
    result = await manager.cleanup_task_session(task.id)
    assert result.action == "deleted"
    assert result.session_id == session.id
    assert session.id not in manager.sessions
    assert deleted_tabs == [session.tab_id]


@pytest.mark.asyncio
async def test_delete_session_allows_failed_task_binding(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A timed-out subagent must not permanently consume an idle agent seat."""
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo, ws = _cleanup_workspace(tmp_path, manager)
    session = _caller_owned_ephemeral_session(ws=ws, repo=repo)
    now = datetime.now(timezone.utc)
    task = WorkspaceTask(
        id="task-failed",
        workspace_id=ws.id,
        title="timed out",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.FAILED,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.SUBAGENT,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    manager.tasks[task.id] = task
    discarded: list[tuple[str, str]] = []
    deleted_tabs: list[str] = []

    async def fake_discard(workspace_id: str, session_id: str) -> None:
        discarded.append((workspace_id, session_id))

    async def fake_delete_tab(tab_id: str) -> None:
        deleted_tabs.append(tab_id)

    import claude_hub.services.agent_stream as agent_stream

    monkeypatch.setattr(agent_stream, "discard_session_stream", fake_discard)
    monkeypatch.setattr(ttyd_manager, "delete_tab", fake_delete_tab)

    await manager.delete_session(session.id)

    assert session.id not in manager.sessions
    assert discarded == [(ws.id, session.id)]
    assert deleted_tabs == [session.tab_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        WorkspaceTaskStatus.TODO,
        WorkspaceTaskStatus.QUEUED,
        WorkspaceTaskStatus.WORKING,
        WorkspaceTaskStatus.REVIEW,
    ],
)
async def test_task_cleanup_skips_non_done_task(
    tmp_path: Path, monkeypatch: MonkeyPatch, status: WorkspaceTaskStatus
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo, ws = _cleanup_workspace(tmp_path, manager)
    session = _caller_owned_ephemeral_session(ws=ws, repo=repo)
    now = datetime.now(timezone.utc)
    task = WorkspaceTask(
        id="task-open",
        workspace_id=ws.id,
        title="open",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=status,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    manager.tasks[task.id] = task
    result = await manager.cleanup_task_session(task.id)
    assert result.action == "skipped"
    assert "must be done" in (result.reason or "")
    assert session.id in manager.sessions


@pytest.mark.asyncio
async def test_task_cleanup_skips_non_idle_runtime(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo, ws = _cleanup_workspace(tmp_path, manager)
    session = _caller_owned_ephemeral_session(
        ws=ws,
        repo=repo,
        runtime_status=AgentRuntimeStatus.WORKING,
    )
    task = _done_task_for_session(ws=ws, session_id=session.id)
    manager.sessions[session.id] = session
    manager.tasks[task.id] = task
    result = await manager.cleanup_task_session(task.id)
    assert result.action == "skipped"
    assert "runtime_status" in (result.reason or "")
    assert session.id in manager.sessions


@pytest.mark.asyncio
async def test_task_cleanup_skips_other_task_session_id_reference(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo, ws = _cleanup_workspace(tmp_path, manager)
    session = _caller_owned_ephemeral_session(ws=ws, repo=repo)
    task = _done_task_for_session(ws=ws, session_id=session.id, task_id="task-done")
    now = datetime.now(timezone.utc)
    other = WorkspaceTask(
        id="task-working",
        workspace_id=ws.id,
        title="working",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    manager.tasks[task.id] = task
    manager.tasks[other.id] = other
    result = await manager.cleanup_task_session(task.id)
    assert result.action == "skipped"
    assert "other non-terminal tasks" in (result.reason or "")
    assert session.id in manager.sessions


@pytest.mark.asyncio
async def test_task_cleanup_skips_other_task_review_session_reference(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo, ws = _cleanup_workspace(tmp_path, manager)
    session = _caller_owned_ephemeral_session(ws=ws, repo=repo)
    task = _done_task_for_session(ws=ws, session_id=session.id, task_id="task-done")
    now = datetime.now(timezone.utc)
    other = WorkspaceTask(
        id="task-review",
        workspace_id=ws.id,
        title="review",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.REVIEW,
        session_id="some-other-session",
        review_session_id=session.id,
        task_mode=WorkspaceTaskMode.REVIEWED,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    manager.tasks[task.id] = task
    manager.tasks[other.id] = other
    result = await manager.cleanup_task_session(task.id)
    assert result.action == "skipped"
    assert "other non-terminal tasks" in (result.reason or "")
    assert session.id in manager.sessions


@pytest.mark.asyncio
async def test_task_cleanup_skips_workspace_mismatch(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    ws_a = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo_a)))
    ws_b = manager.create_workspace(WorkspaceCreate(name="B", path=str(repo_b)))
    session = _caller_owned_ephemeral_session(
        ws=ws_b,
        repo=repo_b,
        workspace_id=ws_b.id,
    )
    task = _done_task_for_session(
        ws=ws_a,
        session_id=session.id,
        workspace_id=ws_a.id,
    )
    manager.sessions[session.id] = session
    manager.tasks[task.id] = task
    result = await manager.cleanup_task_session(task.id)
    assert result.action == "skipped"
    assert "different workspace" in (result.reason or "")
    assert session.id in manager.sessions


@pytest.mark.asyncio
async def test_task_cleanup_skips_active_raw_session_binding_unset_canonical(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo, ws = _cleanup_workspace(tmp_path, manager)
    now = datetime.now(timezone.utc)
    session = _caller_owned_ephemeral_session(
        ws=ws,
        repo=repo,
        task_id="other-working",
        current_task_id="other-working",
    )
    task = _done_task_for_session(ws=ws, session_id=session.id, task_id="task-done")
    other = WorkspaceTask(
        id="other-working",
        workspace_id=ws.id,
        title="binding window",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id=None,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    manager.tasks[task.id] = task
    manager.tasks[other.id] = other
    result = await manager.cleanup_task_session(task.id)
    assert result.action == "skipped"
    assert "raw task binding" in (result.reason or "")
    assert session.id in manager.sessions


@pytest.mark.asyncio
async def test_cleanup_skips_non_ephemeral(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="agent-1",
        workspace_id=ws.id,
        task_id=None,
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.SPAWNING,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Agent",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        caller_owned_ephemeral=False,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    task = WorkspaceTask(
        id="task-1",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.DONE,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    result = await manager.cleanup_task_session(task.id)
    assert result.action == "skipped"
    assert session.id in manager.sessions


@pytest.mark.asyncio
async def test_cleanup_skips_internal_ephemeral_without_caller_owned(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="internal-ephemeral-1",
        workspace_id=ws.id,
        task_id=None,
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Internal ephemeral",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        ephemeral=True,
        caller_owned_ephemeral=False,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    task = WorkspaceTask(
        id="task-1",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.DONE,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    result = await manager.cleanup_task_session(task.id)
    assert result.action == "skipped"
    assert "caller-owned" in (result.reason or "")


@pytest.mark.asyncio
async def test_caller_owned_ephemeral_requires_ephemeral_flag(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    with pytest.raises(ValueError, match="caller_owned_ephemeral requires ephemeral"):
        await manager.ensure_workspace_agent(
            ws.id,
            EnsureWorkspaceAgentRequest(
                agent_type=AgentType.CLAUDE,
                caller_owned_ephemeral=True,
                ephemeral=False,
            ),
        )


@pytest.mark.asyncio
async def test_ephemeral_without_caller_owned_provenance_on_create(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        return TerminalTab(
            id="tab-ephemeral",
            name="ephemeral",
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CLAUDE,
            port=9100,
            created_at=datetime.now(timezone.utc),
            is_active=True,
        )

    monkeypatch.setattr(ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", lambda *_a, **_k: None)
    session = await manager.ensure_workspace_agent(
        ws.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            ephemeral=True,
            caller_owned_ephemeral=False,
            reuse_existing=False,
        ),
    )
    assert session.ephemeral is True
    assert session.caller_owned_ephemeral is False


@pytest.mark.asyncio
async def test_internal_ephemeral_reviewer_not_caller_owned(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        return TerminalTab(
            id="tab-reviewer",
            name="reviewer",
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CLAUDE,
            port=9101,
            created_at=datetime.now(timezone.utc),
            is_active=True,
        )

    monkeypatch.setattr(ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", lambda *_a, **_k: None)
    session = await manager.ensure_workspace_agent(
        ws.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            title=f"{ws.name} Temporary Reviewer",
            role=WorkspaceSessionRole.REVIEWER,
            reuse_existing=False,
            cwd=str(repo),
            ephemeral=True,
            caller_owned_ephemeral=False,
        ),
    )
    assert session.role == WorkspaceSessionRole.REVIEWER
    assert session.ephemeral is True
    assert session.caller_owned_ephemeral is False


def test_session_blocks_reuse_ignores_stale_task_id_fields_only(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="agent-1",
        workspace_id=ws.id,
        task_id="done-task",
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id="done-task",
        queued_count=0,
        title="Agent",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    manager.sessions[session.id] = session
    manager.tasks["done-task"] = WorkspaceTask(
        id="done-task",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.DONE,
        session_id="other-agent",
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    assert manager._session_blocks_agent_reuse(session) is False


@pytest.mark.asyncio
async def test_create_workspace_stores_normalized_remote_cwd(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from importlib import import_module

    workspace_module = import_module("claude_hub.services.workspace_manager")
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(
        workspace_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: object() if profile_id == "profile-a" else None,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = manager.create_workspace(
        WorkspaceCreate(
            name="Remote",
            path=str(repo),
            target=ExecutionTarget.REMOTE,
            remote_profile_id="profile-a",
            remote_cwd="~/evals/../ff45d//",
            session_prefix="rem",
        )
    )
    assert workspace.remote_cwd == "~/ff45d"


@pytest.mark.asyncio
async def test_ensure_workspace_agent_omitted_target_on_remote_workspace_creates_local(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from importlib import import_module

    workspace_module = import_module("claude_hub.services.workspace_manager")
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", lambda *_a, **_k: None)
    monkeypatch.setattr(
        workspace_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: object() if profile_id == "profile-a" else None,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(
        WorkspaceCreate(
            name="Remote",
            path=str(repo),
            target=ExecutionTarget.REMOTE,
            remote_profile_id="profile-a",
            remote_cwd="~/ff45d",
        )
    )
    captured: list[dict[str, object]] = []

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        captured.append(dict(kwargs))
        return TerminalTab(
            id="tab-local-default",
            name="local",
            cwd=str(repo.resolve()),
            solo_mode=True,
            agent_type=AgentType.CLAUDE,
            port=9100,
            created_at=datetime.now(timezone.utc),
            is_active=True,
        )

    monkeypatch.setattr(ttyd_manager, "create_tab", fake_create_tab)
    session = await manager.ensure_workspace_agent(
        ws.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            reuse_existing=False,
        ),
    )
    assert session.target == ExecutionTarget.LOCAL
    assert session.remote_profile_id is None
    assert session.workspace_path == str(repo.resolve())
    assert captured[0]["target"] == ExecutionTarget.LOCAL
    assert captured[0]["cwd"] == str(repo.resolve())


@pytest.mark.asyncio
async def test_ensure_workspace_agent_explicit_remote_target_creates_remote(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from importlib import import_module

    workspace_module = import_module("claude_hub.services.workspace_manager")
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", lambda *_a, **_k: None)
    monkeypatch.setattr(
        workspace_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: object() if profile_id == "profile-a" else None,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(
        WorkspaceCreate(
            name="Remote",
            path=str(repo),
            target=ExecutionTarget.REMOTE,
            remote_profile_id="profile-a",
            remote_cwd="~/evals/../ff45d//",
        )
    )
    captured: list[dict[str, object]] = []

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        captured.append(dict(kwargs))
        return TerminalTab(
            id="tab-remote",
            name="remote",
            cwd=None,
            solo_mode=True,
            agent_type=AgentType.CLAUDE,
            port=9101,
            created_at=datetime.now(timezone.utc),
            is_active=True,
        )

    monkeypatch.setattr(ttyd_manager, "create_tab", fake_create_tab)
    session = await manager.ensure_workspace_agent(
        ws.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            target=ExecutionTarget.REMOTE,
            reuse_existing=False,
        ),
    )
    assert session.target == ExecutionTarget.REMOTE
    assert session.remote_profile_id == "profile-a"
    assert session.remote_cwd == "~/ff45d"
    assert captured[0]["target"] == ExecutionTarget.REMOTE
    assert captured[0]["remote_profile_id"] == "profile-a"
    assert captured[0]["remote_cwd"] == "~/ff45d"


def test_find_compatible_on_remote_workspace_omitted_target_matches_local_only(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from importlib import import_module

    workspace_module = import_module("claude_hub.services.workspace_manager")
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(
        workspace_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: object() if profile_id == "profile-a" else None,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(
        WorkspaceCreate(
            name="Remote",
            path=str(repo),
            target=ExecutionTarget.REMOTE,
            remote_profile_id="profile-a",
            remote_cwd="~/ff45d",
        )
    )
    now = datetime.now(timezone.utc)
    local_agent = ManagedSession(
        id="local-agent",
        workspace_id=ws.id,
        task_id=None,
        tab_id="tab-local",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Local",
        branch=None,
        workspace_path=str(repo.resolve()),
        tmux_session="tmux-local",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    remote_agent = local_agent.model_copy(
        update={
            "id": "remote-agent",
            "tab_id": "tab-remote",
            "tmux_session": "tmux-remote",
            "target": ExecutionTarget.REMOTE,
            "remote_profile_id": "profile-a",
            "remote_cwd": "~/ff45d",
            "workspace_path": "~/ff45d",
        }
    )
    manager.sessions[local_agent.id] = local_agent
    manager.sessions[remote_agent.id] = remote_agent

    match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(agent_type=AgentType.CLAUDE, reuse_existing=True),
    )
    assert match is not None
    assert match.id == "local-agent"


def test_reconcile_workspace_session_pointers_clears_stale_ids(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    manager.workspaces[ws.id] = ws.model_copy(
        update={
            "dispatcher_session_id": "missing-dispatcher",
            "resident_agent_session_id": "missing-resident",
        }
    )
    manager._reconcile_workspace_session_pointers(ws.id)
    updated = manager.workspaces[ws.id]
    assert updated.dispatcher_session_id is None
    assert updated.resident_agent_session_id is None


@pytest.mark.asyncio
async def test_find_compatible_ignores_stale_task_id_when_task_done(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="agent-1",
        workspace_id=ws.id,
        task_id="done-task",
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.SPAWNING,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Agent",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    manager.sessions[session.id] = session
    manager.tasks["done-task"] = WorkspaceTask(
        id="done-task",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.DONE,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(agent_type=AgentType.CLAUDE, cwd=str(repo)),
    )
    assert match is not None
    assert match.id == "agent-1"
    cleared = manager.sessions["agent-1"]
    assert cleared.task_id is None
    assert cleared.current_task_id is None


@pytest.mark.asyncio
async def test_find_compatible_blocked_by_active_canonical_ownership(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="agent-1",
        workspace_id=ws.id,
        task_id="active-task",
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id="active-task",
        queued_count=0,
        title="Agent",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    manager.sessions[session.id] = session
    manager.tasks["active-task"] = WorkspaceTask(
        id="active-task",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(agent_type=AgentType.CLAUDE, cwd=str(repo)),
    )
    assert match is None


@pytest.mark.asyncio
async def test_find_compatible_reuses_after_canonical_session_id_mismatch_orphan(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    saves: list[None] = []

    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: saves.append(None))
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    canonical_owner = ManagedSession(
        id="agent-2",
        workspace_id=ws.id,
        task_id="active-task",
        tab_id="tab-2",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id="active-task",
        queued_count=0,
        title="Owner",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-2",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    orphaned = ManagedSession(
        id="agent-1",
        workspace_id=ws.id,
        task_id="active-task",
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id="active-task",
        queued_count=0,
        title="Stale",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    manager.sessions[canonical_owner.id] = canonical_owner
    manager.sessions[orphaned.id] = orphaned
    manager.tasks["active-task"] = WorkspaceTask(
        id="active-task",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id=canonical_owner.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(agent_type=AgentType.CLAUDE, cwd=str(repo)),
    )
    assert match is not None
    assert match.id == "agent-1"
    assert saves, "orphaned assignment should reconcile under save"
    cleared = manager.sessions["agent-1"]
    assert cleared.task_id is None
    assert cleared.current_task_id is None


@pytest.mark.asyncio
async def test_find_compatible_clears_terminal_task_orphan_under_save(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    saves: list[None] = []

    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: saves.append(None))
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="agent-1",
        workspace_id=ws.id,
        task_id="done-task",
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id="done-task",
        queued_count=0,
        title="Agent",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    manager.sessions[session.id] = session
    manager.tasks["done-task"] = WorkspaceTask(
        id="done-task",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.DONE,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(agent_type=AgentType.CLAUDE, cwd=str(repo)),
    )
    assert match is not None
    assert match.id == "agent-1"
    assert saves, "terminal-task orphan should reconcile under save"
    cleared = manager.sessions["agent-1"]
    assert cleared.task_id is None
    assert cleared.current_task_id is None


@pytest.mark.asyncio
async def test_ensure_workspace_agent_reconciles_stale_binding_under_lock(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    saves: list[None] = []

    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: saves.append(None))
    monkeypatch.setattr(manager, "_sync_session_tab_metadata", lambda _s: None)
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", lambda *_a, **_k: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="agent-1",
        workspace_id=ws.id,
        task_id="done-task",
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id="done-task",
        queued_count=0,
        title="Agent",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    manager.sessions[session.id] = session
    manager.tasks["done-task"] = WorkspaceTask(
        id="done-task",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.DONE,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )

    reused = await manager.ensure_workspace_agent(
        ws.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            cwd=str(repo),
            reuse_existing=True,
            solo_mode=True,
        ),
    )
    assert reused.id == "agent-1"
    assert saves, "ensure reuse should reconcile stale bindings under lock"
    cleared = manager.sessions["agent-1"]
    assert cleared.task_id is None
    assert cleared.current_task_id is None


@pytest.mark.asyncio
async def test_concurrent_reuse_existing_is_best_effort_and_creates_distinct_sessions(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", lambda *_a, **_k: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    create_calls = 0
    gate = asyncio.Event()

    async def delayed_create_tab(**kwargs: object) -> TerminalTab:
        nonlocal create_calls
        create_calls += 1
        call_no = create_calls
        gate.set()
        await asyncio.sleep(0.05)
        return TerminalTab(
            id=f"tab-{call_no}",
            name="agent",
            cwd=str(kwargs.get("cwd")),
            solo_mode=True,
            agent_type=AgentType.CLAUDE,
            port=9100 + call_no,
            created_at=datetime.now(timezone.utc),
            is_active=True,
        )

    monkeypatch.setattr(ttyd_manager, "create_tab", delayed_create_tab)
    payload = EnsureWorkspaceAgentRequest(
        agent_type=AgentType.CLAUDE,
        cwd=str(repo.resolve()),
        reuse_existing=True,
        solo_mode=True,
    )

    first = asyncio.create_task(manager.ensure_workspace_agent(ws.id, payload))
    await gate.wait()
    second = asyncio.create_task(manager.ensure_workspace_agent(ws.id, payload))
    session_a, session_b = await asyncio.gather(first, second)

    assert session_a.id != session_b.id
    assert session_a.tab_id != session_b.tab_id
    assert create_calls == 2
    assert len([s for s in manager.sessions.values() if s.workspace_id == ws.id]) == 2


@pytest.mark.asyncio
async def test_reuse_existing_failed_create_tab_leaves_no_phantom_session(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", lambda *_a, **_k: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))

    async def failing_create_tab(**kwargs: object) -> TerminalTab:
        raise RuntimeError("create_tab failed")

    monkeypatch.setattr(ttyd_manager, "create_tab", failing_create_tab)

    before_ids = {sid for sid, session in manager.sessions.items() if session.workspace_id == ws.id}
    with pytest.raises(RuntimeError, match="create_tab failed"):
        await manager.ensure_workspace_agent(
            ws.id,
            EnsureWorkspaceAgentRequest(
                agent_type=AgentType.CLAUDE,
                cwd=str(repo.resolve()),
                reuse_existing=True,
            ),
        )
    after_ids = {sid for sid, session in manager.sessions.items() if session.workspace_id == ws.id}
    assert before_ids == after_ids


def test_cleanup_stale_orchestrator_assignments_preserves_active_owner(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="agent-1",
        workspace_id=ws.id,
        task_id="active-task",
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id="active-task",
        queued_count=0,
        title="Agent",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    manager.sessions[session.id] = session
    manager.tasks["active-task"] = WorkspaceTask(
        id="active-task",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    changed = manager._cleanup_stale_orchestrator_assignments(ws.id)
    assert changed is False
    assert manager.sessions["agent-1"].current_task_id == "active-task"


def test_cleanup_preserves_working_task_with_unset_canonical_session_id(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Dispatch window: WORKING task with session_id=None must not clear bindings."""
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="agent-1",
        workspace_id=ws.id,
        task_id="t1",
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id="t1",
        queued_count=0,
        title="Agent",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    manager.sessions[session.id] = session
    manager.tasks["t1"] = WorkspaceTask(
        id="t1",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id=None,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    changed = manager._cleanup_stale_orchestrator_assignments(ws.id)
    assert changed is False
    assert manager.sessions["agent-1"].task_id == "t1"
    assert manager.sessions["agent-1"].current_task_id == "t1"


def test_cleanup_preserves_working_task_with_missing_or_stopped_canonical_session(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    stale_holder = ManagedSession(
        id="agent-1",
        workspace_id=ws.id,
        task_id="t1",
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id="t1",
        queued_count=0,
        title="Stale holder",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    stopped_owner = ManagedSession(
        id="stopped-owner",
        workspace_id=ws.id,
        task_id=None,
        tab_id="tab-2",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.STOPPED,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Stopped",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-2",
        target=ExecutionTarget.LOCAL,
        created_at=now,
        updated_at=now,
    )
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    manager.sessions[stopped_owner.id] = stopped_owner
    manager.tasks["missing-canonical"] = WorkspaceTask(
        id="missing-canonical",
        workspace_id=ws.id,
        title="missing owner",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id="gone-agent",
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    manager.tasks["stopped-canonical"] = WorkspaceTask(
        id="stopped-canonical",
        workspace_id=ws.id,
        title="stopped owner",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.WORKING,
        session_id=stopped_owner.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )

    missing_case = stale_holder.model_copy(
        update={"task_id": "missing-canonical", "current_task_id": "missing-canonical"}
    )
    manager.sessions["agent-missing"] = missing_case
    stopped_case = stale_holder.model_copy(
        update={
            "id": "agent-stopped",
            "tab_id": "tab-3",
            "tmux_session": "tmux-3",
            "task_id": "stopped-canonical",
            "current_task_id": "stopped-canonical",
        }
    )
    manager.sessions["agent-stopped"] = stopped_case

    assert manager._cleanup_stale_orchestrator_assignments(ws.id) is False
    assert manager.sessions["agent-missing"].task_id == "missing-canonical"
    assert manager.sessions["agent-stopped"].task_id == "stopped-canonical"


@pytest.mark.asyncio
async def test_cleanup_rejects_non_orchestrator_role(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="ephemeral-reviewer",
        workspace_id=ws.id,
        task_id=None,
        tab_id="tab-1",
        role=WorkspaceSessionRole.REVIEWER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.SPAWNING,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Reviewer",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        ephemeral=True,
        caller_owned_ephemeral=True,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    task = WorkspaceTask(
        id="task-1",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.DONE,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    result = await manager.cleanup_task_session(task.id)
    assert session.caller_owned_ephemeral is True
    assert result.action == "skipped"
    assert "reviewer" in (result.reason or "")
    assert "caller-owned" not in (result.reason or "").lower()


@pytest.mark.asyncio
async def test_cleanup_rejects_resident_role_even_if_caller_owned(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="resident-role-1",
        workspace_id=ws.id,
        task_id=None,
        tab_id="tab-1",
        role=WorkspaceSessionRole.RESIDENT,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Resident role",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        ephemeral=True,
        caller_owned_ephemeral=True,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    task = WorkspaceTask(
        id="task-1",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.DONE,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    result = await manager.cleanup_task_session(task.id)
    assert session.caller_owned_ephemeral is True
    assert result.action == "skipped"
    assert "resident" in (result.reason or "")
    assert "caller-owned" not in (result.reason or "").lower()


@pytest.mark.asyncio
async def test_cleanup_rejects_resident_session_even_if_caller_owned(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="resident-1",
        workspace_id=ws.id,
        task_id=None,
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Resident",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        ephemeral=True,
        caller_owned_ephemeral=True,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    manager.workspaces[ws.id] = ws.model_copy(update={"resident_agent_session_id": session.id})
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    task = WorkspaceTask(
        id="task-1",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.DONE,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    result = await manager.cleanup_task_session(task.id)
    assert result.action == "skipped"
    assert "resident" in (result.reason or "")


@pytest.mark.asyncio
async def test_cleanup_rejects_internal_task(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="agent-1",
        workspace_id=ws.id,
        task_id="task-1",
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        current_task_id=None,
        queued_count=0,
        title="Agent",
        branch=None,
        workspace_path=str(repo),
        tmux_session="tmux-1",
        target=ExecutionTarget.LOCAL,
        ephemeral=True,
        caller_owned_ephemeral=True,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session
    from claude_hub.models import WorkspaceTask, WorkspaceTaskMode

    task = WorkspaceTask(
        id="task-1",
        workspace_id=ws.id,
        title="t",
        prompt="p",
        agent_type=AgentType.CLAUDE,
        status=WorkspaceTaskStatus.DONE,
        session_id=session.id,
        task_mode=WorkspaceTaskMode.DIRECT,
        system_internal=True,
        internal_kind="feedback_reaper",
        created_at=now,
        updated_at=now,
    )
    manager.tasks[task.id] = task
    result = await manager.cleanup_task_session(task.id)
    assert result.action == "skipped"
    assert "internal" in (result.reason or "")


DAY1_RESPONSE_SENTINEL = "SENTINEL_DAY1_RESPONSE_PROBE_XYZ789"


def test_redact_managed_session_env() -> None:
    from claude_hub.models import PUBLIC_REDACTED_ENV_VALUE, redact_managed_session_env

    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="s1",
        workspace_id="ws",
        tab_id="t1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.SPAWNING,
        title="Agent",
        workspace_path="/repo",
        tmux_session="tmux",
        env={"SECRET": "token"},
        created_at=now,
        updated_at=now,
    )
    redacted = redact_managed_session_env(session)
    assert redacted.env == {"SECRET": PUBLIC_REDACTED_ENV_VALUE}
    assert session.env == {"SECRET": "token"}


def test_managed_session_public_never_exposes_literal_env() -> None:
    from claude_hub.models import PUBLIC_REDACTED_ENV_VALUE, ManagedSessionPublic

    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="s1",
        workspace_id="ws",
        tab_id="t1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.SPAWNING,
        title="Agent",
        workspace_path="/repo",
        tmux_session="tmux",
        env={"ANTHROPIC_AUTH_TOKEN": DAY1_RESPONSE_SENTINEL},
        created_at=now,
        updated_at=now,
    )
    public = ManagedSessionPublic.from_managed_session(session)
    assert DAY1_RESPONSE_SENTINEL not in public.model_dump_json()
    assert public.env == {"ANTHROPIC_AUTH_TOKEN": PUBLIC_REDACTED_ENV_VALUE}
    leaked = ManagedSessionPublic.model_validate(
        {**session.model_dump(mode="json"), "env": {"ANTHROPIC_AUTH_TOKEN": DAY1_RESPONSE_SENTINEL}}
    )
    assert leaked.env == {"ANTHROPIC_AUTH_TOKEN": PUBLIC_REDACTED_ENV_VALUE}


def test_agent_create_redacts_env_in_text_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "s1", "env": {"ANTHROPIC_AUTH_TOKEN": "secret"}},
        )

    patch_get_client(monkeypatch, handler)
    runner = CliRunner()
    result = runner.invoke(cli, ["agent", "create", "ws1", "--agent-type", "claude"])
    assert result.exit_code == 0, result.output
    assert "secret" not in result.output
    assert "[redacted]" in result.output


@pytest.mark.asyncio
async def test_agent_create_env_preset_redacts_api_and_cli_json(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    import claude_hub.services.env_preset_resolver as resolver_module
    from claude_hub.auth.dependencies import get_current_user
    from claude_hub.main import app
    from claude_hub.models import PUBLIC_REDACTED_ENV_VALUE, User, WorkspaceCreate
    from claude_hub.services.env_presets import EnvPresetManager

    preset_manager = EnvPresetManager(path=tmp_path / "env_presets.json")
    preset_manager.create_preset(
        name="day1",
        text=(
            f"export ANTHROPIC_AUTH_TOKEN={DAY1_RESPONSE_SENTINEL}\n"
            "export ANTHROPIC_MODEL=doubao-seed-2.0-code"
        ),
        preset_id="day1-test",
    )
    monkeypatch.setattr(resolver_module, "env_preset_manager", preset_manager)
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)

    repo = tmp_path / "repo"
    repo.mkdir()
    ws = workspace_manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        assert env.get("ANTHROPIC_AUTH_TOKEN") == DAY1_RESPONSE_SENTINEL
        return TerminalTab(
            id="tab-day1",
            name="day1-agent",
            cwd=str(repo),
            solo_mode=True,
            agent_type=AgentType.CLAUDE,
            port=9101,
            created_at=now,
            is_active=True,
        )

    monkeypatch.setattr(ttyd_manager, "create_tab", fake_create_tab)
    monkeypatch.setattr(
        workspace_manager, "_build_session_bootstrap_prompt", lambda *_a, **_k: None
    )

    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="u1",
        name="tester",
        email="tester@example.com",
    )
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/workspaces/{ws.id}/agent",
            json={
                "agent_type": AgentType.CLAUDE.value,
                "env_preset": "day1",
                "reuse_existing": False,
            },
        )
        assert response.status_code == 201, response.text
        assert DAY1_RESPONSE_SENTINEL not in response.text
        payload = response.json()
        assert payload["env"]["ANTHROPIC_AUTH_TOKEN"] == PUBLIC_REDACTED_ENV_VALUE
        internal = workspace_manager.sessions[payload["id"]]
        assert internal.env["ANTHROPIC_AUTH_TOKEN"] == DAY1_RESPONSE_SENTINEL

        def handler(request: httpx.Request) -> httpx.Response:
            api_response = client.request(
                request.method,
                request.url.path,
                content=request.content,
                headers={
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() not in {"host", "content-length"}
                },
            )
            if api_response.headers.get("content-type", "").startswith("application/json"):
                return httpx.Response(
                    status_code=api_response.status_code,
                    json=api_response.json(),
                )
            return httpx.Response(
                status_code=api_response.status_code,
                content=api_response.content,
            )

        patch_get_client(monkeypatch, handler)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--json",
                "agent",
                "create",
                ws.id,
                "--env-preset",
                "day1",
                "--no-reuse-existing",
            ],
        )
        assert result.exit_code == 0, result.output
        assert DAY1_RESPONSE_SENTINEL not in result.output
        cli_payload = json.loads(result.output)
        assert cli_payload["env"]["ANTHROPIC_AUTH_TOKEN"] == PUBLIC_REDACTED_ENV_VALUE

        result_text = runner.invoke(
            cli,
            [
                "agent",
                "create",
                ws.id,
                "--env-preset",
                "day1",
                "--no-reuse-existing",
            ],
        )
        assert result_text.exit_code == 0, result_text.output
        assert DAY1_RESPONSE_SENTINEL not in result_text.output
        assert PUBLIC_REDACTED_ENV_VALUE in result_text.output
    finally:
        app.dependency_overrides.clear()


def test_legacy_session_normalizes_caller_owned_ephemeral() -> None:
    manager = WorkspaceManager()
    normalized = manager._normalize_session_item({"id": "s", "tab_id": "t"})
    assert normalized.get("caller_owned_ephemeral") is False
    assert normalized.get("session_kind") == "terminal"

    retired_chat = manager._normalize_session_item(
        {
            "id": "chat-s",
            "tab_id": "chat-t",
            "session_kind": "agent",
            "chat_mode": "plan",
        }
    )
    assert retired_chat.get("session_kind") == "terminal"
    assert retired_chat.get("chat_mode") == "default"

    accidental_chat = manager._normalize_session_item(
        {
            "id": "accidental-chat-s",
            "tab_id": "accidental-chat-t",
            "session_kind": "chat",
            "chat_mode": "plan",
        }
    )
    assert accidental_chat.get("session_kind") == "terminal"
    assert accidental_chat.get("chat_mode") == "default"


def test_find_compatible_same_env_reuses(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    env = {"ANTHROPIC_BASE_URL": "https://example.test", "FOO": "bar"}
    session = ManagedSession(
        id="agent-env",
        workspace_id=ws.id,
        tab_id="tab-env",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.SPAWNING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="Agent",
        workspace_path=str(repo),
        tmux_session="tmux-env",
        target=ExecutionTarget.LOCAL,
        env=env,
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session

    match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            reuse_existing=True,
            env=dict(env),
        ),
    )
    assert match is not None
    assert match.id == session.id


def test_find_compatible_different_env_no_reuse(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="agent-env",
        workspace_id=ws.id,
        tab_id="tab-env",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.SPAWNING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="Agent",
        workspace_path=str(repo),
        tmux_session="tmux-env",
        target=ExecutionTarget.LOCAL,
        env={"FOO": "one"},
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session

    match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            reuse_existing=True,
            env={"FOO": "two"},
        ),
    )
    assert match is None


def test_find_compatible_empty_env_vs_nonempty_no_reuse(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="agent-plain",
        workspace_id=ws.id,
        tab_id="tab-plain",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.SPAWNING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="Agent",
        workspace_path=str(repo),
        tmux_session="tmux-plain",
        target=ExecutionTarget.LOCAL,
        env={},
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session

    no_match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            reuse_existing=True,
            env={"HTTP_PROXY": "http://127.0.0.1:7890"},
        ),
    )
    assert no_match is None

    session_with_env = session.model_copy(update={"env": {"HTTP_PROXY": "http://127.0.0.1:7890"}})
    manager.sessions[session_with_env.id] = session_with_env
    match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            reuse_existing=True,
            env={"HTTP_PROXY": "http://127.0.0.1:7890"},
        ),
    )
    assert match is not None


@pytest.mark.asyncio
async def test_env_preset_merged_before_reuse_match(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    import claude_hub.services.env_preset_resolver as resolver_module
    from claude_hub.services.env_presets import EnvPresetManager

    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    preset_manager = EnvPresetManager(path=tmp_path / "env_presets.json")
    preset_manager.create_preset(
        name="proxy",
        text="HTTP_PROXY=http://127.0.0.1:7890",
        preset_id="proxy",
    )
    monkeypatch.setattr(resolver_module, "env_preset_manager", preset_manager)

    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    session = ManagedSession(
        id="agent-preset",
        workspace_id=ws.id,
        tab_id="tab-preset",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.SPAWNING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="Agent",
        workspace_path=str(repo),
        tmux_session="tmux-preset",
        target=ExecutionTarget.LOCAL,
        env={"HTTP_PROXY": "http://127.0.0.1:7890", "FOO": "override"},
        created_at=now,
        updated_at=now,
    )
    manager.sessions[session.id] = session

    async def fake_create_tab(**kwargs: Any) -> TerminalTab:
        return TerminalTab(id="new-tab", name="Agent", port=9000)

    monkeypatch.setattr(ttyd_manager, "create_tab", fake_create_tab)

    reused = await manager.ensure_workspace_agent(
        ws.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CLAUDE,
            reuse_existing=True,
            env_preset="proxy",
            env={"FOO": "override"},
        ),
    )
    assert reused.id == session.id


def test_cli_workspace_update_resolves_relative_path_in_request_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "ws1", "name": "A", "path": str(repo.resolve())})

    patch_get_client(monkeypatch, handler)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["workspace", "update", "ws1", "--path", "./repo"])
    assert result.exit_code == 0, result.output
    assert bodies[0]["path"] == str(repo.resolve())
    assert Path(bodies[0]["path"]).is_absolute()


def test_update_workspace_rejects_relative_path(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from claude_hub.services.workspace_identity import WorkspaceIdentityError

    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    original_path = ws.path

    with pytest.raises(WorkspaceIdentityError, match="absolute local path"):
        manager.update_workspace(ws.id, WorkspaceUpdate(path="relative/repo"))

    assert manager.workspaces[ws.id].path == original_path


def test_update_workspace_canonicalizes_worktree_to_primary(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo, wt = _init_git_repo_with_worktree(tmp_path)
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))

    updated = manager.update_workspace(ws.id, WorkspaceUpdate(path=str(wt)))
    assert updated.path == str(repo.resolve())
    assert manager.workspaces[ws.id].path == str(repo.resolve())


def test_update_workspace_rejects_identity_collision(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _init_git_repo(repo_a)
    _init_git_repo(repo_b)
    ws_a = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo_a)))
    ws_b = manager.create_workspace(WorkspaceCreate(name="B", path=str(repo_b)))
    original_b_path = ws_b.path
    original_b_name = ws_b.name

    with pytest.raises(DuplicateWorkspaceError):
        manager.update_workspace(ws_b.id, WorkspaceUpdate(path=str(repo_a)))

    assert manager.workspaces[ws_b.id].path == original_b_path
    assert manager.workspaces[ws_a.id].path == str(repo_a.resolve())


def test_update_workspace_failed_path_does_not_mutate_name(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from claude_hub.services.workspace_identity import WorkspaceIdentityError

    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="Original", path=str(repo)))

    with pytest.raises(WorkspaceIdentityError):
        manager.update_workspace(
            ws.id,
            WorkspaceUpdate(name="Renamed", path="relative/repo"),
        )

    unchanged = manager.workspaces[ws.id]
    assert unchanged.name == "Original"
    assert unchanged.path == ws.path


def test_update_workspace_combined_path_remote_cwd_uses_effective_identity(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Collision must use post-patch path+remote_cwd, not stale remote_cwd alone."""
    from importlib import import_module

    workspace_module = import_module("claude_hub.services.workspace_manager")
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(
        workspace_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: object() if profile_id == "profile-a" else None,
    )
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    ws_a = manager.create_workspace(
        WorkspaceCreate(
            name="Remote A",
            path=str(repo_a),
            target=ExecutionTarget.REMOTE,
            remote_profile_id="profile-a",
            remote_cwd="~/target",
        )
    )
    ws_b = manager.create_workspace(
        WorkspaceCreate(
            name="Remote B",
            path=str(repo_b),
            target=ExecutionTarget.REMOTE,
            remote_profile_id="profile-a",
            remote_cwd="~/other",
        )
    )
    original_b_path = ws_b.path
    original_b_remote_cwd = ws_b.remote_cwd

    with pytest.raises(DuplicateWorkspaceError):
        manager.update_workspace(
            ws_b.id,
            WorkspaceUpdate(path=str(repo_a), remote_cwd="~/target"),
        )
    unchanged = manager.workspaces[ws_b.id]
    assert unchanged.path == original_b_path
    assert unchanged.remote_cwd == original_b_remote_cwd
    assert manager.workspaces[ws_a.id].path == str(repo_a.resolve())
    assert manager.workspaces[ws_a.id].remote_cwd == "~/target"


def test_update_workspace_remote_cwd_only_collision_fails_closed(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from importlib import import_module

    workspace_module = import_module("claude_hub.services.workspace_manager")
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(
        workspace_module.remote_profile_manager,
        "get_profile",
        lambda profile_id: object() if profile_id == "profile-a" else None,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    ws_a = manager.create_workspace(
        WorkspaceCreate(
            name="Remote A",
            path=str(repo),
            target=ExecutionTarget.REMOTE,
            remote_profile_id="profile-a",
            remote_cwd="~/target",
        )
    )
    ws_b = manager.create_workspace(
        WorkspaceCreate(
            name="Remote B",
            path=str(repo),
            target=ExecutionTarget.REMOTE,
            remote_profile_id="profile-a",
            remote_cwd="~/other",
        )
    )

    with pytest.raises(DuplicateWorkspaceError):
        manager.update_workspace(ws_b.id, WorkspaceUpdate(remote_cwd="~/target"))

    assert manager.workspaces[ws_b.id].remote_cwd == "~/other"
    assert manager.workspaces[ws_a.id].remote_cwd == "~/target"


def test_find_compatible_internal_dispatcher_empty_env_reuses(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    now = datetime.now(timezone.utc)
    dispatcher = ManagedSession(
        id=f"{ws.session_prefix}-dispatcher",
        workspace_id=ws.id,
        tab_id="tab-disp",
        role=WorkspaceSessionRole.DISPATCHER,
        agent_type=AgentType.CODEX,
        status=ManagedSessionStatus.SPAWNING,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="Dispatcher",
        workspace_path=str(repo),
        tmux_session="tmux-disp",
        target=ExecutionTarget.LOCAL,
        env={},
        created_at=now,
        updated_at=now,
    )
    manager.sessions[dispatcher.id] = dispatcher

    match = manager._find_compatible_workspace_agent(
        ws,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.CODEX,
            role=WorkspaceSessionRole.DISPATCHER,
            reuse_existing=True,
        ),
    )
    assert match is not None
    assert match.id == dispatcher.id


@pytest.mark.asyncio
async def test_api_update_workspace_rejects_relative_path(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from claude_hub.auth.dependencies import get_current_user
    from claude_hub.main import app
    from claude_hub.models import User

    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = workspace_manager.create_workspace(WorkspaceCreate(name="A", path=str(repo)))
    original_path = ws.path

    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="u1", name="tester", email="tester@example.com"
    )
    try:
        client = TestClient(app)
        response = client.patch(
            f"/api/workspaces/{ws.id}",
            json={"path": "relative/repo"},
        )
        assert response.status_code == 400
        assert "absolute local path" in response.json()["detail"]
        assert workspace_manager.workspaces[ws.id].path == original_path
    finally:
        app.dependency_overrides.clear()
