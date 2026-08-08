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
        # Cap active lessons for dedup context. Keep the backend-assigned
        # fingerprint (short sha1 scope-prefixed hash) plus a truncated summary
        # so the Reaper can deterministically avoid posting near-duplicates:
        # matching fingerprint -> skip or echo it back in POST body to merge.
        # Title+tags alone are insufficient because backend fingerprint spans
        # seven fields (scope/title/summary/applies_when/do/avoid/tags).
        _SUMMARY_MAX = 120
        lessons = self.feedback_lessons(workspace.id, limit=20)

        def _clip(text: str, n: int) -> str:
            return text if len(text) <= n else text[: n - 3] + "..."

        lesson_payload = [
            {
                "id": lesson.id,
                "fingerprint": lesson.fingerprint,
                "title": lesson.title[:80],
                "summary": _clip(lesson.summary or "", _SUMMARY_MAX),
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
                "  scope ('workspace'), confidence (<=0.6 single / <=0.85 multi), evidence_task_ids.",
                "Server enforcement: applies_when/do/avoid non-empty; single-evidence tasks need review_failed>=1 or needs_input>=2;",
                "multi-evidence needs >=2 cited tasks with >=1 showing iteration. Pure summary similarity is not evidence.",
                "",
                "Dedup (deterministic via fingerprint): compare against active_lessons. If a new lesson would duplicate an",
                "existing one (matching core meaning even if wording differs), EITHER skip creation OR POST with the existing",
                "lesson's fingerprint field echoed back — the server merges on fingerprint match. Do not rely on title+tags",
                "alone; use the summary snippet to judge semantic equivalence.",
                "",
                f"POST /api/workspaces/{workspace.id}/lessons",
                'Payload: {"title":"...","summary":"...","applies_when":["..."],"do":"...","avoid":"...","tags":["..."],"scope":"workspace","confidence":0.6,"evidence_task_ids":["..."],"fingerprint":"<existing-fingerprint-if-merge>"}',
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
