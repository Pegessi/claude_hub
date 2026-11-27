"""End-to-end integration tests across Dot delegation, task contracts, and Subagent blind review."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from claude_hub.models import (
    AgentReportCreate,
    AgentReportState,
    AgentType,
    SessionKind,
    TaskPhase,
    WorkspaceCreate,
    WorkspaceTaskCreate,
    WorkspaceTaskStatus,
)
from claude_hub.models.subagent import SubagentTurnStatus
from claude_hub.services import managed_chat
from claude_hub.services import subagents as subagents_mod
from claude_hub.services.cross_reviewer import CrossReviewCandidate
from claude_hub.services.dot import DotService, DotStore
from claude_hub.services.workspace_manager import subagent_review
from tests.test_work_pipeline import _setup_fake_chat_runtime


class _ScriptedSubagentService:
    """Deterministic SubagentService double recording prompts and returning scripted outputs."""

    def __init__(self, responses: list[str]) -> None:
        self._started = True
        self._responses = list(responses)
        self.calls: list[Any] = []

    async def create(self, payload: Any) -> tuple[Any, Any]:
        idx = len(self.calls) + 1
        self.calls.append(payload)
        text = self._responses.pop(0)
        record = SimpleNamespace(
            id=f"sa-e2e-{idx}",
            tab_id=f"tab-e2e-{idx}",
            provider=payload.agent_type.value,
            agent_type=payload.agent_type,
            cwd=payload.cwd,
            keep=payload.keep,
            solo_mode=payload.solo_mode,
        )
        turn = SimpleNamespace(
            turn_id=f"turn-e2e-{idx}",
            status=SubagentTurnStatus.COMPLETED,
            final_text=text,
        )
        return record, turn

    async def wait_turn(
        self, subagent_id: str, turn_id: str, *, wait_seconds: float = 30.0
    ) -> tuple[Any, Any]:
        raise AssertionError("Completed turns should not enter wait_turn loop")

    async def close(self, subagent_id: str, *, reason: str = "") -> None:
        return None


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)


async def _drain_review_tasks(manager: Any) -> None:
    pending = list(getattr(manager, "_subagent_review_tasks", {}).values())
    if pending:
        await asyncio.gather(*pending)


@pytest.mark.asyncio
async def test_dot_delegate_to_subagent_p2_pass_and_dot_notification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager, _ = _setup_fake_chat_runtime(monkeypatch, tmp_path)
    sent_messages: list[tuple[str, str]] = []

    async def capture_send(session: Any, text: str, _attachments: Any, _call_id: Any) -> None:
        sent_messages.append((session.id, text))

    monkeypatch.setattr(managed_chat, "send_message", capture_send)
    monkeypatch.setattr("claude_hub.services.workspace_manager", manager)

    fake_subagents = _ScriptedSubagentService(
        [
            'Looks clean.\n<task-review>{"verdict":"review_failed","summary":"Implementation meets contract with minor style note.","validation":"pytest passed","risks":"low","findings":[{"severity":"P2","file":"app.py:2","summary":"Consider docstring","fix":"Optional docstring"}]}</task-review>'
        ]
    )
    monkeypatch.setattr(subagents_mod, "get_subagent_service", lambda: fake_subagents)
    monkeypatch.setattr(
        subagent_review,
        "select_cross_reviewer_candidates",
        lambda *_args, **_kwargs: [CrossReviewCandidate(provider="codex", effort="high")],
    )
    monkeypatch.setattr(
        subagent_review,
        "read_cached_quota_map",
        lambda: asyncio.sleep(0, result={}),
    )
    monkeypatch.setattr(
        subagent_review,
        "read_cross_review_settings",
        lambda: SimpleNamespace(preferred_candidates=None),
    )

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    ws = manager.create_workspace(WorkspaceCreate(name="StreamlineRepo", path=str(repo)))

    store = DotStore(tmp_path / "dot")
    store.ensure()
    store.update_state(tab_id="dot-tab")
    dot = DotService(store)

    res = await dot.delegate(
        workspace_id=ws.id,
        title="Add subtract helper",
        goal="Provide a verified subtract(a, b) helper in app.py",
        done_when=["subtract(5, 3) returns 2 in unit tests"],
        context="Repository root is app.py; keep existing add() signature intact.",
        output="Updated app.py and passing pytest output",
        out_of_scope="CLI packaging",
        agent_type="claude",
        why="Claude handles concise Python helper edits cleanly",
        confirm=True,
        actor_key="actor-e2e-1",
    )
    assert res["held"] is False
    assert "receipt_summary" in res and "subtract(5, 3) returns 2" in res["receipt_summary"]
    task_id = res["work"]["task_id"]
    task = manager.tasks[task_id]

    # Contract & fast-lane defaults verified on the created task
    assert task.goal == "Provide a verified subtract(a, b) helper in app.py"
    assert task.done_when == ["subtract(5, 3) returns 2 in unit tests"]
    assert task.out_of_scope == ["CLI packaging"]
    assert task.goal_packet is not None
    assert task.goal_packet.status.value == "approved"
    assert task.session_kind == SessionKind.CHAT
    assert task.auto_worktree is True
    assert task.auto_push_remote is False
    assert task.status == WorkspaceTaskStatus.WORKING
    assert task.session_id is not None

    worker = manager.sessions[task.session_id]
    assert worker.ephemeral is True
    # Zero bootstrap check-in turn: only the single assignment message was sent
    sent_to_worker = [m[1] for m in sent_messages if m[0] == worker.id]
    assert len(sent_to_worker) == 1
    assert "stop and wait" not in sent_to_worker[0]

    # Worker edits isolated worktree and submits completion report
    wt_path = Path(task.worktree_path or repo)
    (wt_path / "app.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n\n"
        "def subtract(a: int, b: int) -> int:\n    return a - b\n"
    )
    await manager.create_report(
        worker.id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            message="Implemented subtract() and verified unit tests.",
            changed_files=["app.py"],
            validation="pytest -q -> 2 passed",
            call_id="worker-complete-1",
        ),
    )
    await _drain_review_tasks(manager)

    # Subagent ran top-level, solo, non-kept, saw the contract & diff, and P2-only auto-passed
    assert len(fake_subagents.calls) == 1
    sa_call = fake_subagents.calls[0]
    assert sa_call.parent_tab_id is None
    assert sa_call.keep is False
    assert sa_call.solo_mode is True
    assert "subtract(5, 3) returns 2 in unit tests" in sa_call.prompt
    assert "def subtract" in sa_call.prompt

    reviewed_task = manager.tasks[task.id]
    assert reviewed_task.status == WorkspaceTaskStatus.REVIEW
    assert reviewed_task.human_acceptance_requested_at is not None
    lifecycle = manager.task_lifecycles([reviewed_task])[task.id]
    assert lifecycle.phase == TaskPhase.AWAITING_ACCEPTANCE

    # Dot wakes with final review verdict without requiring any ManagedSession reviewer seat
    assert await dot.check_work() == 1
    inbox = dot.store.list_inbox()
    assert len(inbox) == 1
    assert inbox[0]["kind"] == "task_review_passed"


@pytest.mark.asyncio
async def test_subagent_two_round_rework_cap_parks_needs_user_and_blocks_liveness_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager, _ = _setup_fake_chat_runtime(monkeypatch, tmp_path)
    sent_messages: list[tuple[str, str]] = []

    async def capture_send(session: Any, text: str, _attachments: Any, _call_id: Any) -> None:
        sent_messages.append((session.id, text))

    monkeypatch.setattr(managed_chat, "send_message", capture_send)
    p0_failure = (
        '<task-review>{"verdict":"review_failed","summary":"Contract unmet","validation":"failed","risks":"high",'
        '"findings":[{"severity":"P0","file":"app.py:1","summary":"Edge case broken","fix":"Handle zero"}]}</task-review>'
    )
    fake_subagents = _ScriptedSubagentService([p0_failure, p0_failure, p0_failure])
    monkeypatch.setattr(subagents_mod, "get_subagent_service", lambda: fake_subagents)
    monkeypatch.setattr(
        subagent_review,
        "select_cross_reviewer_candidates",
        lambda *_args, **_kwargs: [CrossReviewCandidate(provider="codex", effort="high")],
    )
    monkeypatch.setattr(
        subagent_review,
        "read_cached_quota_map",
        lambda: asyncio.sleep(0, result={}),
    )
    monkeypatch.setattr(
        subagent_review,
        "read_cross_review_settings",
        lambda: SimpleNamespace(preferred_candidates=None),
    )

    repo = tmp_path / "repo_cap"
    _init_git_repo(repo)
    ws = manager.create_workspace(WorkspaceCreate(name="CapRepo", path=str(repo)))

    task = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Fix edge case",
            prompt="Fix zero handling",
            goal="Zero input returns 0 safely",
            done_when=["add(0, 0) == 0"],
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
        ),
    )
    started = await manager.start_task(task.id)
    worker_id = started.session_id
    assert worker_id is not None

    # Round 1 failure -> Rework 1 dispatched; Round 2 failure -> Rework 2 dispatched
    for attempt in (1, 2):
        await manager.create_report(
            worker_id,
            AgentReportCreate(
                task_id=task.id,
                state=AgentReportState.COMPLETED,
                message=f"Attempt {attempt}",
                call_id=f"attempt-{attempt}",
            ),
        )
        await _drain_review_tasks(manager)
        after = manager.tasks[task.id]
        assert after.status == WorkspaceTaskStatus.WORKING, f"Round {attempt} should reopen worker"

    # Verify rework prompt contains the 3 false-positive rebuttal rules
    rework_messages = [m[1] for m in sent_messages if m[0] == worker_id]
    assert any('state="needs_input"' in msg and "path:line" in msg for msg in rework_messages)

    # Round 3 failure (after 2 rework rounds) -> Hard circuit breaker stops auto-continue!
    await manager.create_report(
        worker_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            message="Attempt 3",
            call_id="attempt-3",
        ),
    )
    await _drain_review_tasks(manager)

    parked = manager.tasks[task.id]
    assert parked.status == WorkspaceTaskStatus.REVIEW
    assert parked.human_acceptance_requested_at is None
    lifecycle = manager.task_lifecycles([parked])[task.id]
    assert lifecycle.phase == TaskPhase.NEEDS_USER
    assert lifecycle.reason == "review_failed_not_resumed"

    # Liveness facts must NOT mark resume_verdict=True once cap is reached
    facts = await manager._liveness_facts(parked)
    assert facts.resume_verdict is False


@pytest.mark.asyncio
async def test_update_task_persists_contract_fields_and_syncs_goal_packet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from claude_hub.models import WorkspaceTaskRecipe, WorkspaceTaskUpdate

    manager, _ = _setup_fake_chat_runtime(monkeypatch, tmp_path)
    repo = tmp_path / "repo_edit"
    _init_git_repo(repo)
    ws = manager.create_workspace(WorkspaceCreate(name="EditRepo", path=str(repo)))

    task = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Initial title",
            prompt="Initial prompt",
            goal="Old goal",
            done_when=["old check"],
        ),
    )
    updated = await manager.update_task(
        task.id,
        WorkspaceTaskUpdate(
            goal="  Updated goal  ",
            done_when=["  new check 1  ", "", "new check 2"],
            out_of_scope=["  no deploy  "],
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
        ),
    )
    assert updated.goal == "Updated goal"
    assert updated.done_when == ["new check 1", "new check 2"]
    assert updated.out_of_scope == ["no deploy"]
    assert updated.auto_worktree is True
    assert updated.auto_push_remote is False
    assert updated.goal_packet is not None
    assert updated.goal_packet.status.value == "approved"
    assert updated.goal_packet.objective == "Updated goal"
    assert updated.goal_packet.acceptance_criteria == ["new check 1", "new check 2"]
    assert updated.goal_packet.out_of_scope == ["no deploy"]

    # Explicit clear removes approved goal_packet even when validation_plan existed
    with_plan = await manager.update_task(
        task.id,
        WorkspaceTaskUpdate(
            goal_packet=updated.goal_packet.model_copy(update={"validation_plan": ["pytest"]})
        ),
    )
    assert with_plan.goal_packet is not None and with_plan.goal_packet.validation_plan == ["pytest"]
    cleared = await manager.update_task(
        task.id,
        WorkspaceTaskUpdate(goal="", done_when=[], out_of_scope=[]),
    )
    assert cleared.goal is None
    assert cleared.done_when == []
    assert cleared.out_of_scope == []
    assert cleared.goal_packet is None


@pytest.mark.asyncio
async def test_rework_cap_counts_first_failure_even_when_worker_sent_working_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager, _ = _setup_fake_chat_runtime(monkeypatch, tmp_path)
    p0_failure = (
        '<task-review>{"verdict":"review_failed","summary":"Still broken","validation":"failed","risks":"high",'
        '"findings":[{"severity":"P0","file":"app.py:1","summary":"Bug","fix":"Fix it"}]}</task-review>'
    )
    fake_subagents = _ScriptedSubagentService([p0_failure, p0_failure, p0_failure])
    monkeypatch.setattr(subagents_mod, "get_subagent_service", lambda: fake_subagents)
    monkeypatch.setattr(
        subagent_review,
        "select_cross_reviewer_candidates",
        lambda *_args, **_kwargs: [CrossReviewCandidate(provider="codex", effort="high")],
    )
    monkeypatch.setattr(
        subagent_review,
        "read_cached_quota_map",
        lambda: asyncio.sleep(0, result={}),
    )
    monkeypatch.setattr(
        subagent_review,
        "read_cross_review_settings",
        lambda: SimpleNamespace(preferred_candidates=None),
    )

    repo = tmp_path / "repo_working_first"
    _init_git_repo(repo)
    ws = manager.create_workspace(WorkspaceCreate(name="WorkingFirstRepo", path=str(repo)))
    task = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="With progress report",
            prompt="Implement feature",
            goal="Feature works",
            done_when=["unit test passes"],
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
        ),
    )
    started = await manager.start_task(task.id)
    worker_id = started.session_id
    assert worker_id is not None

    # Worker emits a normal WORKING milestone in cycle 0 before COMPLETED
    await manager.create_report(
        worker_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.WORKING,
            message="Derived plan and started editing app.py",
            call_id="progress-0",
        ),
    )
    for attempt in (1, 2):
        await manager.create_report(
            worker_id,
            AgentReportCreate(
                task_id=task.id,
                state=AgentReportState.COMPLETED,
                message=f"Done attempt {attempt}",
                call_id=f"done-{attempt}",
            ),
        )
        await _drain_review_tasks(manager)
        assert manager.tasks[task.id].status == WorkspaceTaskStatus.WORKING

    # 3rd implementation failure MUST hit cap (count == 3), NOT be miscounted as 2
    await manager.create_report(
        worker_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            message="Done attempt 3",
            call_id="done-3",
        ),
    )
    await _drain_review_tasks(manager)
    assert manager._implementation_review_failure_count(task.id) == 3
    parked = manager.tasks[task.id]
    assert parked.status == WorkspaceTaskStatus.REVIEW
    assert parked.human_acceptance_requested_at is None
    assert manager.task_lifecycles([parked])[task.id].reason == "review_failed_not_resumed"


@pytest.mark.asyncio
async def test_subagent_blind_review_sees_committed_branch_diff_when_worktree_is_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager, _ = _setup_fake_chat_runtime(monkeypatch, tmp_path)
    fake_subagents = _ScriptedSubagentService(
        [
            '<task-review>{"verdict":"review_passed","summary":"Verified committed diff","validation":"ok","risks":"none","findings":[]}</task-review>'
        ]
    )
    monkeypatch.setattr(subagents_mod, "get_subagent_service", lambda: fake_subagents)
    monkeypatch.setattr(
        subagent_review,
        "select_cross_reviewer_candidates",
        lambda *_args, **_kwargs: [CrossReviewCandidate(provider="codex", effort="high")],
    )
    monkeypatch.setattr(
        subagent_review,
        "read_cached_quota_map",
        lambda: asyncio.sleep(0, result={}),
    )
    monkeypatch.setattr(
        subagent_review,
        "read_cross_review_settings",
        lambda: SimpleNamespace(preferred_candidates=None),
    )

    repo = tmp_path / "repo_committed"
    _init_git_repo(repo)
    ws = manager.create_workspace(WorkspaceCreate(name="CommittedRepo", path=str(repo)))
    task = manager.create_task(
        ws.id,
        WorkspaceTaskCreate(
            title="Committed implementation",
            prompt="Add multiply()",
            goal="Add multiply(a, b)",
            done_when=["multiply(3, 4) == 12"],
            agent_type=AgentType.CLAUDE,
            session_kind=SessionKind.CHAT,
            auto_worktree=True,
        ),
    )
    started = await manager.start_task(task.id)
    assert started.worktree_path is not None
    wt = Path(started.worktree_path)
    (wt / "app.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n\n"
        "def multiply(a: int, b: int) -> int:\n    return a * b\n"
    )
    subprocess.run(["git", "add", "app.py"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-m", "feat: add multiply"], cwd=wt, check=True)

    assert started.session_id is not None
    await manager.create_report(
        started.session_id,
        AgentReportCreate(
            task_id=task.id,
            state=AgentReportState.COMPLETED,
            message="Committed multiply() on feature worktree.",
            changed_files=["app.py"],
            validation="pytest -q",
            call_id="committed-1",
        ),
    )
    await _drain_review_tasks(manager)
    assert len(fake_subagents.calls) == 1
    assert "def multiply" in fake_subagents.calls[0].prompt
