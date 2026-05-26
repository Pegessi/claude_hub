import re
from dataclasses import dataclass
from typing import Optional, Sequence

from ..models import (
    AgentReportState,
    AgentRuntimeStatus,
    AutonomousRunPhase,
    EvaluationDecision,
    EvaluationStrictness,
    ManagedSessionStatus,
    ReviewDecision,
    ReviewProfile,
    WorkspaceSessionRole,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)

AUTO_CONTINUE_INTERRUPTION_PATTERNS = (
    "api error",
    "api_error",
    "api request failed",
    "api returned",
    "failed to call api",
    "provider returned error",
    "unknown error",
    "rate limit",
    "429",
    "400 unknown error",
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "connection reset",
    "connection aborted",
    "stream error",
    "network error",
    "temporarily unavailable",
    "overloaded",
)

AUTO_CONTINUE_COMPLETION_PATTERNS = (
    "ready_for_review",
    "ready for review",
    "ready for human review",
    "completed report",
    "task complete",
    "task is complete",
    "work is complete",
    "changed_files",
    "changed files",
    "validation:",
    "risks:",
)

COMPLETION_EVIDENCE_STATES = {
    AgentReportState.READY_FOR_REVIEW,
    AgentReportState.COMPLETED,
}

REVIEW_GATE_STATES = {
    AgentReportState.READY_FOR_REVIEW,
    AgentReportState.COMPLETED,
    AgentReportState.BLOCKED,
    AgentReportState.NEEDS_INPUT,
}


@dataclass(frozen=True)
class ReviewSkipContext:
    """Inputs needed to decide whether an agent may skip AI review."""

    report_state: AgentReportState
    evidence_gaps: Sequence[str]
    changed_files: Sequence[str]
    risk_level: Optional[str]
    latest_review_state: Optional[AgentReportState]
    workspace_has_tracked_changes: bool


@dataclass(frozen=True)
class ReviewProfileContext:
    """Inputs used to infer review profiles for one reviewer assignment."""

    task_mode: WorkspaceTaskMode
    report_state: AgentReportState
    title: str = ""
    prompt: str = ""
    changed_files: Sequence[str] = ()
    validation: Optional[str] = None
    risks: Optional[str] = None
    message: str = ""
    explicit_profiles: Sequence[ReviewProfile] = ()
    require_artifact_review: bool = False
    evaluation_strictness: EvaluationStrictness = EvaluationStrictness.BALANCED
    attachment_count: int = 0


REVIEW_PROFILE_LABELS = {
    ReviewProfile.GENERAL: "General",
    ReviewProfile.CODE: "Code",
    ReviewProfile.UI: "UI",
    ReviewProfile.ARTIFACT: "Artifact",
    ReviewProfile.DELIVERY: "Delivery",
    ReviewProfile.BOUNDARY: "Boundary",
}


REVIEW_PROFILE_GUIDANCE = {
    ReviewProfile.GENERAL: (
        "Check goal fidelity, scope control, validation adequacy, regression risk, "
        "and final handoff quality."
    ),
    ReviewProfile.CODE: (
        "Inspect changed code paths, tests, typing/lint evidence, API contracts, "
        "state migrations, concurrency, and compatibility with local architecture."
    ),
    ReviewProfile.UI: (
        "Review UI behavior, responsive layout, console/browser evidence, interaction "
        "states, accessibility basics, and whether screenshots or browser checks cover risk."
    ),
    ReviewProfile.ARTIFACT: (
        "Inspect referenced artifacts such as images, screenshots, documents, logs, "
        "or generated outputs. Do not rely only on text summaries when artifacts are required."
    ),
    ReviewProfile.DELIVERY: (
        "Verify external delivery evidence such as target, message IDs, recipient/channel, "
        "retry/failure state, and whether destination-side proof is sufficient."
    ),
    ReviewProfile.BOUNDARY: (
        "Evaluate high-risk boundary actions such as credential use, destructive operations, "
        "external sending, deployment, or cross-workspace writes. Escalate when approval is unclear."
    ),
}


def _append_profile(
    profiles: list[ReviewProfile],
    profile: ReviewProfile,
) -> None:
    if profile not in profiles:
        profiles.append(profile)


