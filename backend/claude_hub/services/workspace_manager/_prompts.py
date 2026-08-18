"""Bootstrap, assignment, review, and continue prompt builders."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

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
            # Find the resident's root run so the bootstrap prompt includes its
            # id and persisted ACK cursor. The root run is created before the
            # session in _run_resident_agent.
            root_run_id = None
            ack_sequence = 0
            for run in self.agent_tree._runs.values():
                if run.workspace_id == workspace.id and run.parent_id is None:
                    root_run_id = run.id
                    ack_sequence = run.ack_sequence
                    break
            return _wm.build_resident_agent_prompt(
                workspace,
                self._report_base_url(session),
                session.id,
                root_run_id,
                ack_sequence,
            )
        return self._build_workspace_agent_prompt(workspace, session)

    def _report_base_url(self, session: ManagedSession) -> str:
        if session.remote_forward_port:
            return f"http://127.0.0.1:{session.remote_forward_port}"
        return f"http://localhost:{settings.port}"

    def _report_endpoint_curl(self, session: ManagedSession, task_id: str | None = None) -> str:
        """Render the report-endpoint curl example for a session.

        The report endpoint otherwise only appears in the bootstrap/assignment/
        review prompts. Any follow-up message that asks an agent to report after
        its context may have been cleared must restate this endpoint, or a
        cleared agent has no curl target to POST to.
        """
        task_field = task_id if task_id is not None else "TASK_ID"
        return (
            "Report endpoint:\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}"
            f"/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task_field}","state":"working","message":"Progress update",'
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
            "Report endpoint (POST JSON for assigned tasks):\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            '-d \'{"task_id":"TASK_ID","state":"working","message":"Progress update",'
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
            "- Use review_failed when the impl agent can fix concrete defects; review_needs_input only for "
            "genuine product/credential/environment blockers you cannot infer.\n\n"
            "Report endpoint (task_id supplied with each assignment):\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            '-d \'{"task_id":"TASK_ID","state":"review_started",'
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
        clear_note = (
            "This task is unrelated to prior work. Treat prior conversation context as stale.\n\n"
            if task.clear_context
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
            f"Task description:\n{task.prompt}\n\n"
            f"{attachment_note}"
            f"{lesson_context_block}"
            f"{self._execution_complexity_assignment_block(task)}"
            f"{self._autonomous_assignment_block(task, session.agent_type)}"
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
            '"message":"Goal Packet; awaiting approval.","message_en":"Goal Packet; awaiting approval.",'
            '"message_zh":"目标包已创建，等待审核。","goal_packet":{'
            '"objective":"...","acceptance_criteria":["..."],"validation_plan":["..."],'
            '"assumptions":["..."],"out_of_scope":["..."],"handoff_requirements":["..."]}}\'\n\n'
            "Every report must include message_en (concise English) and message_zh (concise 中文); keep "
            "the legacy `message` field as a short English fallback. Final reports must include "
            "task_id/state/message/message_en/message_zh/changed_files/validation/risks/acceptance_check/"
            "review_decision/review_reason/risk_level; acceptance_check maps each Goal Packet criterion "
            "to passed/failed/partial/not_checked with evidence.\n\n"
            "Report endpoint (POST JSON for other states):\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"started",'
            '"message":"Started","message_en":"Started","message_zh":"已开始"}}\''
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

    def _execution_complexity_assignment_block(self, task: WorkspaceTask) -> str:
        if task.execution_complexity == WorkspaceTaskExecutionComplexity.SIMPLE:
            guidance = "Small task. Execute directly, keep the plan compact; spawn subagents only for a concrete blocker."
        elif task.execution_complexity == WorkspaceTaskExecutionComplexity.COMPLEX:
            guidance = (
                "Complex task. Act as orchestrator: decompose, delegate bounded subtasks to subagents "
                "(implement/test/research/review), keep scopes explicit, and personally integrate+validate "
                "before reporting completion."
            )
        else:
            guidance = (
                "Auto: before implementation judge simple vs complex and state your choice in the first working "
                "report. If complex, orchestrate and delegate; if simple, execute directly."
            )
        cost_guard = (
            "Orchestrator mode is expensive (10-15x token cost of a linear agent). Use it ONLY when "
            "(1) breadth-first parallel across >=3 independent threads, "
            "(2) a single context cannot hold the material, or "
            "(3) subtasks are cleanly isolated so a sub-agent mistake will not pollute the main thread. "
            "Otherwise prefer a single linear agent."
        )
        return (
            "Execution complexity guidance:\n"
            f"- Selected complexity: {task.execution_complexity.value}\n"
            f"- {guidance}\n"
            f"- {cost_guard}\n\n"
        )

    def _subagent_capability_hint(self, agent_type: AgentType) -> str:
        """Per-CLI sub-agent invocation hints for the orchestrator contract."""
        if agent_type == AgentType.CLAUDE:
            return (
                "claude runtime: use the Task tool with subagent_type set to a built-in or repo-shipped "
                "agent (general-purpose, Explore, Plan, code-reviewer, .claude/agents/*.md). Pass model "
                'explicitly per the pinning below. Example: Task(subagent_type="general-purpose", '
                'model="opus", description="<role.id>", prompt="<envelope>").\n'
            )
        if agent_type == AgentType.CURSOR:
            return (
                "cursor runtime: use cursor's native sub-agent/spawn capability (YOLO on by default). "
                "If per-role model pinning is unsupported in your version, run the parent at the highest "
                "available tier and note the limitation in workflow.notes.\n"
            )
        if agent_type == AgentType.CODEX:
            return (
                "codex runtime: use codex's subtask/fan-out capability for bounded delegations. "
                "If per-role model pinning is unsupported, note it in workflow.notes.\n"
            )
        return (
            "This runtime has no native sub-agent capability. Degrade to single-agent execution and "
            "record the degradation in Goal Packet assumptions. Do NOT fabricate a subagent ledger.\n"
        )

    def _model_evidence_contract_block(self, agent_type: AgentType) -> str:
        """Runtime-aware model/API evidence rules for autonomous subtask ledgers."""
        if agent_type == AgentType.CLAUDE:
            return (
                "Primitive -> Model pinning (claude; users CANNOT override):\n"
                "  P-PLAN/P-EXECUTE/P-JUDGE/P-INTEGRATE -> opus;  P-VALIDATE/P-RESEARCH -> sonnet.\n"
                "  P-EXECUTE that only calls an external API (image-gen, TTS, ...) records "
                "model_or_api=external:<api-name> instead of an LLM model.\n\n"
            )
        if agent_type in {AgentType.CURSOR, AgentType.CODEX}:
            return (
                f"Primitive -> Model evidence ({agent_type.value}): Claude opus/sonnet pinning is NOT "
                "required. Record the actual model/tier used in `model_or_api`, or `runtime-default` / "
                "`unsupported:<short-reason>` with a note in workflow.notes. External-API P-EXECUTE "
                "records model_or_api=external:<api-name>.\n\n"
            )
        return (
            "Terminal runtime: no sub-agent model pinning. Record `model_or_api=runtime-default` and "
            "explain single-agent degradation in assumptions/workflow.notes. Do NOT claim Claude "
            "opus/sonnet pinning.\n\n"
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
            enforcement = (
                "Enforcement (simple): execute directly, but you MUST spawn one P-JUDGE sub-agent for an "
                "independent pre-flight review before posting the review-gate report.\n"
            )
        elif complexity == WorkspaceTaskExecutionComplexity.COMPLEX:
            enforcement = (
                "Enforcement (complex): orchestrator mode REQUIRED. Workflow MUST include at least one "
                "P-EXECUTE and one P-JUDGE dispatch. Posting a review-gate report without a complete "
                "subagent ledger is a contract violation.\n"
            )
        else:  # AUTO
            enforcement = (
                "Enforcement (auto): declare orchestrator vs single-agent mode in your first working "
                "report and justify in goal_packet.assumptions. If orchestrator, the contract below is "
                "mandatory; if single-agent, still spawn one P-JUDGE before review-gate.\n"
            )

        capability_hint = self._subagent_capability_hint(agent_type)
        model_evidence = self._model_evidence_contract_block(agent_type)

        return (
            "## Orchestrator Contract (Auto Mode)\n\n"
            "You are the orchestrator and the only voice the user hears for this task. Do NOT do bulk "
            "execution, validation, or judging in your own context. Decompose into bounded subtasks and "
            "delegate via your runtime's native sub-agent capability.\n\n"
            f"{capability_hint}\n"
            "Role primitives: P-PLAN (decompose/spec), P-EXECUTE (produce artifact), P-VALIDATE (mechanical "
            "checks: tests/lint/schema/hashes), P-JUDGE (qualitative critique vs acceptance), "
            "P-INTEGRATE (combine outputs into deliverable), P-RESEARCH (external knowledge).\n\n"
            f"{model_evidence}"
            "In your first working report declare a `workflow:` block: concrete roles (from primitives above), "
            "dependency edges, and `notes:` justifying the schema. Any non-trivial workflow MUST contain "
            ">=1 P-EXECUTE and >=1 P-JUDGE; P-VALIDATE is required when an objective check exists. "
            "P-VALIDATE and P-JUDGE are SEPARATE -- do not fold either into your own context.\n\n"
            "Observability: for any sub-agent/API/validation step that runs >few minutes, post a working "
            "heartbeat (role.id, primitive, elapsed, last artifact, next action) before/during the wait. "
            "Do NOT post blocked/needs_input while an autonomous step is still running; those are only "
            "allowed when no autonomous next action remains and must name the blocker with evidence. Bare "
            '"needs your response" is a contract violation.\n\n'
            "Subtask envelope (use for EVERY dispatch, regardless of runtime):\n"
            "  [subtask-envelope]\n"
            "  role.id / primitive / objective (one-sentence contract) / success_criteria (maps to "
            "goal_packet.acceptance) / inputs (files/links/artifacts) / output_schema (patch/prompt/URI/"
            "report/...) / tools_allowed (whitelist) / context_budget (token/step budget) / "
            "return_mode: final-only (default; full-transcript only when auditing).\n\n"
            "Subagent ledger (REQUIRED in validation on review-gate):\n"
            "  subagent-ledger:\n"
            "    - role.id=<id> primitive=<P-*> agent=<runtime:tool#kind> "
            "model_or_api=<opus|sonnet|actual|runtime-default|unsupported:reason|external:api>\n"
            "      goal=<...> decision=<accepted|rejected|retried> evidence=<paths/uris/test-names>\n"
            "    - ...\n\n"
            f"{enforcement}\n"
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
            "Report workflow: first POST review_started, then exactly one final verdict:\n"
            f"{INTERNAL_API_CURL} -X POST {self._report_base_url(reviewer)}/api/workspaces/sessions/{reviewer.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"review_passed",'
            '"message":"Verdict + summary + acceptance rollup + notes",'
            '"message_en":"Verdict + summary + acceptance rollup + notes",'
            '"message_zh":"结论 + 摘要 + 验收汇总 + 备注",'
            '"validation":"Checks reviewed","risks":"Residual risk or none",'
            '"review_profiles":["general"],"profile_results":[{"profile":"general",'
            '"status":"passed","evidence":"Evidence reviewed.","blocking_findings":[],'
            '"non_blocking_findings":[]}],"artifact_refs":[],"confidence":0.8,'
            '"requires_human_judgment":false}}\'\n\n'
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
                "- Inspect changed files and related code paths enough to verify correctness and scope.\n"
                "- Adversarial defect hunt (BEFORE the verdict): actively try to break the change by "
                "enumerating failure modes and checking each against the actual code: edge/boundary "
                "inputs; error/exception paths; concurrency/ordering/shared-state races; regressions to "
                "existing flows/persistence/migrations; scope leakage/side effects; security/permission "
                "assumptions. Treat anything you cannot rule out by reading code as a candidate defect, not fine.\n"
                "- Evaluate validation evidence: independently spot-check highest-risk claims rather than "
                "accepting them at face value; decide whether missing tests/checks are acceptable or blocking.\n"
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
            "- Verify the implementation strategy matched the selected complexity. "
            "For simple tasks, unnecessary delegation and process overhead are scope risks. "
            "For complex tasks, lack of decomposition, delegated specialist work where available, "
            "or missing integrator-level validation can be blocking. For auto tasks, verify the "
            "agent explicitly chose and followed a simple or complex strategy.\n\n"
        )

    def _autonomous_review_block(self, task: WorkspaceTask) -> str:
        if task.task_mode != WorkspaceTaskMode.AUTONOMOUS:
            return ""
        policy = task.autonomy_policy or AutonomyPolicy()
        run = task.autonomous_run
        if task.agent_type == AgentType.CLAUDE:
            model_verification = (
                "- Verify model pinning (claude): P-PLAN/P-EXECUTE/P-JUDGE/P-INTEGRATE must be opus; "
                "P-VALIDATE/P-RESEARCH may be sonnet. External-API P-EXECUTE records "
                "model_or_api=external:<api>. Wrong-tier model on a key primitive is a contract violation.\n"
            )
        elif task.agent_type in {AgentType.CURSOR, AgentType.CODEX}:
            model_verification = (
                f"- Verify model/API evidence for {task.agent_type.value}. Do NOT fail solely because "
                "Claude opus/sonnet pinning is absent (this runtime may not expose it). Accept "
                "model_or_api=runtime-default, unsupported:<reason>, an actual runtime model name, or "
                "external:<api> when workflow.notes explains. Treat missing evidence as a ledger quality "
                "issue, not as a wrong-tier violation.\n"
            )
        else:
            model_verification = (
                "- Verify terminal-runtime degradation honestly records direct execution or "
                "model_or_api=runtime-default. Do NOT require Claude pinning for plain terminal, and "
                "do not accept fabricated sub-agent/model claims.\n"
            )
        return (
            "Autonomous evaluation context:\n"
            f"- Run: {run.model_dump_json() if run else 'null'}; worker runtime: {task.agent_type.value}\n"
            f"- Max iterations: {policy.max_iterations}; strictness: {policy.evaluation_strictness.value}; "
            f"artifact review: {policy.require_artifact_review}.\n"
            "Act as the evaluator for this iteration. Score against the Goal Packet, rubric/run evidence, "
            "validation, artifacts, and prior evaluation history. Use review_passed when the run should "
            "move to passed (awaiting human acceptance), review_failed when targeted revision is possible "
            "within budget, review_needs_input when product judgment/credentials/unavailable artifacts/unsafe "
            "scope prevents evaluation.\n\n"
            "Subagent ledger verification (orchestrator contract):\n"
            "- Complex autonomous tasks MUST embed a `subagent-ledger:` section in the review-gate "
            "validation field. Missing/empty ledger on a complex task is a contract violation "
            "(review_failed with a blocking issue).\n"
            "- Each ledger entry must carry role.id, primitive (P-*), agent, model_or_api, decision, evidence.\n"
            f"{model_verification}"
            "- Verify workflow.roles from the first working report matches the ledger; at least one "
            "P-EXECUTE and one P-JUDGE actually ran; P-VALIDATE is present when objective checks exist.\n\n"
        )

    def _build_continue_prompt(
        self,
        task: WorkspaceTask,
        payload: ContinueTaskRequest,
        session: ManagedSession,
    ) -> str:
        message = payload.message.strip() if payload.message else ""
        attachments = self._persist_attachments(
            task.workspace_id,
            f"{task.id}-continue-{uuid.uuid4().hex[:8]}",
            payload.attachments,
        )
        follow_up = self._append_attachment_block(
            message or "Continue addressing the review feedback.",
            attachments,
        )
        return (
            "Continue workspace task from review.\n\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Follow-up instructions:\n{follow_up}\n\n"
            f"{self._autonomous_continue_orchestrator_reminder(task)}"
            "The task is back in working state. Report progress with the same task_id.\n\n"
            f"{self._report_endpoint_curl(session, task.id)}"
        )

    def _autonomous_continue_orchestrator_reminder(self, task: WorkspaceTask) -> str:
        if task.task_mode != WorkspaceTaskMode.AUTONOMOUS:
            return ""
        return (
            "Orchestrator-mode reminder: stay in orchestrator mode for this revision. Address the "
            "evaluator's blocking issues by dispatching new sub-agent subtasks (P-EXECUTE for fixes, "
            "P-VALIDATE for re-tests, P-JUDGE for re-review) rather than folding fixes into your own "
            "context. Append new ledger entries to your existing subagent ledger; do not restart it. "
            "If your own context feels decayed (confused about earlier decisions, contradictory "
            "instructions), prefer a fresh sub-agent rather than reasoning in the main thread.\n\n"
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
            "Resume steps:\n"
            "1. Re-read the state snapshot and inspect the files listed above before editing.\n"
            "2. Stay in orchestrator mode; address ONLY the blocking issues above (or pick up "
            "from the last working state if no reviewer feedback exists). Append new entries to "
            "your subagent ledger rather than restarting it.\n"
            "3. If the task was already ready_for_review/completed before the error, repost that "
            "report immediately instead of redoing work.\n"
            "4. Report working/progress/blocked/completed with the same task_id.\n\n"
            f"{self._report_endpoint_curl(session, task.id)}"
        )

    def _build_hard_recovery_worker_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
        interruption_reason: str,
    ) -> str:
        """Prompt sent after hard recovery (interrupt + /clear) for a worker agent.

        The agent's context has been wiped by /clear but the CLI conversation id is preserved,
        so the agent can resume work without losing the session entirely.

        For autonomous tasks past the first iteration (iteration>=2 or review_cycle>=2), use
        the compact revision-resume briefing instead of replaying the full assignment prompt,
        to avoid repiling prompt text on a cleared context.
        """
        run = task.autonomous_run
        iteration = run.iteration if run else 0
        use_resume = task.task_mode == WorkspaceTaskMode.AUTONOMOUS and (
            iteration >= 2 or task.review_cycle >= 2
        )
        if use_resume:
            return self._build_revision_resume_prompt(
                workspace, task, session, interruption_reason=interruption_reason
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
            "Resume work now. Start by reading the state snapshot and checking the current state "
            "of any files you were editing. If the task was already complete (e.g., you already "
            "posted a ready_for_review report before the error), post a completed report immediately.\n\n"
            f"{self._report_endpoint_curl(session, task.id)}"
        )

    def _build_hard_recovery_reviewer_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
        trigger_report: AgentReport,
        interruption_reason: str,
    ) -> str:
        """Prompt sent after hard recovery (interrupt + /clear) for a reviewer agent."""
        report_payload = self._serialize_task_reports_for_review(task, trigger_report)
        agent_session_id = self._agent_session_id_for_session(session)
        session_line = f"Conversation ID: {agent_session_id}\n" if agent_session_id else ""
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
            "evidence, and issue review_passed, review_failed, or review_needs_input.\n\n"
            f"{self._report_endpoint_curl(session, task.id)}"
        )

    def _agent_session_id_for_session(self, session: ManagedSession) -> str | None:
        """Look up the CLI conversation id (agent_session_id) from ttyd_manager for a session."""
        try:
            tab = ttyd_manager.get_tab(session.tab_id)
            return tab.agent_session_id if tab else None
        except Exception:
            return None
