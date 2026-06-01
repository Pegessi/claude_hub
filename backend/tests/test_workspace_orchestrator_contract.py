"""Prompt-content tests for the Auto Mode orchestrator contract (Phase 1).

These tests exercise the prompt-builder helpers in isolation; they do NOT
spin up the workspace state machine. The intent is to assert the new
orchestrator-contract wording survives future refactors.
"""

from __future__ import annotations

from datetime import datetime

from claude_hub.models import (
    AgentType,
    AutonomousRun,
    AutonomyPolicy,
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
) -> WorkspaceTask:
    now = datetime.utcnow()
    return WorkspaceTask(
        id="t-1",
        workspace_id="ws-1",
        agent_type=AgentType.CLAUDE,
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
        assert "10-15x" in block, f"missing cost multiplier on {level}"
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


def test_autonomous_block_complex_includes_orchestrator_contract_and_examples():
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
    # Two worked examples
    assert "Example 1" in block and "Example 2" in block
    assert "soft-delete" in block.lower() or "/api/orders" in block
    assert "image" in block.lower() and "external:t2i.v3" in block
    # Envelope schema
    assert "subtask-envelope" in block
    assert "return_mode: final-only" in block
    # Ledger schema
    assert "subagent-ledger" in block
    # Model pinning
    assert "P-PLAN, P-EXECUTE, P-JUDGE, P-INTEGRATE -> opus" in block
    # Hard enforcement on complex
    assert "REQUIRED" in block
    # Per-CLI hint embedded
    assert "claude runtime" in block


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
