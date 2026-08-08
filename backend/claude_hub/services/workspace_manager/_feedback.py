"""Feedback lesson management."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _FeedbackMixin:
    def create_feedback_lesson(
        self,
        workspace_id: str,
        payload: FeedbackLessonCreate,
    ) -> FeedbackLesson:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        return self._feedback_store().create_lesson(workspace_id, payload)

    def delete_feedback_lesson(self, workspace_id: str, lesson_id: str) -> FeedbackLesson:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        return self._feedback_store().archive_lesson(workspace_id, lesson_id)

    def get_feedback_lesson(self, workspace_id: str, lesson_id: str) -> FeedbackLesson:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        store = self._feedback_store()
        store.record_lesson_take(workspace_id, [lesson_id])
        return store.get_lesson(workspace_id, lesson_id)

    def _build_workspace_feedback_summary_prompt(
        self,
        workspace: Workspace,
        summary_input: dict[str, Any],
    ) -> str:
        # Cap active lessons for dedup context: full summaries aren't needed,
        # just id/title/tags/confidence to detect duplicates.
        lessons = self.feedback_lessons(workspace.id, limit=20)
        lesson_payload = [
            {
                "id": lesson.id,
                "title": lesson.title[:80],
                "tags": lesson.tags[:8],
                "confidence": lesson.confidence,
            }
            for lesson in lessons
        ]
        package = {
            "summary_run": {
                "id": summary_input["run_id"],
                "mode": summary_input["mode"],
                "input_record_ids": summary_input["input_record_ids"],
            },
            "active_lessons": lesson_payload,
            "input_task_digests": summary_input["input_records"],
        }
        return "\n".join(
            [
                "You are the internal Feedback Reaper. System-internal task — do not ask the human for acceptance.",
                "",
                "Goal: extract reusable workspace lessons from input_task_digests. Quality > quantity. "
                "Zero lessons is correct when no signal exists. Use only the input package; do not scan the workspace.",
                "",
                "Extraction signals (at least one required per lesson):",
                "  A) Iteration cost: single task with review_failed_count>=1 OR needs_input_count>=2.",
                "  B) Cross-task recurrence: same root pattern in >=2 digests.",
                "Implementation-detail lessons need A or B with observable evidence; a single clean-pass final_summary is not a lesson.",
                "",
                "Required lesson fields (server validates; HTTP 400 on violation — do not retry):",
                "  title (short), summary (1-2 sentences), applies_when (>=1), do, avoid, tags,",
                "  scope ('workspace'), confidence (≤0.6 single / ≤0.85 multi), evidence_task_ids.",
                "Server enforcement: applies_when/do/avoid non-empty; single-evidence tasks need review_failed≥1 or needs_input≥2;",
                "multi-evidence needs ≥2 cited tasks with ≥1 showing iteration. Pure summary similarity is not evidence.",
                "",
                "Dedup: compare against active_lessons; match existing via matching title/tags so backend merges by fingerprint.",
                "",
                f"POST /api/workspaces/{workspace.id}/lessons",
                'Payload: {"title":"...","summary":"...","applies_when":["..."],"do":"...","avoid":"...","tags":["..."],"scope":"workspace","confidence":0.6,"evidence_task_ids":["..."]}',
                "",
                "Completion: POST completed report (changed_files=[], review_decision=skip, risk_level=system_audit) with validation:",
                "created_lesson_ids=<ids>|merged_lesson_ids=<ids>|skipped_reason=<reason>",
                "",
                "Input package JSON:",
                json.dumps(package, indent=2, ensure_ascii=False),
            ]
        )

    def feedback_lessons(
        self,
        workspace_id: str,
        *,
        query: str = "",
        limit: int = 20,
        include_inactive: bool = False,
    ) -> list[FeedbackLesson]:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        capped_limit = min(max(limit, 1), 50)
        store = self._feedback_store()
        if query.strip():
            return store.search_lessons(workspace_id, query, limit=capped_limit)
        return store.list_lessons(workspace_id, include_inactive=include_inactive)[:capped_limit]
