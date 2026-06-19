"""Prompt-content tests for the Auto Mode orchestrator contract (Phase 1).

These tests exercise the prompt-builder helpers in isolation; they do NOT
spin up the workspace state machine. The intent is to assert the new
orchestrator-contract wording survives future refactors.
"""

from __future__ import annotations

from datetime import datetime

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    AutonomousRun,
    AutonomyPolicy,
    ContinueTaskRequest,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskExecutionComplexity,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)
from claude_hub.services.workspace_manager import workspace_manager


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
    assert "version-dependent" in hint


def test_capability_hint_codex_acknowledges_version_dependence():
    hint = workspace_manager._subagent_capability_hint(AgentType.CODEX)
    assert "codex" in hint.lower()
    assert "version-dependent" in hint


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


def test_autonomous_block_complex_includes_orchestrator_contract_and_skeleton():
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
    # Compact skeleton (shape, not a verbatim template)
    assert "Compact skeleton" in block
    assert "soft-delete" in block.lower() or "/api/orders" in block
    assert "external:<api>" in block
    # Envelope schema
    assert "subtask-envelope" in block
    assert "return_mode: final-only" in block
    # Ledger schema
    assert "subagent-ledger" in block
    # Model pinning
    assert "P-PLAN, P-EXECUTE, P-JUDGE, P-INTEGRATE -> opus" in block
    # Hard enforcement on complex
    assert "REQUIRED" in block
    # Opaque delegated/external work must remain observable.
    assert "Orchestrator observability requirements" in block
    assert "working heartbeat" in block
    assert "role.id" in block and "elapsed time" in block
    assert "image/API job" in block
    assert "Bare placeholders" in block and "contract violations" in block
    # Per-CLI hint embedded
    assert "claude runtime" in block


def test_autonomous_block_forbids_bare_blocked_or_needs_input_reports():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
    )
    block = workspace_manager._autonomous_assignment_block(task, AgentType.CLAUDE)
    assert (
        "blocked or needs_input report is allowed only when no autonomous next action remains"
        in block
    )
    assert "name the blocker" in block
    assert "include evidence for the blocker" in block
    assert "next action already attempted or ruled out" in block
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
    assert "P-PLAN, P-EXECUTE, P-JUDGE, and P-INTEGRATE" in block
    assert "external:" in block
    # Specific guidance to fail when ledger is missing
    assert "review_failed" in block


def test_review_block_codex_model_pinning_is_runtime_aware():
    task = _make_task(
        mode=WorkspaceTaskMode.AUTONOMOUS,
        complexity=WorkspaceTaskExecutionComplexity.COMPLEX,
        agent_type=AgentType.CODEX,
    )
    block = workspace_manager._autonomous_review_block(task)
    assert "Worker runtime: codex" in block
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
    assert "curl -sS -X POST" in snippet
    assert _endpoint_path(session) in snippet
    assert '"task_id":"task-42"' in snippet
    # Defaults to a placeholder when no task id is supplied.
    assert '"task_id":"TASK_ID"' in workspace_manager._report_endpoint_curl(session)


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
    assert "curl -sS -X POST" in prompt
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
        assert "curl -sS -X POST" in message
        assert '"task_id":"task-7"' in message
    assert monitor_module.AUTO_CONTINUE_MESSAGE.split("\n")[0] in sent[0]
    assert monitor_module.AUTO_REPORT_MISSING_MESSAGE.split("\n")[0] in sent[1]
