import asyncio
import base64
import binascii
import json
import logging
import re
import tempfile
import uuid
from datetime import datetime
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
    ReviewDecision,
    ReviewProfile,
    StartTaskRequest,
    TerminalAgentStatus,
    Workspace,
    WorkspaceArtifactPreview,
    WorkspaceAttachment,
    WorkspaceAttachmentCreate,
    WorkspaceBoard,
    WorkspaceCreate,
    WorkspaceMarkdownDocument,
    WorkspaceMarkdownDocumentSource,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskExecutionComplexity,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
    WorkspaceTaskUpdate,
    WorkspaceUpdate,
)
from .. import workspace_state_policy as state_policy
from ..feedback_lessons import FeedbackLessonStore
from ..remote_profiles import remote_profile_manager
from ..ttyd_manager import ttyd_manager

logger = logging.getLogger(__name__)

STATE_ROOT = Path.home() / ".claude_hub" / "workspaces"
INDEX_FILE = STATE_ROOT / "index.json"
LEGACY_STATE_FILE = Path.home() / ".claude_hub" / "workspaces.json"
REMOTE_FORWARD_PORT_BASE = 18173
TMUX_SUBMIT_ATTEMPTS = 3
TMUX_PASTE_SETTLE_SECONDS = 0.35
TMUX_SUBMIT_SETTLE_SECONDS = 0.7
AUTO_CONTINUE_MAX_ATTEMPTS = 10
AUTO_CONTINUE_MIN_INTERVAL_SECONDS = 15
AUTO_CONTINUE_IDLE_GRACE_SECONDS = 20
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
AUTO_REPORT_MISSING_MESSAGE = (
    "The task appears complete but no workspace report was recorded. Please immediately POST "
    "the final ready_for_review or completed report with changed_files, validation, risks, "
    "the stored Goal Packet if it has not been reported yet, and acceptance_check evidence; "
    "only continue work if you find it is actually unfinished."
)
ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024
ARTIFACT_PREVIEW_MAX_BYTES = 512 * 1024
MARKDOWN_ARTIFACT_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd"}
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


# Re-export everything (including single-underscore helpers like _now/_slug)
# so ``from ._constants import *`` carries them into the mixins and package.
__all__ = [
    "ARTIFACT_PREVIEW_MAX_BYTES",
    "ATTACHMENT_MAX_BYTES",
    "AUTO_CONTINUE_IDLE_GRACE_SECONDS",
    "AUTO_CONTINUE_MAX_ATTEMPTS",
    "AUTO_CONTINUE_MESSAGE",
    "AUTO_CONTINUE_MIN_INTERVAL_SECONDS",
    "AUTO_REPORT_MISSING_MESSAGE",
    "AgentReport",
    "AgentReportCreate",
    "AgentReportState",
    "AgentRuntimeStatus",
    "AgentType",
    "Any",
    "AutonomousIteration",
    "AutonomousRun",
    "AutonomousRunPhase",
    "AutonomyPolicy",
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
    "IMAGE_ATTACHMENT_TYPES",
    "INDEX_FILE",
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
    "REVIEW_REAPER_DISPATCH_GRACE_SECONDS",
    "REVIEW_RUNTIME_REOPEN_GRACE_SECONDS",
    "RequestTaskReviewRequest",
    "ReviewDecision",
    "ReviewProfile",
    "STATE_ROOT",
    "StartTaskRequest",
    "TMUX_PASTE_SETTLE_SECONDS",
    "TMUX_SUBMIT_ATTEMPTS",
    "TMUX_SUBMIT_SETTLE_SECONDS",
    "TerminalAgentStatus",
    "WORKSPACE_MONITOR_INTERVAL_SECONDS",
    "Workspace",
    "WorkspaceArtifactPreview",
    "WorkspaceAttachment",
    "WorkspaceAttachmentCreate",
    "WorkspaceBoard",
    "WorkspaceCreate",
    "WorkspaceMarkdownDocument",
    "WorkspaceMarkdownDocumentSource",
    "WorkspaceSessionRole",
    "WorkspaceTask",
    "WorkspaceTaskCreate",
    "WorkspaceTaskExecutionComplexity",
    "WorkspaceTaskMode",
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
    "json",
    "logger",
    "logging",
    "re",
    "remote_profile_manager",
    "settings",
    "state_policy",
    "tempfile",
    "ttyd_manager",
    "uuid",
]
