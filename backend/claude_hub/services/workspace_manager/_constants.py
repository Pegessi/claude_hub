import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ...config import settings
from ...models import (
    AgentReport,
    AgentReportCreate,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    AutonomousIteration,
    AutonomousRun,
    AutonomousRunPhase,
    AutonomyPolicy,
    ContinueTaskRequest,
    DispatchDecisionRequest,
    EnsureWorkspaceAgentRequest,
    EvaluationDecision,
    EvaluationReport,
    ExecutionTarget,
    FeedbackLesson,
    FeedbackLessonCreate,
    FeedbackReaperRequest,
    FeedbackReaperRun,
    FeedbackSummaryRequest,
    FeedbackSummaryRun,
    GoalPacketStatus,
    ManagedSession,
    ManagedSessionStatus,
    ManualTaskControlRequest,
    RequestTaskReviewRequest,
    ResidentPeriodicTask,
    ReviewDecision,
    ReviewProfile,
    StartTaskRequest,
    TaskCleanupResult,
    TerminalAgentStatus,
    Workspace,
    WorkspaceArtifactPreview,
    WorkspaceAttachment,
    WorkspaceAttachmentCreate,
    WorkspaceBoard,
    WorkspaceCreate,
    WorkspaceEnsure,
    WorkspaceMarkdownDocument,
    WorkspaceMarkdownDocumentSource,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskExecutionComplexity,
    WorkspaceTaskMode,
    WorkspaceTaskOrigin,
    WorkspaceTaskStatus,
    WorkspaceTaskUpdate,
    WorkspaceUpdate,
)
from ...models.schemas import AGENT_TAG_MAX_LENGTH, normalize_agent_tag
from .. import workspace_state_policy as state_policy
from ..feedback_lessons import FeedbackLessonStore
from ..remote_profiles import remote_profile_manager
from ..runtime_isolation import resolve_state_root
from ..ttyd_manager import ttyd_manager

logger = logging.getLogger(__name__)

