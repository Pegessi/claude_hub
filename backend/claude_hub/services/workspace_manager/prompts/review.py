"""Prompt builders split from the compatibility mixin."""

from .._constants import (
    INTERNAL_API_CURL,
    AgentReport,
    Any,
    AutonomyPolicy,
    ExecutionTarget,
    ManagedSession,
    Path,
    ReviewProfile,
    Workspace,
    WorkspaceTask,
    WorkspaceTaskMode,
    json,
    state_policy,
)
from .assignment import _AssignmentPromptsMixin


class _ReviewPromptsMixin(_AssignmentPromptsMixin):
    _SUMMARY_VERBOSE_FIELD_MAX = 240
    _FULL_REPORT_WINDOW = 4
    _MAX_REPORT_HISTORY = 12

    def _effective_review_profiles(
        self,
        task: WorkspaceTask,
        trigger_report: AgentReport,
    ) -> list[ReviewProfile]:
        policy = task.autonomy_policy or AutonomyPolicy()
        explicit_profiles = [
            *task.review_profiles,
            *trigger_report.review_profiles,
            *(policy.review_profiles if task.task_mode == WorkspaceTaskMode.AUTONOMOUS else []),
        ]
        return state_policy.infer_review_profiles(
            state_policy.ReviewProfileContext(
                task_mode=task.task_mode,
                report_state=trigger_report.state,
                title=task.title,
                prompt=task.prompt,
                changed_files=trigger_report.changed_files,
                validation=trigger_report.validation,
                risks=trigger_report.risks,
                message=trigger_report.message,
                explicit_profiles=explicit_profiles,
                require_artifact_review=(
                    bool(policy.require_artifact_review)
                    if task.task_mode == WorkspaceTaskMode.AUTONOMOUS
                    else False
                ),
                evaluation_strictness=policy.evaluation_strictness,
                attachment_count=len(task.attachments),
            )
        )

    def _review_profile_prompt_block(self, profiles: list[ReviewProfile]) -> str:
        return (
            "Enabled review profiles JSON:\n"
            f"{json.dumps([profile.value for profile in profiles])}\n\n"
            "Review profile checklist:\n"
            f"{chr(10).join(state_policy.review_profile_prompt_lines(profiles))}\n\n"
        )

    def _review_guidance_block(
        self,
        workspace: Workspace,
        trigger_report: AgentReport,
    ) -> str:
        guidance = self._review_guidance_documents(workspace, trigger_report.changed_files)
        if not guidance:
            return ""
        sections = [f"### {path}\n{text}" for path, text in guidance]
        return "Repository review guidance:\n" + "\n\n".join(sections) + "\n\n"

    def _review_guidance_documents(
        self,
        workspace: Workspace,
        changed_files: list[str],
    ) -> list[tuple[str, str]]:
        if workspace.target != ExecutionTarget.LOCAL:
            return []
        try:
            root = Path(workspace.path).expanduser().resolve()
        except OSError:
            return []
        candidates: list[Path] = [root / "REVIEW.md"]
        for changed_file in changed_files[:12]:
            if not changed_file:
                continue
            if not self._path_looks_like_real_file(changed_file):
                continue
            try:
                candidate = Path(changed_file)
            except (OSError, ValueError):
                continue
            try:
                is_absolute = candidate.is_absolute()
            except OSError:
                is_absolute = False
            path = candidate if is_absolute else root / candidate
            try:
                resolved = path.resolve(strict=False)
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            try:
                is_dir = resolved.is_dir()
            except OSError:
                is_dir = False
            directory = resolved if is_dir else resolved.parent
            while True:
                candidates.append(directory / "REVIEW.md")
                if directory == root:
                    break
                directory = directory.parent

        seen: set[Path] = set()
        documents: list[tuple[str, str]] = []
        for candidate in candidates:
            if candidate in seen or not candidate.exists() or not candidate.is_file():
                continue
            seen.add(candidate)
            try:
                text = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not text:
                continue
            if len(text) > 4000:
                text = text[:4000].rstrip() + "\n...[truncated]"
            try:
                display_path = str(candidate.relative_to(root))
            except ValueError:
                display_path = str(candidate)
            documents.append((display_path, text))
            if len(documents) >= 6:
                break
        return documents

    def _truncate_verbose(self, value: Any, limit: int = _SUMMARY_VERBOSE_FIELD_MAX) -> Any:
        """Truncate free-text fields in summarized reports to keep reviewer prompts bounded."""
        if value is None:
            return None
        if isinstance(value, str):
            if len(value) <= limit:
                return value
            return value[:limit].rstrip() + f"...[truncated {len(value)-limit} chars]"
        if isinstance(value, list):
            return [self._truncate_verbose(v, limit) for v in value[:8]]
        if isinstance(value, dict):
            return {k: self._truncate_verbose(v, limit) for k, v in list(value.items())[:8]}
        return value

    def _serialize_report_for_review(
        self,
        report: AgentReport,
        *,
        full: bool,
    ) -> dict[str, Any]:
        """Serialize one task report for the reviewer prompt.

        ``full=True`` includes full verbose fields (validation/risks/acceptance_check/
        profile_results); ``full=False`` truncates them to a bounded size so older
        history does not grow the prompt linearly with task length.
        """
        payload: dict[str, Any] = {
            "state": report.state.value,
            "session_id": report.session_id,
            "message": report.message,
            "changed_files": report.changed_files,
            "review_decision": report.review_decision.value,
            "risk_level": report.risk_level,
            "created_at": report.created_at.isoformat(),
        }
        if full:
            payload.update(
                {
                    "validation": report.validation,
                    "risks": report.risks,
                    "acceptance_check": [
                        item.model_dump(mode="json") for item in report.acceptance_check
                    ],
                    "review_profiles": [p.value for p in report.review_profiles],
                    "profile_results": [
                        item.model_dump(mode="json") for item in report.profile_results
                    ],
                    "artifact_refs": report.artifact_refs,
                    "confidence": report.confidence,
                    "requires_human_judgment": report.requires_human_judgment,
                    "review_reason": report.review_reason,
                }
            )
        else:
            # Summarized: include validation/risks truncated; omit bulky structured
            # fields (full acceptance_check/profile_results) to keep bounded.
            payload.update(
                {
                    "validation": self._truncate_verbose(report.validation),
                    "risks": self._truncate_verbose(report.risks),
                    "artifact_refs_count": len(report.artifact_refs),
                    "acceptance_check_count": len(report.acceptance_check),
                }
            )
        return payload

    def _serialize_task_reports_for_review(
        self,
        task: WorkspaceTask,
        trigger_report: AgentReport,
        *,
        include_trigger: bool = False,
    ) -> list[dict[str, Any]]:
        """Tiered serialization of task reports for reviewer prompts.

        Strategy: the most recent ``_FULL_REPORT_WINDOW`` reports (ending with
        ``trigger_report`` when ``include_trigger`` is True, or the report just
        before it otherwise) are serialized verbatim; earlier reports are
        summarized with verbose fields truncated. This keeps reviewer prompt
        size bounded across iterations while preserving the latest verdicts
        fully.

        The default ``include_trigger=False`` is used when ``trigger_report``
        is already rendered verbatim elsewhere in the prompt (e.g. the
        "Trigger report (full JSON)" block) so history does not duplicate it.
        """
        task_reports = [
            r for r in self.reports_for_workspace(task.workspace_id) if r.task_id == task.id
        ][-self._MAX_REPORT_HISTORY :]
        if not task_reports:
            return []
        # Find index of trigger_report; fall back to last if not found.
        trigger_idx = -1
        for i, r in enumerate(task_reports):
            if r.id == trigger_report.id:
                trigger_idx = i
                break
        if not include_trigger and trigger_idx >= 0:
            # Exclude trigger: history ends at the report before it. The
            # full-window anchor moves to trigger_idx-1 so the N reports
            # immediately preceding the trigger are still full.
            task_reports = task_reports[:trigger_idx]
            full_anchor = len(task_reports) - 1
        else:
            full_anchor = trigger_idx if trigger_idx >= 0 else len(task_reports) - 1
        if not task_reports:
            return []
        full_start = max(0, full_anchor - self._FULL_REPORT_WINDOW + 1)
        out: list[dict[str, Any]] = []
        for i, report in enumerate(task_reports):
            out.append(self._serialize_report_for_review(report, full=(i >= full_start)))
        return out

    def _build_review_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        reviewer: ManagedSession,
        trigger_report: AgentReport,
        lesson_context: list[dict[str, Any]] | None = None,
    ) -> str:
        report_payload = self._serialize_task_reports_for_review(task, trigger_report)
        profiles = self._effective_review_profiles(task, trigger_report)
        lesson_context_block = self._lesson_context_block_from_payload(
            (
                lesson_context
                if lesson_context is not None
                else self._lesson_context_payload(
                    workspace,
                    f"{task.title}\n{task.prompt}\n{trigger_report.message}",
                )
            ),
            workspace_id=workspace.id,
        )
        return (
            "Review workspace task.\n\n"
            f"Workspace: {workspace.name}; Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Task mode: {task.task_mode.value}\n"
            f"Task execution complexity: {task.execution_complexity.value}\n"
            f"Implementation session: {task.session_id or 'unknown'}; Reviewer: {reviewer.id}\n"
            f"{self._session_environment_lines(workspace, reviewer)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            f"Task description: {task.prompt}\n\n"
            f"{self._execution_complexity_review_block(task)}"
            "Stored Goal Packet JSON (null if plan-gate):\n"
            f"{task.goal_packet.model_dump_json() if task.goal_packet else 'null'}\n\n"
            f"{self._autonomous_review_block(task)}"
            f"{self._review_profile_prompt_block(profiles)}"
            f"{self._review_guidance_block(workspace, trigger_report)}"
            f"{lesson_context_block}"
            f"{self._review_workflow_block(task, trigger_report)}"
            "Final report format: keep the message SHORT (<=12 lines total). Detailed "
            "evidence goes in structured fields (validation/risks/acceptance_check/"
            "profile_results/artifact_refs), not in the message body. Every report must "
            "include message_en (English) and message_zh (中文); `message` is a short "
            "English fallback.\n"
            "Message body sections: Verdict (review_passed|review_failed|review_needs_input); "
            'Summary (1-2 sentences); Acceptance criteria rollup (e.g. "3/4 passed (1 '
            'partial: <criterion>)"); Required fixes (1-3 concrete, only for review_failed); '
            "Notes (residual risk, at most one line).\n\n"
            f"Trigger report (full JSON):\n{trigger_report.model_dump_json()}\n\n"
            f"Task history JSON (prior reports; most recent {self._FULL_REPORT_WINDOW} full; "
            f"earlier summarized with verbose fields truncated; trigger report is above):\n"
            f"{json.dumps(report_payload, indent=2)}\n\n"
            f"{self._review_submission_block(task, reviewer)}"
        )

    def _review_submission_block(self, task: WorkspaceTask, reviewer: ManagedSession) -> str:
        review_attempt = max(task.review_attempts, 1)
        review_started_call_id = self._report_prompt_call_id(
            task.id, "review-started", attempt=review_attempt
        )
        review_passed_call_id = self._report_prompt_call_id(
            task.id, "review-passed", attempt=review_attempt
        )
        return (
            "Canonical report submission (copy these commands; retain the supplied host/port).\n"
            "Do not POST to /api/sessions/... or any preview* path: those are not report "
            "routes and return 404. A 404 means delivery FAILED; terminal text is not a "
            "recorded verdict. If this exact URL fails, inspect the Hub connection/port "
            "and surface the delivery error; do not stop silently or guess a route.\n"
            "Fill in actual evidence and choose the appropriate final verdict; never infer "
            "review_passed from this example. Require HTTP 201 and a report id as receipt.\n"
            "Report workflow: first POST review_started, then exactly one final verdict "
            "(use a stable call_id per report; reuse the same call_id on retry; include a "
            "round counter so repeated reviews don't collide):\n"
            "```bash\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(reviewer)}/api/workspaces/sessions/{reviewer.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"review_started",'
            f'"call_id":"{review_started_call_id}",'
            '"message":"Started review","message_en":"Started review",'
            '"message_zh":"开始评审"}\'\n'
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(reviewer)}/api/workspaces/sessions/{reviewer.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"review_passed",'
            f'"call_id":"{review_passed_call_id}",'
            '"message":"Verdict + summary + acceptance rollup + notes",'
            '"message_en":"Verdict + summary + acceptance rollup + notes",'
            '"message_zh":"结论 + 摘要 + 验收汇总 + 备注",'
            '"validation":"Checks reviewed","risks":"Residual risk or none",'
            '"review_profiles":["general"],"profile_results":[{"profile":"general",'
            '"status":"passed","evidence":"Evidence reviewed.","blocking_findings":[],'
            '"non_blocking_findings":[]}],"artifact_refs":[],"confidence":0.8,'
            '"requires_human_judgment":false}\'\n\n'
            "```\n"
            "Use review_failed when the implementation agent can still fix concrete defects. "
            "Use review_needs_input only for genuine blockers outside its control."
        )

    def _review_workflow_block(
        self,
        task: WorkspaceTask,
        trigger_report: AgentReport,
    ) -> str:
        is_gp = self._is_goal_packet_approval_review(task, trigger_report)
        gp_intro = (
            "Goal Packet approval review (plan gate). "
            "This is a pre-implementation plan gate. There should be no substantive "
            "implementation yet. Do not judge implementation completeness — judge the plan.\n"
            if is_gp
            else ""
        )
        gp_extra = (
            "- Verify the packet has reviewer-checkable acceptance criteria, validation plan, "
            "assumptions, out-of-scope boundaries, and handoff requirements; treat missing editable/"
            "non-editable boundaries or vague validation as blocking.\n"
            "- Check execution order: the implementation agent must wait for this approval before "
            "substantive development and stay within the approved packet unless it submits a revision.\n"
            if is_gp
            else (
                "- Derive a task-specific acceptance checklist from the task title/description, the "
                "stored Goal Packet (objective/acceptance/validation/assumptions/out-of-scope/handoff), "
                "explicit user requirements/attachments, the trigger's changed_files/validation/risks/"
                "acceptance_check, enabled review profiles + REVIEW.md guidance, repo conventions, "
                "and any blocked/needs_input context.\n"
                "- Start from requirements and the actual checkout diff/call paths, using the worker report "
                "as claims to verify. Record execution cwd and base/head SHA; check that they identify the "
                "candidate under review before running tests.\n"
                "- Adversarial defect hunt (BEFORE the verdict): actively try to break the change by "
                "enumerating failure modes and checking each against the actual code: edge/boundary "
                "inputs; error/exception paths; concurrency/ordering/shared-state races; regressions to "
                "existing flows/persistence/migrations; scope leakage/side effects; security/permission "
                "assumptions. Seek counterevidence before reporting a finding; distinguish a demonstrated "
                "defect from an unverified risk.\n"
                "- Independently replay checks appropriate to risk. In validation/artifact_refs record "
                "commands, cwd, outcomes and evidence paths; in risks record checks not run and limits. "
                "Each finding should give severity, confidence, file/line, causal failure scenario and "
                "supporting evidence. Missing evidence is not a passing result.\n"
            )
        )
        return (
            "Review workflow:\n"
            "1. Stay read-only: do not edit files, run writing formatters, or revert work.\n"
            f"2. {gp_intro}"
            "Check the stored Goal Packet faithfully preserves the original task prompt and does not "
            "narrow/distort scope; fail if it does.\n"
            f"3. {gp_extra}"
            "4. Produce one final verdict using the exit criteria below.\n\n"
            + (
                "Plan-gate acceptance standards:\n"
                "- Goal fidelity; boundary quality (editable areas/non-goals/deps explicit enough to constrain impl); "
                "reviewability (acceptance/validation concrete enough to check later); handoff quality.\n\n"
                "Plan-gate exit criteria:\n"
                "- review_passed means the implementation agent may begin development from the approved Goal "
                "Packet. It does NOT mean implementation is complete or ready for human acceptance.\n"
                "- review_failed means the implementation agent must revise only the Goal Packet and resubmit "
                "it for approval before development. Include a Required fixes section.\n"
                "- review_needs_input means the packet cannot be judged without user/product clarification, "
                "credentials, unavailable environment, or a decision the implementation agent cannot safely infer.\n\n"
                if is_gp
                else "Acceptance standards:\n"
                "- Goal fidelity; functional correctness end-to-end; scope control (no unrelated churn); "
                "integration fit (architecture/state/API/UI conventions); regression safety; validation quality "
                "matching the risk level; handoff quality (changed_files/validation/risks understandable).\n\n"
                "Review exit criteria:\n"
                "- review_passed: you actively tried to break the change (step 3 defect hunt) and found no blocking "
                "defect; every acceptance criterion is satisfied; validation is adequate (gaps explicitly non-blocking); "
                "residual risks acceptable for human acceptance. Do NOT pass merely because the implementation report "
                "looked confident or because nothing obvious looked wrong.\n"
                "- review_failed: at least one blocking defect/regression/scope issue/missing required validation that "
                "the implementation agent can fix. Include a Required fixes section.\n"
                "- review_needs_input: review cannot finish without a user/product decision, credential, unavailable "
                "environment, or other judgment the implementation agent cannot safely infer.\n\n"
            )
        )

    def _execution_complexity_review_block(self, task: WorkspaceTask) -> str:
        return (
            "Execution complexity review context:\n"
            f"- Selected complexity: {task.execution_complexity.value}\n"
            "- Check the declared strategy against dependencies, scope ownership and evidence. "
            "Simple work may run directly; complex work needs decomposition and integration checks, "
            "but not a minimum agent count. Auto must record a simple/complex choice. Missing required "
            "validation is blocking; choosing serial execution is not itself a defect.\n\n"
        )

    def _autonomous_review_block(self, task: WorkspaceTask) -> str:
        if task.task_mode != WorkspaceTaskMode.AUTONOMOUS:
            return ""
        policy = task.autonomy_policy or AutonomyPolicy()
        run = task.autonomous_run
        return (
            "Autonomous evaluation context:\n"
            f"- Run: {run.model_dump_json() if run else 'null'}; worker runtime: {task.agent_type.value}\n"
            f"- Max iterations: {policy.max_iterations}; strictness: {policy.evaluation_strictness.value}; "
            f"artifact review: {policy.require_artifact_review}.\n"
            "You are the independent Hub evaluator; native sub-agent review or worker self-review "
            "does not replace this gate. Score against the Goal Packet and verified evidence. "
            "Use review_passed for passed work awaiting human acceptance, review_failed for fixable "
            "blocking issues, review_needs_input for unavailable evidence or decisions outside the "
            "worker's control.\n\n"
            "Subagent ledger verification (when delegation occurred):\n"
            "- Match each claimed delegation to its owner, inputs, scope, actual runtime/model evidence "
            "and accepted/rejected/retried result. Missing evidence for claimed work is blocking.\n"
            "- For direct execution, assess commands/results and the strategy rationale; do not demand "
            "a fabricated subagent-ledger or fail solely because no native P-JUDGE ran.\n"
            "- Respect user model choices. Accept runtime-default, unsupported:<reason>, actual model "
            "names or external:<api> with honest limits; do not enforce a fixed provider/tier.\n\n"
        )
