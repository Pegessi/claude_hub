"""Backward-compatible facade for Claude Hub's domain schemas."""

# ruff: noqa: F401

from claude_hub.models.schema_domains.boards import (
    BoardTasksPagination,
    WorkspaceBoard,
    redact_workspace_board_for_public,
)
from claude_hub.models.schema_domains.evaluation import (
    AcceptanceCheck,
    AcceptanceCheckStatus,
    AutonomousIteration,
    AutonomousRun,
    AutonomyPolicy,
    CriterionResult,
    EvaluationReport,
    GoalPacket,
    ReviewProfileResult,
    RubricCriterion,
)
from claude_hub.models.schema_domains.execution import (
    TaskExecutionControl,
    TaskExecutionHandoffRequest,
    TaskExecutionReference,
    TaskManualProgressRequest,
    TaskProgressRequest,
    TaskProgressSnapshot,
    TaskProgressState,
    TaskRuntimeObservation,
    TaskSourceReference,
    validate_workspace_session_surface,
)
from claude_hub.models.schema_domains.feedback import (
    FeedbackFailureEvidence,
    FeedbackLesson,
    FeedbackLessonCreate,
    FeedbackLessonDraft,
    FeedbackLessonDraftCreate,
    FeedbackProcessedTaskRecord,
    FeedbackReaperRequest,
    FeedbackReaperRun,
    FeedbackRecord,
    FeedbackSummaryRequest,
    FeedbackSummaryRun,
    FeedbackTaskDigest,
)
from claude_hub.models.schema_domains.quota import (
    ProviderQuota,
    QuotaLimit,
    QuotaResponse,
)
from claude_hub.models.schema_domains.reports import (
    AgentReport,
    AgentReportCreate,
    StreamAttachmentPreview,
    WorkspaceArtifactPreview,
    WorkspaceMarkdownDocument,
    WorkspaceMarkdownDocumentSource,
)
from claude_hub.models.schema_domains.requests import (
    ContinueTaskRequest,
    DispatchDecisionRequest,
    EnsureWorkspaceAgentRequest,
    ManualTaskControlRequest,
    RequestTaskReviewRequest,
    RetryUncertainDeliveryRequest,
    SendSessionMessageRequest,
    SpawnWorkerRequest,
    StartTaskRequest,
    TaskCleanupResult,
    TaskFollowupRequest,
    TaskMailboxAckRequest,
)
from claude_hub.models.schema_domains.runtime import (
    AGENT_TAG_MAX_LENGTH,
    CHAT_REMOTE_UNSUPPORTED,
    NONINTERACTIVE_REMOTE_UNSUPPORTED,
    STDIN_SHELL_REMOTE_UNSUPPORTED,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    AutonomousRunPhase,
    ChatMode,
    EvaluationDecision,
    EvaluationStrictness,
    ExecutionTarget,
    FeedbackLessonScope,
    FeedbackLessonStatus,
    FeedbackSourceType,
    FeedbackSummaryMode,
    GoalPacketStatus,
    HumanCheckpointPolicy,
    ManagedSessionStatus,
    RemoteTransport,
    ReviewDecision,
    ReviewProfile,
    ReviewProfileResultStatus,
    SessionKind,
    StreamCapabilities,
    StreamModelOption,
    StreamModeOption,
    StreamReasoningEffortOption,
    WorkspaceSessionRole,
    WorkspaceTaskExecutionComplexity,
    WorkspaceTaskMode,
    WorkspaceTaskOrigin,
    WorkspaceTaskStatus,
    normalize_agent_tag,
)
from claude_hub.models.schema_domains.schedules import (
    ScheduledTask,
    ScheduledTaskCreate,
    ScheduledTaskKind,
    ScheduledTaskRun,
    ScheduledTaskRunResult,
    ScheduledTaskRunStatus,
    ScheduledTaskUpdate,
    ScheduledTaskView,
)
from claude_hub.models.schema_domains.sessions import (
    PUBLIC_REDACTED_ENV_VALUE,
    ManagedSession,
    ManagedSessionPublic,
    redact_env_for_public_response,
    redact_managed_session_env,
    redact_managed_sessions_for_public,
    redact_session_json_payload,
)
from claude_hub.models.schema_domains.system import (
    DirectoryListing,
    EnvPreset,
    EnvPresetBulkImport,
    EnvPresetCreate,
    EnvPresetHiddenRequest,
    EnvPresetsResponse,
    EnvPresetUpdate,
    FileInfo,
    LoginSession,
    User,
)
from claude_hub.models.schema_domains.tasks import (
    TaskExecutionCapabilities,
    TaskExecutionMutationResult,
    WorkspaceAttachment,
    WorkspaceAttachmentCreate,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskUpdate,
)
from claude_hub.models.schema_domains.terminal import (
    RemoteProfile,
    ResidentPeriodicTask,
    SwitchEnvRequest,
    TerminalAgentStatus,
    TerminalTab,
    TerminalTabBase,
    TerminalTabCreate,
    TerminalTabUpdate,
)
from claude_hub.models.schema_domains.workspaces import (
    Workspace,
    WorkspaceCreate,
    WorkspaceEnsure,
    WorkspaceUpdate,
)
from claude_hub.models.task_mailbox import TaskEvent
