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
        lessons = self.feedback_lessons(workspace.id, limit=50)
        lesson_payload = [
            {
                "id": lesson.id,
                "title": lesson.title,
                "fingerprint": lesson.fingerprint,
                "summary": lesson.summary,
                "tags": lesson.tags,
                "confidence": lesson.confidence,
            }
            for lesson in lessons
        ]
        package = {
            "summary_run": {
                "id": summary_input["run_id"],
                "mode": summary_input["mode"],
                "force": summary_input["force"],
                "limit": summary_input["limit"],
                "prompt_version": summary_input["prompt_version"],
                "first_scan": summary_input["first_scan"],
                "processed_count": summary_input["processed_count"],
                "input_record_ids": summary_input["input_record_ids"],
            },
            "workspace": {
                "id": workspace.id,
                "name": workspace.name,
                "target": workspace.target.value,
                "path": workspace.path,
            },
            "active_lessons": lesson_payload,
            "input_task_digests": summary_input["input_records"],
        }
        return "\n".join(
            [
                "You are the internal Feedback Reaper for this Claude Hub workspace.",
                "",
                "This is a system-internal task. Do not ask the human user for acceptance, and do "
                "not treat this as an ordinary visible workspace task.",
                "",
                "Use only the bounded input package below. Do not scan the entire workspace or "
                "read unrelated old task records unless a listed digest explicitly points to a "
                "missing artifact you must inspect.",
                "",
                "Goal: extract reusable workspace lessons that help future tasks avoid known "
                "pitfalls. Quality matters more than quantity. Emitting zero lessons is the "
                "correct answer when no signal is present.",
                "",
                "Extraction signals — emit a lesson ONLY when at least one of these is supported "
                "by the input_task_digests:",
                "  Signal A — Iteration cost. A single task whose report_state_sequence shows "
                "review_failed_count >= 1 OR needs_input_count >= 2, OR whose risks describe "
                "rework. The lesson is the underlying issue that caused the extra rounds, NOT "
                "the final fix recipe.",
                "  Signal B — Cross-task recurrence. The same root problem (or a close variant) "
                "appears in >= 2 distinct task digests. The lesson is the recurring pattern.",
                "",
                "Specific implementation-detail lessons (a particular file, function, error "
                "message, or version-specific quirk) are allowed ONLY if Signal A or Signal B "
                "applies AND the evidence makes the difficulty observable. A lesson whose only "
                "support is a single task's final_summary with no review_failed / needs_input "
                "trail is a fix recipe, not a lesson — skip it.",
                "",
                "Required fields per lesson (the backend rejects payloads that violate this):",
                "  - title (short)",
                "  - summary (one-to-two sentence description)",
                "  - applies_when (>=1 condition: file glob, runtime, command, env, task shape)",
                "  - do (recommended action; non-empty)",
                "  - avoid (failure pattern to avoid; non-empty)",
                "  - tags",
                "  - scope (default 'workspace')",
                "  - confidence: server-capped at 0.6 for single-evidence lessons and at 0.85 "
                "for multi-evidence lessons; pick a value that already respects this.",
                "  - evidence_task_ids (cite the supporting input task_ids; for Signal A "
                "single-task lessons cite that one task; for Signal B cite all supporting tasks)",
                "",
                "Server-side enforcement (these rules are mechanically checked against "
                "input_task_digests, NOT inferred from prose; lessons that fail are rejected "
                "with HTTP 400 and you must move on rather than retrying with massaged text):",
                "  - applies_when / do / avoid must be non-empty.",
                "  - Single-evidence lessons must cite a task whose report_state_sequence has "
                "review_failed_count >= 1 OR needs_input_count >= 2. If no input digest meets "
                "this bar, do NOT submit a single-evidence lesson — emit a multi-evidence one "
                "or skip the candidate.",
                "  - Multi-evidence lessons must cite >=2 evidence_task_ids and at least one "
                "cited task must show review_failed_count + needs_input_count >= 1. Pure "
                "final_summary text similarity is NOT a substitute for iteration evidence.",
                "",
                "Deduplication rule: before creating a lesson, compare against active_lessons. "
                "If the idea already exists, call the lesson API with matching title/summary/tags "
                "so the backend merges evidence by fingerprint instead of creating a duplicate.",
                "",
                "Lessons API:",
                f"POST /api/workspaces/{workspace.id}/lessons",
                "Payload shape:",
                '{"title":"short title","summary":"one-sentence description",'
                '"applies_when":["condition"],"do":"recommended action",'
                '"avoid":"failure pattern","tags":["tag"],"scope":"workspace",'
                '"confidence":0.6,"evidence_task_ids":["task-id"]}',
                "",
                "Completion report requirement:",
                "POST a completed report for this internal task with changed_files=[], "
                "review_decision=skip, risk_level=system_audit, and validation containing one "
                "of these exact fields (do NOT append free prose after the value; if you need "
                "commentary put it on a separate line):",
                "- created_lesson_ids=<comma-separated ids>",
                "- merged_lesson_ids=<comma-separated ids>",
                "- skipped_reason=<reason>",
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
