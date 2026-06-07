import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import (
    AgentReport,
    FeedbackLesson,
    FeedbackLessonCreate,
    FeedbackLessonDraft,
    FeedbackLessonDraftCreate,
    FeedbackLessonStatus,
    FeedbackReaperRequest,
    FeedbackReaperRun,
    FeedbackRecord,
    Workspace,
    WorkspaceTask,
)


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


class FeedbackLessonStore:
    """Workspace-scoped feedback records, lesson drafts, and active lesson index."""

    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root

    def _feedback_dir(self, workspace_id: str) -> Path:
        return self.state_root / workspace_id / "feedback"

    def _records_dir(self, workspace_id: str) -> Path:
        return self._feedback_dir(workspace_id) / "records"

    def _drafts_dir(self, workspace_id: str) -> Path:
        return self._feedback_dir(workspace_id) / "lesson-drafts"

    def _lesson_index_path(self, workspace_id: str) -> Path:
        return self._feedback_dir(workspace_id) / "lesson-index.json"

    def reap_task_feedback(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        reports: list[AgentReport],
        payload: FeedbackReaperRequest,
    ) -> FeedbackReaperRun:
        now = _now()
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
    ) -> FeedbackLesson:
        now = now or _now()
        summary = payload.summary.strip()
        if not summary:
            raise ValueError("Lesson summary is required")
        lessons = self.list_lessons(workspace_id, include_inactive=True)
        lesson_id = payload.id or self._unique_lesson_id(summary, lessons)
        title = (payload.title or "").strip() or self._title_from_summary(summary)
        lesson = FeedbackLesson(
            id=lesson_id,
            workspace_id=workspace_id,
            title=title,
            status=FeedbackLessonStatus.ACTIVE,
            scope=payload.scope,
            summary=summary,
            applies_when=self._clean_list(payload.applies_when),
            do=payload.do.strip(),
            avoid=payload.avoid.strip(),
            tags=self._clean_list(payload.tags),
            evidence_task_ids=self._clean_list(payload.evidence_task_ids),
            source_draft_ids=self._clean_list(payload.source_draft_ids),
            confidence=payload.confidence,
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
        now = now or _now()
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

    def search_lessons(
        self,
        workspace_id: str,
        query: str,
        *,
        limit: int = 8,
    ) -> list[FeedbackLesson]:
        query_tokens = _tokens(query)
        lessons = self.list_lessons(workspace_id)
        if not query_tokens:
            if query.strip():
                return []
            return lessons[:limit]

        scored: list[tuple[float, FeedbackLesson]] = []
        for lesson in lessons:
            haystack = " ".join(
                [
                    lesson.id,
                    lesson.title,
                    lesson.summary,
                    lesson.do,
                    lesson.avoid,
                    " ".join(lesson.applies_when),
                    " ".join(lesson.tags),
                ]
            )
            lesson_tokens = _tokens(haystack)
            overlap = len(query_tokens & lesson_tokens)
            if overlap == 0:
                continue
            confidence = lesson.confidence if lesson.confidence is not None else 0.5
            score = overlap + min(2, lesson.hit_count) * 0.25 + confidence * 0.2
            scored.append((score, lesson))
        return [lesson for _, lesson in sorted(scored, key=lambda item: item[0], reverse=True)][
            :limit
        ]

    def lesson_context_payload(
        self,
        workspace_id: str,
        query: str,
        *,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": lesson.id,
                "title": lesson.title,
                "scope": lesson.scope.value,
                "summary": lesson.summary,
                "applies_when": lesson.applies_when,
                "do": lesson.do,
                "avoid": lesson.avoid,
                "evidence_task_ids": lesson.evidence_task_ids,
                "confidence": lesson.confidence,
            }
            for lesson in self.search_lessons(workspace_id, query, limit=limit)
        ]

    def build_reaper_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        reports: list[AgentReport],
        record: FeedbackRecord,
    ) -> str:
        package = {
            "workspace": {
                "id": workspace.id,
                "name": workspace.name,
                "target": workspace.target.value,
                "path": workspace.path,
            },
            "task": task.model_dump(mode="json"),
            "feedback_record": record.model_dump(mode="json"),
            "reports": [self._compact_report(report) for report in reports],
        }
        return (
            "You are an internal Feedback Reaper for Claude Hub.\n\n"
            "Condense the task evidence into reusable lesson_drafts. Keep lessons specific, "
            "actionable, and scoped to where they are valid. Do not invent evidence. Prefer "
            "workspace scope unless the same lesson is clearly reusable across related workspaces.\n\n"
            "Return JSON only in this shape:\n"
            "{\n"
            '  "lesson_drafts": [\n'
            "    {\n"
            '      "summary": "short reusable lesson",\n'
            '      "applies_when": ["conditions"],\n'
            '      "do": "recommended action",\n'
            '      "avoid": "failure pattern to avoid",\n'
            '      "tags": ["tag"],\n'
            '      "scope": "workspace",\n'
            '      "confidence": 0.8,\n'
            '      "promote_to_active": false\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Task feedback package JSON:\n"
            f"{json.dumps(package, indent=2, ensure_ascii=False)}"
        )

    def _create_feedback_record(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        reports: list[AgentReport],
        payload: FeedbackReaperRequest,
        now: datetime,
    ) -> FeedbackRecord:
        summary = payload.summary.strip() if payload.summary else ""
        if not summary:
            summary = self._default_feedback_summary(task, reports)
        record = FeedbackRecord(
            id=str(uuid.uuid4()),
            workspace_id=workspace.id,
            task_id=task.id,
            source=payload.source,
            source_id=None,
            summary=summary,
            tags=self._clean_list(payload.tags),
            report_ids=[report.id for report in reports],
            artifact_refs=self._unique_strings(
                artifact for report in reports for artifact in report.artifact_refs
            ),
            created_at=now,
        )
        self._write_record(workspace.id, task.id, record)
        return record

    def _create_lesson_draft(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        record: FeedbackRecord,
        payload: FeedbackLessonDraftCreate,
        now: datetime,
    ) -> FeedbackLessonDraft:
        summary = payload.summary.strip()
        if not summary:
            raise ValueError("Lesson draft summary is required")
        draft = FeedbackLessonDraft(
            id=str(uuid.uuid4()),
            workspace_id=workspace.id,
            task_id=task.id,
            source_record_ids=[record.id],
            summary=summary,
            applies_when=self._clean_list(payload.applies_when),
            do=payload.do.strip(),
            avoid=payload.avoid.strip(),
            tags=self._clean_list(payload.tags),
            scope=payload.scope,
            evidence_task_ids=[task.id],
            confidence=payload.confidence,
            created_at=now,
        )
        self._write_draft(workspace.id, task.id, draft)
        return draft

    def _lesson_create_from_draft(self, draft: FeedbackLessonDraft) -> FeedbackLessonCreate:
        return FeedbackLessonCreate(
            id=self._unique_lesson_slug(draft.summary),
            title=self._title_from_summary(draft.summary),
            summary=draft.summary,
            applies_when=draft.applies_when,
            do=draft.do,
            avoid=draft.avoid,
            tags=draft.tags,
            scope=draft.scope,
            evidence_task_ids=draft.evidence_task_ids,
            source_draft_ids=[draft.id],
            confidence=draft.confidence,
        )

    def _write_record(self, workspace_id: str, task_id: str, record: FeedbackRecord) -> None:
        directory = self._records_dir(workspace_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self._timestamp(record.created_at)}-{task_id}-{record.id}.json"
        path.write_text(record.model_dump_json(indent=2), encoding="utf-8")

    def _write_draft(self, workspace_id: str, task_id: str, draft: FeedbackLessonDraft) -> None:
        directory = self._drafts_dir(workspace_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self._timestamp(draft.created_at)}-{task_id}-{draft.id}.json"
        path.write_text(draft.model_dump_json(indent=2), encoding="utf-8")

    def _write_run(self, workspace_id: str, run: FeedbackReaperRun) -> None:
        directory = self._feedback_dir(workspace_id) / "runs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self._timestamp(run.created_at)}-{run.task_id}-{run.id}.json"
        path.write_text(run.model_dump_json(indent=2), encoding="utf-8")

    def _write_lesson_index(self, workspace_id: str, lessons: list[FeedbackLesson]) -> None:
        path = self._lesson_index_path(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "updated_at": _now().isoformat(),
            "lessons": [lesson.model_dump(mode="json") for lesson in lessons],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _unique_lesson_id(
        self,
        summary: str,
        lessons: list[FeedbackLesson],
    ) -> str:
        base = self._unique_lesson_slug(summary)
        existing = {lesson.id for lesson in lessons}
        if base not in existing:
            return base
        suffix = 2
        while f"{base}-{suffix}" in existing:
            suffix += 1
        return f"{base}-{suffix}"

    def _unique_lesson_slug(self, summary: str) -> str:
        return _slug(summary)[:80].strip("-") or f"lesson-{uuid.uuid4().hex[:8]}"

    def _title_from_summary(self, summary: str) -> str:
        title = re.split(r"[。.!?]\s*", summary.strip(), maxsplit=1)[0].strip()
        if len(title) > 80:
            return f"{title[:77].rstrip()}..."
        return title or "Workspace lesson"

    def _default_feedback_summary(
        self,
        task: WorkspaceTask,
        reports: list[AgentReport],
    ) -> str:
        if reports:
            return f"Manual feedback reaper run for task {task.title}: {reports[-1].message}"
        return f"Manual feedback reaper run for task {task.title}."

    def _compact_report(self, report: AgentReport) -> dict[str, Any]:
        return {
            "id": report.id,
            "state": report.state.value,
            "session_id": report.session_id,
            "message": report.message,
            "message_en": report.message_en,
            "message_zh": report.message_zh,
            "changed_files": report.changed_files,
            "validation": report.validation,
            "risks": report.risks,
            "acceptance_check": [item.model_dump(mode="json") for item in report.acceptance_check],
            "artifact_refs": report.artifact_refs,
            "review_decision": report.review_decision.value,
            "review_reason": report.review_reason,
            "risk_level": report.risk_level,
            "created_at": report.created_at.isoformat(),
        }

    def _clean_list(self, values: list[str]) -> list[str]:
        return self._unique_strings(str(value).strip() for value in values if str(value).strip())

    def _unique_strings(self, values: Any) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            text = str(value).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    def _timestamp(self, value: datetime) -> str:
        return value.isoformat(timespec="seconds").replace(":", "-")