STATE_ROOT = resolve_state_root()
INDEX_FILE = STATE_ROOT / "index.json"
LEGACY_STATE_FILE = Path.home() / ".claude_hub" / "workspaces.json"
REMOTE_FORWARD_PORT_BASE = 18173
TMUX_SUBMIT_ATTEMPTS = 3
TMUX_PASTE_SETTLE_SECONDS = 0.35
TMUX_SUBMIT_SETTLE_SECONDS = 0.7
AUTO_CONTINUE_MAX_ATTEMPTS = 10
AUTO_CONTINUE_MIN_INTERVAL_SECONDS = 15
AUTO_CONTINUE_IDLE_GRACE_SECONDS = 20
# Clean-idle grace: when an agent is idle at a prompt with no error patterns
# and no completion patterns, wait this long before sending the first nudge.
# Longer than the error/completion grace because agents legitimately sit at a
# clean prompt while reading files, thinking, or between output bursts — but a
# prompt that was never delivered (e.g. continue_task Enter failed) leaves the
# agent at a permanently clean idle, and we need to recover within ~1 minute
# rather than leaving the task stuck in WORKING forever.
AUTO_CONTINUE_CLEAN_IDLE_GRACE_SECONDS = 60
# After this many soft auto-continue prompts fail to revive an agent stuck on
# an API error, escalate to hard recovery: interrupt, /clear, re-inject prompt.
AUTO_CONTINUE_SOFT_ATTEMPTS_BEFORE_HARD_RECOVERY = 3
# Maximum hard recoveries per task before giving up and marking NEEDS_INPUT.
AUTO_CONTINUE_MAX_HARD_RECOVERIES = 2
# Wait this long after sending /clear for the CLI to settle before re-pasting.
CLEAR_CONTEXT_SETTLE_SECONDS = 1.5
# Wait this long after interrupt (Escape + C-c) before sending /clear.
INTERRUPT_SETTLE_SECONDS = 1.0
REVIEW_RUNTIME_REOPEN_GRACE_SECONDS = 20
MAX_AUTOMATED_REVIEW_FAILURES = 3
PROMPT_DISPATCH_STALL_GRACE_SECONDS = 20
PROMPT_DISPATCH_RETRY_GRACE_SECONDS = 10
# How long the fallback reaper waits after a review_requested_at timestamp
# (or after the reviewer's last terminal activity) before treating an
# idle-looking reviewer as stuck. Covers the gap between dispatching a
# review prompt and the reviewer actually emitting first tokens — without
# this grace, a slow-to-start reviewer is repeatedly re-dispatched.
REVIEW_REAPER_DISPATCH_GRACE_SECONDS = 60
PROMPT_STUCK_RISK_LEVEL = "prompt_dispatch_stalled"
WORKSPACE_MONITOR_INTERVAL_SECONDS = 5
# Agent-facing examples call the Hub over localhost (or a loopback SSH
# forward). Keep external traffic on the configured proxy while forcing these
# internal control-plane requests to stay on the machine. --fail-with-body also
# prevents an HTTP 4xx/5xx from being mistaken for a successful report.
INTERNAL_API_CURL = "curl --noproxy 'localhost,127.0.0.1,::1' --fail-with-body -sS"
# Event-gated resident trigger: minimum gap between activity-triggered resident
# runs. When real workspace activity is detected, the resident may fire as soon
# as this debounce floor has elapsed since its last run (far shorter than the
# configured interval), so the agent reacts promptly to bursts without firing
# once per burst event. The idle backstop still uses the full
# resident_agent_interval_minutes (+ jitter).
RESIDENT_ACTIVITY_DEBOUNCE_SECONDS = 300
# How long after a managed terminal tab is created we refuse to prune it as an
# orphan. Tab creation (ttyd_manager.create_tab) and ManagedSession
# registration (_create_managed_session) are two separate steps; the orphan
# reconciler can observe the tab before its session row exists. This grace
# window keeps the reconciler from deleting a tab that belongs to an agent
# whose session is still being registered.
ORPHAN_TAB_PRUNE_GRACE_SECONDS = 60
AUTO_CONTINUE_MESSAGE = (
    "Please inspect the current task state. If the task was interrupted or is unfinished, "
    "continue from the last actionable step. If the task is already complete and only missed "
    "the workspace report, immediately POST a ready_for_review or completed report instead of "
    "doing more work."
)
AUTO_CONTINUE_IDLE_PROMPT_MESSAGE = (
    "You are the workspace agent assigned to this task and appear to be at a clean idle prompt. "
    "Read the state snapshot at the path given in your original assignment, inspect the current "
    "state of any files you were editing, and resume work from the last actionable step. "
    "If the task was already complete before this nudge (for example, if you already posted a "
    "ready_for_review report that did not reach the server), immediately POST a ready_for_review "
    "or completed report with changed_files, validation, risks, the stored Goal Packet, and "
    "acceptance_check evidence."
)
AUTO_CONTINUE_REVIEWER_MESSAGE = (
    "Please inspect the review state. If the review was interrupted by an API error, continue "
    "from the last step: read the worker's report and changed files, judge against the Goal "
    "Packet, and issue a review_passed, review_failed, or review_needs_input verdict. "
    "If you already posted your verdict and the message did not reach the server, re-POST it now."
)
AUTO_REPORT_MISSING_MESSAGE = (
    "The task appears complete but no workspace report was recorded. Please immediately POST "
    "the final ready_for_review or completed report with changed_files, validation, risks, "
    "the stored Goal Packet if it has not been reported yet, and acceptance_check evidence; "
    "only continue work if you find it is actually unfinished."
)
HARD_RECOVERY_WORKER_MESSAGE = (
    "⚠️  Your previous context was automatically cleared because the agent encountered a persistent "
    "API error and could not continue. A fresh context has been started for you within the SAME "
    "conversation (session_id preserved).\n\n"
    "Please continue the assigned workspace task from where it left off. Re-read the state snapshot "
    "at the path given in the original assignment, review any files you changed, and pick up from "
    "the last actionable step. The task details and report endpoint are repeated below.\n\n"
    "If the task was already complete before the error, immediately post a ready_for_review or "
    "completed report instead of redoing work."
)
HARD_RECOVERY_REVIEWER_MESSAGE = (
    "⚠️  Your previous context was automatically cleared because the reviewer encountered a "
    "persistent API error and could not continue. A fresh context has been started for you within "
    "the SAME conversation (session_id preserved).\n\n"
    "Please continue the review for the assigned workspace task. Re-read the worker's latest "
    "report and judge the task against the Goal Packet acceptance criteria. The review prompt "
    "and report endpoint are repeated below."
)
ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024
ARTIFACT_PREVIEW_MAX_BYTES = 512 * 1024
MARKDOWN_ARTIFACT_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd"}
# Agents do their work inside isolated git worktrees (sibling dirs created per the
# mandatory workflow), so a report's markdown artifact frequently lives only in a
# worktree rather than under the main workspace path. We enumerate worktrees via
# ``git worktree list`` so artifact previews can resolve those files. The git call
# is bounded by this timeout and its result cached for the TTL below so building the
# board (which resolves every report ref) does not spawn one subprocess per ref.
WORKTREE_LIST_TIMEOUT_SECONDS = 5
WORKTREE_ROOT_CACHE_TTL_SECONDS = 30
# macOS NAME_MAX = 255 bytes per path component; Linux NAME_MAX is typically 255 too.
# Any changed_files / artifact_ref entry with a path component longer than this is
# certainly not a real filesystem path (it is a descriptive string accidentally placed
# in a changed_files slot). Rejecting these early avoids OSError(ENAMETOOLONG) deep
# inside pathlib / syscalls when we later join the workspace root or call .resolve().
_PATH_COMPONENT_NAME_MAX_BYTES = 255
_PATH_TOTAL_MAX_BYTES = 1024
MARKDOWN_DISCOVERY_LIMIT = 20
MARKDOWN_DISCOVERY_EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "dist",
    "node_modules",
}
IMAGE_ATTACHMENT_TYPES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _now() -> datetime:
    return datetime.now()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "workspace"