def infer_review_profiles(context: ReviewProfileContext) -> list[ReviewProfile]:
    """Infer review lenses for a reviewer assignment."""

    profiles: list[ReviewProfile] = []
    for profile in context.explicit_profiles:
        _append_profile(profiles, profile)
    _append_profile(profiles, ReviewProfile.GENERAL)

    text = " ".join(
        item
        for item in (
            context.title,
            context.prompt,
            context.message,
            context.validation or "",
            context.risks or "",
        )
        if item
    ).lower()
    changed_files = [str(item).lower() for item in context.changed_files]
    has_changed_files = bool(changed_files)

    if context.task_mode == WorkspaceTaskMode.REVIEWED or has_changed_files:
        _append_profile(profiles, ReviewProfile.CODE)
    if context.task_mode == WorkspaceTaskMode.AUTONOMOUS:
        _append_profile(profiles, ReviewProfile.ARTIFACT)
    if context.require_artifact_review or context.attachment_count > 0:
        _append_profile(profiles, ReviewProfile.ARTIFACT)

    if any(
        marker in path
        for path in changed_files
        for marker in ("frontend/", ".vue", ".tsx", ".jsx", ".css", "playwright")
    ) or any(marker in text for marker in ("ui", "screenshot", "browser", "responsive")):
        _append_profile(profiles, ReviewProfile.UI)

    if any(
        marker in text
        for marker in (
            "artifact",
            "image",
            "screenshot",
            "generated",
            "wallpaper",
            "document",
            "pdf",
            "output",
        )
    ):
        _append_profile(profiles, ReviewProfile.ARTIFACT)

    if any(
        marker in text
        for marker in (
            "feishu",
            "lark",
            "send",
            "sent",
            "deliver",
            "delivery",
            "message id",
            "recipient",
            "channel",
        )
    ):
        _append_profile(profiles, ReviewProfile.DELIVERY)

    if context.evaluation_strictness == EvaluationStrictness.STRICT or any(
        marker in text
        for marker in (
            "credential",
            "secret",
            "token",
            "delete",
            "deploy",
            "production",
            "external",
            "destructive",
            "permission",
        )
    ):
        _append_profile(profiles, ReviewProfile.BOUNDARY)

    return profiles


def review_profile_prompt_lines(profiles: Sequence[ReviewProfile]) -> list[str]:
    """Return reviewer prompt bullets for enabled review profiles."""

    return [
        f"- {REVIEW_PROFILE_LABELS[profile]} ({profile.value}): {REVIEW_PROFILE_GUIDANCE[profile]}"
        for profile in profiles
    ]


def managed_status_from_runtime(runtime_status: AgentRuntimeStatus) -> ManagedSessionStatus:
    """Map sampled terminal runtime status to persisted managed-session status."""

    if runtime_status == AgentRuntimeStatus.ATTENTION:
        return ManagedSessionStatus.NEEDS_INPUT
    if runtime_status == AgentRuntimeStatus.WORKING:
        return ManagedSessionStatus.WORKING
    if runtime_status == AgentRuntimeStatus.OFFLINE:
        return ManagedSessionStatus.STOPPED
    return ManagedSessionStatus.IDLE


def managed_status_from_report(
    state: AgentReportState,
    role: WorkspaceSessionRole,
    fallback: ManagedSessionStatus,
) -> ManagedSessionStatus:
    """Map a structured agent report into a managed-session lifecycle status."""

    if role == WorkspaceSessionRole.ORCHESTRATOR:
        if state in {AgentReportState.COMPLETED, AgentReportState.READY_FOR_REVIEW}:
            return ManagedSessionStatus.IDLE
        if state == AgentReportState.BLOCKED:
            return ManagedSessionStatus.NEEDS_INPUT
    if role == WorkspaceSessionRole.REVIEWER:
        if state == AgentReportState.REVIEW_STARTED:
            return ManagedSessionStatus.WORKING
        if state in {AgentReportState.REVIEW_PASSED, AgentReportState.REVIEW_FAILED}:
            return ManagedSessionStatus.IDLE
        if state == AgentReportState.REVIEW_NEEDS_INPUT:
            return ManagedSessionStatus.NEEDS_INPUT
    if state == AgentReportState.BLOCKED:
        return ManagedSessionStatus.ERROR
    if state == AgentReportState.NEEDS_INPUT:
        return ManagedSessionStatus.NEEDS_INPUT
    if state == AgentReportState.COMPLETED:
        return ManagedSessionStatus.DONE
    if state in {
        AgentReportState.STARTED,
        AgentReportState.WORKING,
        AgentReportState.READY_FOR_REVIEW,
    }:
        return ManagedSessionStatus.WORKING
    return fallback


