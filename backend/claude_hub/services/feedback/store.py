"""Feedback summary selection, staging, and prompt behavior."""

from claude_hub.services.feedback.catalog import FeedbackLessonCatalogStore
from claude_hub.services.feedback.common import (
    AgentReport,
    Any,
    ChatCorrection,
    FeedbackProcessedTaskRecord,
    FeedbackRecord,
    FeedbackSummaryMode,
    FeedbackSummaryRun,
    FeedbackTaskDigest,
    Path,
    Workspace,
    WorkspaceTask,
    bisect_right,
    datetime,
    hashlib,
    json,
    uuid,
)


class FeedbackLessonStore(FeedbackLessonCatalogStore):

    # Maximum digests per reaper run across ALL modes. Without this cap a
    # long-inactive workspace can accumulate hundreds of unprocessed records,
    # producing a 500K+ token prompt that exceeds smaller agents' context.
    _REAPER_MAX_DIGESTS_PER_RUN = 30
    # Global hard char budget for the assembled Reaper prompt (instructions +
    # package JSON). Enforced by the WorkspaceManager after building the
    # prompt; if exceeded, oldest digests are dropped until the prompt fits.
    # 100K chars ≈ 25K tokens, leaving safe margin under codex's ~1M-char
    # request ceiling and well under our 128K/32K target.
    REAPER_PROMPT_HARD_CHAR_LIMIT = 100_000

    def prepare_summary_input(
        self,
        workspace_id: str,
        task_records_dir: Path,
        *,
        mode: FeedbackSummaryMode,
        limit: int,
        force: bool,
        now: datetime | None = None,
        automatic_since: datetime | None = None,
        automatic_cursor: str = "",
    ) -> dict[str, Any]:
        """Build the reaper input selection (candidates + compact records) WITHOUT
        writing the processed-records index yet. The caller must invoke
        commit_summary_input() with the subset of selected entries that actually
        made it into the final prompt (after the global budget drops excess
        digests), so budget-dropped records stay unprocessed and are retried on
        the next run."""
        now = now or self._feedback_now()
        limit = min(max(limit, 1), self._REAPER_MAX_DIGESTS_PER_RUN)
        index = self._read_feedback_index(workspace_id)
        existing_entries = {
            str(item.get("path") or ""): item
            for item in index.get("processed_task_records", [])
            if item.get("path")
        }
        record_paths = sorted(task_records_dir.glob("*.json")) if task_records_dir.exists() else []
        correction_dir = self._feedback_dir(workspace_id) / "chat-corrections"
        correction_paths = sorted(correction_dir.glob("chat-*.json"))
        record_paths.extend(correction_paths)
        first_scan = not existing_entries
        candidates: list[FeedbackProcessedTaskRecord] = []
        # Rotate a durable cursor rather than truncating by filename: correction
        # IDs are hashes, and a lexicographic cap would permanently miss some.
        scan_paths = record_paths
        scan_cursor = automatic_cursor
        if automatic_since is not None:
            fresh_paths = []
            for path in record_paths:
                try:
                    if path.stat().st_mtime >= automatic_since.timestamp():
                        fresh_paths.append(path)
                except OSError:
                    continue
            fresh_paths.sort(key=str)
            pivot = bisect_right([str(path) for path in fresh_paths], automatic_cursor)
            scan_paths = (fresh_paths[pivot:] + fresh_paths[:pivot])[:1000]
        read_bytes = 0
        eligible_paths: set[str] = set()
        for path in scan_paths:
            path_key = str(path)
            try:
                if automatic_since is not None:
                    remaining = 8 * 1024 * 1024 - read_bytes
                    # Reserve a full per-file probe so growing/oversized files
                    # cannot exceed the total byte budget or advance past a
                    # record read only partially at the end of a scan.
                    if remaining < 1024 * 1024 + 1:
                        break
                    with path.open("rb") as source:
                        digest_bytes = source.read(1024 * 1024 + 1)
                    read_bytes += len(digest_bytes)
                    scan_cursor = path_key
                    if len(digest_bytes) > 1024 * 1024:
                        continue
                else:
                    digest_bytes = path.read_bytes()
            except OSError:
                continue
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
                if not isinstance(record_payload, dict):
                    continue
                if path.parent == correction_dir:
                    try:
                        correction = ChatCorrection.model_validate(record_payload)
                    except ValueError:
                        continue
                    if correction.workspace_id != workspace_id or correction.id != path.stem:
                        continue
                    if automatic_since and correction.created_at < automatic_since:
                        continue
                    digest = FeedbackTaskDigest(
                        task_id="",
                        source_record_id=correction.id,
                        source_tab_id=correction.tab_id,
                        source_turn_id=correction.turn_id,
                        source_message_id=correction.message_id,
                        correction=correction.quote,
                    )
                    raw_task_id = correction.id
                else:
                    task_block = record_payload.get("task")
                    if not isinstance(task_block, dict) or task_block.get("system_internal"):
                        continue
                    if record_payload.get("workspace_id") not in (None, workspace_id):
                        continue
                    if task_block.get("workspace_id") not in (None, workspace_id):
                        continue
                    digest = self._digest_task_record(record_payload)
                    if automatic_since:
                        try:
                            archived_at = datetime.fromisoformat(record_payload["archived_at"])
                            if archived_at < automatic_since:
                                continue
                        except (KeyError, TypeError, ValueError):
                            continue
                        # A clean pass is not a reusable lesson; require a
                        # supported iteration signal before spending a model call.
                        if not (digest.review_failed_count >= 1 or digest.needs_input_count >= 2):
                            continue
                    raw_task_id = str(task_block.get("id") or path.stem)
                eligible_paths.add(path_key)
                # Clamp task_id at the outer record level too so compact wrapper
                # and input_record_ids never leak an oversized id into the prompt.
                clamped_task_id = self._truncate_str(
                    raw_task_id, self._feedback_runtime_settings().digest_max_task_id
                )
                entry = FeedbackProcessedTaskRecord(
                    task_id=clamped_task_id,
                    path=path_key,
                    sha256=sha256,
                    digest=digest,
                    summarized_at=now,
                )
                candidates.append(entry)

        # Cap ALL modes (including incremental) to prevent unbounded prompts
        # when many records accumulate between reaper runs. Remaining records
        # stay un-cached (not added to processed_entries) and will be picked
        # up on the next run.
        selected_entries = candidates[-limit:]

        # Fallback for force/full with no new records: re-select from existing
        # processed entries that are still on disk so the Reaper gets data to
        # work from on an explicit full rescan.
        if not selected_entries and (force or mode == FeedbackSummaryMode.FULL):
            fallback_candidates: list[FeedbackProcessedTaskRecord] = []
            for key in sorted(existing_entries):
                if key not in eligible_paths:
                    continue
                try:
                    fallback_candidates.append(FeedbackProcessedTaskRecord(**existing_entries[key]))
                except Exception:
                    continue
            selected_entries = fallback_candidates[-limit:]

        # Defensively clamp task_id on fallback entries loaded from a pre-v5
        # index that may have stored unclamped ids.
        for entry in selected_entries:
            if len(entry.task_id) > self._feedback_runtime_settings().digest_max_task_id:
                entry.task_id = self._truncate_str(
                    entry.task_id, self._feedback_runtime_settings().digest_max_task_id
                )

        compact_records = []
        selected_dumps_by_path: dict[str, dict[str, Any]] = {}
        for entry in selected_entries:
            compact_records.append(
                {
                    (
                        "source_record_id" if entry.digest.source_record_id else "task_id"
                    ): entry.task_id,
                    "digest": self._compact_digest_for_prompt(entry.digest),
                    "_path": entry.path,  # internal, stripped before serialization
                }
            )
            selected_dumps_by_path[entry.path] = entry.model_dump(mode="json")
        return {
            "run_id": str(uuid.uuid4()),
            "workspace_id": workspace_id,
            "mode": mode.value,
            "force": force,
            "limit": limit,
            "cache_hit": not selected_entries,
            "prompt_version": self._feedback_runtime_settings().summary_prompt_version,
            "first_scan": bool(first_scan and selected_entries),
            "processed_count": 0,  # set after commit, when final count is known
            "input_records": compact_records,
            "input_record_ids": [entry.task_id for entry in selected_entries],
            "skipped_reason": "no_new_task_records" if not selected_entries else None,
            # Internal book-keeping for commit_summary_input():
            "_workspace_id": workspace_id,
            "_now": now,
            "_existing_entries": existing_entries,
            "_record_paths": [str(p) for p in record_paths],
            "_first_scan_raw": first_scan,
            "_selected_dumps_by_path": selected_dumps_by_path,
            "_scan_cursor": scan_cursor,
        }

    def commit_summary_input(
        self,
        summary_input: dict[str, Any],
        committed_paths: list[str],
    ) -> int:
        """Write the processed-records index after prompt assembly. Only the
        entries whose paths appear in committed_paths are marked processed;
        records dropped by the global budget stay unprocessed and will be
        retried on the next run (carry-over). Returns the new processed_count.

        Caller MUST call this after _build_workspace_feedback_summary_prompt
        returns (since that is where global-budget trimming happens), passing
        the _path values from the compact records that actually survived
        budget trimming. If no records are committed (all dropped), the index
        is still pruned of deleted-disk entries but no new paths are marked."""
        workspace_id = summary_input["_workspace_id"]
        now = summary_input["_now"]
        existing_entries: dict[str, dict[str, Any]] = dict(summary_input["_existing_entries"])
        record_path_set: set[str] = set(summary_input["_record_paths"])
        first_scan: bool = summary_input["_first_scan_raw"]
        selected_dumps_by_path: dict[str, dict[str, Any]] = summary_input.get(
            "_selected_dumps_by_path", {}
        )

        committed_set = set(committed_paths)
        for p in committed_set:
            dump = selected_dumps_by_path.get(p)
            if dump:
                existing_entries[p] = dump

        # Prune entries whose files no longer exist on disk.
        sorted_processed = [
            existing_entries[key] for key in sorted(existing_entries) if key in record_path_set
        ]
        index = self._read_feedback_index(workspace_id)
        index.update(
            {
                "schema_version": self._feedback_runtime_settings().index_schema_version,
                "workspace_id": workspace_id,
                "prompt_version": self._feedback_runtime_settings().summary_prompt_version,
                "processed_task_records": sorted_processed,
                "last_full_scan_at": (
                    now.isoformat()
                    if first_scan and committed_set
                    else index.get("last_full_scan_at")
                ),
            }
        )
        self._write_feedback_index(workspace_id, index)
        return len(sorted_processed)

    def stage_summary_input(
        self,
        summary_input: dict[str, Any],
        committed_paths: list[str],
    ) -> None:
        """Persist the exact post-budget selection until its task succeeds.

        A Feedback Reaper task must not mark records processed merely because
        its prompt was dispatched.  Keeping this bounded commit package beside
        the summary run lets completion commit the index after a restart, while
        abort/delete can discard it so a later trigger retries the records.
        """

        workspace_id = str(summary_input["_workspace_id"])
        run_id = str(summary_input["run_id"])
        selected_dumps_by_path: dict[str, dict[str, Any]] = summary_input.get(
            "_selected_dumps_by_path", {}
        )
        committed_set = set(committed_paths)
        payload = {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "prepared_at": summary_input["_now"].isoformat(),
            "record_paths": list(summary_input["_record_paths"]),
            "first_scan": bool(summary_input["_first_scan_raw"]),
            "committed_paths": list(committed_paths),
            "selected_dumps_by_path": {
                path: dump for path, dump in selected_dumps_by_path.items() if path in committed_set
            },
        }
        path = self._summary_stage_path(workspace_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def commit_staged_summary_input(self, workspace_id: str, run_id: str) -> int | None:
        """Idempotently commit a successful run's staged records.

        The stage file is intentionally retained until the caller also
        persists the completed run.  A crash between the two writes can then
        replay this commit safely instead of losing the recovery package.
        """

        path = self._summary_stage_path(workspace_id, run_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("workspace_id") != workspace_id or data.get("run_id") != run_id:
            raise ValueError("Feedback summary pending input does not match its run")
        index = self._read_feedback_index(workspace_id)
        existing_entries = {
            str(item.get("path")): item
            for item in index.get("processed_task_records", [])
            if isinstance(item, dict) and item.get("path")
        }
        summary_input = {
            "_workspace_id": workspace_id,
            "_now": datetime.fromisoformat(str(data["prepared_at"])),
            "_existing_entries": existing_entries,
            "_record_paths": list(data.get("record_paths", [])),
            "_first_scan_raw": bool(data.get("first_scan")),
            "_selected_dumps_by_path": dict(data.get("selected_dumps_by_path", {})),
        }
        processed_count = self.commit_summary_input(
            summary_input,
            list(data.get("committed_paths", [])),
        )
        return processed_count

    def discard_staged_summary_input(self, workspace_id: str, run_id: str) -> None:
        """Remove a pending package after completion or explicit abandonment."""

        self._summary_stage_path(workspace_id, run_id).unlink(missing_ok=True)

    def has_staged_summary_input(self, workspace_id: str, run_id: str) -> bool:
        """Return whether a summary run has a valid prepared commit package."""

        path = self._summary_stage_path(workspace_id, run_id)
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(data, dict)
            and data.get("workspace_id") == workspace_id
            and data.get("run_id") == run_id
        )

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
        now = now or self._feedback_now()
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

    def summary_run_for_task(
        self,
        workspace_id: str,
        task_id: str,
    ) -> FeedbackSummaryRun | None:
        """Return the persisted run associated with a managed summary task."""

        _, run = self._find_summary_run_by_task(workspace_id, task_id)
        return run

    def abandon_summary_run(
        self,
        workspace_id: str,
        task_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> FeedbackSummaryRun | None:
        """Make an unfinished run terminal without consuming its staged input."""

        now = now or self._feedback_now()
        run_path, run = self._find_summary_run_by_task(workspace_id, task_id)
        if not run_path or not run:
            return None
        self._summary_stage_path(workspace_id, run.id).unlink(missing_ok=True)
        if run.completed_at is not None:
            return run
        updated = run.model_copy(update={"skipped_reason": reason, "completed_at": now})
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
