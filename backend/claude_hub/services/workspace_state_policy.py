import re
from dataclasses import dataclass
from typing import Optional, Sequence

from ..models import (
    AgentReportState,
    AgentRuntimeStatus,
    ManagedSessionStatus,
    ReviewDecision,
    WorkspaceSessionRole,
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
