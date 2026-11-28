import hashlib
import json
import re
import uuid
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ...models import (
    AgentReport,
    FeedbackFailureEvidence,
    FeedbackLesson,
    FeedbackLessonCreate,
    FeedbackLessonDraft,
    FeedbackLessonDraftCreate,
    FeedbackLessonStatus,
    FeedbackProcessedTaskRecord,
    FeedbackReaperRequest,
    FeedbackReaperRun,
    FeedbackRecord,
    FeedbackSummaryMode,
    FeedbackSummaryRun,
    FeedbackTaskDigest,
    Workspace,
    WorkspaceTask,
)
from ..feedback_automation import ChatCorrection, FeedbackAutomationStore

FEEDBACK_INDEX_SCHEMA_VERSION = 1
FEEDBACK_SUMMARY_PROMPT_VERSION = 8

INLINE_LESSONS_MAX_COUNT = 3
INLINE_LESSONS_MAX_CHARS = 1200
INLINE_LESSON_MIN_CONFIDENCE = 0.4
INLINE_LESSON_MIN_RELEVANCE = 1.5

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*")
_ASCII_SUBTOKEN_SPLIT_RE = re.compile(r"[._/-]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]{2,}")
_STOP_WORDS = frozenset(
    "the and for with that this are not when must should from into each all any "
    "you your what where how its has have was were will can "
    "to in of on at by is be as or an if it do we they our their".split()
)

# Reaper-prompt digest truncation limits keep Feedback Reaper prompts bounded
# so smaller-context agents (codex, cursor) can process them. All free-text
# fields that flow into the prompt are capped to a fixed char/item maximum.
# A global hard char budget (_REAPER_PROMPT_HARD_CHAR_LIMIT) is enforced at
# prompt-assembly time as defense in depth. Target worst-case (30 digests,
# all fields at cap) fits safely under ~128K chars / ~32K tokens.
_DIGEST_MAX_TASK_ID = 64
_DIGEST_MAX_TITLE = 80
_DIGEST_MAX_FINAL_SUMMARY = 200
_DIGEST_MAX_ITEM_CHARS = 160
_DIGEST_MAX_PATH_CHARS = 120
_DIGEST_MAX_VALIDATION_ITEMS = 2
_DIGEST_MAX_RISKS_ITEMS = 2
_DIGEST_MAX_CHANGED_FILES = 6
_DIGEST_MAX_FAILURE_EVIDENCE = 3
# Active-lesson payload caps (these must keep the fingerprint field EXACT —
# it is the authoritative merge key; Reaper echoes it back verbatim).
_LESSON_MAX_ID = 80
_LESSON_MAX_TITLE = 80
_LESSON_MAX_SUMMARY = 120
_LESSON_MAX_TAG_LEN = 24
_LESSON_MAX_TAGS = 6


def _now() -> datetime:
    return datetime.now()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "lesson"


def _tokens(value: str) -> set[str]:
    """Extract ASCII path/identifier tokens and overlapping 2-char CJK bigrams."""
    text = value.lower()
    tokens: set[str] = set()
    for match in _ASCII_TOKEN_RE.findall(text):
        cleaned = match.strip("._/-")
        if len(cleaned) >= 2 and cleaned not in _STOP_WORDS:
            tokens.add(cleaned)
        for part in _ASCII_SUBTOKEN_SPLIT_RE.split(cleaned):
            if len(part) >= 2 and part not in _STOP_WORDS:
                tokens.add(part)
    for chunk in _CJK_RUN_RE.findall(text):
        for index in range(len(chunk) - 1):
            tokens.add(chunk[index : index + 2])
    return tokens


def _clip_inline_text(value: str, limit: int) -> str:
    collapsed = re.sub(r"\s+", " ", value).strip()
    if len(collapsed) <= limit:
        return collapsed
    if limit <= 3:
        return collapsed[:limit]
    return collapsed[: limit - 3].rstrip() + "..."


