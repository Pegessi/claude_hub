"""Active lesson lifecycle and catalog behavior."""

from abc import ABC, abstractmethod

from claude_hub.services.feedback.common import (
    INLINE_LESSON_MIN_CONFIDENCE,
    INLINE_LESSON_MIN_RELEVANCE,
    INLINE_LESSONS_MAX_COUNT,
    AgentReport,
    Any,
    FeedbackLesson,
    FeedbackLessonCreate,
    FeedbackLessonDraft,
    FeedbackLessonStatus,
    FeedbackLessonValidationError,
    FeedbackReaperRequest,
    FeedbackReaperRun,
    FeedbackRecord,
    Workspace,
    WorkspaceTask,
    _tokens,
    datetime,
    json,
    uuid,
)
from claude_hub.services.feedback.persistence import FeedbackPersistenceStore


class FeedbackLessonCatalogStore(FeedbackPersistenceStore, ABC):
    @abstractmethod
    def build_reaper_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        reports: list[AgentReport],
        record: FeedbackRecord,
    ) -> str:
        """Build the bounded Reaper prompt for one persisted feedback record."""
        ...

    def reap_task_feedback(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        reports: list[AgentReport],
        payload: FeedbackReaperRequest,
    ) -> FeedbackReaperRun:
        now = self._feedback_now()
        record = self._create_feedback_record(workspace, task, reports, payload, now)
        drafts: list[FeedbackLessonDraft] = [
            self._create_lesson_draft(
                workspace,
                task,
                record,
                draft_payload,
                now,
            )
            for draft_payload in payload.lesson_drafts
        ]
        promoted_lessons = [
            self.create_lesson(
                workspace.id,
                self._lesson_create_from_draft(draft),
                now=now,
                enforce_iteration_signal=False,
            )
            for draft, draft_payload in zip(drafts, payload.lesson_drafts)
            if draft_payload.promote_to_active
        ]
        run = FeedbackReaperRun(
            id=str(uuid.uuid4()),
            workspace_id=workspace.id,
            task_id=task.id,
            record=record,
            lesson_drafts=drafts,
            promoted_lessons=promoted_lessons,
            reaper_prompt=self.build_reaper_prompt(workspace, task, reports, record),
            created_at=now,
        )
        self._write_run(workspace.id, run)
        return run

    def create_lesson(
        self,
        workspace_id: str,
        payload: FeedbackLessonCreate,
        *,
        now: datetime | None = None,
        enforce_iteration_signal: bool = True,
        allow_manual_without_evidence: bool = False,
    ) -> FeedbackLesson:
        now = now or self._feedback_now()
        summary = payload.summary.strip()
        if not summary:
            raise ValueError("Lesson summary is required")
        lessons = self.list_lessons(workspace_id, include_inactive=True)
        title = (payload.title or "").strip() or self._title_from_summary(summary)
        applies_when = self._clean_list(payload.applies_when)
        tags = self._clean_list(payload.tags)
        do = payload.do.strip()
        avoid = payload.avoid.strip()
        evidence_task_ids = self._clean_list(payload.evidence_task_ids)
        capped_confidence = self._validate_lesson_payload(
            workspace_id=workspace_id,
            applies_when=applies_when,
            do=do,
            avoid=avoid,
            evidence_task_ids=evidence_task_ids,
            source_record_ids=self._clean_list(payload.source_record_ids),
            confidence=payload.confidence,
            enforce_iteration_signal=enforce_iteration_signal,
            allow_manual_without_evidence=allow_manual_without_evidence,
        )
        fingerprint = (payload.fingerprint or "").strip()
        existing_fingerprints = {
            lesson.fingerprint
            for lesson in lessons
            if lesson.status == FeedbackLessonStatus.ACTIVE and lesson.fingerprint
        }
        if fingerprint:
            # Client-provided fingerprints must reference an existing active
            # lesson (echo-merge contract). Reject made-up fingerprints rather
            # than creating a lesson with a non-deterministic fingerprint that
            # can never be matched again.
            if fingerprint not in existing_fingerprints:
                raise FeedbackLessonValidationError(
                    f"fingerprint '{fingerprint}' does not match any active lesson; "
                    "either omit the field (server will compute a canonical fingerprint) "
                    "or echo an existing fingerprint from active_lessons verbatim to merge."
                )
        else:
            fingerprint = self._lesson_fingerprint(
                scope=payload.scope.value,
                title=title,
                summary=summary,
                applies_when=applies_when,
                do=do,
                avoid=avoid,
                tags=tags,
            )
        existing = next(
            (
                lesson
                for lesson in lessons
                if lesson.status == FeedbackLessonStatus.ACTIVE
                and lesson.fingerprint
                and lesson.fingerprint == fingerprint
            ),
            None,
        )
        if existing:
            updated = existing.model_copy(
                update={
                    "evidence_task_ids": self._unique_strings(
                        [*existing.evidence_task_ids, *evidence_task_ids]
                    ),
                    "source_draft_ids": self._unique_strings(
                        [*existing.source_draft_ids, *payload.source_draft_ids]
                    ),
                    "source_record_ids": self._unique_strings(
                        [*existing.source_record_ids, *payload.source_record_ids]
                    ),
                    "merged_from_ids": self._unique_strings(
                        [
                            *existing.merged_from_ids,
                            *([payload.id] if payload.id and payload.id != existing.id else []),
                        ]
                    ),
                    "confidence": self._max_confidence(existing.confidence, capped_confidence),
                    "last_seen_at": now,
                    "updated_at": now,
                }
            )
            self._write_lesson_index(
                workspace_id,
                [updated if lesson.id == existing.id else lesson for lesson in lessons],
            )
            return updated

        lesson_id = payload.id or self._unique_lesson_id(summary, lessons)
        lesson = FeedbackLesson(
            id=lesson_id,
            workspace_id=workspace_id,
            title=title,
            fingerprint=fingerprint,
            status=FeedbackLessonStatus.ACTIVE,
            scope=payload.scope,
            summary=summary,
            applies_when=applies_when,
            do=do,
            avoid=avoid,
            tags=tags,
            evidence_task_ids=evidence_task_ids,
            source_draft_ids=self._clean_list(payload.source_draft_ids),
            source_record_ids=self._clean_list(payload.source_record_ids),
            confidence=capped_confidence,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
        merged = [item for item in lessons if item.id != lesson.id]
        merged.append(lesson)
        self._write_lesson_index(workspace_id, merged)
        return lesson

    def archive_lesson(
        self,
        workspace_id: str,
        lesson_id: str,
        *,
        now: datetime | None = None,
    ) -> FeedbackLesson:
        now = now or self._feedback_now()
        lessons = self.list_lessons(workspace_id, include_inactive=True)
        for index, lesson in enumerate(lessons):
            if lesson.id != lesson_id:
                continue
            archived = lesson.model_copy(
                update={
                    "status": FeedbackLessonStatus.ARCHIVED,
                    "updated_at": now,
                }
            )
            lessons[index] = archived
            self._write_lesson_index(workspace_id, lessons)
            return archived
        raise KeyError(lesson_id)

    def list_lessons(
        self,
        workspace_id: str,
        *,
        include_inactive: bool = False,
    ) -> list[FeedbackLesson]:
        path = self._lesson_index_path(workspace_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        items = data.get("lessons", data if isinstance(data, list) else [])
        lessons: list[FeedbackLesson] = []
        for item in items:
            try:
                lesson = FeedbackLesson(**item)
            except Exception:
                continue
            if include_inactive or lesson.status == FeedbackLessonStatus.ACTIVE:
                lessons.append(lesson)
        return sorted(lessons, key=lambda item: (item.scope.value, item.id))

    _RECORDED_USAGE_KEY_LIMIT = 500

    @staticmethod
    def _lesson_relevance(lesson: FeedbackLesson, query_tokens: set[str]) -> float:
        """Compute field-aware lexical relevance using bilingual tokens.

        Matches in high-signal metadata (id, title, tags, applies_when) carry
        1.5 weight so a single targeted bilingual keyword clears the threshold,
        while body-only incidental hits carry 1.0 weight and require >= 2 matches.
        """
        direct_tokens = _tokens(
            " ".join(
                [
                    lesson.id,
                    lesson.title,
                    " ".join(lesson.tags),
                    " ".join(lesson.applies_when),
                ]
            )
        )
        body_tokens = _tokens(" ".join([lesson.summary, lesson.do, lesson.avoid]))
        direct_overlap = len(query_tokens & direct_tokens)
        body_only_overlap = len((query_tokens & body_tokens) - direct_tokens)
        return direct_overlap * 1.5 + body_only_overlap * 1.0

    def search_lessons(
        self,
        workspace_id: str,
        query: str,
        *,
        limit: int = 8,
        min_relevance: float = 0.0,
        min_confidence: float | None = None,
    ) -> list[FeedbackLesson]:
        query_tokens = _tokens(query)
        lessons = self.list_lessons(workspace_id)
        if not query_tokens:
            if query.strip():
                return []
            return lessons[:limit]

        scored: list[tuple[float, str, FeedbackLesson]] = []
        for lesson in lessons:
            confidence = lesson.confidence if lesson.confidence is not None else 0.5
            if min_confidence is not None and confidence < min_confidence:
                continue
            relevance = self._lesson_relevance(lesson, query_tokens)
            if relevance <= 0.0 or relevance < min_relevance:
                continue
            score = relevance + min(2, lesson.hit_count) * 0.25 + confidence * 0.5
            scored.append((score, lesson.id, lesson))
        return [lesson for _, _, lesson in sorted(scored, key=lambda item: (-item[0], item[1]))][
            :limit
        ]

    def get_lesson(
        self,
        workspace_id: str,
        lesson_id: str,
    ) -> FeedbackLesson:
        lessons = self.list_lessons(workspace_id, include_inactive=True)
        for lesson in lessons:
            if lesson.id == lesson_id:
                return lesson
        raise KeyError(lesson_id)

    _CONTEXT_LIMIT_DEFAULT = INLINE_LESSONS_MAX_COUNT

    def lesson_context_payload(
        self,
        workspace_id: str,
        query: str,
        *,
        limit: int = _CONTEXT_LIMIT_DEFAULT,
        min_confidence: float = INLINE_LESSON_MIN_CONFIDENCE,
        min_relevance: float = INLINE_LESSON_MIN_RELEVANCE,
    ) -> list[dict[str, Any]]:
        """Return relevance- and confidence-filtered actionable lessons for inline prompt use.

        Empty queries, zero matches, and lessons below relevance/confidence
        thresholds return an empty list so prompts incur zero bloat.
        """
        matched: list[FeedbackLesson]
        limit = min(max(limit, 1), 10)
        if query.strip() and _tokens(query):
            matched = self.search_lessons(
                workspace_id,
                query,
                limit=limit,
                min_relevance=min_relevance,
                min_confidence=min_confidence,
            )
        else:
            matched = []

        index: list[dict[str, Any]] = []
        for lesson in matched:
            title = (lesson.title or "").strip() or self._title_from_summary(lesson.summary)
            index.append(
                {
                    "id": lesson.id[:80],
                    "title": title[:100],
                    "summary": lesson.summary[:200],
                    "applies_when": [item[:60] for item in lesson.applies_when[:4]],
                    "do": lesson.do[:200],
                    "avoid": lesson.avoid[:200],
                    "tags": [tag[:24] for tag in lesson.tags[:6]],
                    "confidence": lesson.confidence,
                }
            )
        return index

    def record_lesson_hit(
        self,
        workspace_id: str,
        lesson_ids: list[str],
        *,
        task_id: str | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        """Record hit usage for matched/inlined lessons, deduplicated per (lesson_id, task_id)."""
        unique_ids = self._clean_list(lesson_ids)
        if not unique_ids:
            return []
        if not task_id or not task_id.strip():
            self.increment_lesson_usage(workspace_id, unique_ids, success=False, now=now)
            return unique_ids

        clean_task_id = task_id.strip()
        index = self._read_feedback_index(workspace_id)
        recorded_hits = [
            str(entry) for entry in index.get("recorded_hits", []) if str(entry).strip()
        ]
        seen = set(recorded_hits)
        newly_hit: list[str] = []
        for lid in unique_ids:
            key = f"{clean_task_id}:{lid}"
            if key in seen:
                continue
            seen.add(key)
            recorded_hits.append(key)
            newly_hit.append(lid)
        if not newly_hit:
            return []
        self.increment_lesson_usage(workspace_id, newly_hit, success=False, now=now)
        index = self._read_feedback_index(workspace_id)
        index["recorded_hits"] = recorded_hits[-self._RECORDED_USAGE_KEY_LIMIT :]
        self._write_feedback_index(workspace_id, index)
        return newly_hit

    def record_lesson_success(
        self,
        workspace_id: str,
        lesson_ids: list[str],
        *,
        task_id: str | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        """Record completion success for lessons without double-counting prior dispatch hits."""
        unique_ids = self._clean_list(lesson_ids)
        if not unique_ids:
            return []
        if not task_id or not task_id.strip():
            self.increment_lesson_usage(workspace_id, unique_ids, success=True, now=now)
            return unique_ids

        clean_task_id = task_id.strip()
        index = self._read_feedback_index(workspace_id)
        recorded_hits = [
            str(entry) for entry in index.get("recorded_hits", []) if str(entry).strip()
        ]
        recorded_successes = [
            str(entry) for entry in index.get("recorded_successes", []) if str(entry).strip()
        ]
        seen_hits = set(recorded_hits)
        seen_successes = set(recorded_successes)

        with_hit: list[str] = []
        success_only: list[str] = []
        recorded_now: list[str] = []
        for lid in unique_ids:
            key = f"{clean_task_id}:{lid}"
            if key in seen_successes:
                continue
            seen_successes.add(key)
            recorded_successes.append(key)
            recorded_now.append(lid)
            if key in seen_hits:
                success_only.append(lid)
            else:
                seen_hits.add(key)
                recorded_hits.append(key)
                with_hit.append(lid)

        if with_hit:
            self.increment_lesson_usage(
                workspace_id, with_hit, success=True, count_hit=True, now=now
            )
        if success_only:
            self.increment_lesson_usage(
                workspace_id, success_only, success=True, count_hit=False, now=now
            )
        if recorded_now:
            index = self._read_feedback_index(workspace_id)
            index["recorded_hits"] = recorded_hits[-self._RECORDED_USAGE_KEY_LIMIT :]
            index["recorded_successes"] = recorded_successes[-self._RECORDED_USAGE_KEY_LIMIT :]
            self._write_feedback_index(workspace_id, index)
        return recorded_now

    def record_lesson_take(
        self,
        workspace_id: str,
        lesson_ids: list[str],
        *,
        now: datetime | None = None,
    ) -> None:
        self.increment_lesson_usage(workspace_id, lesson_ids, success=False, now=now)

    def increment_lesson_usage(
        self,
        workspace_id: str,
        lesson_ids: list[str],
        *,
        success: bool = False,
        count_hit: bool = True,
        now: datetime | None = None,
    ) -> None:
        if not lesson_ids:
            return
        now = now or self._feedback_now()
        lessons = self.list_lessons(workspace_id, include_inactive=True)
        id_set = {str(lid) for lid in lesson_ids if str(lid).strip()}
        if not id_set:
            return
        changed = False
        for index, lesson in enumerate(lessons):
            if lesson.id not in id_set:
                continue
            update: dict[str, Any] = {
                "last_used_at": now,
            }
            if count_hit:
                update["hit_count"] = (lesson.hit_count or 0) + 1
            if success:
                update["success_count"] = (lesson.success_count or 0) + 1
            lessons[index] = lesson.model_copy(update=update)
            changed = True
        if changed:
            self._write_lesson_index(workspace_id, lessons)

    def render_lessons_catalog_md(
        self,
        workspace_id: str,
        workspace_name: str | None = None,
    ) -> str:
        lessons = self.list_lessons(workspace_id, include_inactive=True)
        active = [l for l in lessons if l.status == FeedbackLessonStatus.ACTIVE]
        archived = [l for l in lessons if l.status == FeedbackLessonStatus.ARCHIVED]
        lines: list[str] = []
        title = f"Lessons Catalog — {workspace_name or workspace_id}"
        lines.append(f"# {title}")
        lines.append("")
        lines.append(
            f"Auto-generated from `feedback/lesson-index.json`. "
            f"{len(active)} active, {len(archived)} archived."
        )
        lines.append("")
        lines.append("## Active Lessons")
        lines.append("")
        if not active:
            lines.append("_No active lessons yet._")
            lines.append("")
        for lesson in active:
            confidence = lesson.confidence if lesson.confidence is not None else 0.0
            lines.append(f"### {lesson.title}")
            lines.append("")
            lines.append(
                f"- **id**: `{lesson.id}`  "
                f"**scope**: `{lesson.scope.value}`  "
                f"**confidence**: {confidence:.2f}  "
                f"**hits**: {lesson.hit_count or 0}  "
                f"**successes**: {lesson.success_count or 0}"
            )
            if lesson.last_used_at:
                lines.append(f"- **last_used_at**: {lesson.last_used_at.isoformat()}")
            if lesson.tags:
                lines.append(f"- **tags**: {', '.join(f'`{t}`' for t in lesson.tags)}")
            lines.append("")
            lines.append(f"**Summary**: {lesson.summary}")
            lines.append("")
            if lesson.applies_when:
                lines.append("**Applies when**:")
                for item in lesson.applies_when:
                    lines.append(f"- {item}")
                lines.append("")
            if lesson.do:
                lines.append(f"**Do**: {lesson.do}")
                lines.append("")
            if lesson.avoid:
                lines.append(f"**Avoid**: {lesson.avoid}")
                lines.append("")
            if lesson.evidence_task_ids:
                lines.append(
                    "**Evidence tasks**: "
                    + ", ".join(f"`{tid}`" for tid in lesson.evidence_task_ids)
                )
                lines.append("")
        if archived:
            lines.append("## Archived Lessons")
            lines.append("")
            for lesson in archived:
                lines.append(f"- `{lesson.id}` — {lesson.title}")
            lines.append("")
        return "\n".join(lines)
