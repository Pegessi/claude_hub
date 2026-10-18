"""Prompt builders split from the compatibility mixin."""

from .._constants import *  # noqa: F401,F403
from .review import _ReviewPromptsMixin


class _RecoveryPromptsMixin(_ReviewPromptsMixin):
    def _build_continue_prompt(
        self,
        task: WorkspaceTask,
        payload: ContinueTaskRequest,
        session: ManagedSession,
        *,
        failure_reason: str | None = None,
    ) -> str:
        message = payload.message.strip() if payload.message else ""
        attachments = self._persist_attachments(
            task.workspace_id,
            f"{task.id}-continue-{uuid.uuid4().hex[:8]}",
            payload.attachments,
        )
        if failure_reason:
            header = "Continue workspace task after failure.\n\n"
            failure_block = f"Previous failure reason: {failure_reason}\n\n"
            default_followup = f"Retry the task, addressing the failure above. {message}".strip()
        else:
            header = "Continue workspace task from review.\n\n"
            failure_block = ""
            default_followup = "Continue addressing the review feedback."
        follow_up = self._append_attachment_block(
            message or default_followup,
            attachments,
        )
        return (
            f"{header}"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"{failure_block}"
            f"Follow-up instructions:\n{follow_up}\n\n"
            f"{self._autonomous_continue_orchestrator_reminder(task)}"
            "The task is back in working state. Report progress with the same task_id.\n\n"
            f"{self._report_endpoint_curl(session, task.id, purpose='continue-progress', attempt=task.review_cycle)}"
        )

    def _autonomous_continue_orchestrator_reminder(self, task: WorkspaceTask) -> str:
        if task.task_mode != WorkspaceTaskMode.AUTONOMOUS:
            return ""
        return (
            "Revision strategy: preserve the recorded simple/complex strategy and ownership boundaries. "
            "Fix only blocking issues, rerun relevant checks, and retain passing evidence. Reuse "
            "delegation only when it helps; append actual results to an existing ledger, do not restart "
            "or fabricate it. If context is stale, reload current task/report records and Git evidence. "
            "The independent Hub evaluator remains mandatory.\n\n"
        )

    def _latest_reviewer_blocking_feedback(self, task: WorkspaceTask) -> str | None:
        """Return the most recent review_failed message text, or None."""
        reports = [r for r in self.reports_for_workspace(task.workspace_id) if r.task_id == task.id]
        for r in reversed(reports):
            if r.state == AgentReportState.REVIEW_FAILED and r.message:
                return r.message
        return None

    def _current_changed_files(self, task: WorkspaceTask) -> list[str]:
        """Collect changed_files mentioned in the worker's most recent reports."""
        files: list[str] = []
        seen: set[str] = set()
        reports = [
            r
            for r in self.reports_for_workspace(task.workspace_id)
            if r.task_id == task.id
            and r.session_id == task.session_id
            and r.state
            in (
                AgentReportState.WORKING,
                AgentReportState.READY_FOR_REVIEW,
                AgentReportState.COMPLETED,
            )
        ]
        for r in reports:
            for f in r.changed_files:
                if f and f not in seen:
                    seen.add(f)
                    files.append(f)
        return files[-20:]

    def _build_revision_resume_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
        *,
        interruption_reason: str,
        recovery_attempt: int,
    ) -> str:
        """Compact briefing for an autonomous worker whose context was cleared mid-task.

        Used by hard-recovery on iteration>=2. Replaces the full assignment prompt
        with a tight briefing (compact GP, changed files, one-paragraph progress,
        reviewer's exact blocking feedback) so the cleared agent resumes without
        replaying every prior instruction verbatim.
        """
        agent_session_id = self._agent_session_id_for_session(session)
        session_line = f"Conversation ID: {agent_session_id}\n" if agent_session_id else ""
        run = task.autonomous_run
        iteration = run.iteration if run else task.review_cycle
        # Compact Goal Packet: only objective/acceptance/out_of_scope -- skip verbose plans.
        gp = task.goal_packet
        if gp:
            gp_block = (
                "Approved Goal Packet (compact):\n"
                f"- objective: {gp.objective}\n"
                f"- acceptance_criteria ({len(gp.acceptance_criteria)}):\n"
                + "".join(f"    - {c}\n" for c in gp.acceptance_criteria[:8])
                + (
                    f"    - ... ({len(gp.acceptance_criteria)-8} more)\n"
                    if len(gp.acceptance_criteria) > 8
                    else ""
                )
                + (f"- out_of_scope: {'; '.join(gp.out_of_scope[:6])}\n" if gp.out_of_scope else "")
                + "\n"
            )
        else:
            gp_block = ""
        changed = self._current_changed_files(task)
        changed_block = (
            (
                "Files already changed in this task (verify before editing):\n"
                + "".join(f"  - {f}\n" for f in changed)
                + "\n"
            )
            if changed
            else ""
        )
        feedback = self._latest_reviewer_blocking_feedback(task)
        feedback_block = ""
        if feedback:
            # Truncate very long reviewer messages to keep briefing tight.
            if len(feedback) > 1500:
                feedback = feedback[:1500].rstrip() + "...[truncated]"
            feedback_block = (
                f"Latest reviewer blocking feedback (address this round):\n{feedback}\n\n"
            )
        return (
            "⚠️  Context refreshed after error. A fresh context has been started within the same "
            "conversation; prior turns are no longer visible. You are resuming an in-flight task "
            "at a revision step -- do NOT restart from scratch.\n\n"
            f"Error: {interruption_reason}\n"
            f"Workspace: {workspace.name}\n"
            f"Task: {task.id} ({task.title})  mode={task.task_mode.value}  "
            f"complexity={task.execution_complexity.value}  iteration={iteration}\n"
            f"{session_line}"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n"
            f"Task description: {task.prompt}\n\n"
            f"{gp_block}{changed_block}{feedback_block}"
            f"{self._task_context_contract_block()}"
            f"{self._autonomous_continue_orchestrator_reminder(task)}"
            "Resume steps:\n"
            "1. Re-read the state snapshot and inspect the files listed above before editing.\n"
            "2. Reload the full Goal Packet and latest report if the compact briefing omits needed "
            "boundaries, validation steps or the recorded strategy. Address blocking feedback, or "
            "resume the last verified working state.\n"
            "3. Check whether a prior ready_for_review/completed report was persisted before retrying "
            "it; reuse its call_id only for the same report. Do not infer completion from a summary.\n"
            "4. Report working/progress/blocked/completed with the same task_id.\n\n"
            f"{self._report_endpoint_curl(session, task.id, purpose='worker-recovery-progress', attempt=recovery_attempt)}"
        )

    def _build_hard_recovery_worker_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
        interruption_reason: str,
        recovery_attempt: int | None = None,
    ) -> str:
        """Prompt sent after hard recovery (interrupt + /clear) for a worker agent.

        The agent's context has been wiped by /clear but the CLI conversation id is preserved,
        so the agent can resume work without losing the session entirely.

        For autonomous tasks past the first iteration (iteration>=2 or review_cycle>=2), use
        the compact revision-resume briefing instead of replaying the full assignment prompt,
        to avoid repiling prompt text on a cleared context.
        """
        if recovery_attempt is None:
            prior_attempts = (
                session.hard_recovery_attempts if session.hard_recovery_task_id == task.id else 0
            )
            recovery_attempt = prior_attempts + 1
        run = task.autonomous_run
        iteration = run.iteration if run else 0
        use_resume = task.task_mode == WorkspaceTaskMode.AUTONOMOUS and (
            iteration >= 2 or task.review_cycle >= 2
        )
        if use_resume:
            return self._build_revision_resume_prompt(
                workspace,
                task,
                session,
                interruption_reason=interruption_reason,
                recovery_attempt=recovery_attempt,
            )
        # Cold-start-style hard recovery (first iteration, or non-autonomous task)
        agent_session_id = self._agent_session_id_for_session(session)
        session_line = f"Conversation ID: {agent_session_id}\n" if agent_session_id else ""
        goal_packet_line = (
            f"Previously approved Goal Packet JSON:\n{task.goal_packet.model_dump_json()}\n\n"
            if task.goal_packet
            else ""
        )
        return (
            f"{HARD_RECOVERY_WORKER_MESSAGE}\n\n"
            f"Error detected: {interruption_reason}\n\n"
            f"Workspace: {workspace.name}\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Task mode: {task.task_mode.value}\n"
            f"{session_line}"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            f"Task description:\n{task.prompt}\n\n"
            f"{goal_packet_line}"
            f"{self._autonomous_continue_orchestrator_reminder(task)}"
            f"{self._task_context_contract_block()}"
            "Resume work now. Check the current task/report and files before acting. If a prior report "
            "was not persisted, retry that same report with its original call_id; do not turn "
            "ready_for_review into completed based only on a conversation summary.\n\n"
            f"{self._report_endpoint_curl(session, task.id, purpose='worker-recovery-progress', attempt=recovery_attempt)}"
        )

    def _reviewer_recovery_call_ids(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        *,
        recovery_attempt: int | None = None,
    ) -> tuple[int, str, dict[str, str]]:
        """Return the durable recovery attempt and verdict-specific report IDs.

        ``recovery_attempt`` must come from durable session state so a retried
        paste of the same hard-recovery prompt keeps its IDs, while a later
        recovery gets a new attempt.
        """
        if recovery_attempt is None:
            prior_attempts = (
                session.hard_recovery_attempts if session.hard_recovery_task_id == task.id else 0
            )
            recovery_attempt = prior_attempts + 1
        review_started_call_id = self._report_prompt_call_id(
            task.id, "review-started-recovery", attempt=recovery_attempt
        )
        verdict_call_ids = {
            verdict: self._report_prompt_call_id(
                task.id, f"{verdict.replace('_', '-')}-recovery", attempt=recovery_attempt
            )
            for verdict in (
                "review_passed",
                "review_failed",
                "review_needs_input",
            )
        }
        return recovery_attempt, review_started_call_id, verdict_call_ids

    def _build_hard_recovery_reviewer_fallback_prompt(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        interruption_reason: str,
        recovery_attempt: int | None = None,
    ) -> str:
        """Compact reviewer recovery prompt when no trigger report exists."""
        recovery_attempt, review_started_call_id, verdict_call_ids = (
            self._reviewer_recovery_call_ids(task, session, recovery_attempt=recovery_attempt)
        )
        verdict_call_id_block = "\n".join(
            f"- {verdict}: `{call_id}`" for verdict, call_id in verdict_call_ids.items()
        )
        return (
            f"{HARD_RECOVERY_REVIEWER_MESSAGE}\n\n"
            f"Error detected: {interruption_reason}\n\n"
            f"Task ID: {task.id}\nTask title: {task.title}\n\n"
            "No trigger report is available. Resume the review and issue "
            "review_passed, review_failed, or review_needs_input. "
            "Reuse the exact ID when retrying the same report. Use the matching "
            "verdict-specific ID for the one final verdict:\n"
            f"- review_started: `{review_started_call_id}`\n"
            f"{verdict_call_id_block}\n\n"
            f"{self._report_endpoint_curl(session, task.id, purpose='review-started-recovery', attempt=recovery_attempt, state='review_started')}"
        )

    def _build_hard_recovery_reviewer_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
        trigger_report: AgentReport,
        interruption_reason: str,
        recovery_attempt: int | None = None,
    ) -> str:
        """Prompt sent after hard recovery (interrupt + /clear) for a reviewer agent."""
        recovery_attempt, review_started_call_id, verdict_call_ids = (
            self._reviewer_recovery_call_ids(task, session, recovery_attempt=recovery_attempt)
        )
        report_payload = self._serialize_task_reports_for_review(task, trigger_report)
        agent_session_id = self._agent_session_id_for_session(session)
        session_line = f"Conversation ID: {agent_session_id}\n" if agent_session_id else ""
        verdict_call_id_block = "\n".join(
            f"- {verdict}: `{call_id}`" for verdict, call_id in verdict_call_ids.items()
        )
        return (
            f"{HARD_RECOVERY_REVIEWER_MESSAGE}\n\n"
            f"Error detected: {interruption_reason}\n\n"
            f"Workspace: {workspace.name}; Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Task mode: {task.task_mode.value}\n"
            f"Task execution complexity: {task.execution_complexity.value}\n"
            f"Implementation session: {task.session_id or 'unknown'}; Reviewer: {session.id}\n"
            f"{session_line}"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            f"Task description: {task.prompt}\n\n"
            f"Stored Goal Packet JSON:\n"
            f"{task.goal_packet.model_dump_json() if task.goal_packet else 'null'}\n\n"
            f"Trigger report (full JSON):\n{trigger_report.model_dump_json()}\n\n"
            f"Task history JSON (prior reports; recent {self._FULL_REPORT_WINDOW} full; "
            f"earlier summarized; trigger report is above):\n"
            f"{json.dumps(report_payload, indent=2)}\n\n"
            "Resume the review now. Read the worker's latest report, check changed files for "
            "evidence, and issue review_passed, review_failed, or review_needs_input. "
            "Reuse the exact ID when retrying the same report. Use the matching "
            "verdict-specific ID for the one final verdict:\n"
            f"- review_started: `{review_started_call_id}`\n"
            f"{verdict_call_id_block}\n\n"
            f"{self._report_endpoint_curl(session, task.id, purpose='review-started-recovery', attempt=recovery_attempt, state='review_started')}"
        )

    def _agent_session_id_for_session(self, session: ManagedSession) -> str | None:
        """Look up the CLI conversation id (agent_session_id) from ttyd_manager for a session."""
        try:
            tab = ttyd_manager.get_tab(session.tab_id)
            return tab.agent_session_id if tab else None
        except Exception:
            return None
