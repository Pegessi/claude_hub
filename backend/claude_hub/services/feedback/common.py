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
FEEDBACK_SUMMARY_PROMPT_VERSION = 7

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
    text = value.lower()
    tokens = {item for item in re.findall(r"[a-zA-Z0-9_\-.]+", text) if len(item) >= 2}
    for chunk in re.findall(r"[\u3400-\u9fff]+", text):
        for size in (2, 3):
            if len(chunk) < size:
                continue
            tokens.update(chunk[index : index + size] for index in range(len(chunk) - size + 1))
    return tokens


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
