"""Dispatch contracts, lightweight assignment, and reusable workspace seats."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from claude_hub.cli.main import cli
from claude_hub.models import (
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    EnsureWorkspaceAgentRequest,
    GoalPacket,
    GoalPacketStatus,
    SessionKind,
    StartTaskRequest,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTaskCreate,
    WorkspaceTaskExecutionComplexity,
    WorkspaceTaskMode,
    WorkspaceTaskRecipe,
    WorkspaceTaskStatus,
)
from claude_hub.services import workspace_state_policy
from tests.test_work_pipeline import _setup_fake_chat_runtime


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    manager, _ = _setup_fake_chat_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "claude_hub.services.maso_discovery.discover_maso_service",
        AsyncMock(return_value=("http://127.0.0.1:11004", [], "gpt-6-astra")),
    )
    repo = tmp_path / "non_git_workspace"
    repo.mkdir()
    workspace = manager.create_workspace(WorkspaceCreate(name="Contracts", path=str(repo)))
    return manager, workspace


def test_create_contract_approved_and_legacy_defaults(runtime):
    manager, workspace = runtime
    task = manager.create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Fallback goal",
            prompt="Implement",
            goal="  Desired outcome  ",
            done_when=["  verified test  ", "", "second criterion"],
            out_of_scope=["deploy"],
        ),
    )
    assert task.goal == "Desired outcome"
    assert task.done_when == ["verified test", "second criterion"]
    assert task.goal_packet is not None
    assert task.goal_packet.status == GoalPacketStatus.APPROVED
    assert task.goal_packet.objective == task.goal
    assert task.goal_packet.acceptance_criteria == task.done_when
    assert task.goal_packet.out_of_scope == ["deploy"]
    assert task.goal_packet.created_at == task.created_at
    assert task.goal_packet.updated_at == task.updated_at
    legacy = task.model_dump(mode="json")
    for field in ("goal", "done_when", "out_of_scope"):
        legacy.pop(field)
    normalized = manager._normalize_task_item(legacy)
    assert normalized["goal"] is None
    assert normalized["done_when"] == normalized["out_of_scope"] == []
    legacy_worker = manager._normalize_session_item({"role": "worker", "status": "idle"})
    assert legacy_worker["role"] == "orchestrator"
    legacy_dispatcher = manager._normalize_session_item({"role": "dispatcher", "status": "idle"})
    assert legacy_dispatcher["role"] == "dispatcher"  # readable, but not newly creatable


@pytest.mark.parametrize(
    "packet", [None, GoalPacket(objective="Explicit plan", status=GoalPacketStatus.PENDING_REVIEW)]
)
def test_explicit_goal_packet_is_not_overwritten(runtime, packet):
    manager, workspace = runtime
    task = manager.create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Title",
            prompt="Work",
            goal="Goal",
            done_when=["test"],
            goal_packet=packet,
        ),
    )
    assert task.goal_packet == packet


def test_done_when_without_goal_uses_title(runtime):
    manager, workspace = runtime
    task = manager.create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="  Title  ",
            prompt="Work",
            done_when=["test"],
        ),
    )
    assert task.goal_packet is not None
    assert task.goal_packet.objective == "Title"


@pytest.mark.asyncio
async def test_assignment_default_is_direct_and_explicit_gate_is_preserved(runtime):
    manager, workspace = runtime
    session = await manager.ensure_workspace_agent(
        workspace.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
        ),
    )
    task = manager.create_task(workspace.id, WorkspaceTaskCreate(title="Title", prompt="Work"))
    prompt = manager._build_task_assignment_prompt(workspace, task, session, lesson_context=[])
    assert "continue implementation immediately" in prompt
    for excluded in (
        "stop and wait",
        "message_en",
        "message_zh",
        "subtask-envelope",
        "subagent-ledger",
    ):
        assert excluded not in prompt
    assert f"claude-hub session report {session.id}" in prompt
    explicit = task.model_copy(
        update={
            "goal_packet": GoalPacket(
                objective="Plan",
                status=GoalPacketStatus.PENDING_REVIEW,
            )
        }
    )
    explicit_prompt = manager._build_task_assignment_prompt(
        workspace,
        explicit,
        session,
        lesson_context=[],
    )
    assert "stop and wait" in explicit_prompt
    assert "continue implementation immediately" not in explicit_prompt
    with_contract = manager.create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Title",
            prompt="Work",
            done_when=["UI renders contract"],
        ),
    )
    prompt = manager._build_task_assignment_prompt(
        workspace, with_contract, session, lesson_context=[]
    )
    assert "UI renders contract" in prompt
    assert "verification evidence for each criterion" in prompt
    autonomous = task.model_copy(update={"task_mode": WorkspaceTaskMode.AUTONOMOUS})
    assert "subtask-envelope" not in manager._build_task_assignment_prompt(
        workspace, autonomous, session, lesson_context=[]
    )
    complex_task = autonomous.model_copy(
        update={"execution_complexity": WorkspaceTaskExecutionComplexity.COMPLEX}
    )
    assert "subtask-envelope" in manager._build_task_assignment_prompt(
        workspace, complex_task, session, lesson_context=[]
    )


@pytest.mark.asyncio
async def test_non_git_recipe_degrades_without_enabling_push(runtime):
    manager, workspace = runtime
    task = manager.create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Non-Git files",
            prompt="Process data",
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
            agent_type=AgentType.MASO,
        ),
    )
    assert task.auto_worktree is True
    assert task.auto_push_remote is False
    started = await manager.start_task(task.id)
    assert started.status == WorkspaceTaskStatus.WORKING
    assert started.auto_worktree is False
    assert started.worktree_path is None
    assert manager.sessions[started.session_id].workspace_path == workspace.path


@pytest.mark.asyncio
async def test_worker_normalizes_and_dispatches_without_bootstrap(runtime, monkeypatch):
    manager, workspace = runtime
    bootstrap = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", bootstrap)
    monkeypatch.setattr(manager, "send_session_message", send)
    session = await manager.ensure_workspace_agent(
        workspace.id,
        EnsureWorkspaceAgentRequest(
            role=WorkspaceSessionRole.WORKER,
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
        ),
        bootstrap=True,
    )
    assert session.role == WorkspaceSessionRole.ORCHESTRATOR
    bootstrap.assert_not_called()
    send.assert_not_called()
    task = manager.create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Work",
            prompt="Implement",
            session_id=session.id,
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
        ),
    )
    started = await manager.start_task(task.id, StartTaskRequest(target_session_id=session.id))
    assert started.status == WorkspaceTaskStatus.WORKING
    assert send.call_count == 1  # first turn is the assignment, never an empty check-in
    with pytest.raises(ValueError, match="deprecated"):
        EnsureWorkspaceAgentRequest(role=WorkspaceSessionRole.DISPATCHER)


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["human_acceptance_requested_at", "review_skipped_at"])
async def test_awaiting_acceptance_frees_seat_but_inflight_review_holds_it(runtime, marker):
    manager, workspace = runtime
    session = await manager.ensure_workspace_agent(
        workspace.id,
        EnsureWorkspaceAgentRequest(
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
        ),
    )
    previous = manager.create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Old",
            prompt="Work",
            agent_type=AgentType.MASO,
            session_id=session.id,
            session_kind=SessionKind.CHAT,
        ),
    )
    now = datetime.now()
    manager.tasks[previous.id] = previous.model_copy(
        update={
            "status": WorkspaceTaskStatus.REVIEW,
            marker: now,
            "review_requested_at": now - timedelta(seconds=10),
            "review_completed_at": now,
        }
    )
    session = session.model_copy(
        update={
            "task_id": previous.id,
            "current_task_id": previous.id,
            "runtime_status": AgentRuntimeStatus.IDLE,
        }
    )
    manager.sessions[session.id] = session
    assert manager._can_dispatch_to(session)
    assert not manager._is_holding_unresolved_review_task(session)
    manager.tasks[previous.id] = manager.tasks[previous.id].model_copy(
        update={"review_completed_at": None}
    )
    assert not manager._can_dispatch_to(session)
    manager.tasks[previous.id] = manager.tasks[previous.id].model_copy(
        update={"review_completed_at": now}
    )
    following = manager.create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Next",
            prompt="Implement",
            agent_type=AgentType.MASO,
            session_id=session.id,
            session_kind=SessionKind.CHAT,
        ),
    )
    started = await manager.start_task(following.id, StartTaskRequest(target_session_id=session.id))
    assert started.status == WorkspaceTaskStatus.WORKING
    assert manager.sessions[session.id].current_task_id == following.id
    assert manager.tasks[previous.id].status == WorkspaceTaskStatus.REVIEW


def test_completion_does_not_require_backfilled_plan():
    assert (
        workspace_state_policy.completion_evidence_gaps(
            AgentReportState.COMPLETED,
            has_goal_packet=False,
            has_acceptance_check=False,
        )
        == []
    )


def test_cli_create_contract_options(monkeypatch):
    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def create_task(self, workspace_id, body):
            assert workspace_id == "workspace"
            assert body["goal"] == "Goal"
            assert body["done_when"] == ["one", "two"]
            assert body["out_of_scope"] == ["deploy"]
            return {"id": "task", **body}

    monkeypatch.setattr("claude_hub.cli.main.get_client", lambda _ctx: Client())
    result = CliRunner().invoke(
        cli,
        [
            "task",
            "create",
            "workspace",
            "--title",
            "Title",
            "--prompt",
            "Do work",
            "--goal",
            "Goal",
            "--done-when",
            "one",
            "--done-when",
            "two",
            "--out-of-scope",
            "deploy",
        ],
    )
    assert result.exit_code == 0, result.output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        (124, "", "command timed out"),
        (128, "", "fatal: detected dubious ownership in repository"),
        (128, "", "fatal: unable to read config file: Permission denied"),
    ],
)
async def test_git_inspection_failure_does_not_disable_isolation(runtime, monkeypatch, failure):
    from claude_hub.services import work_pipeline

    manager, workspace = runtime
    task = manager.create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Keep isolation",
            prompt="Work",
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
            agent_type=AgentType.MASO,
        ),
    )
    monkeypatch.setattr(work_pipeline, "_run_bounded_subprocess", AsyncMock(return_value=failure))
    with pytest.raises(RuntimeError, match="not a valid Git repository"):
        await manager.start_task(task.id)
    assert manager.tasks[task.id].auto_worktree is True
    assert manager.tasks[task.id].status == WorkspaceTaskStatus.TODO
    assert manager.sessions == {}


@pytest.mark.asyncio
async def test_non_git_downgrade_preserves_concurrent_task_edit(runtime, monkeypatch):
    from claude_hub.models import WorkspaceTaskUpdate
    from claude_hub.services import work_pipeline

    manager, workspace = runtime
    task = manager.create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Before edit",
            prompt="Work",
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
            agent_type=AgentType.MASO,
        ),
    )
    resolver = work_pipeline._resolve_canonical_git_repo_root

    async def edit_during_probe(path):
        await manager.update_task(task.id, WorkspaceTaskUpdate(title="Concurrent saved edit"))
        return await resolver(path)

    monkeypatch.setattr(work_pipeline, "_resolve_canonical_git_repo_root", edit_during_probe)
    started = await manager.start_task(task.id)
    assert started.title == "Concurrent saved edit"
    assert started.auto_worktree is False
    assert started.status == WorkspaceTaskStatus.WORKING


@pytest.mark.asyncio
async def test_unreadable_git_head_does_not_downgrade_to_original_checkout(runtime, tmp_path):
    import os

    from tests.test_work_pipeline import _init_git_repo_with_bare_origin

    if os.geteuid() == 0:
        pytest.skip("root bypasses the metadata permission fault")
    manager, _ = runtime
    repo, _ = _init_git_repo_with_bare_origin(tmp_path)
    workspace = manager.create_workspace(WorkspaceCreate(name="Unreadable Git", path=str(repo)))
    task = manager.create_task(
        workspace.id,
        WorkspaceTaskCreate(
            title="Keep isolation",
            prompt="Work",
            recipe=WorkspaceTaskRecipe.SPEC_IMPLEMENT_REVIEW,
            agent_type=AgentType.MASO,
        ),
    )
    head = repo / ".git" / "HEAD"
    original_mode = head.stat().st_mode
    try:
        head.chmod(0)
        with pytest.raises(RuntimeError, match="not a valid Git repository"):
            await manager.start_task(task.id)
        assert manager.tasks[task.id].auto_worktree is True
        assert manager.tasks[task.id].status == WorkspaceTaskStatus.TODO
        assert manager.sessions == {}
    finally:
        head.chmod(original_mode)
