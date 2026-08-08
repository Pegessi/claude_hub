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
    ) -> tuple[str, list[str], list[str]]:
        """Build the Reaper prompt, enforcing the global hard char budget by
        dropping oldest digests until the prompt fits. Returns (prompt,
        committed_task_ids, committed_paths) so the caller can (a) record the
        final id list on FeedbackSummaryRun and (b) commit only the actually-
        sent records to the processed index.

        Budget is strict: we allow dropping ALL the way to zero digests if
        necessary (e.g. the instruction preamble + active_lessons alone could
        theoretically exceed budget, though our caps prevent that in practice).
        A zero-digest prompt is still well-formed JSON and the Reaper simply
        produces zero lessons."""
        # Cap active lessons for dedup context. Keep the backend-assigned
        # fingerprint EXACT (it is the 16-char sha1 scope:prefixed hash that
        # drives server-side merge on POST; truncating it would break dedup).
        # All other fields are bounded to fixed maxima so adversarial
        # title/summary/tags/id values cannot blow the prompt.
        store = self._feedback_store()
        _HARD_BUDGET = store.REAPER_PROMPT_HARD_CHAR_LIMIT  # defense in depth
        _LESSON_MAX = 20
        _LESSON_MAX_ID = 80
        _LESSON_MAX_TITLE = 80
        _LESSON_MAX_SUMMARY = 120
        _LESSON_MAX_TAG_LEN = 24
        _LESSON_MAX_TAGS = 6
        # Instruction preamble (fixed text). Measured ~1.9K chars; keep small.
        _INSTRUCTIONS = (
            "You are the internal Feedback Reaper. System-internal task — do not ask the human for acceptance.\n"
            "\n"
            "Goal: extract reusable workspace lessons from input_task_digests. Quality > quantity. "
            "Zero lessons is correct when no signal exists. Use only the input package; do not scan the workspace.\n"
            "\n"
            "Extraction signals (at least one required per lesson):\n"
            "  A) Iteration cost: single task with review_failed_count>=1 OR needs_input_count>=2.\n"
            "  B) Cross-task recurrence: same root pattern in >=2 digests.\n"
            "Implementation-detail lessons need A or B with observable evidence; a single clean-pass final_summary is not a lesson.\n"
            "\n"
            "Required lesson fields (server validates; HTTP 400 on violation — do not retry):\n"
            "  title (short), summary (1-2 sentences, <=200 chars), applies_when (>=1, <=4 items each <=60 chars), do (<=200 chars),\n"
            "  avoid (<=200 chars), tags (<=6, each <=24 chars, lowercase-dashed), scope ('workspace'),\n"
            "  confidence (<=0.6 single / <=0.85 multi), evidence_task_ids (<=5 items from input).\n"
            "Server enforcement: applies_when/do/avoid non-empty; single-evidence tasks need review_failed>=1 or needs_input>=2;\n"
            "multi-evidence needs >=2 cited tasks with >=1 showing iteration. Pure summary similarity is not evidence.\n"
            "\n"
            "Dedup (deterministic via fingerprint): compare against active_lessons. If a new lesson would duplicate an\n"
            "existing one (matching core meaning even if wording differs), EITHER skip creation OR POST with the existing\n"
            "lesson's fingerprint field echoed verbatim — the server merges on exact fingerprint match. The fingerprint is\n"
            "the authoritative merge key; do not try to recompute it. Do not rely on title+tags alone.\n"
            "\n"
            f"POST /api/workspaces/{workspace.id}/lessons\n"
            'Payload: {"title":"...","summary":"...","applies_when":["..."],"do":"...","avoid":"...","tags":["..."],"scope":"workspace","confidence":0.6,"evidence_task_ids":["..."],"fingerprint":"<existing-fingerprint-if-merge>"}\n'
            "\n"
            "Completion: POST completed report (changed_files=[], review_decision=skip, risk_level=system_audit) with validation:\n"
            "created_lesson_ids=<ids>|merged_lesson_ids=<ids>|skipped_reason=<reason>\n"
            "\n"
            "Input package JSON:\n"
        )

        def _clip(text: str, n: int) -> str:
            if len(text) <= n:
                return text
            if n <= 3:
                return text[:n]
            return text[: n - 3].rstrip() + "..."

        def _clamp_tags(tags: list[str]) -> list[str]:
            return [_clip(str(t), _LESSON_MAX_TAG_LEN) for t in tags[:_LESSON_MAX_TAGS]]

        lessons = self.feedback_lessons(workspace.id, limit=_LESSON_MAX)
        lesson_payload = [
            {
                "id": _clip(str(lesson.id or ""), _LESSON_MAX_ID),
                "fingerprint": lesson.fingerprint or "",  # EXACT, never truncated
                "title": _clip(lesson.title or "", _LESSON_MAX_TITLE),
                "summary": _clip(lesson.summary or "", _LESSON_MAX_SUMMARY),
                "tags": _clamp_tags(lesson.tags or []),
                "confidence": lesson.confidence,
            }
            for lesson in lessons
        ]

        digests = list(summary_input["input_records"])  # defensive copy

        def _assemble(selected_digests: list[dict[str, Any]]) -> str:
            # Strip internal bookkeeping (_path) before serializing into the
            # prompt — the Reaper does not need local filesystem paths.
            clean = []
            for d in selected_digests:
                clean.append({k: v for k, v in d.items() if not k.startswith("_")})
            package = {
                "summary_run": {
                    "id": summary_input["run_id"],
                    "mode": summary_input["mode"],
                    "input_record_ids": [d.get("task_id", "") for d in clean],
                },
                "active_lessons": lesson_payload,
                "input_task_digests": clean,
            }
            return _INSTRUCTIONS + json.dumps(package, indent=2, ensure_ascii=False)

        prompt = _assemble(digests)
        # Defense-in-depth global budget: if the prompt exceeds the hard
        # budget, drop oldest digests (list is oldest-first appended by
        # input_records; keep the newest) until it fits OR zero digests
        # remain (strict — allow dropping all if truly necessary).
        while len(prompt) > _HARD_BUDGET and len(digests) >= 1:
            digests.pop(0)
            prompt = _assemble(digests)
        committed_task_ids = [d.get("task_id", "") for d in digests]
        committed_paths = [d.get("_path", "") for d in digests if d.get("_path")]
        return prompt, committed_task_ids, committed_paths

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
