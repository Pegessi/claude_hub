from datetime import datetime

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


def test_workspace_assignment_keeps_managed_report_channel() -> None:
    now = datetime.utcnow()
    task = WorkspaceTask(
        id="task-1",
        workspace_id="ws-1",
        agent_type=AgentType.CLAUDE,
        title="T",
        prompt="P",
        status=WorkspaceTaskStatus.WORKING,
        task_mode=WorkspaceTaskMode.REVIEWED,
        execution_complexity=WorkspaceTaskExecutionComplexity.SIMPLE,
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
