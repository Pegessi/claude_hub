"""Compatibility contracts for the schema and CLI domain splits."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Dict, Tuple

import click
from pydantic import BaseModel

from claude_hub.cli import feishu_cards
from claude_hub.cli.commands import tasks as task_facade
from claude_hub.cli.feishu_cards_impl import contract as card_contract
from claude_hub.cli.feishu_cards_impl import interactive as interactive_cards
from claude_hub.cli.feishu_cards_impl import inventory as inventory_cards
from claude_hub.cli.feishu_cards_impl import simple as simple_cards
from claude_hub.cli.feishu_cards_impl import workspace as workspace_cards
from claude_hub.cli.task_commands import creation as task_creation
from claude_hub.cli.task_commands import execution as task_execution
from claude_hub.cli.task_commands import feedback as task_feedback
from claude_hub.cli.task_commands import lifecycle as task_lifecycle
from claude_hub.cli.task_commands import observation as task_observation
from claude_hub.cli.task_commands import read as task_read
from claude_hub.models import schemas
from claude_hub.models.schemas import TaskEvent as FacadeTaskEvent
from claude_hub.models.task_mailbox import TaskEvent

SCHEMA_MODULE_CLASSES: Dict[str, Tuple[str, ...]] = {
    "boards": ("BoardTasksPagination", "WorkspaceBoard"),
    "evaluation": (
        "AcceptanceCheckStatus",
        "GoalPacket",
        "AcceptanceCheck",
        "ReviewProfileResult",
        "AutonomyPolicy",
        "RubricCriterion",
        "CriterionResult",
        "EvaluationReport",
        "AutonomousIteration",
        "AutonomousRun",
    ),
    "execution": (
        "TaskExecutionControl",
        "TaskProgressState",
        "TaskSourceReference",
        "TaskExecutionReference",
        "TaskProgressSnapshot",
        "TaskRuntimeObservation",
        "TaskProgressRequest",
        "TaskManualProgressRequest",
        "TaskExecutionHandoffRequest",
    ),
    "feedback": (
        "FeedbackLessonDraftCreate",
        "FeedbackReaperRequest",
        "FeedbackSummaryRequest",
        "FeedbackRecord",
        "FeedbackLessonDraft",
        "FeedbackLessonCreate",
        "FeedbackLesson",
        "FeedbackReaperRun",
        "FeedbackFailureEvidence",
        "FeedbackTaskDigest",
        "FeedbackProcessedTaskRecord",
        "FeedbackSummaryRun",
    ),
    "quota": ("QuotaLimit", "ProviderQuota", "QuotaResponse"),
    "reports": (
        "AgentReportCreate",
        "AgentReport",
        "WorkspaceArtifactPreview",
        "WorkspaceMarkdownDocumentSource",
        "WorkspaceMarkdownDocument",
    ),
    "requests": (
        "SpawnWorkerRequest",
        "EnsureWorkspaceAgentRequest",
        "TaskCleanupResult",
        "StartTaskRequest",
        "ContinueTaskRequest",
        "TaskFollowupRequest",
        "TaskMailboxAckRequest",
        "RequestTaskReviewRequest",
        "ManualTaskControlRequest",
        "DispatchDecisionRequest",
        "SendSessionMessageRequest",
        "RetryUncertainDeliveryRequest",
    ),
    "runtime": (
        "AgentType",
        "SessionKind",
        "RemoteTransport",
        "ChatMode",
        "StreamModeOption",
        "StreamReasoningEffortOption",
        "StreamModelOption",
        "StreamCapabilities",
        "ExecutionTarget",
        "AgentRuntimeStatus",
        "WorkspaceSessionRole",
        "WorkspaceTaskStatus",
        "WorkspaceTaskMode",
        "WorkspaceTaskExecutionComplexity",
        "WorkspaceTaskOrigin",
        "EvaluationStrictness",
        "HumanCheckpointPolicy",
        "AutonomousRunPhase",
        "EvaluationDecision",
        "ManagedSessionStatus",
        "AgentReportState",
        "ReviewDecision",
        "FeedbackSourceType",
        "FeedbackLessonScope",
        "FeedbackLessonStatus",
        "FeedbackSummaryMode",
        "ReviewProfile",
        "ReviewProfileResultStatus",
        "GoalPacketStatus",
    ),
    "schedules": (
        "ScheduledTaskKind",
        "ScheduledTaskRunStatus",
        "ScheduledTaskRun",
        "ScheduledTask",
        "ScheduledTaskCreate",
        "ScheduledTaskUpdate",
        "ScheduledTaskRunResult",
        "ScheduledTaskView",
    ),
    "sessions": ("ManagedSession", "ManagedSessionPublic"),
    "system": (
        "User",
        "LoginSession",
        "DirectoryListing",
        "FileInfo",
        "EnvPreset",
        "EnvPresetCreate",
        "EnvPresetUpdate",
        "EnvPresetHiddenRequest",
        "EnvPresetBulkImport",
        "EnvPresetsResponse",
    ),
    "tasks": (
        "WorkspaceTaskCreate",
        "WorkspaceAttachmentCreate",
        "WorkspaceAttachment",
        "WorkspaceTaskUpdate",
        "WorkspaceTask",
        "TaskExecutionMutationResult",
        "TaskExecutionCapabilities",
    ),
    "terminal": (
        "TerminalTabBase",
        "TerminalTabCreate",
        "SwitchEnvRequest",
        "TerminalTabUpdate",
        "TerminalTab",
        "TerminalAgentStatus",
        "RemoteProfile",
        "ResidentPeriodicTask",
    ),
    "workspaces": (
        "WorkspaceCreate",
        "WorkspaceEnsure",
        "Workspace",
        "WorkspaceUpdate",
    ),
}

EXPECTED_TASK_COMMANDS = (
    "list",
    "get",
    "status",
    "report",
    "review",
    "create",
    "run",
    "start",
    "continue",
    "update",
    "accept",
    "cleanup",
    "tree",
    "events",
    "wait",
    "ack",
    "followup",
    "send",
    "abort",
    "request-review",
    "delete",
    "spawn",
    "dispatch-decision",
    "feedback",
    "register",
    "progress",
    "handoff",
    "dispatch",
    "context",
)


def test_schema_facade_preserves_task_event_import_identity() -> None:
    assert FacadeTaskEvent is TaskEvent
    assert schemas.TaskEvent is TaskEvent


def test_schema_facade_reexports_real_domain_classes_with_stable_schema_names() -> None:
    for module_name, class_names in SCHEMA_MODULE_CLASSES.items():
        module = importlib.import_module(f"claude_hub.models.schema_domains.{module_name}")
        for class_name in class_names:
            domain_class = getattr(module, class_name)
            assert getattr(schemas, class_name) is domain_class
            assert domain_class.__module__ == module.__name__
            if isinstance(domain_class, type) and issubclass(domain_class, BaseModel):
                assert domain_class.model_json_schema()["title"] == class_name


def test_task_facade_preserves_single_click_group_and_registration_order() -> None:
    assert isinstance(task_facade.task, click.Group)
    assert tuple(task_facade.task.commands) == EXPECTED_TASK_COMMANDS
    assert task_facade.task_list is task_read.task_list
    assert task_facade.task_create is task_creation.task_create
    assert task_facade.task_start is task_lifecycle.task_start
    assert task_facade.task_wait is task_observation.task_wait
    assert task_facade.task_feedback_reap is task_feedback.task_feedback_reap
    assert task_facade.task_register is task_execution.task_register
    assert task_facade._find_task_board is task_read._find_task_board
    assert task_facade._latest_acceptance_report is task_read._latest_acceptance_report


def test_feishu_card_facade_reexports_contract_and_builders() -> None:
    assert feishu_cards.parse_card_action is card_contract.parse_card_action
    assert feishu_cards.build_approval_card is interactive_cards.build_approval_card
    assert feishu_cards.build_status_card is inventory_cards.build_status_card
    assert feishu_cards.build_task_detail_card is workspace_cards.build_task_detail_card
    assert feishu_cards.build_terminal_card is simple_cards.build_terminal_card
    assert feishu_cards.CARD_KINDS == card_contract.CARD_KINDS


def test_domain_implementations_do_not_import_their_compatibility_facades() -> None:
    backend_root = Path(__file__).parents[1] / "claude_hub"
    forbidden_imports = {
        backend_root
        / "models"
        / "schema_domains": (
            "claude_hub.models.schemas",
            "from claude_hub.models import",
        ),
        backend_root / "cli" / "task_commands": ("claude_hub.cli.commands.tasks",),
        backend_root / "cli" / "feishu_cards_impl": ("from claude_hub.cli.feishu_cards import",),
    }
    violations = []
    for directory, forbidden in forbidden_imports.items():
        for path in directory.glob("*.py"):
            source = path.read_text()
            for import_path in forbidden:
                if import_path in source:
                    violations.append(f"{path}: {import_path}")
    assert violations == []


def test_restructured_production_modules_stay_below_legacy_size_limit() -> None:
    backend_root = Path(__file__).parents[1] / "claude_hub"
    paths = [
        backend_root / "models" / "schemas.py",
        backend_root / "cli" / "commands" / "tasks.py",
        backend_root / "cli" / "feishu_cards.py",
    ]
    for directory in (
        backend_root / "models" / "schema_domains",
        backend_root / "cli" / "task_commands",
        backend_root / "cli" / "feishu_cards_impl",
    ):
        paths.extend(directory.glob("*.py"))

    oversized = {
        str(path): len(path.read_text().splitlines())
        for path in paths
        if len(path.read_text().splitlines()) > 800
    }
    assert oversized == {}