def render_inline_lessons_block(
    lessons: list[dict[str, Any]] | None,
    *,
    workspace_id: str | None = None,
    role: str = "worker",
    max_chars: int = INLINE_LESSONS_MAX_CHARS,
    max_items: int = INLINE_LESSONS_MAX_COUNT,
) -> str:
    """Render Top-N actionable workspace lessons directly inline within a strict char cap.

    Returns an empty string (0 characters) when no valid lessons are provided.
    """
    if not lessons or not isinstance(lessons, list):
        return ""
    valid = [
        item for item in lessons if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    if not valid:
        return ""
    selected = valid[: max(1, min(max_items, INLINE_LESSONS_MAX_COUNT))]

    if role == "review":
        header = "Workspace review checkpoints (verify matching lessons against the diff):"
        footer_parts = []
        if workspace_id:
            footer_parts.append(
                f"Provenance: claude-hub lessons get <id> --workspace-id {workspace_id} "
                f"(GET /api/workspaces/{workspace_id}/lessons/<id>)."
            )
        footer_parts.append("Cite violated or verified lesson IDs in findings/risks.")
        footer = " ".join(footer_parts)
    elif role == "strict_review":
        header = ""
        footer = ""
    else:
        header = "Workspace execution guardrails (apply matching lessons below):"
        footer_parts = []
        if workspace_id:
            footer_parts.append(
                f"Provenance: claude-hub lessons get <id> --workspace-id {workspace_id} "
                f"(GET /api/workspaces/{workspace_id}/lessons/<id>)."
            )
        footer_parts.append("List applied lesson IDs (or 'none') in report risks.")
        footer = " ".join(footer_parts)

    def _format_entry(
        item: dict[str, Any],
        *,
        title_cap: int,
        when_cap: int,
        action_cap: int,
    ) -> str:
        lid = _clip_inline_text(str(item.get("id") or ""), 64)
        title = _clip_inline_text(str(item.get("title") or lid), title_cap)
        raw_when = item.get("applies_when") or []
        when_items = (
            [str(w).strip() for w in raw_when if str(w).strip()]
            if isinstance(raw_when, list)
            else []
        )
        when_text = _clip_inline_text("; ".join(when_items), when_cap) if when_items else ""
        do_text = _clip_inline_text(str(item.get("do") or ""), action_cap)
        avoid_text = _clip_inline_text(str(item.get("avoid") or ""), action_cap)
        summary_text = _clip_inline_text(str(item.get("summary") or ""), action_cap)

        parts = [f"- `{lid}` | {title}"]
        if when_text:
            parts.append(f"  When: {when_text}")
        if do_text:
            parts.append(f"  Do: {do_text}")
        if avoid_text:
            parts.append(f"  Avoid: {avoid_text}")
        if not do_text and not avoid_text and summary_text:
            parts.append(f"  Rule: {summary_text}")
        return "\n".join(parts)

    def _assemble(entries: list[str]) -> str:
        lines: list[str] = []
        if header:
            lines.append(header)
        lines.extend(entries)
        if footer:
            lines.append(footer)
        body = "\n".join(lines)
        return body if role == "strict_review" else body + "\n\n"

    # Try progressively tighter per-field caps so up to 3 lessons fit cleanly within max_chars.
    for title_cap, when_cap, action_cap in (
        (72, 90, 140),
        (60, 70, 100),
        (48, 50, 72),
        (40, 36, 48),
    ):
        entries = [
            _format_entry(item, title_cap=title_cap, when_cap=when_cap, action_cap=action_cap)
            for item in selected
        ]
        rendered = _assemble(entries)
        if len(rendered) <= max_chars:
            return rendered

    # Drop lowest-ranked entries if still above budget.
    while selected:
        entries = [
            _format_entry(item, title_cap=40, when_cap=36, action_cap=48) for item in selected
        ]
        rendered = _assemble(entries)
        if len(rendered) <= max_chars:
            return rendered
        selected = selected[:-1]

    return ""


class FeedbackLessonValidationError(ValueError):
    """Raised when a lesson payload fails the workspace lesson contract."""


@dataclass(frozen=True)
class _TaskIterationSignal:
    task_id: str
    review_failed_count: int
    needs_input_count: int
    report_total: int

    @property
    def has_signal_a(self) -> bool:
        return self.review_failed_count >= 1 or self.needs_input_count >= 2

    @property
    def has_any_iteration(self) -> bool:
        return self.review_failed_count + self.needs_input_count >= 1


SINGLE_EVIDENCE_CONFIDENCE_CAP = 0.6
MULTI_EVIDENCE_CONFIDENCE_CAP = 0.85


@dataclass(frozen=True)
class FeedbackRuntimeSettings:
    """Facade-owned values that feedback stores must read at operation time."""

    index_schema_version: int
    summary_prompt_version: int
    digest_max_task_id: int
    digest_max_title: int
    digest_max_final_summary: int
    digest_max_item_chars: int
    digest_max_path_chars: int
    digest_max_validation_items: int
    digest_max_risks_items: int
    digest_max_changed_files: int
    digest_max_failure_evidence: int
    single_evidence_confidence_cap: float
    multi_evidence_confidence_cap: float


def feedback_runtime_settings() -> FeedbackRuntimeSettings:
    return FeedbackRuntimeSettings(
        index_schema_version=FEEDBACK_INDEX_SCHEMA_VERSION,
        summary_prompt_version=FEEDBACK_SUMMARY_PROMPT_VERSION,
        digest_max_task_id=_DIGEST_MAX_TASK_ID,
        digest_max_title=_DIGEST_MAX_TITLE,
        digest_max_final_summary=_DIGEST_MAX_FINAL_SUMMARY,
        digest_max_item_chars=_DIGEST_MAX_ITEM_CHARS,
        digest_max_path_chars=_DIGEST_MAX_PATH_CHARS,
        digest_max_validation_items=_DIGEST_MAX_VALIDATION_ITEMS,
        digest_max_risks_items=_DIGEST_MAX_RISKS_ITEMS,
        digest_max_changed_files=_DIGEST_MAX_CHANGED_FILES,
        digest_max_failure_evidence=_DIGEST_MAX_FAILURE_EVIDENCE,
        single_evidence_confidence_cap=SINGLE_EVIDENCE_CONFIDENCE_CAP,
        multi_evidence_confidence_cap=MULTI_EVIDENCE_CONFIDENCE_CAP,
    )
