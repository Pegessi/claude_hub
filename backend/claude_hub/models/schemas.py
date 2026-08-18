import uuid
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class AgentType(str, Enum):
    """Type of agent to run in the terminal."""

    CLAUDE = "claude"
    CODEX = "codex"
    CURSOR = "cursor"
    TERMINAL = "terminal"


class ExecutionTarget(str, Enum):
    """Where the terminal command should run."""

    LOCAL = "local"
    REMOTE = "remote"


class AgentRuntimeStatus(str, Enum):
    """Best-effort runtime status for a terminal agent."""

    IDLE = "idle"
    WORKING = "working"
    ATTENTION = "attention"
    OFFLINE = "offline"


class WorkspaceSessionRole(str, Enum):
    """Role of a managed workspace session."""

    WORKER = "worker"
    ORCHESTRATOR = "orchestrator"
    REVIEWER = "reviewer"
    DISPATCHER = "dispatcher"
    RESIDENT = "resident"


class WorkspaceTaskStatus(str, Enum):
    """Status of a human-orchestrated workspace task."""

    TODO = "todo"
    QUEUED = "queued"
    WORKING = "working"
    REVIEW = "review"
    DONE = "done"


class WorkspaceTaskMode(str, Enum):
    """Automation mode for a workspace task."""

    DIRECT = "direct"
    REVIEWED = "reviewed"
    AUTONOMOUS = "autonomous"


class WorkspaceTaskExecutionComplexity(str, Enum):
    """Execution complexity hint for a workspace task."""

    AUTO = "auto"
    SIMPLE = "simple"
    COMPLEX = "complex"


class WorkspaceTaskOrigin(str, Enum):
    """Who created a workspace task.

    ``human`` (default) is a task created via the frontend / API by a person.
    ``resident`` is a task created by the workspace's resident self-driven agent
    (read-only proposal mode or master-mode orchestration). The value is
    self-declared by the resident in its create payload — the ``POST /tasks``
    endpoint sees only the authenticated user, not the calling agent session —
    so this is a display hint, not a backend-enforced ownership guarantee.
    """

    HUMAN = "human"
    RESIDENT = "resident"


class EvaluationStrictness(str, Enum):
    """How strict autonomous evaluation should be."""

    LENIENT = "lenient"
    BALANCED = "balanced"
    STRICT = "strict"


class HumanCheckpointPolicy(str, Enum):
    """When an autonomous run should stop for human input."""

    FINAL_ONLY = "final_only"
    AFTER_RUBRIC = "after_rubric"
    EVERY_ITERATION = "every_iteration"


class AutonomousRunPhase(str, Enum):
    """Fine-grained state for an autonomous task run."""

    INTAKE = "intake"
    RUBRIC_RESEARCH = "rubric_research"
    PLANNING = "planning"
    DISPATCHING = "dispatching"
    WORKING = "working"
    EVALUATING = "evaluating"
    REVISING = "revising"
    WAITING_FOR_HUMAN = "waiting_for_human"
    PASSED = "passed"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"


class EvaluationDecision(str, Enum):
    """Evaluator decision for one autonomous iteration."""

    PASS = "pass"
    REVISE = "revise"
    NEEDS_INPUT = "needs_input"
    FAIL = "fail"
    ESCALATE = "escalate"


class ManagedSessionStatus(str, Enum):
    """Lifecycle status for a managed workspace session."""

    SPAWNING = "spawning"
    WORKING = "working"
    IDLE = "idle"
    NEEDS_INPUT = "needs_input"
    DONE = "done"
    STOPPED = "stopped"
    ERROR = "error"


class AgentReportState(str, Enum):
    """Self-reported workflow state from a managed worker session."""

    STARTED = "started"
    WORKING = "working"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"
    READY_FOR_REVIEW = "ready_for_review"
    COMPLETED = "completed"
    REVIEW_STARTED = "review_started"
    REVIEW_PASSED = "review_passed"
    REVIEW_FAILED = "review_failed"
    REVIEW_NEEDS_INPUT = "review_needs_input"


class ReviewDecision(str, Enum):
    """Agent's requested reviewer routing for a report."""

    AUTO = "auto"
    REQUEST = "request"
    SKIP = "skip"


class FeedbackSourceType(str, Enum):
    """Where a feedback record came from."""

    AGENT = "agent"
    REVIEWER = "reviewer"
    HUMAN = "human"
    RUNTIME = "runtime"
    MANUAL = "manual"
    SYSTEM = "system"


class FeedbackLessonScope(str, Enum):
    """Visibility scope for a reusable lesson."""

    WORKSPACE = "workspace"
    FAMILY = "family"
    GLOBAL = "global"


