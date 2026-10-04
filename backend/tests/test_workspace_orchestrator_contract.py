"""Prompt-content tests for the Auto Mode orchestrator contract (Phase 1).

These tests exercise the prompt-builder helpers in isolation; they do NOT
spin up the workspace state machine. The intent is to assert the new
orchestrator-contract wording survives future refactors.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from claude_hub.models import (
    AcceptanceCheck,
    AcceptanceCheckStatus,
    AgentReport,
    AgentReportCreate,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    AutonomousRun,
    AutonomousRunPhase,
    AutonomyPolicy,
    ContinueTaskRequest,
    ExecutionTarget,
    GoalPacket,
    ManagedSession,
    ManagedSessionStatus,
    ReviewDecision,
    ReviewProfile,
    Workspace,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskExecutionComplexity,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)
from claude_hub.services.workspace_manager import workspace_manager
from claude_hub.services.workspace_manager._constants import INTERNAL_API_CURL


def _make_task(
    *,
    mode: WorkspaceTaskMode,
    complexity: WorkspaceTaskExecutionComplexity,
    agent_type: AgentType = AgentType.CLAUDE,
) -> WorkspaceTask:
    now = datetime.utcnow()
    return WorkspaceTask(
        id="t-1",
        workspace_id="ws-1",
        agent_type=agent_type,
        title="t",
        prompt="p",
        status=WorkspaceTaskStatus.WORKING,
        task_mode=mode,
        execution_complexity=complexity,
        autonomy_policy=(AutonomyPolicy() if mode == WorkspaceTaskMode.AUTONOMOUS else None),
        autonomous_run=(
            AutonomousRun(id="run-t-1", task_id="t-1")
            if mode == WorkspaceTaskMode.AUTONOMOUS
            else None
        ),
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("complexity", list(WorkspaceTaskExecutionComplexity))
def test_complexity_matches_cost_and_ownership_contract(complexity):
    task = _make_task(mode=WorkspaceTaskMode.AUTONOMOUS, complexity=complexity)
    block = workspace_manager._execution_complexity_assignment_block(task)
    assert "no minimum agent count" in block
    assert "one writer per owned scope" in block
    assert "isolated worktrees/resources" in block
    assert "10-15x" not in block
    assert ">=3" not in block
    if complexity == WorkspaceTaskExecutionComplexity.SIMPLE:
        assert "Execute directly" in block
    elif complexity == WorkspaceTaskExecutionComplexity.COMPLEX:
        assert "dependencies" in block and "tightly coupled changes serial" in block
    else:
        assert "goal_packet.assumptions" in block


@pytest.mark.parametrize("agent_type", list(AgentType))
def test_runtime_contract_uses_actual_capabilities_and_model_evidence(agent_type):
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
        agent_type=agent_type,
    )
    block = workspace_manager._autonomous_assignment_block(task, agent_type)
    assert "runtime-default" in block and "external:<api>" in block
    assert "respect explicit user model choices" in block
    assert "users CANNOT override" not in block
    assert "-> opus" not in block and "-> sonnet" not in block
    if agent_type == AgentType.TERMINAL:
        assert "no native sub-agent capability" in block
        assert "Do NOT fabricate" in block
    else:
        assert "actually available in this session" in block
        assert "unsupported" in block
    for field in (
        "owner",
        "inputs",
        "depends_on",
        "allowed_paths",
        "budget",
        "stop_condition",
    ):
        assert field in block
    assert "Research/review delegates are read-only" in block
    assert "Evidence handoff" in block
    assert "only for actual delegations" in block


def test_observability_retains_evidence_backed_blockers():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
    block = workspace_manager._autonomous_assignment_block(task)
    assert "heartbeat" in block
    assert "no autonomous next action remains" in block
    assert "name the blocker with evidence" in block


@pytest.mark.parametrize("mode", [WorkspaceTaskMode.DIRECT, WorkspaceTaskMode.REVIEWED])
def test_autonomous_contract_does_not_leak_into_other_task_modes(mode):
    task = _make_task(mode=mode, complexity=WorkspaceTaskExecutionComplexity.AUTO)
    assert workspace_manager._autonomous_assignment_block(task) == ""
    assert workspace_manager._autonomous_review_block(task) == ""
    assert workspace_manager._autonomous_continue_orchestrator_reminder(task) == ""


# ---------------------------------------------------------------------------
# Report-endpoint survives /clear: the curl target must be restated in every
# follow-up message that asks a (possibly context-cleared) agent to report.
# ---------------------------------------------------------------------------


def _make_session(
    *, session_id: str = "cb-agent-9", remote_forward_port: int | None = None
) -> ManagedSession:
    now = datetime.utcnow()
    return ManagedSession(
        id=session_id,
        workspace_id="ws-1",
        task_id=None,
        tab_id=f"tab-{session_id}",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.WORKING,
        current_task_id=None,
        queued_count=0,
        title="Agent 9",
        branch=None,
        workspace_path="/tmp/ws",
        tmux_session="claude-hub-tab-ws",
        target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_cwd=None,
        remote_reconnect=True,
        solo_mode=True,
        remote_forward_port=remote_forward_port,
        created_at=now,
        updated_at=now,
    )


def _endpoint_path(session: ManagedSession) -> str:
    return f"/api/workspaces/sessions/{session.id}/reports"


def test_report_endpoint_curl_uses_session_and_task():
    session = _make_session()
    snippet = workspace_manager._report_endpoint_curl(session, "task-42")
    assert f"{INTERNAL_API_CURL} -X POST" in snippet
    assert _endpoint_path(session) in snippet
    assert '"task_id":"task-42"' in snippet
    # Defaults to a placeholder when no task id is supplied.
    assert '"task_id":"TASK_ID"' in workspace_manager._report_endpoint_curl(session)


def test_all_internal_api_curl_templates_use_loopback_proxy_bypass():
    """Every agent-facing Hub API command must bypass proxies for loopback hosts."""
    package_root = Path(__file__).parents[1] / "claude_hub" / "services" / "workspace_manager"
    for module_name in ("_prompts.py", "_reports.py", "_workspaces.py"):
        source = (package_root / module_name).read_text()
        assert "curl -sS" not in source, f"{module_name} still has a proxyable curl example"
        assert (
            "INTERNAL_API_CURL" in source
        ), f"{module_name} does not use the shared Hub curl command"


def test_report_endpoint_curl_honors_remote_forward_port():
    session = _make_session(remote_forward_port=9123)
    snippet = workspace_manager._report_endpoint_curl(session, "task-1")
    assert "http://127.0.0.1:9123" in snippet


def test_continue_prompt_includes_report_endpoint():
    task = _make_task(
        mode=WorkspaceTaskMode.REVIEWED,
        complexity=WorkspaceTaskExecutionComplexity.AUTO,
    )
    session = _make_session()
    prompt = workspace_manager._build_continue_prompt(task, ContinueTaskRequest(), session)
    assert _endpoint_path(session) in prompt
    assert f"{INTERNAL_API_CURL} -X POST" in prompt
    assert f'"task_id":"{task.id}"' in prompt


def test_auto_continue_messages_carry_endpoint_when_sent(monkeypatch):
    """Both auto-continue nudges must restate the endpoint at send time.

    Drives the real ``_auto_continue_stopped_task`` path with the tmux capture
    and send helpers stubbed, asserting the message actually pushed to the agent
    contains the report endpoint — so a context-cleared agent always has a curl
    target. Exercises both branches: interruption (continue) and
    report-missing (completion).
    """
    import asyncio

    from claude_hub.services.workspace_manager import _monitor as monitor_module

    session = _make_session()
    task = _make_task(
        mode=WorkspaceTaskMode.REVIEWED,
        complexity=WorkspaceTaskExecutionComplexity.AUTO,
    )
    task = task.model_copy(update={"id": "task-7", "session_id": session.id})
    workspace_manager.tasks[task.id] = task
    endpoint = _endpoint_path(session)

    sent: list[str] = []

    async def fake_capture(_tmux_session: str) -> str:
        return "idle output"

    async def fake_send(_tmux_session: str, message: str) -> None:
        sent.append(message)

    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send)
    monkeypatch.setattr(workspace_manager, "_auto_continue_output_looks_busy", lambda _o: False)
    monkeypatch.setattr(workspace_manager, "_latest_report_state", lambda _t: None)
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)

    sampled = datetime.utcnow()

    # Branch 1: interruption detected -> AUTO_CONTINUE_MESSAGE.
    monkeypatch.setattr(
        workspace_manager,
        "_auto_continue_interruption_reason",
        lambda _o: "interrupted",
    )
    asyncio.run(workspace_manager._auto_continue_stopped_task(session, task, sampled))

    # Branch 2: no interruption, completion detected -> AUTO_REPORT_MISSING_MESSAGE.
    monkeypatch.setattr(workspace_manager, "_auto_continue_interruption_reason", lambda _o: None)
    monkeypatch.setattr(workspace_manager, "_auto_continue_completion_reason", lambda _o: "done")
    fresh_session = workspace_manager.sessions.get(session.id, session)
    asyncio.run(workspace_manager._auto_continue_stopped_task(fresh_session, task, sampled))

    workspace_manager.tasks.pop(task.id, None)

    assert len(sent) == 2, "both auto-continue branches should send a nudge"
    reminder_call_id = "task-7-monitor-reminder-cycle-1-attempt-1"
    for message in sent:
        assert endpoint in message, "auto-continue nudge must restate report endpoint"
        assert f"{INTERNAL_API_CURL} -X POST" in message
        assert '"task_id":"task-7"' in message
        assert reminder_call_id in message
        assert "working-progress" not in message
    assert monitor_module.AUTO_CONTINUE_MESSAGE.split("\n")[0] in sent[0]
    assert monitor_module.AUTO_REPORT_MISSING_MESSAGE.split("\n")[0] in sent[1]


def test_auto_continue_reminder_call_id_stable_per_attempt_then_advances(monkeypatch):
    """Same durable reminder attempt keeps its call_id; the next attempt gets a new one."""
    import asyncio
    from datetime import timedelta

    session = _make_session()
    task = _make_task(
        mode=WorkspaceTaskMode.REVIEWED,
        complexity=WorkspaceTaskExecutionComplexity.AUTO,
    )
    task = task.model_copy(update={"id": "task-reminder", "session_id": session.id})
    workspace_manager.tasks[task.id] = task

    sent: list[str] = []

    async def fake_capture(_tmux_session: str) -> str:
        return "idle output"

    async def fake_send(_tmux_session: str, message: str) -> None:
        sent.append(message)

    monkeypatch.setattr(workspace_manager, "_capture_tmux_output", fake_capture)
    monkeypatch.setattr(workspace_manager, "_send_tmux_message", fake_send)
    monkeypatch.setattr(workspace_manager, "_auto_continue_output_looks_busy", lambda _o: False)
    monkeypatch.setattr(workspace_manager, "_latest_report_state", lambda _t: None)
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)
    monkeypatch.setattr(
        workspace_manager,
        "_auto_continue_interruption_reason",
        lambda _o: "interrupted",
    )

    sampled = datetime.utcnow()
    first = asyncio.run(workspace_manager._auto_continue_stopped_task(session, task, sampled))
    retry = asyncio.run(workspace_manager._auto_continue_stopped_task(session, task, sampled))
    assert first is not None and retry is not None
    assert "task-reminder-monitor-reminder-cycle-1-attempt-1" in sent[0]
    assert "task-reminder-monitor-reminder-cycle-1-attempt-1" in sent[1]

    next_session = session.model_copy(
        update={
            "auto_continue_task_id": task.id,
            "auto_continue_attempts": first["auto_continue_attempts"],
            "last_auto_continue_at": sampled,
        }
    )
    later = sampled + timedelta(seconds=30)
    asyncio.run(workspace_manager._auto_continue_stopped_task(next_session, task, later))
    workspace_manager.tasks.pop(task.id, None)

    assert "task-reminder-monitor-reminder-cycle-1-attempt-2" in sent[2]
    assert sent[0] != sent[2]


# ---------------------------------------------------------------------------
# Tiered reviewer history + revision-resume briefing (prompt compaction work)
# ---------------------------------------------------------------------------


def _seed_report(
    task: WorkspaceTask,
    session_id: str,
    state: AgentReportState,
    *,
    idx: int,
    message: str = "m",
    validation_len: int = 2000,
    with_acceptance: bool = True,
) -> None:
    """Append an AgentReport to workspace_manager.reports for ``task``."""
    now = datetime.utcnow()
    report = AgentReport(
        id=f"r-{task.id}-{idx}",
        workspace_id=task.workspace_id,
        task_id=task.id,
        session_id=session_id,
        state=state,
        message=message,
        message_en=message,
        message_zh=message,
        changed_files=[f"backend/file_{idx}.py"] if idx % 2 == 0 else [],
        validation=("subagent-ledger line " * (validation_len // 20)),
        risks="risks " * 200,
        acceptance_check=(
            [
                AcceptanceCheck(
                    criterion=f"c{j}",
                    status=AcceptanceCheckStatus.PASSED,
                    evidence="ev" * 30,
                )
                for j in range(3)
            ]
            if with_acceptance
            else []
        ),
        review_profiles=[ReviewProfile.GENERAL],
        profile_results=[],
        artifact_refs=[f"backend/file_{idx}.py"] if idx % 2 == 0 else [],
        confidence=0.8,
        requires_human_judgment=False,
        review_decision=ReviewDecision.AUTO,
        review_reason=None,
        risk_level="low",
        review_cycle=max(1, idx // 2),
        created_at=now,
    )
    workspace_manager.reports[report.id] = report


def test_review_prompt_tiered_history_bounds_size_on_long_tasks():
    """Reviews for a task with many prior reports must NOT grow linearly."""
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
    task = task.model_copy(update={"id": "t-tier"})
    session = _make_session(session_id="cb-tier-w")
    reviewer = _make_session(session_id="cb-tier-r")
    workspace_manager.sessions[session.id] = session
    workspace_manager.sessions[reviewer.id] = reviewer
    workspace_manager.tasks[task.id] = task

    # Seed 10 verbose prior reports.
    for i in range(10):
        state = AgentReportState.REVIEW_FAILED if i % 2 == 1 else AgentReportState.READY_FOR_REVIEW
        _seed_report(task, session.id if i % 2 == 0 else reviewer.id, state, idx=i)
    trigger_idx = 10
    _seed_report(
        task,
        session.id,
        AgentReportState.READY_FOR_REVIEW,
        idx=trigger_idx,
        message="trigger",
    )
    trigger = workspace_manager.reports[f"r-{task.id}-{trigger_idx}"]

    w = Workspace(
        id=task.workspace_id,
        name="w",
        path="/tmp",
        target=ExecutionTarget.LOCAL,
        default_agent_type=AgentType.CLAUDE,
        default_branch="main",
        session_prefix="cb",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    workspace_manager.workspaces[w.id] = w

    prompt = workspace_manager._build_review_prompt(w, task, reviewer, trigger, lesson_context=[])

    # Trigger and the 3 prior reports must be FULL (include the bulky acceptance_check key).
    # Earlier reports must be SUMMARIZED (acceptance_check_count present, acceptance_check absent).
    assert '"acceptance_check"' in prompt  # at least one full payload
    assert "acceptance_check_count" in prompt  # at least one summary entry
    # Bounded-growth guard: with a 4-report full window the rest are summarized
    # (validation/risks truncated to 240 chars; bulky acceptance_check/profile_results
    # replaced by counts). The 11 verbose reports in this fixture must stay well under
    # a fully-verbose dump (~50k+ chars with these verbose ledger/risks lengths).
    assert len(prompt) < 40_000, f"review prompt grew unbounded: {len(prompt)} chars"

    # Cleanup seeded state.
    for key in list(workspace_manager.reports.keys()):
        if key.startswith(f"r-{task.id}-"):
            del workspace_manager.reports[key]
    workspace_manager.tasks.pop(task.id, None)
    workspace_manager.workspaces.pop(w.id, None)
    workspace_manager.sessions.pop(session.id, None)
    workspace_manager.sessions.pop(reviewer.id, None)


def test_hard_recovery_worker_uses_resume_briefing_after_first_iteration():
    """On iteration>=2 the worker gets a tight resume briefing, not a full assignment replay."""
    now = datetime.utcnow()
    w = Workspace(
        id="ws-resume",
        name="w",
        path="/tmp",
        target=ExecutionTarget.LOCAL,
        default_agent_type=AgentType.CLAUDE,
        default_branch="main",
        session_prefix="cb",
        created_at=now,
        updated_at=now,
    )
    session = _make_session(session_id="cb-resume-w")
    reviewer_session = _make_session(session_id="cb-resume-r")
    # iteration=3 → should hit the resume-briefing branch.
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
    task = task.model_copy(
        update={
            "id": "t-resume",
            "workspace_id": w.id,
            "session_id": session.id,
            "review_cycle": 3,
            "goal_packet": GoalPacket(
                objective="Ship feature X",
                acceptance_criteria=["A works"],
                out_of_scope=["Y", "Z"],
                assumptions=[],
            ),
            "autonomous_run": AutonomousRun(
                id="run-resume",
                task_id="t-resume",
                phase=AutonomousRunPhase.REVISING,
                iteration=3,
            ),
        }
    )
    workspace_manager.workspaces[w.id] = w
    workspace_manager.sessions[session.id] = session
    workspace_manager.sessions[reviewer_session.id] = reviewer_session
    workspace_manager.tasks[task.id] = task
    # Seed one blocking feedback report from a reviewer.
    _seed_report(
        task,
        reviewer_session.id,
        AgentReportState.REVIEW_FAILED,
        idx=1,
        message="Missing validation for edge case E; see tests/test_x.py.",
        validation_len=200,
        with_acceptance=False,
    )

    resume_prompt = workspace_manager._build_hard_recovery_worker_prompt(w, task, session, "err529")

    # Anchors unique to the resume briefing.
    assert "Context refreshed after error" in resume_prompt
    assert "Resume steps:" in resume_prompt
    assert "Approved Goal Packet (compact):" in resume_prompt
    assert "Latest reviewer blocking feedback" in resume_prompt
    # The full "Previously approved Goal Packet JSON" block from cold-start MUST be absent.
    assert "Previously approved Goal Packet JSON" not in resume_prompt

    # iteration=1 → cold-start branch with full JSON.
    task_cold = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
    task_cold = task_cold.model_copy(
        update={
            "id": "t-cold",
            "workspace_id": w.id,
            "session_id": session.id,
            "review_cycle": 1,
            "goal_packet": GoalPacket(
                objective="Ship feature X",
                acceptance_criteria=["A works"],
                out_of_scope=["Y"],
                assumptions=[],
            ),
            "autonomous_run": AutonomousRun(
                id="run-cold",
                task_id="t-cold",
                phase=AutonomousRunPhase.INTAKE,
                iteration=1,
            ),
        }
    )
    workspace_manager.tasks[task_cold.id] = task_cold
    cold_prompt = workspace_manager._build_hard_recovery_worker_prompt(
        w, task_cold, session, "err529"
    )
    assert "Previously approved Goal Packet JSON" in cold_prompt
    assert "Context refreshed after error" not in cold_prompt

    # reviewed+complex (non-autonomous) also stays on cold-start even at review_cycle=3.
    task_rev = _make_task(
        mode=WorkspaceTaskMode.REVIEWED,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
    task_rev = task_rev.model_copy(
        update={
            "id": "t-rev",
            "workspace_id": w.id,
            "session_id": session.id,
            "review_cycle": 3,
            "autonomous_run": None,
        }
    )
    workspace_manager.tasks[task_rev.id] = task_rev
    rev_prompt = workspace_manager._build_hard_recovery_worker_prompt(
        w, task_rev, session, "err529"
    )
    assert "Context refreshed after error" not in rev_prompt
    assert "Resume steps:" not in rev_prompt

    # Cleanup.
    for key in list(workspace_manager.reports.keys()):
        if (
            key.startswith("r-t-resume-")
            or key.startswith("r-t-cold-")
            or key.startswith("r-t-rev-")
        ):
            del workspace_manager.reports[key]
    workspace_manager.tasks.pop(task.id, None)
    workspace_manager.tasks.pop(task_cold.id, None)
    workspace_manager.tasks.pop(task_rev.id, None)
    workspace_manager.workspaces.pop(w.id, None)
    workspace_manager.sessions.pop(session.id, None)
    workspace_manager.sessions.pop(reviewer_session.id, None)


@pytest.mark.parametrize("complexity", list(WorkspaceTaskExecutionComplexity))
@pytest.mark.parametrize("agent_type", [AgentType.CLAUDE, AgentType.CODEX, AgentType.TERMINAL])
def test_generated_task_lifecycle_preserves_strategy_and_independent_gate(
    monkeypatch, tmp_path, complexity, agent_type
):
    """Check the assembled agent-visible lifecycle, including both recovery paths."""
    now = datetime.utcnow()
    workspace = Workspace(
        id="ws-lifecycle",
        name="Lifecycle",
        path=str(tmp_path),
        target=ExecutionTarget.LOCAL,
        default_agent_type=agent_type,
        default_branch="main",
        session_prefix="lc",
        created_at=now,
        updated_at=now,
    )
    session = _make_session().model_copy(
        update={
            "workspace_id": workspace.id,
            "workspace_path": str(tmp_path),
            "agent_type": agent_type,
        }
    )
    reviewer = session.model_copy(update={"id": "independent-reviewer"})
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS, complexity=complexity, agent_type=agent_type
    ).model_copy(
        update={
            "workspace_id": workspace.id,
            "session_id": session.id,
            "goal_packet": GoalPacket(
                objective="Preserve task recovery semantics",
                acceptance_criteria=["Recovery keeps the original task identity"],
                assumptions=[f"Execution strategy: {complexity.value}"],
            ),
        }
    )
    trigger = AgentReport(
        id="worker-handoff",
        workspace_id=workspace.id,
        task_id=task.id,
        session_id=session.id,
        state=AgentReportState.COMPLETED,
        message="Candidate for independent evaluation",
        changed_files=["backend/feature.py"],
        validation="pytest tests/test_feature.py => passed",
        created_at=now,
    )
    monkeypatch.setattr(workspace_manager, "workspaces", {workspace.id: workspace})
    monkeypatch.setattr(workspace_manager, "sessions", {session.id: session, reviewer.id: reviewer})
    monkeypatch.setattr(workspace_manager, "tasks", {task.id: task})
    monkeypatch.setattr(workspace_manager, "reports", {trigger.id: trigger})
    assignment = workspace_manager._build_task_assignment_prompt(
        workspace, task, session, lesson_context=[]
    )
    review = workspace_manager._build_review_prompt(
        workspace, task, reviewer, trigger, lesson_context=[]
    )
    continuation = workspace_manager._build_continue_prompt(task, ContinueTaskRequest(), session)
    cold_recovery = workspace_manager._build_hard_recovery_worker_prompt(
        workspace, task, session, "interrupted"
    )
    task.review_cycle = 2
    resumed = workspace_manager._build_hard_recovery_worker_prompt(
        workspace, task, session, "interrupted"
    )

    assert "evaluator routing is mandatory" in assignment
    assert "independent Hub evaluator" in review
    assert "review_passed for passed work awaiting human acceptance" in review
    assert "choosing serial execution is not itself a defect" in review
    assert "commands, cwd, outcomes and evidence paths" in review
    assert "base/head SHA" in review
    for prompt in (assignment, review, continuation, cold_recovery, resumed):
        assert task.id in prompt
        assert "MUST spawn" not in prompt
        assert "P-EXECUTE and one P-JUDGE actually ran" not in prompt
        assert "Stay in orchestrator mode" not in prompt
        assert "users CANNOT override" not in prompt
        bodies = re.findall(r"-d '([^']+)'", prompt)
        bodies.extend(line for line in prompt.splitlines() if line.startswith('{"task_id"'))
        for body in bodies:
            # Every generated executable example must be valid report JSON,
            # including non-f-string tails concatenated with f-string prefixes.
            report = AgentReportCreate.model_validate(json.loads(body))
            assert report.task_id == task.id
            assert report.call_id
    for prompt in (assignment, cold_recovery, resumed):
        assert "snapshot is a generated navigation aid" in prompt
    for prompt in (continuation, cold_recovery, resumed):
        assert "preserve the recorded simple/complex strategy" in prompt
        assert "independent Hub evaluator remains mandatory" in prompt
    assert "reuse its call_id only for the same report" in resumed
    assert "do not turn ready_for_review into completed" in cold_recovery
    if complexity == WorkspaceTaskExecutionComplexity.SIMPLE:
        assert "implement and run mechanical checks directly" in assignment
        assert "sub-agent is optional" in assignment


def test_subagent_completion_example_preserves_lightweight_evidence_handoff(tmp_path):
    now = datetime.utcnow()
    workspace = Workspace(
        id="ws-subagent",
        name="Subagent",
        path=str(tmp_path),
        target=ExecutionTarget.LOCAL,
        default_agent_type=AgentType.CLAUDE,
        default_branch="main",
        session_prefix="sa",
        created_at=now,
        updated_at=now,
    )
    task = _make_task(
        mode=WorkspaceTaskMode.SUBAGENT, complexity=WorkspaceTaskExecutionComplexity.SIMPLE
    )
    session = _make_session()
    prompt = workspace_manager._build_task_assignment_prompt(
        workspace, task, session, lesson_context=[]
    )
    reports = [
        AgentReportCreate.model_validate(json.loads(body))
        for body in re.findall(r"-d '([^']+)'", prompt)
    ]
    completed = next(report for report in reports if report.state == AgentReportState.COMPLETED)
    assert completed.task_id == task.id
    assert completed.call_id
    assert completed.validation and "base/head" in completed.validation
    assert completed.risks and "unverified" in completed.risks
    assert completed.message_en is None and completed.goal_packet is None
    assert "caller owns acceptance and any required review" in prompt
    assert "ownership conflict" in prompt and "stop_condition" in prompt
    assert "Orchestrator Contract" not in prompt
