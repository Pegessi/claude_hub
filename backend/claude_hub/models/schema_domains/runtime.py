"""Runtime and lifecycle schema primitives."""

from enum import Enum
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

AGENT_TAG_MAX_LENGTH = 64
_AGENT_TAG_CONTROL_CHARS = frozenset({*range(0x00, 0x20), 0x7F})


def normalize_agent_tag(value: Any, *, allow_clear: bool = False) -> Optional[str]:
    """Normalize optional task agent labels: trim, bounded length, no enum."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("agent_tag must be a string")
    trimmed = value.strip()
    if not trimmed:
        if allow_clear:
            return None
        raise ValueError("agent_tag must not be empty")
    if any(ord(char) in _AGENT_TAG_CONTROL_CHARS for char in trimmed):
        raise ValueError("agent_tag must be a single-line label without control characters")
    if len(trimmed) > AGENT_TAG_MAX_LENGTH:
        raise ValueError(f"agent_tag must be at most {AGENT_TAG_MAX_LENGTH} characters")
    return trimmed


class AgentType(str, Enum):
    """Type of agent to run in the terminal."""

    CLAUDE = "claude"
    CODEX = "codex"
    TRAEX = "traex"
    MASO = "maso"
    CURSOR = "cursor"
    TERMINAL = "terminal"


class SessionKind(str, Enum):
    """User-facing session surface chosen at creation time."""

    CHAT = "chat"
    TERMINAL = "terminal"


CHAT_REMOTE_UNSUPPORTED = "Chat sessions run on the Hub host; use a Terminal session for remote SSH"
STDIN_SHELL_REMOTE_UNSUPPORTED = (
    "This SSH alias has no usable remote TTY; directory listing works, "
    "but Terminal and remote agents need a PTY host"
)
NONINTERACTIVE_REMOTE_UNSUPPORTED = (
    "This SSH alias is browse-only: it neither accepts an ssh argv command nor "
    "exposes an interactive PTY the Hub can drive. Directory listing may work, "
    "but Terminal tabs and remote agents cannot run on it."
)


class RemoteTransport(str, Enum):
    """How commands and interactive bytes reach a remote SSH host.

    - ``normal``: a conventional sshd. ``ssh host 'cmd'`` runs the argv command
      and a persistent ``ssh -tt host '<bootstrap>'`` carries the interactive
      tmux session.
    - ``pty_gateway``: a jump/gateway sshd (e.g. the merlin_dev Trial proxy)
      that swallows the OpenSSH command channel and any non-tty stdin, but drops
      an argv-less ``ssh -tt`` connection into a *real* interactive PTY after a
      brief connect flash. One-shot commands must be typed into that PTY and the
      interactive session must be bootstrapped by typing after the real prompt.
    """

    NORMAL = "normal"
    PTY_GATEWAY = "pty_gateway"


class ChatMode(str, Enum):
    """Provider execution mode selected for subsequent Chat turns."""

    DEFAULT = "default"
    PLAN = "plan"


class StreamModeOption(BaseModel):
    """One provider-verified mode exposed by the structured Chat surface."""

    id: str
    label: str
    description: str


class StreamReasoningEffortOption(BaseModel):
    """One reasoning-effort level advertised for a specific model."""

    id: str
    label: str = ""
    description: str = ""
    provider_model_id: Optional[str] = None


class StreamModelOption(BaseModel):
    """One provider-verified model exposed by the structured Chat surface."""

    id: str
    label: str
    description: str = ""
    provider_model_id: Optional[str] = None
    default_reasoning_effort: Optional[str] = None
    supported_reasoning_efforts: List[StreamReasoningEffortOption] = Field(default_factory=list)


class StreamCapabilities(BaseModel):
    """What the structured observation plane can offer for a session.

    ``structured=False`` is the fail-closed state: callers retain the raw
    terminal when no supported transcript source is available.
    """

    structured: bool = False
    unavailable_reason: Optional[str] = None
    adapter_id: str = "none"
    schema_version: int = 0
    sources: List[str] = Field(default_factory=list)
    supports_approval_ui: bool = False
    supports_tool_timeline: bool = False
    supports_images: bool = False
    available_modes: List[StreamModeOption] = Field(default_factory=list)
    current_mode: Optional[str] = None
    supports_dynamic_modes: bool = False
    available_models: List[StreamModelOption] = Field(default_factory=list)
    supports_goals: bool = False
    goal_execution_owner: Optional[Literal["provider_native", "hub_managed"]] = None
    goal_usage_quality: Literal["exact", "estimated", "unavailable"] = "unavailable"
    current_model: Optional[str] = None
    current_reasoning_effort: Optional[str] = None


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
    FAILED = "failed"


class WorkspaceTaskMode(str, Enum):
    """Automation mode for a workspace task."""

    DIRECT = "direct"
    REVIEWED = "reviewed"
    AUTONOMOUS = "autonomous"
    SUBAGENT = "subagent"


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
    FAILED = "failed"
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
