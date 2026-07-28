"""Prompt-content tests for the Auto Mode orchestrator contract (Phase 1).

These tests exercise the prompt-builder helpers in isolation; they do NOT
spin up the workspace state machine. The intent is to assert the new
orchestrator-contract wording survives future refactors.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from claude_hub.models import (
    AcceptanceCheck,
    AcceptanceCheckStatus,
    AgentReport,
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
        autonomy_policy=AutonomyPolicy() if mode == WorkspaceTaskMode.AUTONOMOUS else None,
        autonomous_run=(
            AutonomousRun(id="run-t-1", task_id="t-1")
            if mode == WorkspaceTaskMode.AUTONOMOUS
            else None
        ),
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# _execution_complexity_assignment_block
# ---------------------------------------------------------------------------


def test_complexity_block_includes_cost_guardrail_for_all_levels():
    for level in WorkspaceTaskExecutionComplexity:
        task = _make_task(mode=WorkspaceTaskMode.AUTONOMOUS, complexity=level)
        block = workspace_manager._execution_complexity_assignment_block(task)
        assert "expensive" in block, f"missing cost anchor on {level}"
        assert "breadth-first parallel" in block
        assert "cleanly isolated" in block


def test_complexity_block_complex_demands_orchestrator_mode():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
    block = workspace_manager._execution_complexity_assignment_block(task)
    assert "orchestrator" in block.lower()


def test_complexity_block_simple_does_not_force_orchestrator():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.SIMPLE,
    )
    block = workspace_manager._execution_complexity_assignment_block(task)
    # Soft guidance, not a hard mandate
    assert "Execute directly" in block


# ---------------------------------------------------------------------------
# _subagent_capability_hint
# ---------------------------------------------------------------------------


def test_capability_hint_claude_mentions_task_tool_and_model_param():
    hint = workspace_manager._subagent_capability_hint(AgentType.CLAUDE)
    assert "Task tool" in hint
    assert "subagent_type" in hint
    assert "model" in hint


def test_capability_hint_cursor_acknowledges_version_dependence():
    hint = workspace_manager._subagent_capability_hint(AgentType.CURSOR)
    assert "cursor" in hint.lower()
    # Version pinning caveat preserved (shorter wording):
    assert "version" in hint or "unsupported" in hint


def test_capability_hint_codex_acknowledges_version_dependence():
    hint = workspace_manager._subagent_capability_hint(AgentType.CODEX)
    assert "codex" in hint.lower()
    assert "version" in hint or "unsupported" in hint


def test_capability_hint_terminal_degrades_gracefully():
    hint = workspace_manager._subagent_capability_hint(AgentType.TERMINAL)
    assert "Degrade" in hint or "degrade" in hint
    assert "Do NOT fabricate" in hint


# ---------------------------------------------------------------------------
# _autonomous_assignment_block
# ---------------------------------------------------------------------------


def test_autonomous_block_empty_for_non_autonomous():
    task = _make_task(
        mode=WorkspaceTaskMode.REVIEWED,
        complexity=WorkspaceTaskExecutionComplexity.AUTO,
    )
    assert workspace_manager._autonomous_assignment_block(task) == ""


def test_autonomous_block_complex_includes_orchestrator_contract_and_primitives():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
    block = workspace_manager._autonomous_assignment_block(task, AgentType.CLAUDE)
    # Header still there
    assert "Autonomous Mode V1 is enabled" in block
    # Contract keywords
    assert "Orchestrator Contract" in block
    assert "P-PLAN" in block and "P-EXECUTE" in block and "P-JUDGE" in block
    assert "P-INTEGRATE" in block and "P-VALIDATE" in block and "P-RESEARCH" in block
    # External-API model marker preserved
    assert "external:<api>" in block or "external:api" in block
    # Envelope schema
    assert "subtask-envelope" in block
    assert "final-only" in block
    # Ledger schema
    assert "subagent-ledger" in block
    # Model pinning (slash-separated compact form)
    assert (
        "P-PLAN/P-EXECUTE/P-JUDGE/P-INTEGRATE -> opus" in block
        or "P-PLAN, P-EXECUTE, P-JUDGE" in block
    )
    # Hard enforcement on complex
    assert "REQUIRED" in block
    # Opaque delegated/external work must remain observable.
    assert "Observability" in block or "observability" in block
    assert "heartbeat" in block
    assert "role.id" in block
    assert "contract violation" in block
    # Per-CLI hint embedded
    assert "claude runtime" in block


def test_autonomous_block_forbids_bare_blocked_or_needs_input_reports():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
    block = workspace_manager._autonomous_assignment_block(task, AgentType.CLAUDE)
    # Observability rule preserved (shorter wording):
    assert "blocked/needs_input" in block or "blocked or needs_input" in block
    assert "no autonomous next action remains" in block or "no autonomous step is" in block
    assert "name the blocker" in block
    assert "contract violation" in block
    assert "needs your response" in block


def test_autonomous_block_simple_softens_enforcement():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.SIMPLE,
    )
    block = workspace_manager._autonomous_assignment_block(task, AgentType.CLAUDE)
    assert "Enforcement (simple)" in block
    # Even simple tasks must spawn one P-JUDGE pre-flight
    assert "P-JUDGE" in block


def test_autonomous_block_auto_demands_explicit_mode_choice():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.AUTO,
    )
    block = workspace_manager._autonomous_assignment_block(task, AgentType.CLAUDE)
    assert "Enforcement (auto)" in block
    assert "goal_packet.assumptions" in block


def test_autonomous_block_swaps_capability_hint_per_runtime():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
    claude_block = workspace_manager._autonomous_assignment_block(task, AgentType.CLAUDE)
    cursor_block = workspace_manager._autonomous_assignment_block(task, AgentType.CURSOR)
    terminal_block = workspace_manager._autonomous_assignment_block(task, AgentType.TERMINAL)
    assert "claude runtime" in claude_block
    assert "cursor runtime" in cursor_block
    assert "no native sub-agent capability" in terminal_block


def test_autonomous_block_codex_does_not_require_claude_model_pinning():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
        agent_type=AgentType.CODEX,
    )
    block = workspace_manager._autonomous_assignment_block(task, AgentType.CODEX)
    assert "Claude opus/sonnet pinning is NOT required" in block
    assert "runtime-default" in block
    assert "model_or_api" in block


# ---------------------------------------------------------------------------
# _autonomous_review_block
# ---------------------------------------------------------------------------


def test_review_block_empty_for_non_autonomous():
    task = _make_task(
        mode=WorkspaceTaskMode.DIRECT,
        complexity=WorkspaceTaskExecutionComplexity.AUTO,
    )
    assert workspace_manager._autonomous_review_block(task) == ""


def test_review_block_demands_ledger_verification():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
    block = workspace_manager._autonomous_review_block(task)
    assert "Subagent ledger verification" in block
    assert "subagent-ledger" in block
    # Model pinning rule preserved (slash-separated compact form):
    assert (
        "P-PLAN/P-EXECUTE/P-JUDGE/P-INTEGRATE" in block
        or "P-PLAN, P-EXECUTE, P-JUDGE, and P-INTEGRATE" in block
    )
    assert "external:" in block or "external:<api>" in block
    # Specific guidance to fail when ledger is missing
    assert "review_failed" in block


def test_review_block_codex_model_pinning_is_runtime_aware():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
        agent_type=AgentType.CODEX,
    )
    block = workspace_manager._autonomous_review_block(task)
    # Runtime is labelled (compact form: "worker runtime: codex" on the Run line):
    assert "codex" in block
    assert "Do NOT fail solely because Claude opus/sonnet pinning is absent" in block
    assert "runtime-default" in block


# ---------------------------------------------------------------------------
# _autonomous_continue_orchestrator_reminder
# ---------------------------------------------------------------------------


def test_continue_reminder_present_for_autonomous():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
    reminder = workspace_manager._autonomous_continue_orchestrator_reminder(task)
    assert "Orchestrator-mode reminder" in reminder
    assert "P-EXECUTE" in reminder and "P-VALIDATE" in reminder and "P-JUDGE" in reminder
    assert "do not restart" in reminder.lower()


def test_continue_reminder_absent_for_non_autonomous():
    task = _make_task(
        mode=WorkspaceTaskMode.REVIEWED,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
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
        workspace_manager, "_auto_continue_interruption_reason", lambda _o: "interrupted"
    )
    asyncio.run(workspace_manager._auto_continue_stopped_task(session, task, sampled))

    # Branch 2: no interruption, completion detected -> AUTO_REPORT_MISSING_MESSAGE.
    monkeypatch.setattr(workspace_manager, "_auto_continue_interruption_reason", lambda _o: None)
    monkeypatch.setattr(workspace_manager, "_auto_continue_completion_reason", lambda _o: "done")
    fresh_session = workspace_manager.sessions.get(session.id, session)
    asyncio.run(workspace_manager._auto_continue_stopped_task(fresh_session, task, sampled))

    workspace_manager.tasks.pop(task.id, None)

    assert len(sent) == 2, "both auto-continue branches should send a nudge"
    for message in sent:
        assert endpoint in message, "auto-continue nudge must restate report endpoint"
        assert f"{INTERNAL_API_CURL} -X POST" in message
        assert '"task_id":"task-7"' in message
    assert monitor_module.AUTO_CONTINUE_MESSAGE.split("\n")[0] in sent[0]
    assert monitor_module.AUTO_REPORT_MISSING_MESSAGE.split("\n")[0] in sent[1]


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