class FeedbackLessonStatus(str, Enum):
    """Lifecycle state for a reusable lesson."""

    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class FeedbackSummaryMode(str, Enum):
    """How much workspace history an internal feedback summary should scan."""

    INCREMENTAL = "incremental"
    FULL = "full"


class ReviewProfile(str, Enum):
    """Review lens used by reviewer agents."""

    GENERAL = "general"
    CODE = "code"
    UI = "ui"
    ARTIFACT = "artifact"
    DELIVERY = "delivery"
    BOUNDARY = "boundary"


class ReviewProfileResultStatus(str, Enum):
    """Reviewer result for one review profile."""

    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"
    NOT_CHECKED = "not_checked"


class GoalPacketStatus(str, Enum):
    """Lifecycle status for a task-level Goal Packet."""

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FROZEN = "frozen"
    SUPERSEDED = "superseded"


class AcceptanceCheckStatus(str, Enum):
    """Agent-reported result for one acceptance criterion."""

    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"
    NOT_CHECKED = "not_checked"


class GoalPacket(BaseModel):
    """Structured task intent and review criteria generated by the worker."""

    objective: str
    acceptance_criteria: List[str] = Field(default_factory=list)
    validation_plan: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    out_of_scope: List[str] = Field(default_factory=list)
    handoff_requirements: List[str] = Field(default_factory=list)
    source: str = "agent_generated"
    status: GoalPacketStatus = GoalPacketStatus.DRAFT
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AcceptanceCheck(BaseModel):
    """Evidence that one Goal Packet criterion was checked."""

    criterion: str
    status: AcceptanceCheckStatus
    evidence: str


class ReviewProfileResult(BaseModel):
    """Reviewer evidence for one enabled review profile."""

    profile: ReviewProfile
    status: ReviewProfileResultStatus = ReviewProfileResultStatus.NOT_CHECKED
    evidence: str = ""
    blocking_findings: List[str] = Field(default_factory=list)
    non_blocking_findings: List[str] = Field(default_factory=list)


class AutonomyPolicy(BaseModel):
    """Task-level configuration for Autonomous Mode."""

    max_iterations: int = 3
    evaluation_strictness: EvaluationStrictness = EvaluationStrictness.BALANCED
    allow_web_research: bool = False
    require_artifact_review: bool = False
    review_profiles: List[ReviewProfile] = Field(default_factory=list)
    human_checkpoint_policy: HumanCheckpointPolicy = HumanCheckpointPolicy.FINAL_ONLY
    allowed_agent_types: List[AgentType] = Field(default_factory=list)
    stop_on_repeated_failure: bool = True


class RubricCriterion(BaseModel):
    """One criterion used by an autonomous evaluator."""

    id: str
    name: str
    description: str = ""
    weight: float = 1.0
    pass_condition: str = ""
    evaluation_method: str = ""
    blocking_threshold: Optional[float] = None


class CriterionResult(BaseModel):
    """Evaluator result for one rubric or acceptance criterion."""

    criterion_id: Optional[str] = None
    criterion: str
    score: Optional[float] = None
    passed: Optional[bool] = None
    evidence: str = ""


class EvaluationReport(BaseModel):
    """Structured autonomous evaluator output."""

    id: str
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    iteration: int = 1
    evaluator_session_id: Optional[str] = None
    overall_score: Optional[float] = None
    decision: EvaluationDecision
    criterion_results: List[CriterionResult] = Field(default_factory=list)
    profile_results: List[ReviewProfileResult] = Field(default_factory=list)
    blocking_issues: List[str] = Field(default_factory=list)
    suggested_fixes: List[str] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    validation_reviewed: Optional[str] = None
    risks: Optional[str] = None
    confidence: Optional[float] = None
    requires_human_judgment: bool = False
    created_at: Optional[datetime] = None


class AutonomousIteration(BaseModel):
    """Audit trail for one autonomous loop iteration."""

    iteration: int
    worker_session_id: Optional[str] = None
    evaluator_session_id: Optional[str] = None
    worker_report_id: Optional[str] = None
    evaluation_report_id: Optional[str] = None
    revision_prompt: Optional[str] = None
    controller_decision: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class AutonomousRun(BaseModel):
    """Persisted fine-grained run state for an autonomous task."""

    id: str
    task_id: Optional[str] = None
    phase: AutonomousRunPhase = AutonomousRunPhase.INTAKE
    iteration: int = 1
    max_iterations: int = 3
    status_summary: str = "Intake"
    active_session_ids: List[str] = Field(default_factory=list)
    pass_threshold: float = 0.8
    current_score: Optional[float] = None
    next_action: str = "Derive Goal Packet and begin work"
    paused_at: Optional[datetime] = None
    exhausted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    rubric: List[RubricCriterion] = Field(default_factory=list)
    evaluation_reports: List[EvaluationReport] = Field(default_factory=list)
    iterations: List[AutonomousIteration] = Field(default_factory=list)


