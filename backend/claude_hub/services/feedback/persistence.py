"""Persistence and evidence foundations for workspace feedback lessons."""

from claude_hub.services.feedback.common import (
    AgentReport,
    Any,
    FeedbackAutomationStore,
    FeedbackFailureEvidence,
    FeedbackLesson,
    FeedbackLessonCreate,
    FeedbackLessonDraft,
    FeedbackLessonDraftCreate,
    FeedbackLessonStatus,
    FeedbackLessonValidationError,
    FeedbackReaperRequest,
    FeedbackReaperRun,
    FeedbackRecord,
    FeedbackRuntimeSettings,
    FeedbackSummaryRun,
    FeedbackTaskDigest,
    Path,
    Workspace,
    WorkspaceTask,
    _now,
    _slug,
    _TaskIterationSignal,
    datetime,
    feedback_runtime_settings,
    hashlib,
    json,
    re,
    uuid,
)


class FeedbackPersistenceStore(object):
    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root

    def _feedback_runtime_settings(self) -> FeedbackRuntimeSettings:
        return feedback_runtime_settings()

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

    def _summary_stage_path(self, workspace_id: str, run_id: str) -> Path:
        return self._summary_runs_dir(workspace_id) / f"{run_id}.pending.json"

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
                "schema_version": self._feedback_runtime_settings().index_schema_version,
                "workspace_id": workspace_id,
                "prompt_version": self._feedback_runtime_settings().summary_prompt_version,
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
        data.setdefault("schema_version", self._feedback_runtime_settings().index_schema_version)
        data.setdefault("workspace_id", workspace_id)
        data.setdefault("prompt_version", self._feedback_runtime_settings().summary_prompt_version)
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
        failure_reports = [
            report
            for report in reports
            if isinstance(report, dict)
            and str(report.get("state") or "")
            in {"review_failed", "needs_input", "review_needs_input", "blocked", "failed"}
        ]
        report_state_sequence = [
            str(report.get("state"))
            for report in reports
            if isinstance(report, dict) and report.get("state")
        ]
        review_failed_count = sum(1 for state in report_state_sequence if state == "review_failed")
        needs_input_count = sum(1 for state in report_state_sequence if state == "needs_input")
        final_summary = self._truncate_str(
            str(payload.get("final_summary") or ""),
            self._feedback_runtime_settings().digest_max_final_summary,
        )
        # Truncate free-text fields at digest creation too, so cached records
        # don't carry oversized title/status/task_id. _compact_digest_for_prompt
        # re-applies truncation defensively for records cached before v5.
        task_id = self._truncate_str(
            str(task.get("id") or payload.get("task_id") or ""),
            self._feedback_runtime_settings().digest_max_task_id,
        )
        title = self._truncate_str(
            str(task.get("title") or ""), self._feedback_runtime_settings().digest_max_title
        )
        changed_files_raw = self._list_value(artifacts.get("changed_files"))
        changed_files = self._truncate_list(
            [
                self._truncate_str(str(f), self._feedback_runtime_settings().digest_max_path_chars)
                for f in changed_files_raw
            ],
            self._feedback_runtime_settings().digest_max_changed_files,
        )
        return FeedbackTaskDigest(
            task_id=task_id,
            title=title,
            status=self._truncate_str(str(task.get("status") or ""), 16),
            final_summary=final_summary,
            changed_files=changed_files,
            validation=self._truncate_list(
                [
                    self._truncate_str(
                        item, self._feedback_runtime_settings().digest_max_item_chars
                    )
                    for item in self._list_value(artifacts.get("validation"))
                ],
                self._feedback_runtime_settings().digest_max_validation_items,
            ),
            risks=self._truncate_list(
                [
                    self._truncate_str(
                        item, self._feedback_runtime_settings().digest_max_item_chars
                    )
                    for item in self._list_value(artifacts.get("risks"))
                ],
                self._feedback_runtime_settings().digest_max_risks_items,
            ),
            report_states=self._unique_strings(report_state_sequence),
            failure_evidence=[
                self._failure_evidence(report)
                for report in failure_reports[
                    -self._feedback_runtime_settings().digest_max_failure_evidence :
                ]
            ],
            # report_state_sequence intentionally omitted: the unique states + counts
            # (review_failed_count, needs_input_count, report_total) carry the signal
            # the Reaper needs; the full per-report sequence is redundant bulk.
            report_state_sequence=[],
            review_failed_count=review_failed_count,
            needs_input_count=needs_input_count,
            report_total=len(reports),
            completed_at=str(task.get("completed_at") or ""),
        )

    def _failure_evidence(self, report: dict[str, Any]) -> FeedbackFailureEvidence:
        return FeedbackFailureEvidence(
            report_id=self._truncate_str(
                str(report.get("id") or report.get("report_id") or ""), 80
            ),
            state=self._truncate_str(str(report.get("state") or ""), 32),
            message=self._truncate_str(str(report.get("message") or ""), 240),
            validation=self._truncate_str(str(report.get("validation") or ""), 320),
            risks=self._truncate_str(str(report.get("risks") or ""), 160),
        )

    def _compact_digest_for_prompt(self, digest: FeedbackTaskDigest) -> dict[str, Any]:
        """Serialize a digest into the compact prompt format, applying truncation
        even to records loaded from the existing cache (which may have been
        created before truncation was added). Every free-text field is bounded
        so adversarial title/path/validation content cannot blow the budget."""
        if digest.source_record_id:
            return {
                "source_record_id": digest.source_record_id,
                "source_tab_id": digest.source_tab_id,
                "source_turn_id": digest.source_turn_id,
                "source_message_id": digest.source_message_id,
                "correction": digest.correction[:1024],
            }
        return {
            "task_id": self._truncate_str(
                digest.task_id, self._feedback_runtime_settings().digest_max_task_id
            ),
            "title": self._truncate_str(
                digest.title, self._feedback_runtime_settings().digest_max_title
            ),
            "final_summary": self._truncate_str(
                digest.final_summary, self._feedback_runtime_settings().digest_max_final_summary
            ),
            "changed_files": self._truncate_list(
                [
                    self._truncate_str(f, self._feedback_runtime_settings().digest_max_path_chars)
                    for f in digest.changed_files
                ],
                self._feedback_runtime_settings().digest_max_changed_files,
            ),
            "validation": self._truncate_list(
                [
                    self._truncate_str(v, self._feedback_runtime_settings().digest_max_item_chars)
                    for v in digest.validation
                ],
                self._feedback_runtime_settings().digest_max_validation_items,
            ),
            "risks": self._truncate_list(
                [
                    self._truncate_str(r, self._feedback_runtime_settings().digest_max_item_chars)
                    for r in digest.risks
                ],
                self._feedback_runtime_settings().digest_max_risks_items,
            ),
            "review_failed_count": digest.review_failed_count,
            "needs_input_count": digest.needs_input_count,
            "report_total": digest.report_total,
            "failure_evidence": [
                self._failure_evidence(item.model_dump()).model_dump()
                for item in digest.failure_evidence[
                    -self._feedback_runtime_settings().digest_max_failure_evidence :
                ]
            ],
            "report_states": self._truncate_list(
                [self._truncate_str(s, 32) for s in digest.report_states], 8
            ),
        }

    def _truncate_str(self, value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        if max_len <= 3:
            return value[:max_len]
        return value[: max_len - 3].rstrip() + "..."

    def _truncate_list(self, items: list[str], max_items: int) -> list[str]:
        if len(items) <= max_items:
            return items
        return items[:max_items]

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
            if path.name.endswith(".pending.json"):
                continue
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
        source_record_ids: list[str],
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
        - Every cited task must have a readable record in this workspace.

        Confidence cap (always on):
        - Single-evidence lessons use the configured single-evidence cap.
        - Multi-evidence lessons use the configured multi-evidence cap.
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
        correction_ids = [item for item in source_record_ids if item.startswith("chat-")]
        for record_id in correction_ids:
            try:
                FeedbackAutomationStore(self.state_root).correction(workspace_id, record_id)
            except (OSError, ValueError) as exc:
                raise FeedbackLessonValidationError(
                    f"no verified Chat correction record in this workspace: {record_id}"
                ) from exc
        if not evidence_task_ids and not correction_ids:
            raise FeedbackLessonValidationError(
                "evidence_task_ids must cite at least one task that supports the lesson"
            )

        if enforce_iteration_signal and evidence_task_ids:
            signals = [
                self._task_iteration_signal(workspace_id, task_id) for task_id in evidence_task_ids
            ]
            missing_ids = [
                task_id for task_id, signal in zip(evidence_task_ids, signals) if signal is None
            ]
            if missing_ids:
                raise FeedbackLessonValidationError(
                    "no task record was found in this workspace for evidence task(s): "
                    + ", ".join(missing_ids)
                    + "; lesson cannot be verified"
                )
            observed_signals = [signal for signal in signals if signal is not None]

            if correction_ids:
                pass  # exact explicit user correction is an independent signal
            elif len(evidence_task_ids) == 1:
                signal = observed_signals[0]
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
            self._feedback_runtime_settings().single_evidence_confidence_cap
            if len(evidence_task_ids) + len(correction_ids) <= 1
            else self._feedback_runtime_settings().multi_evidence_confidence_cap
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
        # IDs are untrusted report input, not glob patterns or paths. The
        # record's exact task identity is authoritative, including legacy names.
        for path in records_dir.glob("*.json"):
            if task_id not in path.name:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("workspace_id") not in (None, workspace_id):
                continue
            task_block = payload.get("task")
            if not isinstance(task_block, dict):
                continue
            if task_block.get("workspace_id") not in (None, workspace_id):
                continue
            if str(task_block.get("id") or "") != task_id:
                continue
            reports = payload.get("reports")
            if not isinstance(reports, list):
                continue
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
