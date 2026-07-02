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
            return _wm.build_resident_agent_prompt(
                workspace, self._report_base_url(session), session.id
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
            f"curl -sS -X POST {self._report_base_url(session)}"
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
            "Stay in this terminal and wait for assigned tasks. Do not start unrelated work. "
            "This workspace is an environment, not necessarily a single repository. "
            "Do not inspect repositories, run git status, edit files, or report working until "
            "a task is explicitly assigned. Use each task to choose the correct project "
            "directory before editing. "
            "Before editing, read the state snapshot and check for local file changes. "
            "If another agent modified files you need, avoid overwriting them and ask for review. "
            "When you report completed, the workspace may assign an independent reviewer. "
            "If reviewer feedback is sent back to you, continue from that feedback and report "
            "completed again when the fixes are done. "
            "Final reports may include review_decision: auto, request, or skip. This only controls "
            "whether an independent AI reviewer is requested; every completed task still waits for "
            "human acceptance before it is done. Use request when independent reviewer checks are "
            "needed, skip only for no-change analysis, manual follow-up, or explicitly trivial "
            "low-risk changes that do not need AI reviewer checks, and include review_reason. "
            "Report progress to the workspace coordinator only after you receive a task, "
            "when you start, get blocked, need input, are ready for review, or complete the work. "
            "Every report should include both message_en (concise English) and message_zh "
            "(concise 中文) so the workspace UI can render either language; keep the legacy "
            "message field as a short fallback (English is fine).\n\n"
            "Report endpoint for assigned tasks:\n"
            f"curl -sS -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
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
            "You are an independent reviewer agent for this workspace.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Session: {session.id}\n"
            f"{self._session_environment_lines(workspace, session)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            "Wait for explicit review assignments. Do not implement, refactor, format, or edit files.\n\n"
            "Reviewer mindset (read first):\n"
            "- Your primary job is to FIND defects and risks, not to confirm success. A clean, "
            "confident, or well-written implementation report is not evidence that the code is correct.\n"
            "- Approval is the exception, not the default. Assume something is wrong until you have "
            "actively looked for it and failed to find it. Passing without having tried to break the "
            "change is a review failure.\n"
            "- Do not defer to the implementation agent. Its confidence, tone, and report polish carry "
            "no weight; only the actual code and observed state do. Disregard formatting and verbosity "
            "when judging quality — judge substance, not presentation.\n"
            "- It is correct and expected to fail a review or request changes when you find real "
            "blocking defects. Do not soften or wave through borderline issues to avoid friction.\n\n"
            "Reviewer operating contract:\n"
            "- Derive concrete acceptance criteria from the task description, user intent, "
            "recent task reports, changed files, and repository conventions.\n"
            "- Review against those criteria plus regression risk, integration fit, validation quality, "
            "and whether the implementation stayed within scope.\n"
            "- Treat reported validation as claims to verify, not proof. Independently inspect the code "
            "and state behind the highest-risk claims; do not accept self-reported validation at face "
            "value. If you cannot confirm a critical claim, treat it as unverified, not as passing.\n"
            "- Report review_started when you begin.\n"
            "- Finish by reporting exactly one of review_passed, review_failed, or review_needs_input.\n\n"
            "Review exit rules:\n"
            "- Use review_passed only after you have actively tried to find defects (edge cases, error "
            "paths, regressions, scope leakage) and failed to find any blocking one — and all acceptance "
            "criteria are met, validation is adequate for the risk, and residual risks are acceptable for "
            "final human acceptance. Do not pass merely because nothing obvious looked wrong.\n"
            "- Use review_failed when the implementation agent can fix concrete defects or missing checks. "
            "Include required fixes specific enough for the implementation agent to follow.\n"
            "- Use review_needs_input only when a product, credential, environment, or requirement decision "
            "is genuinely required before review can finish.\n\n"
            "Reporting style:\n"
            "- The message field must be a SHORT scannable summary so a human can read it at a glance. "
            "Do NOT dump every finding, validation log, or full criterion list into message. "
            "Put detailed evidence into the structured fields (validation, risks, acceptance_check, "
            "profile_results, artifact_refs) instead.\n"
            "- Every report must include both message_en (concise English) and message_zh (concise 中文); "
            "keep the legacy message field as a short fallback.\n\n"
            "Final review message body (keep each section to 1-3 short bullets, total under ~12 lines):\n"
            "Verdict: review_passed | review_failed | review_needs_input\n"
            "Summary: one or two sentences describing what was actually delivered.\n"
            'Acceptance criteria: rollup like "3/4 passed (1 partial: <criterion>)"; full per-criterion '
            "evidence belongs in the acceptance_check field.\n"
            "Required fixes: only for review_failed; the 1-3 highest-priority concrete fixes.\n"
            "Notes: at most one line for residual risk or follow-up; deeper detail goes in risks.\n\n"
            "Report endpoint for assigned reviews:\n"
            f"curl -sS -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
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
            f"curl -sS -X POST {self._report_base_url(dispatcher)}/api/workspaces/tasks/{task.id}/dispatch-decision "
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
            "Start by reading the state snapshot. This workspace may contain many projects; "
            "use the task description to choose the correct directory before editing. "
            "Check for uncommitted file changes. "
            "Before substantive implementation, derive a Goal Packet from the original task "
            "prompt and include it in your first working report. The Goal Packet must preserve "
            "the user's requested outcome, record assumptions instead of silently narrowing "
            "ambiguous scope, and include concrete reviewer-checkable acceptance criteria, "
            "a validation plan, out-of-scope boundaries, and final handoff requirements. "
            "For reviewed tasks, that first Goal Packet report is an approval gate: after posting "
            "it, stop and wait for reviewer approval or packet-change feedback. Do not begin "
            "substantive implementation until the backend continues the task after review_passed.\n\n"
            "Report state started, then report working as you make progress. "
            "If blocked or waiting for user input, report blocked or needs_input. "
            "When ready for human review, report ready_for_review. When you believe the task is "
            "fully complete, report completed. The task is not finally done until a human accepts it.\n\n"
            "For completed reports, decide reviewer routing explicitly:\n"
            "- review_decision=request when this should go to an independent AI reviewer before human acceptance.\n"
            "- review_decision=skip only for no-change analysis, manual follow-up, or "
            "explicitly trivial low-risk changes where AI reviewer checks are unnecessary; "
            "this still requires human acceptance.\n"
            "- review_decision=auto to use the workspace default reviewer policy.\n"
            "Always include review_reason when choosing request or skip. The backend may still "
            "force review for nontrivial changed files, failed review follow-ups, blocked input, "
            "runtime attention, or other higher-risk work.\n\n"
            "Report endpoint example:\n"
            f"curl -sS -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"started",'
            '"message":"Started task","message_en":"Started task","message_zh":"已开始任务"}\'\n\n'
            "Goal Packet report example:\n"
            f"curl -sS -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"working",'
            '"message":"Goal Packet created; waiting for approval.",'
            '"message_en":"Goal Packet created; waiting for approval.",'
            '"message_zh":"已创建目标包，等待审核。",'
            '"goal_packet":{"objective":"Concrete task objective in your words.",'
            '"acceptance_criteria":["Specific reviewer-checkable condition."],'
            '"validation_plan":["Command, manual check, or evidence source."],'
            '"assumptions":["Assumption made from ambiguity."],'
            '"out_of_scope":["Explicitly excluded work."],'
            '"handoff_requirements":["What final report must include."]}}\'\n\n'
            "Every report should include both message_en (concise English) and message_zh "
            "(concise 中文); keep the legacy message field as a short fallback. "
            "Final reports should include task_id, state, message, message_en, message_zh, "
            "changed_files, validation, risks, acceptance_check, review_decision, review_reason, "
            "and risk_level. acceptance_check should map each Goal Packet acceptance criterion "
            "to status passed, failed, partial, or not_checked with evidence.\n\n"
            f"{self._resident_integration_workflow_block(task)}"
        )

    def _resident_integration_workflow_block(self, task: "WorkspaceTask") -> str:
        """Extra workflow instructions injected only for resident-created tasks.

        Returns an empty string for human-created tasks so their assignment prompt
        is byte-stable (modulo unrelated formatting) and continues to follow the
        CLAUDE.md Mandatory Workflow default of branching from ``main``.

        For resident-created tasks (``origin == RESIDENT``), workers must branch
        from the local ``develop`` integration branch (if it exists) rather than
        directly from ``main``, integrate only into ``develop`` within the scope
        of the task, and keep all existing main-protection rules intact. The
        runtime check for ``develop`` is done by the worker (``git rev-parse
        --verify develop``) so the prompt stays deterministic regardless of
        which repository the workspace is rooted in.
        """
        if task.origin != WorkspaceTaskOrigin.RESIDENT:
            return ""
        return (
            "Resident integration workflow (this task was created by the resident agent):\n"
            "- This workspace uses a `develop` integration branch for resident-created "
            "work. Before creating your feature worktree, check whether a local "
            "branch named `develop` exists (`git rev-parse --verify develop`). "
            "If it does, create your isolated worktree/branch from `develop`: "
            "`git worktree add ../claude_hub-<slug> -b feat/<slug> develop`. "
            "If `develop` does not exist (e.g. on a fresh clone), fall back to "
            "cutting from `main` as documented in CLAUDE.md.\n"
            "- Treat `develop` as your integration target within the scope of this "
            "task. Do NOT merge into `main`, do NOT push to any remote, and do NOT "
            "use destructive git commands on shared branches (no `git reset --hard` "
            "on `main`/`develop`, no `git push --force`, no `git clean -fdx`, no "
            "deleting unmerged branches, no rebasing pushed branches).\n"
            "- All existing safety rules still apply: Goal Packet approval gate, "
            "explicit target_session_id (when dispatching subtasks under master "
            "mode), no auto-creating worker agents, and main merge/push still "
            "require explicit human approval in the task. Nothing in this block "
            "weakens those constraints — it only changes which long-lived branch "
            "you cut from and propose integration into.\n"
        )

    def _lesson_context_payload(self, workspace: Workspace, query: str) -> list[dict[str, Any]]:
        return self._feedback_store().lesson_context_payload(
            workspace.id,
            query,
            limit=20,
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
                "Workspace lessons index: no active lessons yet for this workspace.\n"
                "You do not need to reference any lessons in your report.\n\n"
            )
        lines: list[str] = []
        lines.append("Workspace lessons index (id, title, tags, confidence, hits, successes):")
        for lesson in lessons:
            tags = ", ".join(f"`{t}`" for t in lesson.get("tags", [])) or "—"
            conf = lesson.get("confidence")
            conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
            lines.append(
                f"- `{lesson['id']}` — {lesson['title']}  "
                f"[{tags}]  conf={conf_str}  "
                f"hits={lesson.get('hit_count', 0)}  "
                f"succ={lesson.get('success_count', 0)}"
            )
        lines.append("")
        lines.append(
            "Lessons catalog (human-readable): `docs/working-logs/lessons-catalog.md`  "
            "(read this file for the full do/avoid/applies_when detail of each lesson)."
        )
        lines.append(
            "To inspect a specific lesson, call `GET /api/workspaces/<workspace_id>/lessons/<lesson_id>` "
            "which returns the full lesson body (summary, do, avoid, applies_when, evidence)."
        )
        if workspace_id:
            lines.append(f"This workspace ID: `{workspace_id}`.")
        lines.append(
            "Read lessons only when you judge they may apply to this task — do not "
            "force-fit irrelevant lessons. In the final validation or risks field, "
            "list the IDs of any lessons you read (or state 'no lessons needed')."
        )
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
    ) -> None:
        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=session_id,
            state=AgentReportState.WORKING,
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
            guidance = (
                "Treat this as a small task. Execute directly in this session, keep the plan compact, "
                "and avoid spawning subagents unless you discover a concrete blocker that requires "
                "specialist help."
            )
        elif task.execution_complexity == WorkspaceTaskExecutionComplexity.COMPLEX:
            guidance = (
                "Treat this as a complex task. Act as the task orchestrator: decompose the work, "
                "delegate bounded implementation, testing, research, or review subtasks to subagents "
                "when your runtime supports them, keep ownership and write scopes explicit, and "
                "personally integrate, validate, and accept the final result before reporting completion."
            )
        else:
            guidance = (
                "Auto mode: before implementation, judge whether this task is simple or complex. "
                "State the chosen execution strategy in your first working report. If complex, "
                "orchestrate and delegate bounded subtasks where your runtime supports subagents; "
                "if simple, execute directly."
            )
        cost_guard = (
            "Treat orchestrator mode as expensive. Pick it only when at least one of these holds: "
            "(1) the work is breadth-first parallel across >=3 independent threads, "
            "(2) a single context window cannot hold the needed material, or "
            "(3) subtasks are cleanly isolated so a sub-agent's mistake will not pollute the "
            "main thread. Otherwise prefer a single linear agent."
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
                "Sub-agent invocation on claude runtime:\n"
                "- Use the Task tool with subagent_type set to a built-in or repo-shipped agent "
                "(general-purpose, Explore, Plan, code-reviewer, or any custom .claude/agents/*.md).\n"
                "- Pass model explicitly per the Primitive->Model pinning below; do NOT rely on inheritance.\n"
                '- Example: Task(subagent_type="general-purpose", model="opus", '
                'description="<role.id>", prompt="<envelope>").\n'
            )
        if agent_type == AgentType.CURSOR:
            return (
                "Sub-agent invocation on cursor runtime:\n"
                "- Use cursor's native sub-agent / spawn capability; YOLO is on by default.\n"
                "- Per-role model overrides on cursor sub-agents are version-dependent; if your "
                "version cannot pin a sub-agent model, run the parent at the highest available "
                "tier and document the limitation in the workflow.notes.\n"
            )
        if agent_type == AgentType.CODEX:
            return (
                "Sub-agent invocation on codex runtime:\n"
                "- Use codex's subtask / fan-out capability for bounded delegations.\n"
                "- Per-role model pinning is version-dependent; document the limitation in "
                "workflow.notes if unsupported.\n"
            )
        return (
            "Sub-agent invocation:\n"
            "- This runtime has no native sub-agent capability. Degrade gracefully to a single-agent "
            "execution and record the degradation in your Goal Packet assumptions. Do NOT fabricate "
            "a sub-agent ledger.\n"
        )

    def _model_evidence_contract_block(self, agent_type: AgentType) -> str:
        """Runtime-aware model/API evidence rules for autonomous subtask ledgers."""
        if agent_type == AgentType.CLAUDE:
            return (
                "Primitive -> Model pinning (claude runtime; users CANNOT override):\n"
                "  P-PLAN, P-EXECUTE, P-JUDGE, P-INTEGRATE -> opus\n"
                "  P-VALIDATE, P-RESEARCH                  -> sonnet\n"
                "  P-EXECUTE that calls an external API (image-gen, TTS, ...) records "
                "model_or_api=external:<api-name> instead of an LLM model.\n\n"
            )
        if agent_type in {AgentType.CURSOR, AgentType.CODEX}:
            return (
                f"Primitive -> Model/API evidence ({agent_type.value} runtime):\n"
                "- Use this runtime's native model routing and sub-agent controls. Claude opus/sonnet "
                "pinning is NOT required for this runtime.\n"
                "- If per-role model pinning is available, record the actual model or tier used in "
                "`model_or_api`.\n"
                "- If per-role model pinning is unavailable, record `model_or_api=runtime-default` "
                "or `model_or_api=unsupported:<short-reason>` and document the limitation in "
                "workflow.notes. This is acceptable when the rest of the ledger evidence is complete.\n"
                "- P-EXECUTE that calls an external API (image-gen, TTS, ...) records "
                "`model_or_api=external:<api-name>`.\n\n"
            )
        return (
            "Primitive -> Model/API evidence (terminal runtime):\n"
            "- This runtime has no native sub-agent model pinning. If you execute directly, record "
            "`model_or_api=runtime-default` and explain the single-agent degradation in assumptions "
            "or workflow.notes. Do NOT claim Claude opus/sonnet pinning.\n\n"
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
            f"- Max iterations: {policy.max_iterations}\n"
            f"- Evaluation strictness: {policy.evaluation_strictness.value}\n"
            f"- Allow web research: {policy.allow_web_research}\n"
            f"- Require artifact review: {policy.require_artifact_review}\n"
            f"- Human checkpoints: {policy.human_checkpoint_policy.value}\n"
            f"- Current autonomous phase: {run.phase.value if run else 'intake'}\n\n"
            "Worker rules for Autonomous Mode:\n"
            "- Do not decide final pass yourself; evaluator/reviewer routing is mandatory.\n"
            "- Include concrete artifacts, changed files, validation, risks, and acceptance_check evidence.\n"
            "- On revision, address only the evaluator's blocking issues and preserve passing work.\n\n"
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
                "Enforcement (simple): you may execute directly, but you MUST still spawn one "
                "P-JUDGE sub-agent to do an independent pre-flight review before posting the "
                "review-gate report.\n"
            )
        elif complexity == WorkspaceTaskExecutionComplexity.COMPLEX:
            enforcement = (
                "Enforcement (complex): orchestrator mode is REQUIRED. Your workflow MUST include "
                "at least one P-EXECUTE and one P-JUDGE sub-agent dispatch. Posting a review-gate "
                "report without a complete subagent ledger is a contract violation.\n"
            )
        else:  # AUTO
            enforcement = (
                "Enforcement (auto): in your first working report declare orchestrator vs single-agent "
                "mode and justify the choice in goal_packet.assumptions. If you pick orchestrator "
                "mode, the contract below is mandatory; if you pick single-agent, you must still "
                "spawn one P-JUDGE sub-agent before posting the review-gate report.\n"
            )

        capability_hint = self._subagent_capability_hint(agent_type)
        model_evidence = self._model_evidence_contract_block(agent_type)

        return (
            "## Orchestrator Contract (Auto Mode)\n\n"
            "You are the orchestrator and the only voice the user hears for this task. You must NOT "
            "do bulk execution, validation, or judging in your own context. Instead, decompose the "
            "task into bounded subtasks and delegate them to sub-agents using your runtime's native "
            "sub-agent capability.\n\n"
            f"{capability_hint}\n"
            "Role primitives (domain-agnostic responsibility shapes):\n"
            "  P-PLAN      decompose, decide subtask graph, hold the spec.\n"
            "  P-EXECUTE   produce the artifact (code, prompt, image, doc, query, ...).\n"
            "  P-VALIDATE  mechanical/objective check (tests, lint, schema, hashes).\n"
            "  P-JUDGE     qualitative critique vs acceptance (review, aesthetic judge, fact check).\n"
            "  P-INTEGRATE combine partial outputs into the final deliverable.\n"
            "  P-RESEARCH  fetch external knowledge / docs / references.\n\n"
            f"{model_evidence}"
            "In your first working report you MUST declare a `workflow:` block listing the concrete "
            "roles you allocated, the dependency edges between them, and a `notes:` line explaining "
            "why this schema fits the task. There is NO fixed enum of templates; compose roles freely "
            "from the primitives above. The compact example below is inspiration, not a template to "
            "copy verbatim; non-Claude runtimes record actual runtime model/API evidence or "
            "runtime-default in their ledger.\n\n"
            "Any non-trivial workflow MUST contain at least one P-EXECUTE and one P-JUDGE; "
            "P-VALIDATE is required when the task has any objectively-checkable success criterion. "
            "P-VALIDATE and P-JUDGE are SEPARATE primitives. Do NOT fold either into your own "
            "context.\n\n"
            "Orchestrator observability requirements:\n"
            "- For any delegated, remote, or external-API step that runs more than a few minutes or "
            "produces no immediate terminal output, post a working heartbeat before the wait and at "
            "each checkpoint, with role.id, primitive, elapsed time, last artifact/status, and next "
            "action. While a sub-agent, image/API job, or validation run is in progress, report "
            "working -- do NOT switch to needs_input or blocked just because a step is long-running.\n"
            "- A blocked or needs_input report is allowed only when no autonomous next action remains. "
            "It must name the blocker, include evidence for the blocker, list the next action already "
            "attempted or ruled out, and specify the exact user/product/environment decision required. "
            'Bare placeholders such as "needs your response" are contract violations.\n\n'
            "Compact skeleton (e.g. add a soft-delete endpoint to /api/orders, reject if shipped, "
            "cover with tests) -- shape only, not a template:\n"
            "  workflow:\n"
            "    roles:\n"
            "      - id: planner       primitive: P-PLAN      duty: decompose, list acceptance, blast radius\n"
            "      - id: implementer   primitive: P-EXECUTE   duty: make the change, scoped to the module\n"
            "      - id: tester        primitive: P-VALIDATE  duty: tests covering the success criteria\n"
            "      - id: reviewer      primitive: P-JUDGE     duty: independent review vs acceptance\n"
            "      - id: integrator    primitive: P-INTEGRATE duty: collect ledger, decide ready_for_review\n"
            "    deps: planner -> implementer -> tester -> reviewer -> integrator\n"
            "    notes: add P-RESEARCH for unknowns; add a judge->execute loop for iterative artifacts; "
            "use model: external:<api> for non-LLM tool steps (image/render/etc).\n\n"
            "Subtask envelope (use this schema for EVERY dispatch, regardless of runtime):\n"
            "  [subtask-envelope]\n"
            "  role.id: <as declared in workflow.roles>\n"
            "  primitive: <P-PLAN|P-EXECUTE|P-VALIDATE|P-JUDGE|P-INTEGRATE|P-RESEARCH>\n"
            "  objective: <one sentence -- your contract with the sub-agent>\n"
            "  success_criteria: <bullet list mapping to goal_packet.acceptance>\n"
            "  inputs: <files / links / prior artifacts>\n"
            "  output_schema: <patch summary | prompt string | image URI | lint report | ...>\n"
            "  tools_allowed: <whitelist; deny everything else>\n"
            "  context_budget: <approximate token / step budget>\n"
            "  return_mode: final-only      # default; opt-in to full-transcript only when auditing.\n\n"
            "Subagent ledger (REQUIRED in your final report's validation field):\n"
            "  subagent-ledger:\n"
            "    - role.id=<id> primitive=<P-*> agent=<runtime:tool#kind>\n"
            "      model_or_api=<opus|sonnet|actual-model|runtime-default|unsupported:reason|external:api>\n"
            "      goal=<...>\n"
            "      decision=<accepted|rejected|retried> evidence=<paths/uris/test-names>\n"
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

    def _build_review_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        reviewer: ManagedSession,
        trigger_report: AgentReport,
        lesson_context: list[dict[str, Any]] | None = None,
    ) -> str:
        task_reports = [
            report
            for report in self.reports_for_workspace(task.workspace_id)
            if report.task_id == task.id
        ][-12:]
        report_payload = [
            {
                "state": report.state.value,
                "session_id": report.session_id,
                "message": report.message,
                "changed_files": report.changed_files,
                "validation": report.validation,
                "risks": report.risks,
                "acceptance_check": [
                    item.model_dump(mode="json") for item in report.acceptance_check
                ],
                "review_profiles": [profile.value for profile in report.review_profiles],
                "profile_results": [
                    item.model_dump(mode="json") for item in report.profile_results
                ],
                "artifact_refs": report.artifact_refs,
                "confidence": report.confidence,
                "requires_human_judgment": report.requires_human_judgment,
                "review_decision": report.review_decision.value,
                "review_reason": report.review_reason,
                "risk_level": report.risk_level,
                "created_at": report.created_at.isoformat(),
            }
            for report in task_reports
        ]
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
            f"Workspace: {workspace.name}\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Task mode: {task.task_mode.value}\n"
            f"Task execution complexity: {task.execution_complexity.value}\n"
            f"Implementation agent session: {task.session_id or 'unknown'}\n"
            f"Reviewer session: {reviewer.id}\n"
            f"{self._session_environment_lines(workspace, reviewer)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            "Task description:\n"
            f"{task.prompt}\n\n"
            f"{self._execution_complexity_review_block(task)}"
            "Stored Goal Packet JSON:\n"
            f"{task.goal_packet.model_dump_json() if task.goal_packet else 'null'}\n\n"
            f"{self._autonomous_review_block(task)}"
            f"{self._review_profile_prompt_block(profiles)}"
            f"{self._review_guidance_block(workspace, trigger_report)}"
            f"{lesson_context_block}"
            f"{self._review_workflow_block(task, trigger_report)}"
            "Required final report format:\n"
            "Keep the message itself SHORT and human-scannable. Detailed evidence belongs in the "
            "structured report fields (validation, risks, acceptance_check, profile_results, "
            "artifact_refs), not duplicated in the message body. Aim for under ~12 lines total.\n\n"
            "Message body sections:\n"
            "Verdict: review_passed | review_failed | review_needs_input\n"
            "Summary: one or two sentences on what was actually delivered for this task.\n"
            'Acceptance criteria: a short rollup, e.g. "3/4 passed (1 partial: <criterion>)"; full '
            "per-criterion evidence belongs in the acceptance_check structured field.\n"
            "Required fixes: only for review_failed; the 1-3 highest-priority concrete fixes.\n"
            "Notes: at most one line on residual risk, gaps, or follow-up; deeper detail goes into "
            "the risks/validation fields.\n\n"
            "Bilingual reporting:\n"
            "- Every review report must include message_en (concise English) and message_zh (concise 中文) "
            "with the same structure as above. Keep the legacy message field as a short fallback "
            "(English is fine).\n"
            "- Acceptance criteria details, validation logs, profile results, findings, and required fixes "
            "go into the structured fields — populate acceptance_check, validation, risks, profile_results, "
            "and artifact_refs as before.\n\n"
            "Trigger report JSON:\n"
            f"{trigger_report.model_dump_json()}\n\n"
            "Recent task reports JSON:\n"
            f"{json.dumps(report_payload, indent=2)}\n\n"
            "First report review_started, then finish with exactly one final review report:\n"
            f"curl -sS -X POST {self._report_base_url(reviewer)}/api/workspaces/sessions/{reviewer.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"review_passed",'
            '"message":"Verdict + 1-2 sentence summary + acceptance rollup + notes",'
            '"message_en":"Verdict + 1-2 sentence summary + acceptance rollup + notes",'
            '"message_zh":"结论 + 1-2 句任务摘要 + 验收标准汇总 + 备注",'
            '"validation":"Checks reviewed",'
            '"risks":"Residual risk or none",'
            '"review_profiles":["general"],'
            '"profile_results":[{"profile":"general","status":"passed",'
            '"evidence":"Evidence reviewed.","blocking_findings":[],'
            '"non_blocking_findings":[]}],'
            '"artifact_refs":[],"confidence":0.8,'
            '"requires_human_judgment":false}}\'\n\n'
            "Use review_failed when fixes are required. Use review_needs_input only for genuine blockers "
            "outside the implementation agent's control."
        )

    def _review_workflow_block(
        self,
        task: WorkspaceTask,
        trigger_report: AgentReport,
    ) -> str:
        if self._is_goal_packet_approval_review(task, trigger_report):
            return (
                "Goal Packet approval review:\n"
                "1. Stay read-only. Do not edit files, run formatters that write changes, or revert work.\n"
                "2. This is a pre-implementation plan gate. Do not judge implementation completeness; "
                "there should be no substantive implementation yet.\n"
                "3. Check whether the stored Goal Packet faithfully preserves the original task prompt, "
                "attachments, ambiguity, and requested outcome. Fail the review if the packet narrowed "
                "or distorted scope.\n"
                "4. Verify the packet has reviewer-checkable acceptance criteria, a validation plan, "
                "assumptions, out-of-scope boundaries, and final handoff requirements. Treat missing "
                "editable/non-editable boundaries or vague validation as blocking.\n"
                "5. Check the packet's execution order: the implementation agent must wait for this "
                "approval before substantive development, then stay within the approved packet unless "
                "it submits a revised packet for review.\n"
                "6. Produce one final verdict using the exit criteria below.\n\n"
                "Acceptance standards:\n"
                "- Goal fidelity: the Goal Packet preserves the original prompt and does not hide ambiguous scope.\n"
                "- Boundary quality: editable areas, non-goals, dependencies to avoid, and rejected approaches "
                "are explicit enough to constrain implementation.\n"
                "- Reviewability: acceptance criteria and validation plan are concrete enough for a reviewer "
                "to check later without reconstructing intent.\n"
                "- Handoff quality: final report requirements include changed files, validation evidence, "
                "risks, and acceptance_check mapping.\n\n"
                "Review exit criteria:\n"
                "- review_passed means the implementation agent may begin development from the approved "
                "Goal Packet. It does not mean the task implementation is complete or ready for human acceptance.\n"
                "- review_failed means the implementation agent must revise only the Goal Packet and resubmit "
                "it for approval before development. Include a Required fixes section.\n"
                "- review_needs_input means the packet cannot be judged without user/product clarification, "
                "credentials, unavailable environment, or another decision the implementation agent cannot "
                "safely infer.\n\n"
            )
        return (
            "Review workflow:\n"
            "1. Stay read-only. Do not edit files, run formatters that write changes, or revert work.\n"
            "2. Check whether the stored Goal Packet faithfully preserves the original task prompt. "
            "Fail the review if the packet narrowed or distorted the user's requested outcome.\n"
            "3. Derive a task-specific acceptance checklist before judging the implementation. Use:\n"
            "   - the task title and description,\n"
            "   - the stored Goal Packet objective, acceptance criteria, validation plan, assumptions, "
            "out-of-scope boundaries, and handoff requirements,\n"
            "   - explicit user requirements and attachments,\n"
            "   - changed_files, validation, risks, and acceptance_check evidence from the implementation reports,\n"
            "   - enabled review profiles, profile-specific evidence, artifact_refs, and any REVIEW.md guidance,\n"
            "   - repository conventions and nearby behavior,\n"
            "   - any blocked/needs_input context from the trigger report.\n"
            "4. Inspect changed files and related code paths enough to verify correctness and scope.\n"
            "5. Adversarial defect hunt (do this BEFORE deciding the verdict): actively try to break "
            "the change. Enumerate concrete failure modes and check each against the actual code:\n"
            "   - edge/boundary inputs and empty/null/large values,\n"
            "   - error and exception paths, partial failures, and retries,\n"
            "   - concurrency, ordering, and shared-state races,\n"
            "   - regressions to existing flows, persistence, and migrations,\n"
            "   - scope leakage and unintended side effects in untouched areas,\n"
            "   - security/permission and input-trust assumptions where relevant.\n"
            "   Treat anything you cannot rule out by reading the code as a candidate defect, not as fine.\n"
            "6. Evaluate validation evidence. Independently spot-check the highest-risk claimed checks "
            "instead of accepting them at face value. Decide whether missing tests/checks are acceptable "
            "or blocking.\n"
            "7. Produce one final verdict using the exit criteria below.\n\n"
            "Acceptance standards:\n"
            "- Goal fidelity: the Goal Packet preserves the original prompt and does not hide ambiguous scope.\n"
            "- Functional correctness: the requested behavior is implemented end to end.\n"
            "- Scope control: changes are limited to the task and do not introduce unrelated churn.\n"
            "- Integration fit: code follows local architecture, state flow, API contracts, and UI conventions.\n"
            "- Regression safety: existing user flows, persistence, concurrency, and error paths are not broken.\n"
            "- Validation quality: reported checks match the risk level; missing checks are called out clearly.\n"
            "- Handoff quality: changed_files, validation, and risks are understandable for a human reviewer.\n\n"
            "Review exit criteria:\n"
            "- review_passed: you have actively attempted to break the change (step 5) and found no "
            "blocking defect; every acceptance criterion is satisfied; validation is adequate or any gaps "
            "are explicitly non-blocking; residual risks are acceptable for final human acceptance. "
            "Do not pass on the absence of an attempt or because the implementation report looked confident.\n"
            "- review_failed: at least one blocking defect, regression, scope issue, or missing required validation "
            "can be fixed by the implementation agent. Include a Required fixes section.\n"
            "- review_needs_input: review cannot finish without user/product clarification, credentials, unavailable "
            "environment, or another decision the implementation agent cannot safely infer.\n\n"
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
                "- Verify model pinning: P-PLAN, P-EXECUTE, P-JUDGE, and P-INTEGRATE roles must run "
                "on opus on the claude runtime; P-VALIDATE and P-RESEARCH may run on sonnet. A "
                "P-EXECUTE role that calls an external API may instead record model_or_api=external:<api>. "
                "Wrong-tier model on a key primitive is a contract violation.\n"
            )
        elif task.agent_type in {AgentType.CURSOR, AgentType.CODEX}:
            model_verification = (
                f"- Verify model/API evidence for the {task.agent_type.value} runtime. Do NOT fail solely "
                "because Claude opus/sonnet pinning is absent; this runtime may not expose Claude "
                "pinning. Accept `model_or_api=runtime-default`, `model_or_api=unsupported:<reason>`, "
                "an actual runtime model name, or `model_or_api=external:<api>` when the ledger and "
                "workflow.notes explain the limitation. Treat missing model/API evidence as a ledger "
                "quality issue, not as a Claude wrong-tier violation.\n"
            )
        else:
            model_verification = (
                "- Verify the terminal-runtime degradation honestly records direct execution or "
                "`model_or_api=runtime-default`. Do NOT require Claude opus/sonnet pinning for a "
                "plain terminal worker, and do not accept fabricated sub-agent/model claims.\n"
            )
        return (
            "Autonomous evaluation context:\n"
            f"- Run JSON: {run.model_dump_json() if run else 'null'}\n"
            f"- Worker runtime: {task.agent_type.value}\n"
            f"- Max iterations: {policy.max_iterations}\n"
            f"- Evaluation strictness: {policy.evaluation_strictness.value}\n"
            f"- Require artifact review: {policy.require_artifact_review}\n\n"
            "For Autonomous Mode V1, act as the evaluator for this iteration. "
            "Score against the Goal Packet, any rubric/run evidence, validation, artifacts, "
            "and prior evaluation history. Use review_passed only when the run should move "
            "to passed and await human acceptance. Use review_failed when targeted revision "
            "is possible within budget. Use review_needs_input when product judgment, missing "
            "credentials, unavailable artifacts, or unsafe scope prevents evaluation.\n\n"
            "Subagent ledger verification (orchestrator contract enforcement):\n"
            "- For complex autonomous tasks the orchestrator MUST embed a `subagent-ledger:` "
            "section in its review-gate report's validation field. A missing or empty ledger "
            "on a complex task is a contract violation; recommend review_failed with a blocking "
            "issue stating the ledger is required.\n"
            "- Each ledger entry should carry role.id, primitive (P-PLAN/P-EXECUTE/P-VALIDATE/"
            "P-JUDGE/P-INTEGRATE/P-RESEARCH), agent, model_or_api, decision, and evidence.\n"
            f"{model_verification}"
            "- Verify the workflow.roles declared in the first working report matches the ledger "
            "and that at least one P-EXECUTE and one P-JUDGE actually ran. P-VALIDATE is required "
            "when the task has any objectively-checkable success criterion.\n\n"
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
            "Orchestrator-mode reminder: if you ran in orchestrator mode for this task, stay in "
            "orchestrator mode for this revision. Address the evaluator's blocking issues by "
            "dispatching new sub-agent subtasks (P-EXECUTE for fixes, P-VALIDATE for re-tests, "
            "P-JUDGE for re-review) rather than folding the work into your own context. Append "
            "the new ledger entries to your existing subagent ledger; do not restart it.\n\n"
        )
