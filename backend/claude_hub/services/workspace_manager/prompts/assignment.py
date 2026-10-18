"""Prompt builders split from the compatibility mixin."""

from claude_hub.services.agent_execution_policy import EXECUTION_POLICY

from .._constants import *  # noqa: F401,F403
from .bootstrap import _BootstrapPromptsMixin


class _AssignmentPromptsMixin(_BootstrapPromptsMixin):
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
            created_at=self._runtime_now(),
        )
        self.reports[report.id] = report

    def _task_context_contract_block(self) -> str:
        return (
            "Context is a bounded cache: the snapshot is a generated navigation aid, not the source "
            "of truth. Confirm current Task/report records and Git status/base/head before acting; "
            "read only relevant files and evidence. Resolve conflicts against those records, not old "
            "conversation summaries, and never edit generated state to change task status.\n\n"
            "This is an already assigned workspace-controlled Task. Keep its Task ID, report through "
            "the managed report endpoint shown in this prompt, and do not switch to initiator progress "
            "or manual progress. Use Hub CLI/API controls rather than operating agent tmux sessions.\n\n"
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
