"""Feedback evidence and reusable lesson schemas."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from claude_hub.models.schema_domains.runtime import (
    FeedbackLessonScope,
    FeedbackLessonStatus,
    FeedbackSourceType,
    FeedbackSummaryMode,
)


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


class FeedbackFailureEvidence(BaseModel):
    """Bounded source report excerpt, not an inferred root cause or verdict."""

    report_id: str = Field(default="", max_length=80)
    state: str = Field(default="", max_length=32)
    message: str = Field(default="", max_length=240)
    validation: str = Field(default="", max_length=320)
    risks: str = Field(default="", max_length=160)


class FeedbackTaskDigest(BaseModel):
    """Compact reusable digest for a completed workspace task record."""

    task_id: str
    source_record_id: str = Field(default="", max_length=160)
    source_tab_id: str = Field(default="", max_length=160)
    source_turn_id: str = Field(default="", max_length=160)
    source_message_id: str = Field(default="", max_length=160)
    correction: str = Field(default="", max_length=1024)
    title: str = ""
    status: str = ""
    final_summary: str = ""
    changed_files: List[str] = Field(default_factory=list)
    validation: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    report_states: List[str] = Field(default_factory=list)
    failure_evidence: List[FeedbackFailureEvidence] = Field(default_factory=list, max_length=3)
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
