import hashlib
import json
import re
import uuid
from dataclasses import dataclass
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

FEEDBACK_INDEX_SCHEMA_VERSION = 1
FEEDBACK_SUMMARY_PROMPT_VERSION = 3


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

    def _feedback_index_path(self, workspace_id: str) -> Path:
        return self._feedback_dir(workspace_id) / "index.json"

    def _summary_runs_dir(self, workspace_id: str) -> Path:
        return self._feedback_dir(workspace_id) / "summary-runs"

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
    ) -> FeedbackLesson:
        now = now or _now()
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
            confidence=payload.confidence,
            enforce_iteration_signal=enforce_iteration_signal,
        )
        fingerprint = (payload.fingerprint or "").strip() or self._lesson_fingerprint(
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

    def prepare_summary_input(
        self,
        workspace_id: str,
        task_records_dir: Path,
        *,
        mode: FeedbackSummaryMode,
        limit: int,
        force: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _now()
        limit = min(max(limit, 1), 200)
        index = self._read_feedback_index(workspace_id)
        existing_entries = {
            str(item.get("path") or ""): item
            for item in index.get("processed_task_records", [])
            if item.get("path")
        }
        record_paths = sorted(task_records_dir.glob("*.json")) if task_records_dir.exists() else []
        selected_entries: list[FeedbackProcessedTaskRecord] = []
        processed_entries: dict[str, dict[str, Any]] = dict(existing_entries)

        first_scan = not existing_entries
        for path in record_paths:
            path_key = str(path)
            digest_bytes = path.read_bytes()
            sha256 = hashlib.sha256(digest_bytes).hexdigest()
            cached = existing_entries.get(path_key)
            should_read = (
                force
                or first_scan
                or mode == FeedbackSummaryMode.FULL
                or not cached
                or cached.get("sha256") != sha256
            )
            if should_read:
                try:
                    record_payload = json.loads(digest_bytes.decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                entry = FeedbackProcessedTaskRecord(
                    task_id=str(record_payload.get("task", {}).get("id") or path.stem),
                    path=path_key,
                    sha256=sha256,
                    digest=self._digest_task_record(record_payload),
                    summarized_at=now,
                )
                processed_entries[path_key] = entry.model_dump(mode="json")
                selected_entries.append(entry)

        if first_scan and selected_entries:
            index["last_full_scan_at"] = now.isoformat()
        sorted_processed = [
            processed_entries[key]
            for key in sorted(processed_entries)
            if key in {str(path) for path in record_paths}
        ]
        index.update(
            {
                "schema_version": FEEDBACK_INDEX_SCHEMA_VERSION,
                "workspace_id": workspace_id,
                "prompt_version": FEEDBACK_SUMMARY_PROMPT_VERSION,
                "processed_task_records": sorted_processed,
            }
        )
        self._write_feedback_index(workspace_id, index)

        if not selected_entries and (force or mode == FeedbackSummaryMode.FULL):
            selected_entries = [
                FeedbackProcessedTaskRecord(**item) for item in sorted_processed[-limit:]
            ]
        if mode != FeedbackSummaryMode.INCREMENTAL:
            selected_entries = selected_entries[-limit:]
        return {
            "run_id": str(uuid.uuid4()),
            "workspace_id": workspace_id,
            "mode": mode.value,
            "force": force,
            "limit": limit,
            "cache_hit": not selected_entries,
            "prompt_version": FEEDBACK_SUMMARY_PROMPT_VERSION,
            "first_scan": first_scan,
            "processed_count": len(sorted_processed),
            "input_records": [entry.model_dump(mode="json") for entry in selected_entries],
            "input_record_ids": [entry.task_id for entry in selected_entries],
            "skipped_reason": "no_new_task_records" if not selected_entries else None,
        }

    def write_summary_run(self, workspace_id: str, run: FeedbackSummaryRun) -> None:
        directory = self._summary_runs_dir(workspace_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self._timestamp(run.created_at)}-{run.id}.json"
        path.write_text(run.model_dump_json(indent=2), encoding="utf-8")

    def complete_summary_run(
        self,
        workspace_id: str,
        task_id: str,
        report: AgentReport,
        *,
        now: datetime | None = None,
    ) -> FeedbackSummaryRun | None:
        now = now or _now()
        run_path, run = self._find_summary_run_by_task(workspace_id, task_id)
        if not run_path or not run:
            return None
        created_ids, merged_ids, skipped_reason = self._summary_outcome_from_report(report)
        updated = run.model_copy(
            update={
                "created_lesson_ids": created_ids,
                "merged_lesson_ids": merged_ids,
                "skipped_reason": skipped_reason,
                "completed_at": now,
            }
        )
        run_path.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
        return updated

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
        self._sync_lesson_fingerprints(workspace_id, lessons)

    def _read_feedback_index(self, workspace_id: str) -> dict[str, Any]:
        path = self._feedback_index_path(workspace_id)
        if not path.exists():
            return {
                "schema_version": FEEDBACK_INDEX_SCHEMA_VERSION,
                "workspace_id": workspace_id,
                "prompt_version": FEEDBACK_SUMMARY_PROMPT_VERSION,
                "last_full_scan_at": None,
                "processed_task_records": [],
                "lesson_fingerprints": {},
            }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("schema_version", FEEDBACK_INDEX_SCHEMA_VERSION)
        data.setdefault("workspace_id", workspace_id)
        data.setdefault("prompt_version", FEEDBACK_SUMMARY_PROMPT_VERSION)
        data.setdefault("last_full_scan_at", None)
        data.setdefault("processed_task_records", [])
        data.setdefault("lesson_fingerprints", {})
        return data

    def _write_feedback_index(self, workspace_id: str, payload: dict[str, Any]) -> None:
        path = self._feedback_index_path(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload["updated_at"] = _now().isoformat()
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _sync_lesson_fingerprints(
        self,
        workspace_id: str,
        lessons: list[FeedbackLesson],
    ) -> None:
        index = self._read_feedback_index(workspace_id)
        index["lesson_fingerprints"] = {
            lesson.fingerprint: lesson.id
            for lesson in lessons
            if lesson.status == FeedbackLessonStatus.ACTIVE and lesson.fingerprint
        }
        self._write_feedback_index(workspace_id, index)

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

    def _lesson_fingerprint(
        self,
        *,
        scope: str,
        title: str,
        summary: str,
        applies_when: list[str],
        do: str,
        avoid: str,
        tags: list[str],
    ) -> str:
        normalized = self._normalize_for_fingerprint(
            " ".join(
                [
                    title,
                    summary,
                    " ".join(applies_when),
                    do,
                    avoid,
                    " ".join(sorted(tags)),
                ]
            )
        )
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
        return f"{scope}:{digest}"

    def _normalize_for_fingerprint(self, value: str) -> str:
        lowered = value.lower()
        lowered = re.sub(r"[^\w\u3400-\u9fff]+", " ", lowered)
        return re.sub(r"\s+", " ", lowered).strip()

    def _max_confidence(
        self,
        current: float | None,
        incoming: float | None,
    ) -> float | None:
        if current is None:
            return incoming
        if incoming is None:
            return current
        return max(current, incoming)

    def _title_from_summary(self, summary: str) -> str:
        title = re.split(r"[。.!?]\s*", summary.strip(), maxsplit=1)[0].strip()
        if len(title) > 80:
            return f"{title[:77].rstrip()}..."
        return title or "Workspace lesson"

    def _digest_task_record(self, payload: dict[str, Any]) -> FeedbackTaskDigest:
        raw_task = payload.get("task")
        raw_artifacts = payload.get("artifacts")
        raw_reports = payload.get("reports")
        task: dict[str, Any] = raw_task if isinstance(raw_task, dict) else {}
        artifacts: dict[str, Any] = raw_artifacts if isinstance(raw_artifacts, dict) else {}
        reports: list[Any] = raw_reports if isinstance(raw_reports, list) else []
        report_state_sequence = [
            str(report.get("state"))
            for report in reports
            if isinstance(report, dict) and report.get("state")
        ]
        review_failed_count = sum(1 for state in report_state_sequence if state == "review_failed")
        needs_input_count = sum(1 for state in report_state_sequence if state == "needs_input")
        return FeedbackTaskDigest(
            task_id=str(task.get("id") or payload.get("task_id") or ""),
            title=str(task.get("title") or ""),
            status=str(task.get("status") or ""),
            final_summary=str(payload.get("final_summary") or ""),
            changed_files=self._list_value(artifacts.get("changed_files")),
            validation=self._list_value(artifacts.get("validation")),
            risks=self._list_value(artifacts.get("risks")),
            report_states=self._unique_strings(report_state_sequence),
            report_state_sequence=report_state_sequence,
            review_failed_count=review_failed_count,
            needs_input_count=needs_input_count,
            report_total=len(reports),
            completed_at=str(task.get("completed_at") or ""),
        )

    def _list_value(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item).strip()]

    def _find_summary_run_by_task(
        self,
        workspace_id: str,
        task_id: str,
    ) -> tuple[Path | None, FeedbackSummaryRun | None]:
        directory = self._summary_runs_dir(workspace_id)
        if not directory.exists():
            return None, None
        for path in sorted(directory.glob("*.json"), reverse=True):
            try:
                run = FeedbackSummaryRun(**json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if run.task_id == task_id:
                return path, run
        return None, None

    def _summary_outcome_from_report(
        self,
        report: AgentReport,
    ) -> tuple[list[str], list[str], str | None]:
        text = "\n".join(
            item
            for item in [
                report.message,
                report.validation or "",
                report.risks or "",
                report.review_reason or "",
            ]
            if item
        )
        created = self._extract_named_ids(text, "created_lesson_ids")
        merged = self._extract_named_ids(text, "merged_lesson_ids")
        skipped = self._extract_named_value(text, "skipped_reason")
        if not created and not merged and not skipped:
            skipped = "missing_summary_outcome"
        return created, merged, skipped

    def _extract_named_ids(self, text: str, field: str) -> list[str]:
        value = self._extract_named_value(text, field)
        if not value:
            return []
        tokens = self._clean_list(re.split(r"[,\s]+", value.strip("[] ")))
        slug_re = re.compile(r"^[a-z0-9][a-z0-9_\-.]*$")
        return [token for token in tokens if slug_re.match(token)]

    def _extract_named_value(self, text: str, field: str) -> str | None:
        match = re.search(rf"{re.escape(field)}\s*[:=]\s*([^\n;|]+)", text)
        if not match:
            return None
        return match.group(1).strip().strip('"')

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

    def _validate_lesson_payload(
        self,
        *,
        workspace_id: str,
        applies_when: list[str],
        do: str,
        avoid: str,
        evidence_task_ids: list[str],
        confidence: float | None,
        enforce_iteration_signal: bool = True,
    ) -> float | None:
        """Enforce the workspace lesson contract; return a (possibly capped) confidence.

        Always-on rules:
        - applies_when must contain at least one condition.
        - do and avoid must be non-empty after stripping.
        - evidence_task_ids must cite at least one task.

        Iteration-signal rules (skipped only when enforce_iteration_signal is False,
        which is reserved for human-confirmed manual reaper flows):
        - Single-evidence lessons must mechanically satisfy Signal A in the cited
          task (review_failed_count >= 1 OR needs_input_count >= 2 in the task's
          report state sequence).
        - Multi-evidence (Signal B) lessons require at least one cited task with
          rf+ni >= 1 so recurrence is not asserted from textual similarity alone.

        Confidence cap (always on):
        - Single-evidence lessons: capped at SINGLE_EVIDENCE_CONFIDENCE_CAP (0.6).
        - Multi-evidence lessons: capped at MULTI_EVIDENCE_CONFIDENCE_CAP (0.85).
        """
        if not applies_when:
            raise FeedbackLessonValidationError(
                "applies_when must contain at least one condition (file glob, runtime, "
                "command, env, or task shape)"
            )
        if not do:
            raise FeedbackLessonValidationError("do is required and must be non-empty")
        if not avoid:
            raise FeedbackLessonValidationError("avoid is required and must be non-empty")
        if not evidence_task_ids:
            raise FeedbackLessonValidationError(
                "evidence_task_ids must cite at least one task that supports the lesson"
            )

        if enforce_iteration_signal:
            signals = [
                self._task_iteration_signal(workspace_id, task_id) for task_id in evidence_task_ids
            ]
            observed_signals = [signal for signal in signals if signal is not None]

            if len(evidence_task_ids) == 1:
                signal = signals[0]
                if signal is None:
                    raise FeedbackLessonValidationError(
                        f"single-evidence lesson cites task {evidence_task_ids[0]} but no task "
                        "record was found on disk; lesson cannot be verified"
                    )
                if not signal.has_signal_a:
                    raise FeedbackLessonValidationError(
                        "single-evidence lesson requires Signal A "
                        "(review_failed_count >= 1 OR needs_input_count >= 2) in the cited "
                        f"task; task {signal.task_id} has rf={signal.review_failed_count}, "
                        f"ni={signal.needs_input_count}"
                    )
            else:
                if not any(signal.has_any_iteration for signal in observed_signals):
                    raise FeedbackLessonValidationError(
                        "multi-evidence Signal B lesson requires at least one cited task with "
                        "review_failed_count + needs_input_count >= 1; cross-task recurrence "
                        "cannot be asserted from final_summary similarity alone"
                    )

        cap = (
            SINGLE_EVIDENCE_CONFIDENCE_CAP
            if len(evidence_task_ids) == 1
            else MULTI_EVIDENCE_CONFIDENCE_CAP
        )
        if confidence is None:
            return None
        return min(confidence, cap)

    def _task_iteration_signal(
        self,
        workspace_id: str,
        task_id: str,
    ) -> _TaskIterationSignal | None:
        records_dir = self.state_root / workspace_id / "task_records"
        if not records_dir.exists():
            return None
        for path in records_dir.glob(f"*{task_id}*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            task_block = payload.get("task")
            if not isinstance(task_block, dict):
                continue
            if str(task_block.get("id") or "") != task_id:
                continue
            reports = payload.get("reports") or []
            states = [
                str(report.get("state"))
                for report in reports
                if isinstance(report, dict) and report.get("state")
            ]
            return _TaskIterationSignal(
                task_id=task_id,
                review_failed_count=sum(1 for state in states if state == "review_failed"),
                needs_input_count=sum(1 for state in states if state == "needs_input"),
                report_total=len(reports),
            )
        return None

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
