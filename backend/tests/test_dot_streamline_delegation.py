"""Track 3: Dot's lightweight default delegation lane and the contract receipt.

The fast lane means local code work defaults to a caller-owned ephemeral Chat
session with an isolated Git worktree (never auto-pushed), read-only briefs
default to triage/subagent mode, every delegation carries an APPROVED Goal
Packet, and the caller gets a four-line contract receipt. The blocking Goal
Packet ceremony and the mandatory per-task ``dot review`` wake step are gone.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Generator

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    ExecutionTarget,
    GoalPacketStatus,
    SessionKind,
    Workspace,
    WorkspaceTaskMode,
    WorkspaceTaskRecipe,
)
from claude_hub.services import work_pipeline, workspace_manager
from claude_hub.services.dot import DotService, DotStore, reset_dot_service
from claude_hub.services.dot.prompts import BRIEF_TEMPLATE, TURN_RULES, WAKE_MESSAGE

GOOD: dict[str, Any] = {
    "done_when": ["notes/fa3.md has 5 bullets with numbers", "each bullet cites a section"],
    "output": "notes/fa3.md in the repo",
    "context": "FA3 paper: arxiv 2407.08608",
}


@pytest.fixture()
def service(tmp_path: Path) -> Generator[DotService, None, None]:
    store = DotStore(tmp_path / "dot")
    store.ensure()
    store.update_state(tab_id="tab-1")
    dot = DotService(store)
    reset_dot_service(dot)
    yield dot
    reset_dot_service(None)


def _workspace(path: Path, target: ExecutionTarget = ExecutionTarget.LOCAL) -> Workspace:
    now = datetime.now(timezone.utc)
    return Workspace(
        id="ws-1",
        name="Example",
        path=str(path),
        default_branch="main",
        session_prefix="example",
        target=target,
        created_at=now,
        updated_at=now,
    )


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    return repo


class _HubFakes:
    def __init__(self, monkeypatch: MonkeyPatch, workspace: Workspace, tmp_path: Path) -> None:
        self.payloads: list[Any] = []
        self.ensure_requests: list[Any] = []
        self.start_requests: list[Any] = []
        self.provisioned: list[tuple[str, str]] = []
        self.tasks: dict[str, SimpleNamespace] = {}

        async def fake_register(ws: str, payload: Any, actor_key: str) -> tuple[Any, bool]:
            self.payloads.append(payload)
            task = SimpleNamespace(
                id=f"task-{len(self.payloads)}",
                title=payload.title,
                workspace_id=ws,
                status=SimpleNamespace(value="todo"),
                session_id=None,
                auto_worktree=bool(payload.auto_worktree),
                worktree_path=None,
                worktree_branch=None,
            )
            self.tasks[task.id] = task
            return task, False

        async def fake_provision(ws: Workspace, task: Any) -> tuple[str, str]:
            self.provisioned.append((ws.id, task.id))
            wt_path = tmp_path / "worktrees" / task.id
            wt_path.mkdir(parents=True, exist_ok=True)
            return str(wt_path), "feat/task"

        async def fake_ensure(ws: str, request: Any) -> Any:
            self.ensure_requests.append(request)
            return SimpleNamespace(id=f"eph-{len(self.ensure_requests)}")

        async def fake_start(task_id: str, request: Any) -> Any:
            self.start_requests.append(request)
            task = self.tasks[task_id]
            task.session_id = request.target_session_id
            task.status = SimpleNamespace(value="queued")
            return task

        monkeypatch.setattr(workspace_manager, "workspaces", {workspace.id: workspace})
        monkeypatch.setattr(workspace_manager, "tasks", self.tasks)
        monkeypatch.setattr(workspace_manager, "sessions", {})
        monkeypatch.setattr(workspace_manager, "register_task", fake_register)
        monkeypatch.setattr(workspace_manager, "ensure_workspace_agent", fake_ensure)
        monkeypatch.setattr(workspace_manager, "start_task", fake_start)
        # work.py imports these lazily inside delegate(); patch the source module.
        monkeypatch.setattr(work_pipeline, "provision_task_worktree", fake_provision)

    @property
    def payload(self) -> Any:
        return self.payloads[-1]


async def _delegate(service: DotService, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "workspace_id": "ws-1",
        "title": "Paper",
        "goal": "Summarize FA3",
        **GOOD,
        "actor_key": "actor",
    }
    params.update(overrides)
    return await service.delegate(**params)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", ["codex", "claude"])
async def test_local_chat_agents_default_to_ephemeral_chat_and_isolated_worktree(
    service: DotService, monkeypatch: MonkeyPatch, tmp_path: Path, agent_type: str
) -> None:
    repo = _git_repo(tmp_path)
    hub = _HubFakes(monkeypatch, _workspace(repo), tmp_path)

    result = await _delegate(service, agent_type=agent_type, why="fast local lane")

    payload = hub.payload
    assert payload.session_kind == SessionKind.CHAT
    assert payload.auto_worktree is True and payload.auto_push_remote is False
    packet = payload.goal_packet
    assert packet.status == GoalPacketStatus.APPROVED
    assert packet.objective == "Summarize FA3"
    assert packet.acceptance_criteria == GOOD["done_when"]
    assert packet.out_of_scope == []

    assert len(hub.ensure_requests) == 1
    request = hub.ensure_requests[0]
    assert request.session_kind == SessionKind.CHAT
    assert request.ephemeral is True and request.caller_owned_ephemeral is True
    assert request.reuse_existing is False
    assert request.cwd and request.cwd.endswith("task-1")
    assert hub.provisioned == [("ws-1", "task-1")]
    assert hub.start_requests[0].target_session_id == "eph-1"

    receipt = result["receipt_summary"]
    lines = receipt.splitlines()
    assert len(lines) == 4 and [line[0] for line in lines] == ["①", "②", "③", "④"]
    assert "Summarize FA3" in lines[0]
    assert "1) notes/fa3.md has 5 bullets" in lines[1] and "2) each bullet cites" in lines[1]
    assert agent_type in lines[2] and "chat+ephemeral" in lines[2]
    assert "worktree" in lines[2] and "fast local lane" in lines[2]
    assert "Notify" in lines[3]


@pytest.mark.asyncio
async def test_non_git_directory_skips_worktree_but_keeps_chat_lane(
    service: DotService, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    hub = _HubFakes(monkeypatch, _workspace(plain), tmp_path)

    result = await _delegate(service)

    payload = hub.payload
    assert payload.session_kind == SessionKind.CHAT
    assert not payload.auto_worktree and not payload.auto_push_remote
    assert hub.provisioned == []
    request = hub.ensure_requests[0]
    assert request.ephemeral is True and request.cwd == str(plain)
    assert "workspace path (no worktree)" in result["receipt_summary"]


@pytest.mark.asyncio
async def test_readonly_brief_skips_worktree_and_defaults_to_triage_subagent(
    service: DotService, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    repo = _git_repo(tmp_path)
    hub = _HubFakes(monkeypatch, _workspace(repo), tmp_path)

    result = await _delegate(service, readonly=True, why="diagnosis only")

    payload = hub.payload
    assert not payload.auto_worktree and not payload.auto_push_remote
    assert payload.task_mode == WorkspaceTaskMode.SUBAGENT
    assert payload.recipe == WorkspaceTaskRecipe.TRIAGE_DIAGNOSE
    assert payload.session_kind == SessionKind.CHAT
    assert hub.provisioned == []
    assert hub.ensure_requests[0].ephemeral is True
    assert "readonly investigation" in result["receipt_summary"]


@pytest.mark.asyncio
async def test_triage_recipe_is_effectively_readonly(
    service: DotService, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    repo = _git_repo(tmp_path)
    hub = _HubFakes(monkeypatch, _workspace(repo), tmp_path)

    await _delegate(service, recipe="triage_diagnose")

    payload = hub.payload
    assert not payload.auto_worktree
    assert payload.task_mode == WorkspaceTaskMode.SUBAGENT
    assert payload.recipe == WorkspaceTaskRecipe.TRIAGE_DIAGNOSE
    assert hub.provisioned == []


@pytest.mark.asyncio
async def test_remote_workspace_keeps_terminal_surface_and_no_worktree(
    service: DotService, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    repo = _git_repo(tmp_path)
    hub = _HubFakes(monkeypatch, _workspace(repo, ExecutionTarget.REMOTE), tmp_path)

    await _delegate(service)

    payload = hub.payload
    assert "session_kind" not in payload.model_fields_set
    assert not payload.auto_worktree
    assert hub.ensure_requests == [] and hub.provisioned == []
    assert hub.start_requests[0].target_session_id is None


@pytest.mark.asyncio
async def test_explicit_no_ephemeral_reuses_an_existing_chat_session(
    service: DotService, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    repo = _git_repo(tmp_path)
    hub = _HubFakes(monkeypatch, _workspace(repo), tmp_path)

    await _delegate(service, ephemeral=False)

    request = hub.ensure_requests[0]
    assert request.ephemeral is False and request.caller_owned_ephemeral is False
    assert request.reuse_existing is True


@pytest.mark.asyncio
async def test_goal_packet_carries_parsed_out_of_scope_lines(
    service: DotService, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    repo = _git_repo(tmp_path)
    hub = _HubFakes(monkeypatch, _workspace(repo), tmp_path)

    await _delegate(service, out_of_scope="  do not touch docs/\n\nno deploys\n")

    assert hub.payload.goal_packet.out_of_scope == ["do not touch docs/", "no deploys"]


@pytest.mark.asyncio
async def test_legacy_workspace_double_without_chat_provisioning_still_delegates(
    service: DotService, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """Old test doubles may lack ``workspaces`` / ``ensure_workspace_agent``:
    the delegation must fall back to a plain start instead of crashing."""
    started: list[Any] = []

    class _LegacyHub:
        # Deliberately no ``workspaces`` attribute and no ensure_workspace_agent.
        sessions: dict[str, Any] = {}
        tasks: dict[str, Any] = {}

        async def register_task(self, ws: str, payload: Any, actor_key: str) -> tuple[Any, bool]:
            return (
                SimpleNamespace(
                    id="task-1",
                    title=payload.title,
                    status=SimpleNamespace(value="todo"),
                    session_id=None,
                ),
                False,
            )

        async def start_task(self, task_id: str, request: Any) -> Any:
            started.append(request)
            return SimpleNamespace(
                id=task_id, title="Paper", status=SimpleNamespace(value="queued")
            )

    # All ``from ... import workspace_manager`` calls resolve the parent
    # package attribute, which is the singleton instance; swap in the double.
    monkeypatch.setattr("claude_hub.services.workspace_manager", _LegacyHub())

    result = await _delegate(service)

    assert not result["held"] and result["work"]["task_id"] == "task-1"
    assert started and started[0].target_session_id is None
    # The receipt is still built from the intended fast-lane configuration.
    assert len(result["receipt_summary"].splitlines()) == 4


def test_brief_and_wake_prompts_drop_the_blocking_ceremony() -> None:
    assert "Post your plan" not in BRIEF_TEMPLATE
    assert "do not pause for a" in BRIEF_TEMPLATE
    assert "`path:line`" in BRIEF_TEMPLATE and "needs_input" in BRIEF_TEMPLATE
    assert 'Map each "Done when" item to the exact' in BRIEF_TEMPLATE
    # The wake turn no longer mandates a per-task review accounting step.
    assert "dot review" not in WAKE_MESSAGE
    assert "awaiting_acceptance" in WAKE_MESSAGE
    assert "review_failed_not_resumed" in WAKE_MESSAGE
    assert "4-line contract receipt" in TURN_RULES