def _sort_time(task: WorkspaceTask) -> datetime:
    return task.queued_at or task.created_at


def _format_duration(total_seconds: int) -> str:
    total_seconds = max(0, total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _safe_attachment_filename(value: str, suffix: str) -> str:
    stem = Path(value or "attachment").stem
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip(".-")
    return f"{slug or 'attachment'}{suffix}"


class DeliveryUncertain(RuntimeError):
    """Raised when a message send to tmux failed ambiguously.

    The call_id has been moved to ``uncertain_call_ids`` (fail-closed): we
    cannot prove the message was not delivered, so we do NOT auto-resend and
    do NOT silently mark it delivered. The caller may retry explicitly by
    re-invoking ``send_session_message`` with the same call_id (which moves
    it back to ``pending``) or via ``retry_uncertain_delivery``.
    """


# Re-export everything (including single-underscore helpers like _now/_slug)
# so ``from ._constants import *`` carries them into the mixins and package.
__all__ = [
    "DeliveryUncertain",
    "ARTIFACT_PREVIEW_MAX_BYTES",
    "ATTACHMENT_MAX_BYTES",
    "AUTO_CONTINUE_IDLE_GRACE_SECONDS",
    "AUTO_CONTINUE_CLEAN_IDLE_GRACE_SECONDS",
    "AUTO_CONTINUE_IDLE_PROMPT_MESSAGE",
    "AUTO_CONTINUE_MAX_ATTEMPTS",
    "AUTO_CONTINUE_MAX_HARD_RECOVERIES",
    "AUTO_CONTINUE_MESSAGE",
    "AUTO_CONTINUE_MIN_INTERVAL_SECONDS",
    "AUTO_CONTINUE_REVIEWER_MESSAGE",
    "AUTO_CONTINUE_SOFT_ATTEMPTS_BEFORE_HARD_RECOVERY",
    "AUTO_REPORT_MISSING_MESSAGE",
    "AgentReport",
    "AgentReportCreate",
    "AgentReportState",
    "AgentRuntimeStatus",
    "AgentType",
    "AGENT_TAG_MAX_LENGTH",
    "normalize_agent_tag",
    "Any",
    "AutonomousIteration",
    "AutonomousRun",
    "AutonomousRunPhase",
    "AutonomyPolicy",
    "CLEAR_CONTEXT_SETTLE_SECONDS",
    "ContinueTaskRequest",
    "DispatchDecisionRequest",
    "EnsureWorkspaceAgentRequest",
    "EvaluationDecision",
    "EvaluationReport",
    "ExecutionTarget",
    "FeedbackLesson",
    "FeedbackLessonCreate",
    "FeedbackLessonStore",
    "FeedbackReaperRequest",
    "FeedbackReaperRun",
    "FeedbackSummaryRequest",
    "FeedbackSummaryRun",
    "GoalPacketStatus",
    "HARD_RECOVERY_REVIEWER_MESSAGE",
    "HARD_RECOVERY_WORKER_MESSAGE",
    "IMAGE_ATTACHMENT_TYPES",
    "INDEX_FILE",
    "INTERNAL_API_CURL",
    "INTERRUPT_SETTLE_SECONDS",
    "LEGACY_STATE_FILE",
    "MARKDOWN_ARTIFACT_SUFFIXES",
    "MARKDOWN_DISCOVERY_EXCLUDED_DIRS",
    "MARKDOWN_DISCOVERY_LIMIT",
    "MAX_AUTOMATED_REVIEW_FAILURES",
    "ManagedSession",
    "ManagedSessionStatus",
    "ManualTaskControlRequest",
    "Optional",
    "ORPHAN_TAB_PRUNE_GRACE_SECONDS",
    "PROMPT_DISPATCH_RETRY_GRACE_SECONDS",
    "PROMPT_DISPATCH_STALL_GRACE_SECONDS",
    "PROMPT_STUCK_RISK_LEVEL",
    "Path",
    "REMOTE_FORWARD_PORT_BASE",
    "RESIDENT_ACTIVITY_DEBOUNCE_SECONDS",
    "REVIEW_REAPER_DISPATCH_GRACE_SECONDS",
    "REVIEW_RUNTIME_REOPEN_GRACE_SECONDS",
    "RequestTaskReviewRequest",
    "ResidentPeriodicTask",
    "ReviewDecision",
    "ReviewProfile",
    "STATE_ROOT",
    "StartTaskRequest",
    "TaskCleanupResult",
    "TMUX_PASTE_SETTLE_SECONDS",
    "TMUX_SUBMIT_ATTEMPTS",
    "TMUX_SUBMIT_SETTLE_SECONDS",
    "TerminalAgentStatus",
    "WORKSPACE_MONITOR_INTERVAL_SECONDS",
    "WORKTREE_LIST_TIMEOUT_SECONDS",
    "WORKTREE_ROOT_CACHE_TTL_SECONDS",
    "Workspace",
    "WorkspaceArtifactPreview",
    "WorkspaceAttachment",
    "WorkspaceAttachmentCreate",
    "WorkspaceBoard",
    "WorkspaceCreate",
    "WorkspaceEnsure",
    "WorkspaceMarkdownDocument",
    "WorkspaceMarkdownDocumentSource",
    "WorkspaceSessionRole",
    "WorkspaceTask",
    "WorkspaceTaskCreate",
    "WorkspaceTaskExecutionComplexity",
    "WorkspaceTaskMode",
    "WorkspaceTaskOrigin",
    "WorkspaceTaskStatus",
    "WorkspaceTaskUpdate",
    "WorkspaceUpdate",
    "_PATH_COMPONENT_NAME_MAX_BYTES",
    "_PATH_TOTAL_MAX_BYTES",
    "_format_duration",
    "_now",
    "_safe_attachment_filename",
    "_slug",
    "_sort_time",
    "asyncio",
    "base64",
    "binascii",
    "datetime",
    "hashlib",
    "json",
    "logger",
    "logging",
    "re",
    "remote_profile_manager",
    "settings",
    "shutil",
    "state_policy",
    "tempfile",
    "timedelta",
    "timezone",
    "ttyd_manager",
    "uuid",
]