def runtime_status_from_report(
    state: AgentReportState,
    fallback: AgentRuntimeStatus,
) -> AgentRuntimeStatus:
    """Map a structured agent report into a best-effort runtime status."""

    if state in {AgentReportState.BLOCKED, AgentReportState.NEEDS_INPUT}:
        return AgentRuntimeStatus.ATTENTION
    if state in {AgentReportState.STARTED, AgentReportState.WORKING}:
        return AgentRuntimeStatus.WORKING
    if state in {AgentReportState.READY_FOR_REVIEW, AgentReportState.COMPLETED}:
        return AgentRuntimeStatus.IDLE
    if state == AgentReportState.REVIEW_STARTED:
        return AgentRuntimeStatus.WORKING
    if state in {AgentReportState.REVIEW_PASSED, AgentReportState.REVIEW_FAILED}:
        return AgentRuntimeStatus.IDLE
    if state == AgentReportState.REVIEW_NEEDS_INPUT:
        return AgentRuntimeStatus.ATTENTION
    return fallback


def task_status_from_report(state: AgentReportState) -> Optional[WorkspaceTaskStatus]:
    """Map a report state into a board-column task status when it should affect the task."""

    if state in {AgentReportState.REVIEW_PASSED, AgentReportState.REVIEW_NEEDS_INPUT}:
        return WorkspaceTaskStatus.REVIEW
    if state in {
        AgentReportState.STARTED,
        AgentReportState.WORKING,
        AgentReportState.BLOCKED,
        AgentReportState.NEEDS_INPUT,
        AgentReportState.READY_FOR_REVIEW,
        AgentReportState.COMPLETED,
        AgentReportState.REVIEW_STARTED,
        AgentReportState.REVIEW_FAILED,
    }:
        return WorkspaceTaskStatus.WORKING
    return None


def completion_evidence_gaps(
    state: AgentReportState,
    *,
    has_goal_packet: bool,
    has_acceptance_check: bool,
) -> list[str]:
    """Return audit-evidence fields required before completion-style review routing."""

    if state not in COMPLETION_EVIDENCE_STATES:
        return []
    gaps: list[str] = []
    if not has_goal_packet:
        gaps.append("stored Goal Packet")
    if not has_acceptance_check:
        gaps.append("acceptance_check evidence")
    return gaps


def is_review_gate_state(state: AgentReportState) -> bool:
    return state in REVIEW_GATE_STATES


def can_skip_task_review(context: ReviewSkipContext) -> bool:
    """Decide whether an agent-requested AI-review skip is allowed."""

    if context.report_state != AgentReportState.COMPLETED:
        return False
    if context.evidence_gaps:
        return False
    if context.changed_files:
        return False
    if (context.risk_level or "").strip().lower() not in {"", "low", "none"}:
        return False
    if context.latest_review_state == AgentReportState.REVIEW_FAILED:
        return False
    if context.workspace_has_tracked_changes:
        return False
    return True


def should_request_task_review(
    *,
    trigger_kind: str,
    report_state: AgentReportState,
    review_decision: ReviewDecision,
    can_skip_review: bool,
) -> bool:
    """Resolve reviewer routing for a report after skip eligibility is known."""

    if trigger_kind != "agent_report":
        return True
    if review_decision == ReviewDecision.REQUEST:
        return True
    if report_state in {
        AgentReportState.READY_FOR_REVIEW,
        AgentReportState.BLOCKED,
        AgentReportState.NEEDS_INPUT,
    }:
        return True
    if review_decision != ReviewDecision.SKIP:
        return True
    return not can_skip_review


def autonomous_task_status_from_phase(
    phase: AutonomousRunPhase,
) -> WorkspaceTaskStatus:
    """Map fine-grained autonomous run phase back to the coarse task board."""

    if phase in {
        AutonomousRunPhase.WAITING_FOR_HUMAN,
        AutonomousRunPhase.PASSED,
        AutonomousRunPhase.FAILED,
        AutonomousRunPhase.EXHAUSTED,
        AutonomousRunPhase.CANCELLED,
    }:
        return WorkspaceTaskStatus.REVIEW
    if phase in {
        AutonomousRunPhase.INTAKE,
        AutonomousRunPhase.RUBRIC_RESEARCH,
        AutonomousRunPhase.PLANNING,
        AutonomousRunPhase.DISPATCHING,
    }:
        return WorkspaceTaskStatus.QUEUED
    return WorkspaceTaskStatus.WORKING