class TerminalTabBase(BaseModel):
    """Base schema for TerminalTab."""

    name: str = Field(..., description="Name of the terminal tab")
    shell: Optional[str] = Field(None, description="Shell to use (default: $SHELL)")
    cwd: Optional[str] = Field(None, description="Working directory to start the terminal in")
    solo_mode: bool = Field(False, description="Whether to start in agent solo mode")
    agent_type: AgentType = Field(AgentType.CLAUDE, description="Type of agent to run")
    target: ExecutionTarget = Field(ExecutionTarget.LOCAL, description="Where to run the tab")
    remote_profile_id: Optional[str] = Field(None, description="Remote profile ID for remote tabs")
    remote_cwd: Optional[str] = Field(None, description="Remote working directory")
    remote_reconnect: bool = Field(True, description="Reconnect SSH automatically for remote tabs")
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables to inject into the launched terminal or agent",
    )


class TerminalTabCreate(TerminalTabBase):
    """Schema for creating a TerminalTab.

    ``agent_session_id`` lets a new tab pin to an existing agent conversation
    at creation time — currently used to resume a specific Codex session via
    ``codex resume <id>``. When omitted, Codex tabs start fresh and discover
    their session id from the rollout file shortly after launch (existing
    behavior).
    """

    agent_session_id: Optional[str] = Field(
        None,
        description=(
            "Existing agent conversation id to resume. For Codex tabs this is "
            "a session UUID from ~/.codex/sessions; the tab launches with "
            "codex resume <id>. Omit for a fresh session."
        ),
    )


class SwitchEnvRequest(BaseModel):
    """Payload for switching the environment / model of a live Claude or Codex tab."""

    env: Dict[str, str] = Field(
        ...,
        description="New environment variables to apply (fully replaces existing env)",
    )
    solo_mode: Optional[bool] = Field(
        None,
        description="If set, also toggles solo mode; if omitted, preserves current setting",
    )


class TerminalTabUpdate(BaseModel):
    """Schema for updating a TerminalTab."""

    name: Optional[str] = None
    shell: Optional[str] = None
    cwd: Optional[str] = None
    solo_mode: Optional[bool] = None
    agent_type: Optional[AgentType] = None
    target: Optional[ExecutionTarget] = None
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: Optional[bool] = None
    env: Optional[Dict[str, str]] = None


class TerminalTab(TerminalTabBase):
    """Schema for returning a TerminalTab."""

    id: str
    port: int
    created_at: datetime
    is_active: bool
    workspace_id: Optional[str] = Field(None, description="Workspace that created this tab")
    workspace_name: Optional[str] = Field(None, description="Display name of the owning workspace")
    workspace_role: Optional[WorkspaceSessionRole] = Field(
        None,
        description="Managed workspace role for this tab",
    )
    agent_session_id: Optional[str] = Field(
        None,
        description="Stable agent CLI conversation id (Claude --session-id); used for /resume and diagnostics.",
    )

    class Config:
        from_attributes = True


class TerminalAgentStatus(BaseModel):
    """Status summary for the floating terminal agent panel."""

    tab_id: str
    tab_name: str
    agent_type: AgentType
    status: AgentRuntimeStatus
    status_text: str
    detail: Optional[str] = None
    tmux_session: str
    last_changed_at: Optional[datetime] = None
    sampled_at: datetime


class RemoteProfile(BaseModel):
    """Configured SSH target for remote tabs."""

    id: str
    name: str
    ssh_host: str
    user: Optional[str] = None
    port: int = 22
    default_cwd: Optional[str] = None


