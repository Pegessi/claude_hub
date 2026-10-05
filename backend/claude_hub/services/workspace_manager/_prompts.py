"""Bootstrap, assignment, review, and continue prompt builders."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)
from claude_hub.services.agent_execution_policy import EXECUTION_POLICY

from ._constants import *  # noqa: F401,F403


class _PromptsMixin:
    async def spawn_worker(
        self,
        task_id: str,
        agent_type: Optional[AgentType] = None,
    ) -> ManagedSession:
        del agent_type
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        if task.workspace_id not in self.workspaces:
            raise KeyError(task.workspace_id)
        raise RuntimeError(
            "Worker spawning is disabled. Add a workspace agent and start the task instead."
        )

    def _build_session_bootstrap_prompt(
        self,
        workspace: Workspace,
        session: ManagedSession,
    ) -> str:
        if session.role == WorkspaceSessionRole.DISPATCHER:
            return self._build_dispatcher_bootstrap_prompt(workspace, session)
        if session.role == WorkspaceSessionRole.REVIEWER:
            return self._build_reviewer_bootstrap_prompt(workspace, session)
        if session.role == WorkspaceSessionRole.RESIDENT:
            # A TERMINAL resident is a plain shell with no LLM agent listening,
            # so the self-drive prompt would just be dumped as shell input. Skip
            # it; the user still gets an openable tab. (Mirrors the send guard in
            # _run_resident_agent for the reuse path.)
            if session.agent_type == AgentType.TERMINAL:
                return ""
            return _wm.build_resident_agent_prompt(
                workspace,
                self._report_base_url(session),
                session.id,
            )
        return self._build_workspace_agent_prompt(workspace, session)

    def _report_base_url(self, session: ManagedSession) -> str:
        if session.remote_forward_port:
            return f"http://127.0.0.1:{session.remote_forward_port}"
        return f"http://localhost:{settings.port}"

    def _report_prompt_call_id(
        self,
        task_id: str,
        purpose: str,
        *,
        attempt: int | str | None = None,
        cycle: int | str | None = None,
    ) -> str:
        """Return a stable backend-owned ID for one logical report prompt.

        ``cycle`` separates work/review rounds. ``purpose`` separates the
        different reports requested by one prompt. ``attempt`` must come from
        durable task/session state so replaying the same logical prompt keeps
        its ID while a later prompt gets a new one.
        """

        task = self.tasks.get(task_id)
        if cycle is None:
            cycle = max(task.review_cycle, 1) if task is not None else "REVIEW_CYCLE"
        if attempt is None:
            attempt = cycle if task is not None else "DURABLE_ATTEMPT"
        return f"{task_id}-{purpose}-cycle-{cycle}-attempt-{attempt}"

    def _report_endpoint_curl(
        self,
        session: ManagedSession,
        task_id: str | None = None,
        *,
        purpose: str = "working-progress",
        attempt: int | str | None = None,
        state: str = "working",
    ) -> str:
        """Render the report-endpoint curl example for a session.

        The report endpoint otherwise only appears in the bootstrap/assignment/
        review prompts. Any follow-up message that asks an agent to report after
        its context may have been cleared must restate this endpoint, or a
        cleared agent has no curl target to POST to.
        """
        task_field = task_id if task_id is not None else "TASK_ID"
        call_id = self._report_prompt_call_id(task_field, purpose, attempt=attempt)
        return (
            "Report endpoint (include a stable call_id; reuse the SAME call_id "
            "when resubmitting the same report after a failure or context "
            "reload so the Hub deduplicates it). Use a per-cycle call_id so "
            "repeated reports in the same state (e.g. multiple working updates "
            "across review rounds) do not collide:\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}"
            f"/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task_field}","state":"{state}",'
            f'"call_id":"{call_id}",'
            '"message":"Progress update",'
            '"message_en":"Progress update","message_zh":"进度更新"}\''
        )

    def _remote_target_label(self, session: ManagedSession) -> str:
        if not session.remote_profile_id:
            return "unknown remote host"
        profile = remote_profile_manager.get_profile(session.remote_profile_id)
        if not profile:
            return session.remote_profile_id
        host = f"{profile.user}@{profile.ssh_host}" if profile.user else profile.ssh_host
        if profile.port != 22:
            host = f"{host}:{profile.port}"
        if profile.name and profile.name != profile.id:
            return f"{profile.name} ({host})"
        return host

    def _session_environment_lines(self, workspace: Workspace, session: ManagedSession) -> str:
        lines = [
            f"Runtime target: {session.target.value}",
            f"Local workspace dir: {workspace.path}",
        ]
        if session.target == ExecutionTarget.REMOTE:
            lines.extend(
                [
                    f"SSH development target: {self._remote_target_label(session)}",
                    f"Remote working directory: {session.workspace_path}",
                ]
            )
        else:
            lines.append(f"Default working directory: {session.workspace_path}")
        if session.env:
            lines.append("Custom environment variables: " + ", ".join(sorted(session.env.keys())))
        return "\n".join(lines)

    def _build_workspace_agent_prompt(self, workspace: Workspace, session: ManagedSession) -> str:
        return (
            "You are a resident workspace agent.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Session: {session.id}\n"
            f"{self._session_environment_lines(workspace, session)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            "Wait in this terminal for assigned tasks; do not start unrelated work. When a task "
            "arrives, read the state snapshot first, choose the correct project directory from the "
            "task, and check for uncommitted changes before editing. If another agent modified files "
            "you need, avoid overwriting; ask for review. When reviewer feedback comes back, continue "
            "from the feedback and re-report when done.\n\n"
            "Reports include message_en (English) and message_zh (中文); `message` is a short English "
            "fallback. Final reports include changed_files, validation, risks, acceptance_check, "
            "review_decision (request/skip/auto with review_reason when applicable), and risk_level; "
            "every completed task waits for human acceptance before it is done.\n\n"
            "Every report MUST include a non-empty `call_id`. Use a stable, per-logical-report "
            "call_id that includes a cycle/round counter so repeated reports in the same state "
            "(e.g. multiple working updates across review rounds) do not collide. Format: "
            "`{task_id}-{state}-{n}` where n increments for each new logical report. If you "
            "resubmit the SAME report after a failure or context reload, reuse the SAME call_id "
            "so the Hub deduplicates it. Different reports (e.g. a working update vs. the final "
            "report) use different call_ids.\n\n"
            "Report endpoint (POST JSON for assigned tasks):\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            '-d \'{"task_id":"TASK_ID","state":"working",'
            '"call_id":"TASK_ID-working-progress-cycle-REVIEW_CYCLE-attempt-DURABLE_ATTEMPT",'
            '"message":"Progress update",'
            '"message_en":"Progress update","message_zh":"进度更新"}\''
        )

    def _build_dispatcher_bootstrap_prompt(
        self,
        workspace: Workspace,
        session: ManagedSession,
    ) -> str:
        return (
            "You are the dispatcher agent for this workspace.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Session: {session.id}\n"
            f"{self._session_environment_lines(workspace, session)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            "When asked for a dispatch decision, choose the best workspace agent "
            "for context continuity and decide whether the target should clear context. "
            "Return decisions only by calling the provided local API endpoint. "
            "This dispatcher path is a reserved smart-assignment extension point and is "
            "independent from reviewer workflow decisions."
        )

    def _build_reviewer_bootstrap_prompt(
        self,
        workspace: Workspace,
        session: ManagedSession,
    ) -> str:
        return (
            "You are an independent reviewer agent for this workspace. Wait for explicit review "
            "assignments. Stay read-only: do not implement, refactor, format, or edit files.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Session: {session.id}\n"
            f"{self._session_environment_lines(workspace, session)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            "Reviewer mindset:\n"
            "- Your job is to FIND defects and risks, not confirm success. A confident or "
            "well-written implementation report is not evidence the code is correct. Assume "
            "something is wrong until you have actively looked for it; do not pass merely "
            "because nothing obvious looked wrong.\n"
            "- Do not defer to the implementation agent. Judge code and observed state, not "
            "tone, confidence, or formatting.\n"
            "- It is correct to fail a review for real blocking defects. Do not soften or wave "
            "through borderline issues to avoid friction.\n"
            "- Treat self-reported validation as claims to independently spot-check, not proof; "
            "if you cannot verify a critical claim, it is unverified, not passing.\n\n"
            "Reporting rules (details repeated in each review prompt):\n"
            "- POST review_started when you begin; finish with exactly one review_passed, "
            "review_failed, or review_needs_input.\n"
            "- Keep message SHORT (<=12 lines; Verdicts/Summary/Acceptance rollup/Required fixes/Notes); "
            "put evidence in structured fields (validation/risks/acceptance_check/profile_results/artifact_refs).\n"
            "- Every report carries message_en (English) and message_zh (中文); legacy message is a short fallback.\n"
            "- Every report MUST include a non-empty `call_id` (e.g. `{task_id}-{state}-{n}` "
            "where n increments per review round). Reuse the SAME call_id when resubmitting "
            "the same report after a failure or context reload so the Hub deduplicates it.\n"
            "- Use review_failed when the impl agent can fix concrete defects; review_needs_input only for "
            "genuine product/credential/environment blockers you cannot infer.\n\n"
            "Report endpoint (task_id supplied with each assignment):\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            '-d \'{"task_id":"TASK_ID","state":"review_started",'
            '"call_id":"TASK_ID-review-started-cycle-REVIEW_CYCLE-attempt-DURABLE_ATTEMPT",'
            '"message":"Started review","message_en":"Started review","message_zh":"开始评审"}\''
        )

    def _build_dispatch_decision_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        dispatcher: ManagedSession,
    ) -> str:
        agents = [
            {
                "id": session.id,
                "title": session.title,
                "agent_type": session.agent_type.value,
                "target": session.target.value,
                "workspace_path": session.workspace_path,
                "runtime": session.runtime_status.value,
                "current_task_id": session.current_task_id,
                "queued_count": self._queued_count(session.id),
            }
            for session in self._workspace_agents(workspace.id)
        ]
        recent_tasks = [
            {
                "id": item.id,
                "title": item.title,
                "status": item.status.value,
                "agent": item.session_id,
            }
            for item in sorted(
                [item for item in self.tasks.values() if item.workspace_id == workspace.id],
                key=lambda item: item.updated_at,
                reverse=True,
            )[:12]
        ]
        return (
            "Dispatch decision needed.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Task execution complexity: {task.execution_complexity.value}\n"
            f"Task description:\n{task.prompt}\n\n"
            f"Available agents JSON:\n{json.dumps(agents, indent=2)}\n\n"
            f"Recent tasks JSON:\n{json.dumps(recent_tasks, indent=2)}\n\n"
            "Choose a target_agent_id and whether to clear context. Prefer context continuity "
            "for related work. If the best related agent is busy, still choose that agent so "
            "the workspace queues the task behind its current work.\n\n"
            "Call this endpoint with your decision:\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(dispatcher)}/api/workspaces/tasks/{task.id}/dispatch-decision "
            "-H 'Content-Type: application/json' "
            '-d \'{"target_session_id":"AGENT_ID","clear_context":false,'
            '"reason":"why this agent is best"}\''
        )

    def _build_task_assignment_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
        *,
        lesson_context: list[dict[str, Any]] | None = None,
    ) -> str:
        if task.task_mode == WorkspaceTaskMode.SUBAGENT:
            return self._build_subagent_assignment_prompt(
                workspace, task, session, lesson_context=lesson_context
            )
        clear_note = (
            "This task is unrelated to prior work. Treat prior conversation context as stale.\n\n"
            if task.clear_context
            else ""
        )
        failure_note = (
            f"This task previously failed. Previous failure reason: {task.failure_reason}\n\n"
            if task.failure_reason
            else ""
        )
        attachment_note = (
            f"{self._attachment_prompt_block(task.attachments)}\n\n" if task.attachments else ""
        )
        lesson_context_block = self._lesson_context_block_from_payload(
            (
                lesson_context
                if lesson_context is not None
                else self._lesson_context_payload(workspace, f"{task.title}\n{task.prompt}")
            ),
            workspace_id=workspace.id,
        )
        assignment_attempt = max(task.dispatch_attempt, 1)
        goal_packet_call_id = self._report_prompt_call_id(
            task.id, "goal-packet", attempt=assignment_attempt
        )
        started_call_id = self._report_prompt_call_id(
            task.id, "started", attempt=assignment_attempt
        )
        progress_call_id = self._report_prompt_call_id(
            task.id, "assignment-progress", attempt=assignment_attempt
        )
        return (
            "New workspace task assigned.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Task mode: {task.task_mode.value}\n"
            f"Task execution complexity: {task.execution_complexity.value}\n"
            f"{self._session_environment_lines(workspace, session)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n"
            f"Dispatch reason: {task.dispatch_reason or 'not specified'}\n\n"
            f"{clear_note}"
            f"{failure_note}"
            f"Task description:\n{task.prompt}\n\n"
            f"{attachment_note}"
            f"{lesson_context_block}"
            f"{self._execution_complexity_assignment_block(task)}"
            f"{self._autonomous_assignment_block(task, session.agent_type)}"
            f"{self._task_context_contract_block()}"
            "Start by reading the state snapshot; use the task description to choose the correct project "
            "directory. Check for uncommitted changes before editing.\n\n"
            "Before substantive implementation, derive a Goal Packet from the task prompt and include it "
            "in your first working report. The packet must preserve the user's outcome, record assumptions "
            "(do not silently narrow ambiguous scope), and include concrete reviewer-checkable acceptance "
            "criteria, a validation plan, out-of-scope boundaries, and handoff requirements. For reviewed "
            "tasks the Goal Packet is an approval gate: after posting it, stop and wait for reviewer "
            "feedback; do not begin substantive implementation until the backend continues the task after "
            "review_passed.\n\n"
            "Report state: started -> working (as you progress) -> blocked/needs_input if stuck -> "
            "ready_for_review when ready for AI reviewer -> completed when fully done. The task is not "
            "done until a human accepts it.\n\n"
            "For completed reports decide reviewer routing: review_decision=request (independent AI review "
            "needed; always include review_reason), review_decision=skip (no-change analysis, manual "
            "follow-up, or trivial low-risk changes only; still requires human acceptance), or "
            "review_decision=auto (workspace default). The backend may still force review for nontrivial "
            "changes.\n\n"
            "Goal Packet report example (first working report for reviewed tasks):\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"working",'
            f'"call_id":"{goal_packet_call_id}",'
            '"message":"Goal Packet; awaiting approval.","message_en":"Goal Packet; awaiting approval.",'
            '"message_zh":"目标包已创建，等待审核。","goal_packet":{'
            '"objective":"...","acceptance_criteria":["..."],"validation_plan":["..."],'
            '"assumptions":["..."],"out_of_scope":["..."],"handoff_requirements":["..."]}}\'\n\n'
            "Every report must include message_en (concise English) and message_zh (concise 中文); keep "
            "the legacy `message` field as a short English fallback. Final reports must include "
            "task_id/state/message/message_en/message_zh/changed_files/validation/risks/acceptance_check/"
            "review_decision/review_reason/risk_level; acceptance_check maps each Goal Packet criterion "
            "to passed/failed/partial/not_checked with evidence.\n\n"
            "Every report MUST include a non-empty `call_id`. Use a stable, per-logical-report "
            f"call_id that includes a cycle/round counter (e.g. `{task.id}-{{state}}-{{n}}` where "
            "n increments for each new logical report) so repeated reports in the same state "
            "(e.g. multiple working updates across review rounds) do not collide. If you "
            "resubmit the SAME report after a failure or context reload, reuse the SAME call_id "
            "so the Hub deduplicates it. Different reports (e.g. a working update vs. the final "
            "report) use different call_ids.\n\n"
            "Report endpoint (POST JSON for other states):\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"started",'
            f'"call_id":"{started_call_id}",'
            '"message":"Started","message_en":"Started","message_zh":"已开始"}\'\n\n'
            "Call-id ACK contract (at-least-once delivery to your tmux inbox):\n"
            "Messages from your supervisor may be prefixed with a `[call_id:<id>]` marker "
            "(followups, continue prompts, etc.). The Hub delivers each call_id to your "
            "tmux input buffer. The Hub provides durable dedupe: within a single tmux "
            "session lifetime a call_id is pasted at most once (tmux-server receipt), "
            "and a call_id is never re-sent after you ACK it. If a previous tmux send "
            "failed ambiguously, the call_id stays in an `uncertain` state and the "
            "system does NOT auto-resend it — only an explicit operator retry may "
            "re-deliver it. You do NOT need to keep a scratch-file list of processed "
            "call_ids; just ACK every call_id you process by listing it in "
            "`acked_call_ids` of your report.\n"
            f"- The dispatch call_id `dispatch:{task.id}:{{attempt}}` is ACKed automatically by the Hub "
            "when you submit any report; do NOT list it in acked_call_ids.\n"
            "- For every other call_id you have processed, list it in `acked_call_ids` "
            "of your report. Only call_ids currently pending for this task/session are "
            "moved to delivered; unknown or future call_ids are ignored.\n"
            "- A call_id you do NOT list stays in processing and will be cleaned up only "
            "after you ACK it. List every call_id you process so the Hub can release it.\n"
            "Example report body with ACKs:\n"
            f'{{"task_id":"{task.id}","state":"working","call_id":"{progress_call_id}",'
            '"message":"...","message_en":"...",'
            '"message_zh":"...","acked_call_ids":["followup-abc123"]}\n'
        )

    def _build_subagent_assignment_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
        *,
        lesson_context: list[dict[str, Any]] | None = None,
    ) -> str:
        """Minimal assignment prompt for subagent mode.

        Subagent tasks are delegated by another agent that handles orchestration
        and result judgment. The worker only needs to execute and report — no
        Goal Packet, no bilingual messages, no review_decision.
        """
        clear_note = (
            "This task is unrelated to prior work. Treat prior conversation context as stale.\n\n"
            if task.clear_context
            else ""
        )
        failure_note = (
            f"This task previously failed. Previous failure reason: {task.failure_reason}\n\n"
            if task.failure_reason
            else ""
        )
        attachment_note = (
            f"{self._attachment_prompt_block(task.attachments)}\n\n" if task.attachments else ""
        )
        lesson_context_block = self._lesson_context_block_from_payload(
            (
                lesson_context
                if lesson_context is not None
                else self._lesson_context_payload(workspace, f"{task.title}\n{task.prompt}")
            ),
            workspace_id=workspace.id,
        )
        assignment_attempt = max(task.dispatch_attempt, 1)
        started_call_id = self._report_prompt_call_id(
            task.id, "started", attempt=assignment_attempt
        )
        completed_call_id = self._report_prompt_call_id(
            task.id, "completed", attempt=assignment_attempt
        )
        return (
            "New workspace task assigned (subagent mode).\n\n"
            f"Workspace: {workspace.name}\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"{self._session_environment_lines(workspace, session)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            f"{clear_note}"
            f"{failure_note}"
            f"Task description:\n{task.prompt}\n\n"
            f"{attachment_note}"
            f"{lesson_context_block}"
            "You are a sub-agent executing a task delegated by another agent.\n"
            "The caller owns acceptance and any required review; no separate Goal Packet or bilingual "
            "messages are required here. Work only within the delegated inputs, scope and ownership. "
            "Stop and report missing inputs, an ownership conflict, exhausted budget, or a step beyond "
            "the stated stop_condition. Do not silently broaden the task.\n\n"
            f"{self._task_context_contract_block()}"
            "Start by reading the state snapshot; use the task description to choose the correct "
            "project directory. Check for uncommitted changes before editing.\n\n"
            "Report states: started -> working (as you progress) -> completed when done.\n"
            "If you cannot proceed (missing info, blocked dependency), report state=blocked or\n"
            "needs_input with a message explaining what you need. Your caller will send a\n"
            "followup message; process it and include its call_id in acked_call_ids of your\n"
            "next report.\n\n"
            "Every report MUST include a non-empty call_id. Use a stable, per-logical-report "
            f"call_id (e.g. `{task.id}-{{state}}-{{n}}` where n increments for each new report). "
            "Reuse the SAME call_id if resubmitting after a failure so the Hub deduplicates.\n\n"
            "Minimal report fields: task_id, state, call_id, message, changed_files. On completion, "
            "include validation (cwd, base/head, commands and results or why not run), evidence paths, "
            "risks/unverified criteria, and the next action for your caller.\n\n"
            "Started report example:\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"started",'
            f'"call_id":"{started_call_id}",'
            '"message":"Started"}\'\n\n'
            "Completed report example:\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"completed",'
            f'"call_id":"{completed_call_id}",'
            '"message":"summary and next action","changed_files":["path/to/file"],'
            '"validation":"cwd; base/head; command => result; evidence path",'
            '"risks":"unverified criteria or none"}\'\n\n'
            "Call-id ACK contract: messages from your supervisor may be prefixed with a "
            "`[call_id:<id>]` marker. ACK every call_id you process by listing it in "
            "`acked_call_ids` of your next report. The dispatch call_id "
            f"`dispatch:{task.id}:{{attempt}}` is ACKed automatically; do NOT list it.\n"
        )

    def _lesson_context_payload(self, workspace: Workspace, query: str) -> list[dict[str, Any]]:
        return self._feedback_store().lesson_context_payload(
            workspace.id,
            query,
        )

    def _lesson_context_block(self, workspace: Workspace, query: str) -> str:
        return self._lesson_context_block_from_payload(
            self._lesson_context_payload(workspace, query),
            workspace_id=workspace.id,
        )

    def _lesson_context_block_from_payload(
        self,
        lessons: list[dict[str, Any]],
        *,
        workspace_id: str | None = None,
    ) -> str:
        if not lessons:
            return (
                "Workspace lessons: none active for this workspace. "
                "State 'no lessons needed' in your report risks field.\n\n"
            )
        lines: list[str] = []
        lines.append("Relevant lessons (id | title | tags | conf):")
        for lesson in lessons:
            tags = ",".join(lesson.get("tags", [])[:4]) or "—"
            conf = lesson.get("confidence")
            conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
            title = lesson["title"]
            if len(title) > 50:
                title = title[:47] + "..."
            lines.append(f"- `{lesson['id']}` | {title} | [{tags}] c={conf_str}")
        if workspace_id:
            lines.append(f"Full detail: GET /api/workspaces/{workspace_id}/lessons/<id>")
        lines.append("Apply only relevant lessons; list IDs used (or 'none') in report risks.")
        lines.append("")
        return "\n".join(lines)

    def _record_feedback_lesson_injection(
        self,
        *,
        task: WorkspaceTask,
        session: ManagedSession,
        lesson_ids: list[str],
        prompt_kind: str,
        created_at: datetime,
    ) -> None:
        lesson_list = ", ".join(lesson_ids)
        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=session.id,
            state=AgentReportState.WORKING,
            message=f"Feedback lessons injected into {prompt_kind} prompt: {lesson_list}",
            message_en=f"Feedback lessons injected into {prompt_kind} prompt: {lesson_list}",
            message_zh=f"已将 feedback lessons 注入 {prompt_kind} prompt：{lesson_list}",
            changed_files=[],
            validation=f"prompt_kind={prompt_kind}; feedback_lesson_ids=" + json.dumps(lesson_ids),
            risks=None,
            review_decision=ReviewDecision.SKIP,
            review_reason="System audit event for prompt-time feedback lesson injection.",
            risk_level="system_audit",
            review_cycle=task.review_cycle,
            created_at=created_at,
        )
        self.reports[report.id] = report

    def _record_system_task_audit(
        self,
        *,
        task: WorkspaceTask,
        message: str,
        message_zh: str,
        validation: str,
        session_id: str = "system",
        state: AgentReportState = AgentReportState.WORKING,
    ) -> None:
        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=session_id,
            state=state,
            message=message,
            message_en=message,
            message_zh=message_zh,
            changed_files=[],
            validation=validation,
            risks=None,
            review_decision=ReviewDecision.SKIP,
            review_reason="System audit event for an internal workspace task.",
            risk_level="system_audit",
            review_cycle=task.review_cycle,
            created_at=_wm._now(),
        )
        self.reports[report.id] = report

    def _task_context_contract_block(self) -> str:
        return (
            "Context is a bounded cache: the snapshot is a generated navigation aid, not the source "
            "of truth. Confirm current Task/report records and Git status/base/head before acting; "
            "read only relevant files and evidence. Resolve conflicts against those records, not old "
            "conversation summaries, and never edit generated state to change task status.\n\n"
        )

    def _execution_complexity_assignment_block(self, task: WorkspaceTask) -> str:
        if task.execution_complexity == WorkspaceTaskExecutionComplexity.SIMPLE:
            guidance = "Small task. Execute directly; delegate only a bounded need that improves the outcome."
        elif task.execution_complexity == WorkspaceTaskExecutionComplexity.COMPLEX:
            guidance = (
                "Complex task. Act as orchestrator: map dependencies, delegate independent bounded work "
                "when useful, and integrate and validate the result. Keep tightly coupled changes serial."
            )
        else:
            guidance = (
                "Auto: choose simple or complex before implementation and record the strategy and reason "
                "in goal_packet.assumptions and the first working report."
            )
        return (
            "Execution complexity guidance:\n"
            f"{EXECUTION_POLICY}"
            f"- Selected complexity: {task.execution_complexity.value}\n"
            f"- {guidance}\n"
            "- Delegation must justify its coordination cost through independent work, context isolation, "
            "or specialist evidence. There is no minimum agent count. Use one writer per owned scope; "
            "parallel writers need disjoint files and isolated worktrees/resources.\n\n"
        )

    def _subagent_capability_hint(self, agent_type: AgentType) -> str:
        """Use only capabilities exposed by the running CLI, without guessed flags."""
        if agent_type == AgentType.TERMINAL:
            return (
                "Terminal runtime has no native sub-agent capability. Degrade to direct execution and "
                "record the limitation; the Hub evaluator remains independent. Do NOT fabricate agents.\n"
            )
        return (
            f"{agent_type.value} runtime: use only the sub-agent tools actually available in this session. "
            "If delegation is unsupported in this runtime/version, execute serially and record the "
            "limitation in workflow.notes. Do not invent tool names, CLI flags, or agent/model claims.\n"
        )

    def _model_evidence_contract_block(self, agent_type: AgentType) -> str:
        """Model selection is a user/runtime decision, not a prompt-enforced tier."""
        return (
            f"Model/API evidence ({agent_type.value}): respect explicit user model choices; otherwise "
            "use the configured runtime default. Record the actual model/API when exposed, or "
            "model_or_api=runtime-default / unsupported:<reason>; external calls use external:<api>. "
            "Do not infer a model from role names or require a fixed provider/tier.\n\n"
        )

    def _autonomous_assignment_block(
        self,
        task: WorkspaceTask,
        agent_type: AgentType = AgentType.CLAUDE,
    ) -> str:
        if task.task_mode != WorkspaceTaskMode.AUTONOMOUS:
            return ""
        policy = task.autonomy_policy or AutonomyPolicy()
        run = task.autonomous_run
        header = (
            "Autonomous Mode V1 is enabled for this task.\n"
            f"- Max iterations: {policy.max_iterations}; strictness: {policy.evaluation_strictness.value}; "
            f"web research: {policy.allow_web_research}; artifact review: {policy.require_artifact_review}; "
            f"human checkpoints: {policy.human_checkpoint_policy.value}.\n"
            f"- Current phase: {run.phase.value if run else 'intake'}.\n"
            "- Do not self-pass; evaluator routing is mandatory. Include concrete artifacts/changed_files/"
            "validation/risks/acceptance_check. On revision address ONLY blocking issues and preserve passing work.\n\n"
        )
        contract = self._orchestrator_contract_block(task, agent_type)
        return header + contract

    def _orchestrator_contract_block(
        self,
        task: WorkspaceTask,
        agent_type: AgentType,
    ) -> str:
        complexity = task.execution_complexity
        if complexity == WorkspaceTaskExecutionComplexity.SIMPLE:
            strategy = (
                "Execution (simple): implement and run mechanical checks directly. A native P-JUDGE "
                "sub-agent is optional; the independent Hub evaluator is mandatory.\n"
            )
        elif complexity == WorkspaceTaskExecutionComplexity.COMPLEX:
            strategy = (
                "Execution (complex): declare bounded roles and dependency edges in workflow.notes. "
                "Delegate where isolation helps; record why tightly coupled work stays serial.\n"
            )
        else:
            strategy = (
                "Execution (auto): follow the simple/complex strategy recorded in goal_packet.assumptions; "
                "explain any change before expanding the workflow.\n"
            )
        if complexity == WorkspaceTaskExecutionComplexity.SIMPLE:
            return (
                "## Orchestrator Contract (Auto Mode)\n\n"
                f"{strategy}"
                f"{self._subagent_capability_hint(agent_type)}"
                f"{self._model_evidence_contract_block(agent_type)}"
                "If delegation becomes useful, give each delegate an owner, inputs/base/head, "
                "allowed paths and read/write scope, budget, stop_condition and evidence handoff. "
                "Keep one writer per scope and research/review read-only. Record only actual "
                "delegations in a subagent-ledger; otherwise report your commands/results directly. "
                "Final evidence includes cwd, base/head, artifacts, unverified criteria and risks. "
                "Post progress during long work; name any blocker and the next action.\n\n"
            )
        return (
            "## Orchestrator Contract (Auto Mode)\n\n"
            f"{strategy}"
            "The owner integrates outputs and validates the final result. Role primitives describe work, "
            "not required agent counts: P-PLAN (scope/dependencies), P-EXECUTE (artifact), "
            "P-VALIDATE (mechanical checks), P-JUDGE (independent critique), P-INTEGRATE (integration), "
            "P-RESEARCH (evidence). Run objective checks when available; self-review does not replace "
            "the Hub evaluator.\n\n"
            f"{self._subagent_capability_hint(agent_type)}"
            f"{self._model_evidence_contract_block(agent_type)}"
            "Subtask envelope (EVERY delegation):\n"
            "  [subtask-envelope] role.id / primitive / owner / objective / success_criteria / "
            "inputs (task/report IDs, files, base/head, evidence refs) / depends_on / scope "
            "(allowed_paths, read-only or writer, worktree and resource ownership) / tools_allowed / "
            "budget / stop_condition / output_schema / return_mode: final-only.\n"
            "Research/review delegates are read-only. Stop and report when ownership conflicts, inputs "
            "are missing, budget is exhausted, or the next step exceeds scope; do not silently expand.\n\n"
            "Evidence handoff: result, changed files/artifacts and head SHA, commands with cwd and outcomes, "
            "unverified criteria, risks, and next action. The owner checks evidence before accepting it.\n"
            "Subagent ledger (only for actual delegations, in review-gate validation):\n"
            "  subagent-ledger: role.id / primitive / owner / agent / model_or_api / "
            "decision=<accepted|rejected|retried> / evidence=<paths, commands, outcomes>.\n"
            "For direct execution, report checks and explain the strategy; do not fabricate a ledger.\n\n"
            "Observability: for work taking more than a few minutes, post a working heartbeat with "
            "owner, elapsed time, last evidence and next action. Use blocked/needs_input only when no "
            "autonomous next action remains; name the blocker with evidence. Bare 'needs your response' "
            "is a contract violation.\n\n"
        )

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

    # ---- Tiered report serialization (keeps reviewer prompt bounded) ----

    # Max chars for a verbose field in a summarized (non-trigger, non-latest-verdict) report.
    _SUMMARY_VERBOSE_FIELD_MAX = 240
    # How many recent reports to include verbatim (trigger + latest verdicts/worker outputs).
    _FULL_REPORT_WINDOW = 4
    # Max history depth (full + summarized) even for long-running tasks.
    _MAX_REPORT_HISTORY = 12

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
        review_attempt = max(task.review_attempts, 1)
        review_started_call_id = self._report_prompt_call_id(
            task.id, "review-started", attempt=review_attempt
        )
        review_passed_call_id = self._report_prompt_call_id(
            task.id, "review-passed", attempt=review_attempt
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
            "Report workflow: first POST review_started, then exactly one final verdict "
            "(use a stable call_id per report; reuse the same call_id on retry; include a "
            "round counter so repeated reviews don't collide):\n"
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

    # ---- Revision-resume briefing (used after hard recovery on iteration>=2) ----

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