def autonomous_phase_after_worker_report(
    report_state: AgentReportState,
) -> AutonomousRunPhase | None:
    """Return the autonomous phase implied by a worker report."""

    if report_state in {AgentReportState.STARTED, AgentReportState.WORKING}:
        return AutonomousRunPhase.WORKING
    if report_state in {AgentReportState.READY_FOR_REVIEW, AgentReportState.COMPLETED}:
        return AutonomousRunPhase.EVALUATING
    if report_state in {AgentReportState.BLOCKED, AgentReportState.NEEDS_INPUT}:
        return AutonomousRunPhase.WAITING_FOR_HUMAN
    return None


def autonomous_decision_from_review_state(
    report_state: AgentReportState,
) -> EvaluationDecision | None:
    """Treat reviewer reports as evaluator decisions for Autonomous Mode V1."""

    if report_state == AgentReportState.REVIEW_PASSED:
        return EvaluationDecision.PASS
    if report_state == AgentReportState.REVIEW_FAILED:
        return EvaluationDecision.REVISE
    if report_state == AgentReportState.REVIEW_NEEDS_INPUT:
        return EvaluationDecision.NEEDS_INPUT
    return None


def autonomous_phase_from_evaluation_decision(
    *,
    decision: EvaluationDecision,
    current_iteration: int,
    max_iterations: int,
) -> AutonomousRunPhase:
    """Resolve evaluator decision into the next autonomous run phase."""

    if decision == EvaluationDecision.PASS:
        return AutonomousRunPhase.PASSED
    if decision == EvaluationDecision.NEEDS_INPUT:
        return AutonomousRunPhase.WAITING_FOR_HUMAN
    if decision in {EvaluationDecision.FAIL, EvaluationDecision.ESCALATE}:
        return AutonomousRunPhase.WAITING_FOR_HUMAN
    if current_iteration >= max_iterations:
        return AutonomousRunPhase.EXHAUSTED
    return AutonomousRunPhase.REVISING


def auto_continue_recent_output_segment(output: str) -> str:
    """Return the most recent agent-response segment used for auto-continue classification."""

    lines = output.splitlines()
    tail_start = max(0, len(lines) - 120)
    tail = lines[tail_start:]
    prompt_indices = [
        index
        for index, line in enumerate(tail)
        if line.strip() in {"\u203a", "\u276f"} or line.strip().startswith(("\u203a ", "\u276f "))
    ]
    if not prompt_indices:
        return "\n".join(tail[-60:])

    last_prompt_index = prompt_indices[-1]
    last_prompt = tail[last_prompt_index].strip()
    if last_prompt in {"\u203a", "\u276f"}:
        previous_prompt_index = prompt_indices[-2] if len(prompt_indices) >= 2 else -1
        return "\n".join(tail[previous_prompt_index + 1 : last_prompt_index])
    return "\n".join(tail[last_prompt_index + 1 :])


def auto_continue_interruption_reason(output: str) -> str | None:
    tail = auto_continue_recent_output_segment(output).lower()
    for pattern in AUTO_CONTINUE_INTERRUPTION_PATTERNS:
        if pattern in tail:
            return pattern
    return None


def auto_continue_completion_reason(output: str) -> str | None:
    tail = auto_continue_recent_output_segment(output).lower()
    for pattern in AUTO_CONTINUE_COMPLETION_PATTERNS:
        if pattern in tail:
            return pattern
    return None


def auto_continue_output_looks_busy(output: str) -> bool:
    tail = "\n".join(output.lower().splitlines()[-12:])
    if re.search(
        r"^[\u273b\u2722\u2736\u2733\u2737\u2738\u2739\u273a\u273d\u2726\u2727]\s+\S+\u2026\s+\(",
        tail,
        re.MULTILINE,
    ):
        return True
    return any(
        marker in tail
        for marker in (
            "esc to interrupt",
            "ctrl+c to interrupt",
            "ctrl-c to interrupt",
            "running\u2026",
            "running...",
        )
    )
