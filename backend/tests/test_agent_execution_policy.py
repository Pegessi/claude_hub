from datetime import datetime

import pytest

from claude_hub.models import (
    AgentType,
    WorkspaceTask,
    WorkspaceTaskExecutionComplexity,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)
from claude_hub.services.agent_execution_policy import EXECUTION_POLICY
from claude_hub.services.agent_stream.native import HUB_RUNTIME_GUIDANCE
from claude_hub.services.workspace_manager import workspace_manager


def test_shared_policy_separates_tracking_source_and_execution() -> None:
    assert "one Task per independent goal" in EXECUTION_POLICY
    assert "execution_control=initiator" in EXECUTION_POLICY
    assert "same Task ID" in EXECUTION_POLICY
    assert "native subagents" in EXECUTION_POLICY
    assert "another provider/model" in EXECUTION_POLICY
    assert "Respect explicit provider/model choices" in EXECUTION_POLICY
    assert "never guess available quota" in EXECUTION_POLICY
    assert "new session has new quota" in EXECUTION_POLICY
    assert "routine progress to the Task" in EXECUTION_POLICY
    assert "tmux" in EXECUTION_POLICY


def test_native_guidance_uses_task_cli_without_implied_dispatch() -> None:
    for command in (
        "task context",
        "task register",
        "task progress",
        "task handoff",
        "task dispatch",
    ):
        assert command in HUB_RUNTIME_GUIDANCE
    assert "creates an initiator-controlled record without dispatch" in HUB_RUNTIME_GUIDANCE
    assert "preserves the Task ID" in HUB_RUNTIME_GUIDANCE
    assert "never infer execution control from Task source" in HUB_RUNTIME_GUIDANCE
    assert "whole Task" in HUB_RUNTIME_GUIDANCE
    assert "substep" in HUB_RUNTIME_GUIDANCE
    assert "claude-hub work create" not in HUB_RUNTIME_GUIDANCE
    assert "do not send commands to agent tmux" in HUB_RUNTIME_GUIDANCE


@pytest.mark.parametrize(
    "complexity",
    [WorkspaceTaskExecutionComplexity.SIMPLE, WorkspaceTaskExecutionComplexity.COMPLEX],
)
def test_workspace_assignment_keeps_managed_report_channel(
    complexity: WorkspaceTaskExecutionComplexity,
) -> None:
    now = datetime.utcnow()
    task = WorkspaceTask(
        id="task-1",
        workspace_id="ws-1",
        agent_type=AgentType.CLAUDE,
        title="T",
        prompt="P",
        status=WorkspaceTaskStatus.WORKING,
        task_mode=WorkspaceTaskMode.REVIEWED,
        execution_complexity=complexity,
        created_at=now,
        updated_at=now,
    )
    block = workspace_manager._execution_complexity_assignment_block(task)
    context = workspace_manager._task_context_contract_block()
    assert "already assigned workspace-controlled Task" in context
    assert "managed report endpoint" in context
    assert "do not switch to initiator progress" in context
    assert "Complete small bounded work directly" in block
    assert "no minimum agent count" in block
    assert block.count(EXECUTION_POLICY) == 1


def test_optional_tracker_preserves_owner_and_reporting_authority() -> None:
    assert (
        "For complex, long-running work, consider a progress-tracking subagent" in EXECUTION_POLICY
    )
    assert "coordination benefits justify its overhead" in EXECUTION_POLICY
    assert "same Task reference, concise evidence updates" in EXECUTION_POLICY
    assert (
        "without duplicating execution or declaring completion/release on its own"
        in EXECUTION_POLICY
    )
    assert "Authorize direct Task reporting explicitly" in EXECUTION_POLICY
    assert "otherwise it returns a brief update to the owner" in EXECUTION_POLICY
    assert "Skip unchanged reports" in EXECUTION_POLICY
    assert (
        "avoid repeatedly waking it without new evidence or a requested check" in EXECUTION_POLICY
    )
    assert (
        "owner remains responsible for decisions, evidence integration, and delivery"
        in EXECUTION_POLICY
    )
    assert HUB_RUNTIME_GUIDANCE.count(EXECUTION_POLICY) == 1
    assert "key permits progress, whole-Task completion, and release" in HUB_RUNTIME_GUIDANCE
    assert "A tracking role does not narrow that permission" in HUB_RUNTIME_GUIDANCE
    assert "authorize subagent access explicitly" in HUB_RUNTIME_GUIDANCE
    assert "Do not assume inherited context or permissions" in HUB_RUNTIME_GUIDANCE
    assert "does not complete the whole Task" in HUB_RUNTIME_GUIDANCE


def test_concise_guidance_retains_feedback_and_schedule_boundaries() -> None:
    for command in (
        "feedback context",
        "feedback sources",
        "feedback capture --help",
        "feedback status",
        "feedback configure",
        "lessons get",
        "schedule create --kind hub_task --help",
        "--kind chat_turn",
        "--kind tab_message",
        "schedule list",
        "schedule delete",
    ):
        assert command in HUB_RUNTIME_GUIDANCE
    assert (
        "create one only when the user requests a scheduled follow-up or recurring check"
        in HUB_RUNTIME_GUIDANCE
    )
    assert "queue behind active turns or Goals; they do not interrupt them" in HUB_RUNTIME_GUIDANCE
    assert "plain Terminal sessions, not this Chat" in HUB_RUNTIME_GUIDANCE
    assert "not on every turn" in HUB_RUNTIME_GUIDANCE
    assert "cite the exact quote" in HUB_RUNTIME_GUIDANCE
    assert "Do not invent corrections" in HUB_RUNTIME_GUIDANCE
    assert "use `claude-hub task context` to inspect current context" in HUB_RUNTIME_GUIDANCE