class ResidentPeriodicTask(BaseModel):
    """A single recurring instruction the resident agent runs every cycle.

    Periodic tasks are the structured replacement for burying recurring work as
    prose inside the free-text ``resident_agent_directive``. Each enabled entry
    is rendered as an explicit numbered checklist item in the resident prompt on
    every wake-up. They all run together on each resident cycle at the workspace
    interval — there is deliberately no per-entry independent schedule.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    text: str
    enabled: bool = True


class WorkspaceCreate(BaseModel):
    """Payload for creating an agent workspace."""

    name: str
    path: str
    default_branch: str = "main"
    session_prefix: Optional[str] = None
    target: ExecutionTarget = ExecutionTarget.LOCAL
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: bool = True
    resident_agent_enabled: bool = False
    resident_agent_paused: bool = False
    resident_agent_interval_minutes: int = 60
    resident_agent_directive: Optional[str] = None
    resident_agent_periodic_tasks: List[ResidentPeriodicTask] = Field(default_factory=list)
    resident_agent_type: AgentType = AgentType.CLAUDE
    resident_agent_env: Dict[str, str] = Field(default_factory=dict)
    resident_agent_solo_mode: bool = True
    resident_agent_master_mode: bool = False
    resident_agent_title: Optional[str] = None
    resident_agent_target: ExecutionTarget = ExecutionTarget.LOCAL
    resident_agent_remote_profile_id: Optional[str] = None
    resident_agent_cwd: Optional[str] = None
    resident_agent_remote_reconnect: bool = True


class Workspace(BaseModel):
    """Agent workspace configuration."""

    id: str
    name: str
    path: str
    default_branch: str
    session_prefix: str
    dispatcher_session_id: Optional[str] = None
    target: ExecutionTarget = ExecutionTarget.LOCAL
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: bool = True
    resident_agent_enabled: bool = False
    resident_agent_paused: bool = False
    resident_agent_interval_minutes: int = 60
    resident_agent_session_id: Optional[str] = None
    resident_agent_directive: Optional[str] = None
    resident_agent_periodic_tasks: List[ResidentPeriodicTask] = Field(default_factory=list)
    resident_agent_last_run_at: Optional[datetime] = None
    # Server-managed scheduling hints. ``run_requested_at`` is set by the
    # run-now endpoint to force a single manual cycle on the next monitor tick
    # (cleared once the resident actually runs). ``next_run_at`` is the computed
    # overdue-backstop time (last_run + interval + jitter) surfaced to the UI so
    # users can see WHEN the resident will next wake on its own.
    resident_agent_run_requested_at: Optional[datetime] = None
    resident_agent_next_run_at: Optional[datetime] = None
    resident_agent_type: AgentType = AgentType.CLAUDE
    resident_agent_env: Dict[str, str] = Field(default_factory=dict)
    resident_agent_solo_mode: bool = True
    resident_agent_master_mode: bool = False
    resident_agent_title: Optional[str] = None
    resident_agent_target: ExecutionTarget = ExecutionTarget.LOCAL
    resident_agent_remote_profile_id: Optional[str] = None
    resident_agent_cwd: Optional[str] = None
    resident_agent_remote_reconnect: bool = True
    created_at: datetime
    updated_at: datetime


class WorkspaceUpdate(BaseModel):
    """Editable fields for an existing workspace."""

    name: Optional[str] = None
    path: Optional[str] = None
    default_branch: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: Optional[bool] = None
    resident_agent_enabled: Optional[bool] = None
    resident_agent_paused: Optional[bool] = None
    resident_agent_interval_minutes: Optional[int] = None
    resident_agent_directive: Optional[str] = None
    resident_agent_periodic_tasks: Optional[List[ResidentPeriodicTask]] = None
    resident_agent_type: Optional[AgentType] = None
    resident_agent_env: Optional[Dict[str, str]] = None
    resident_agent_solo_mode: Optional[bool] = None
    resident_agent_master_mode: Optional[bool] = None
    resident_agent_title: Optional[str] = None
    resident_agent_target: Optional[ExecutionTarget] = None
    resident_agent_remote_profile_id: Optional[str] = None
    resident_agent_cwd: Optional[str] = None
    resident_agent_remote_reconnect: Optional[bool] = None


class WorkspaceTaskCreate(BaseModel):
    """Payload for creating a workspace task."""

    title: str
    prompt: str
    agent_type: AgentType = AgentType.CODEX
    task_mode: WorkspaceTaskMode = WorkspaceTaskMode.REVIEWED
    execution_complexity: WorkspaceTaskExecutionComplexity = WorkspaceTaskExecutionComplexity.AUTO
    origin: WorkspaceTaskOrigin = WorkspaceTaskOrigin.HUMAN
    related_task_id: Optional[str] = None
    attachments: List["WorkspaceAttachmentCreate"] = Field(default_factory=list)
    goal_packet: Optional[GoalPacket] = None
    review_profiles: List[ReviewProfile] = Field(default_factory=list)
    autonomy_policy: Optional[AutonomyPolicy] = None
    session_id: Optional[str] = None
    clear_context: Optional[bool] = None
    # Agent tree run id that owns this task. Used by ManagedTaskAdapter to
    # recover from crashes: a retry can find the existing task instead of
    # creating a duplicate.
    agent_run_id: Optional[str] = None


class WorkspaceAttachmentCreate(BaseModel):
    """Browser-provided task attachment data."""

    filename: str
    mime_type: str
    data_url: str


class WorkspaceAttachment(BaseModel):
    """Persisted workspace task attachment."""

    id: str
    filename: str
    mime_type: str
    path: str
    size_bytes: int


class WorkspaceTaskUpdate(BaseModel):
    """Payload for updating a workspace task."""

    title: Optional[str] = None
    prompt: Optional[str] = None
    status: Optional[WorkspaceTaskStatus] = None
    goal_packet: Optional[GoalPacket] = None
    task_mode: Optional[WorkspaceTaskMode] = None
    execution_complexity: Optional[WorkspaceTaskExecutionComplexity] = None
    review_profiles: Optional[List[ReviewProfile]] = None
    autonomy_policy: Optional[AutonomyPolicy] = None
    autonomous_run: Optional[AutonomousRun] = None
    # Todo-task edit fields
    add_attachments: Optional[List["WorkspaceAttachmentCreate"]] = None
    removed_attachment_ids: Optional[List[str]] = None
    related_task_id: Optional[str] = None
    clear_context: Optional[bool] = None
    session_id: Optional[str] = None


class WorkspaceTask(BaseModel):
    """Task tracked by Agent Workspace mode."""

    id: str
    workspace_id: str
    title: str
    prompt: str
    attachments: List[WorkspaceAttachment] = Field(default_factory=list)
    goal_packet: Optional[GoalPacket] = None
    review_profiles: List[ReviewProfile] = Field(default_factory=list)
    agent_type: AgentType
    task_mode: WorkspaceTaskMode = WorkspaceTaskMode.REVIEWED
    execution_complexity: WorkspaceTaskExecutionComplexity = WorkspaceTaskExecutionComplexity.AUTO
    origin: WorkspaceTaskOrigin = WorkspaceTaskOrigin.HUMAN
    autonomy_policy: Optional[AutonomyPolicy] = None
    autonomous_run: Optional[AutonomousRun] = None
    status: WorkspaceTaskStatus
    session_id: Optional[str] = None
    related_task_id: Optional[str] = None
    clear_context: Optional[bool] = None
    # Agent tree run id that owns this task (set by ManagedTaskAdapter.spawn).
    agent_run_id: Optional[str] = None
    # Call ids of followup messages already delivered to this task. Used for
    # exactly-once delivery: a followup with a call_id already in this list
    # is a no-op. Persisted with the task so delivery survives restarts.
    delivered_call_ids: List[str] = Field(default_factory=list)
    # Call ids of followup messages that have been persisted to the outbox
    # but whose delivery may not have completed. On restart, any call_id in
    # this list is re-delivered (idempotently) and then moved to
    # delivered_call_ids. This implements a crash-safe two-phase outbox:
    # the receipt is persisted before delivery, so a crash between delivery
    # and receipt-persist cannot cause a duplicate; a crash before delivery
    # completion causes an idempotent retry, not a loss.
    pending_call_ids: List[str] = Field(default_factory=list)
    dispatch_reason: Optional[str] = None
    dispatch_pending: bool = False
    system_internal: bool = False
    internal_kind: Optional[str] = None
    feedback_lesson_ids: List[str] = Field(default_factory=list)
    review_session_id: Optional[str] = None
    review_attempts: int = 0
    # Review-cycle ordinals. ``review_cycle`` is the current work round (a task is
    # born in round 1; each reopen-to-worker opens the next round).
    # ``reviewed_cycle`` is the round number of the most recently applied reviewer
    # verdict (0 = none judged yet). A gate report opens a new review when its
    # stamped cycle exceeds ``reviewed_cycle``; the current round is already
    # judged when ``reviewed_cycle >= review_cycle``.
    review_cycle: int = 1
    reviewed_cycle: int = 0
    review_requested_at: Optional[datetime] = None
    review_completed_at: Optional[datetime] = None
    review_skipped_at: Optional[datetime] = None
    review_skip_reason: Optional[str] = None
    manual_aborted_at: Optional[datetime] = None
    manual_abort_reason: Optional[str] = None
    human_acceptance_requested_at: Optional[datetime] = None
    human_accepted_at: Optional[datetime] = None
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class ManagedSession(BaseModel):
    """Managed agent session backed by a terminal tab."""

    id: str
    workspace_id: str
    task_id: Optional[str] = None
    tab_id: str
    role: WorkspaceSessionRole
    agent_type: AgentType
    status: ManagedSessionStatus
    runtime_status: AgentRuntimeStatus = AgentRuntimeStatus.IDLE
    current_task_id: Optional[str] = None
    queued_count: int = 0
    title: str
    branch: Optional[str] = None
    workspace_path: str
    tmux_session: str
    target: ExecutionTarget = ExecutionTarget.LOCAL
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: bool = True
    solo_mode: bool = True
    ephemeral: bool = False
    env: Dict[str, str] = Field(default_factory=dict)
    remote_forward_port: Optional[int] = None
    auto_continue_task_id: Optional[str] = None
    auto_continue_attempts: int = 0
    last_auto_continue_at: Optional[datetime] = None
    # Hard recovery: interrupt + /clear + re-inject prompt after repeated API errors.
    # Tracked per task_id so counter resets on new task dispatch.
    hard_recovery_task_id: Optional[str] = None
    hard_recovery_attempts: int = 0
    last_hard_recovery_at: Optional[datetime] = None
    prompt_retry_task_id: Optional[str] = None
    prompt_retry_attempted_at: Optional[datetime] = None
    # For reviewer sessions: the id of the task this session was last dispatched
    # a review prompt for. Drives the cross-task /clear decision independently of
    # any task's mutable review_session_id (which abort/skip/stale-release null).
    last_review_task_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_activity_at: Optional[datetime] = None


class AgentReportCreate(BaseModel):
    """Payload for appending a worker progress report."""

    state: AgentReportState
    message: str
    message_en: Optional[str] = None
    message_zh: Optional[str] = None
    task_id: Optional[str] = None
    changed_files: List[str] = Field(default_factory=list)
    validation: Optional[str] = None
    risks: Optional[str] = None
    acceptance_check: List[AcceptanceCheck] = Field(default_factory=list)
    goal_packet: Optional[GoalPacket] = None
    evaluation_report: Optional[EvaluationReport] = None
    review_profiles: List[ReviewProfile] = Field(default_factory=list)
    profile_results: List[ReviewProfileResult] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    requires_human_judgment: bool = False
    review_decision: ReviewDecision = ReviewDecision.AUTO
    review_reason: Optional[str] = None
    risk_level: Optional[str] = None


class AgentReport(BaseModel):
    """Progress report recorded against a managed session."""

    id: str
    workspace_id: str
    task_id: Optional[str] = None
    session_id: str
    state: AgentReportState
    message: str
    message_en: Optional[str] = None
    message_zh: Optional[str] = None
    changed_files: List[str] = Field(default_factory=list)
    validation: Optional[str] = None
    risks: Optional[str] = None
    acceptance_check: List[AcceptanceCheck] = Field(default_factory=list)
    evaluation_report: Optional[EvaluationReport] = None
    review_profiles: List[ReviewProfile] = Field(default_factory=list)
    profile_results: List[ReviewProfileResult] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    requires_human_judgment: bool = False
    review_decision: ReviewDecision = ReviewDecision.AUTO
    review_reason: Optional[str] = None
    risk_level: Optional[str] = None
    # The owning task's ``review_cycle`` at the moment this report was created.
    # Used to rank gate/verdict reports against the task's ``reviewed_cycle``.
    # Defaults to 0 so legacy on-disk reports rank below any post-migration round.
    review_cycle: int = 0
    created_at: datetime


class WorkspaceArtifactPreview(BaseModel):
    """Previewable Markdown artifact content from a workspace report."""

    path: str
    filename: str
    content: str
    size_bytes: int
    truncated: bool = False


class WorkspaceMarkdownDocumentSource(str, Enum):
    """Where a discoverable Markdown document came from."""

    ARTIFACT = "artifact"
    CHANGED_FILE = "changed_file"
    SNAPSHOT = "snapshot"
    DISCOVERED = "discovered"


class WorkspaceMarkdownDocument(BaseModel):
    """Discoverable local Markdown document for workspace tasks."""

    id: str
    path: str
    label: str
    source: WorkspaceMarkdownDocumentSource
    task_id: Optional[str] = None
    report_id: Optional[str] = None
    session_id: Optional[str] = None
    size_bytes: Optional[int] = None
    updated_at: Optional[datetime] = None


class FeedbackLessonDraftCreate(BaseModel):
    """Manual or AI-produced candidate lesson for a task."""

    summary: str
    applies_when: List[str] = Field(default_factory=list)
    do: str = ""
    avoid: str = ""
    tags: List[str] = Field(default_factory=list)
    scope: FeedbackLessonScope = FeedbackLessonScope.WORKSPACE
    confidence: Optional[float] = None
    promote_to_active: bool = False


class FeedbackReaperRequest(BaseModel):
    """Manual trigger payload for the feedback reaper MVP."""

    source: FeedbackSourceType = FeedbackSourceType.MANUAL
    summary: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    lesson_drafts: List[FeedbackLessonDraftCreate] = Field(default_factory=list)


class FeedbackSummaryRequest(BaseModel):
    """Manual trigger payload for a workspace-level internal feedback summary."""

    mode: FeedbackSummaryMode = FeedbackSummaryMode.INCREMENTAL
    # Request accepts up to 200 for backward compatibility; the store
    # internally clamps to _REAPER_MAX_DIGESTS_PER_RUN (30) to keep prompts
    # bounded for small-context agents (e.g. codex).
    limit: int = Field(default=30, ge=1, le=200)
    force: bool = False
    clear_context: bool = True


class FeedbackRecord(BaseModel):
    """Append-only task feedback evidence captured before lesson condensation."""

    id: str
    workspace_id: str
    task_id: str
    source: FeedbackSourceType
    source_id: Optional[str] = None
    summary: str
    tags: List[str] = Field(default_factory=list)
    report_ids: List[str] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    created_at: datetime


class FeedbackLessonDraft(BaseModel):
    """Stored candidate lesson generated from one or more feedback records."""

    id: str
    workspace_id: str
    task_id: str
    source_record_ids: List[str] = Field(default_factory=list)
    status: FeedbackLessonStatus = FeedbackLessonStatus.DRAFT
    scope: FeedbackLessonScope = FeedbackLessonScope.WORKSPACE
    summary: str
    applies_when: List[str] = Field(default_factory=list)
    do: str = ""
    avoid: str = ""
    tags: List[str] = Field(default_factory=list)
    evidence_task_ids: List[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    created_at: datetime


class FeedbackLessonCreate(BaseModel):
    """Create or promote a reusable active lesson."""

    id: Optional[str] = None
    title: Optional[str] = None
    fingerprint: Optional[str] = None
    summary: str
    applies_when: List[str] = Field(default_factory=list)
    do: str = ""
    avoid: str = ""
    tags: List[str] = Field(default_factory=list)
    scope: FeedbackLessonScope = FeedbackLessonScope.WORKSPACE
    source_draft_ids: List[str] = Field(default_factory=list)
    source_record_ids: List[str] = Field(default_factory=list)
    evidence_task_ids: List[str] = Field(default_factory=list)
    confidence: Optional[float] = None


class FeedbackLesson(BaseModel):
    """Reusable lesson stored in the active lesson index."""

    id: str
    workspace_id: str
    title: str = ""
    fingerprint: str = ""
    scope: FeedbackLessonScope = FeedbackLessonScope.WORKSPACE
    status: FeedbackLessonStatus = FeedbackLessonStatus.ACTIVE
    summary: str
    applies_when: List[str] = Field(default_factory=list)
    do: str = ""
    avoid: str = ""
    tags: List[str] = Field(default_factory=list)
    evidence_task_ids: List[str] = Field(default_factory=list)
    source_draft_ids: List[str] = Field(default_factory=list)
    source_record_ids: List[str] = Field(default_factory=list)
    merged_from_ids: List[str] = Field(default_factory=list)
    superseded_by_id: Optional[str] = None
    hit_count: int = 0
    success_count: int = 0
    confidence: Optional[float] = None
    last_seen_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    last_validated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class FeedbackReaperRun(BaseModel):
    """Result of a manual feedback reaper run."""

    id: str
    workspace_id: str
    task_id: str
    record: FeedbackRecord
    lesson_drafts: List[FeedbackLessonDraft] = Field(default_factory=list)
    promoted_lessons: List[FeedbackLesson] = Field(default_factory=list)
    reaper_prompt: str
    created_at: datetime


class FeedbackTaskDigest(BaseModel):
    """Compact reusable digest for a completed workspace task record."""

    task_id: str
    title: str = ""
    status: str = ""
    final_summary: str = ""
    changed_files: List[str] = Field(default_factory=list)
    validation: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    report_states: List[str] = Field(default_factory=list)
    report_state_sequence: List[str] = Field(default_factory=list)
    review_failed_count: int = 0
    needs_input_count: int = 0
    report_total: int = 0
    completed_at: Optional[str] = None


class FeedbackProcessedTaskRecord(BaseModel):
    """Cache entry for a task record already reduced for feedback summarization."""

    task_id: str
    path: str
    sha256: str
    digest: FeedbackTaskDigest
    summarized_at: datetime


class FeedbackSummaryRun(BaseModel):
    """Audit record for a workspace-level internal feedback summary trigger."""

    id: str
    workspace_id: str
    task_id: Optional[str] = None
    mode: FeedbackSummaryMode = FeedbackSummaryMode.INCREMENTAL
    input_record_ids: List[str] = Field(default_factory=list)
    cache_hit: bool = False
    prompt_version: int = 1
    created_lesson_ids: List[str] = Field(default_factory=list)
    merged_lesson_ids: List[str] = Field(default_factory=list)
    skipped_reason: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None


class WorkspaceBoard(BaseModel):
    """Workspace board response for Agent Workspace mode."""

    workspace: Workspace
    tasks: List[WorkspaceTask]
    sessions: List[ManagedSession]
    reports: List[AgentReport]
    markdown_documents: List[WorkspaceMarkdownDocument] = Field(default_factory=list)
    snapshot_path: Optional[str] = None


class SpawnWorkerRequest(BaseModel):
    """Payload for spawning a worker session for a task."""

    agent_type: Optional[AgentType] = None


class EnsureWorkspaceAgentRequest(BaseModel):
    """Payload for ensuring a resident workspace agent session."""

    agent_type: AgentType = AgentType.CODEX
    title: Optional[str] = None
    role: WorkspaceSessionRole = WorkspaceSessionRole.ORCHESTRATOR
    reuse_existing: bool = False
    cwd: Optional[str] = None
    solo_mode: bool = True
    target: Optional[ExecutionTarget] = None
    remote_profile_id: Optional[str] = None
    remote_cwd: Optional[str] = None
    remote_reconnect: Optional[bool] = None
    ephemeral: bool = False
    env: Dict[str, str] = Field(default_factory=dict)


class StartTaskRequest(BaseModel):
    """Payload for queueing a task and optionally overriding dispatch."""

    agent_type: Optional[AgentType] = None
    target_session_id: Optional[str] = None
    clear_context: Optional[bool] = None
    related_task_id: Optional[str] = None


class ContinueTaskRequest(BaseModel):
    """Payload for continuing a task from review with its original agent."""

    message: Optional[str] = None
    attachments: List[WorkspaceAttachmentCreate] = Field(default_factory=list)


class RequestTaskReviewRequest(BaseModel):
    """Payload for manually requesting reviewer checks."""

    message: Optional[str] = None


class ManualTaskControlRequest(BaseModel):
    """Payload for exceptional manual task state control."""

    reason: str


class DispatchDecisionRequest(BaseModel):
    """Structured dispatch decision produced by a dispatcher agent."""

    target_session_id: str
    clear_context: bool = False
    reason: Optional[str] = None


class SendSessionMessageRequest(BaseModel):
    """Payload for sending a message to a managed session."""

    message: str
    attachments: List[WorkspaceAttachmentCreate] = Field(default_factory=list)


class User(BaseModel):
    """Schema for a user."""

    open_id: str
    name: str
    email: str
    avatar_url: Optional[str] = None


class LoginSession(BaseModel):
    """Schema for a login session."""

    session_id: str
    user: User
    created_at: datetime
    expires_at: datetime
    feishu_access_token: str
    feishu_refresh_token: str


class DirectoryListing(BaseModel):
    """Schema for directory listing."""

    path: str
    files: List["FileInfo"]


class FileInfo(BaseModel):
    """Schema for file information."""

    name: str
    path: str
    type: str  # "file" or "directory"
    is_dir: bool


# ---------------------------------------------------------------------------
# Environment-variable presets (cross-origin persisted)
# ---------------------------------------------------------------------------


class EnvPreset(BaseModel):
    """A saved named environment-variable preset."""

    id: str
    name: str
    text: str  # KEY=VALUE newline-delimited text, same format as the textarea


class EnvPresetCreate(BaseModel):
    """Payload for creating a new custom env preset."""

    name: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)


class EnvPresetUpdate(BaseModel):
    """Payload for updating an existing custom env preset (all fields optional)."""

    name: Optional[str] = Field(None, min_length=1)
    text: Optional[str] = Field(None, min_length=1)


class EnvPresetHiddenRequest(BaseModel):
    """Payload to hide or unhide a built-in preset."""

    hidden: bool


class EnvPresetBulkImport(BaseModel):
    """Bulk-import payload used for one-time localStorage → backend migration."""

    custom_presets: List[EnvPreset] = Field(default_factory=list)
    hidden_builtin_ids: List[str] = Field(default_factory=list)


class EnvPresetsResponse(BaseModel):
    """Full state response for env presets."""

    custom_presets: List[EnvPreset]
    hidden_builtin_ids: List[str]
