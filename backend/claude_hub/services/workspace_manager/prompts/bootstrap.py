"""Prompt builders split from the compatibility mixin."""

from .._base import _WorkspaceManagerBase
from .._constants import *  # noqa: F401,F403


class _BootstrapPromptsMixin(_WorkspaceManagerBase):
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
            return self._runtime_resident_prompt(
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
        if session.session_kind == SessionKind.CHAT:
            lines.append(
                "Execution uses native Chat, without a terminal. Finish this turn when idle; "
                "the Workspace delivers later assignments. Report through the supplied API; "
                "a Chat reply alone does not complete the Task."
            )
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
            "Wait for assigned tasks in this Workspace session; do not start unrelated work. When a task "
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
