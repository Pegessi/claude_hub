"""Task start, dispatch target selection, and dispatch."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403

# Dispatch reasons that pin a queued task to one specific agent so firmly that
# the task must NEVER be migrated to a different free agent — only the operator's
# explicit target selection qualifies. Every other reason (auto queued-behind,
# related-task continuity, previous-assignment continuity, prior reassignment)
# expresses a *preference* for its agent that may yield to a free agent when the
# pinned agent is stuck. See _next_reassignable_queued_task.
_NON_MIGRATABLE_DISPATCH_REASONS = frozenset({"User selected target agent"})


class _DispatchMixin:
    async def start_task(
        self,
        task_id: str,
        payload: StartTaskRequest | None = None,
    ) -> WorkspaceTask:
        payload = payload or StartTaskRequest()
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            raise KeyError(task.workspace_id)
        if task.status == WorkspaceTaskStatus.DONE:
            raise RuntimeError("Done tasks cannot be started")

        logger.info(
            "Starting workspace task id=%s workspace_id=%s title=%r payload_target_session_id=%s "
            "payload_related_task_id=%s stored_related_task_id=%s current_session_id=%s status=%s",
            task.id,
            workspace.id,
            task.title,
            payload.target_session_id,
            payload.related_task_id,
            task.related_task_id,
            task.session_id,
            task.status,
        )
        await self._refresh_session_statuses(workspace.id)

        if not self._workspace_agents(workspace.id, include_stopped=True):
            logger.info(
                "No workspace agents found for workspace_id=%s; creating default agent for task id=%s",
                workspace.id,
                task.id,
            )
            await self.ensure_workspace_agent(
                workspace.id,
                EnsureWorkspaceAgentRequest(
                    agent_type=payload.agent_type or task.agent_type,
                    reuse_existing=False,
                ),
            )

        base_update: dict[str, Any] = {
            "status": WorkspaceTaskStatus.QUEUED,
            "queued_at": task.queued_at or _wm._now(),
            "updated_at": _wm._now(),
            "dispatch_pending": False,
            "manual_aborted_at": None,
            "manual_abort_reason": None,
        }
        if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
            run = task.autonomous_run or self._default_autonomous_run(
                task.id,
                task.autonomy_policy.max_iterations if task.autonomy_policy else 3,
            )
            phase = AutonomousRunPhase.DISPATCHING
            base_update["autonomous_run"] = run.model_copy(
                update={
                    "phase": phase,
                    "status_summary": self._autonomous_phase_label(phase),
                    "next_action": self._autonomous_next_action(phase),
                }
            )
        if payload.agent_type:
            base_update["agent_type"] = payload.agent_type
        if payload.related_task_id:
            if payload.related_task_id not in self.tasks:
                raise KeyError(payload.related_task_id)
            base_update["related_task_id"] = payload.related_task_id

        task = task.model_copy(update=base_update)
        decision = await self._choose_dispatch_target(workspace, task, payload)
        if decision is None:
            task = task.model_copy(
                update={
                    "session_id": None,
                    # Preserve the stored clear-context flag when the start call
                    # does not override it, so the dispatcher decision later
                    # still applies the user's "Clear context" choice.
                    "clear_context": (
                        payload.clear_context
                        if payload.clear_context is not None
                        else task.clear_context
                    ),
                    "dispatch_reason": "Waiting for dispatcher agent decision",
                    "dispatch_pending": True,
                    "updated_at": _wm._now(),
                }
            )
            self.tasks[task.id] = task
            self._save_state()
            logger.info(
                "Workspace task id=%s is waiting for dispatcher decision related_task_id=%s",
                task.id,
                task.related_task_id,
            )
            await self._request_dispatch_decision(workspace, task)
            return self.tasks[task.id]

        target, clear_context, reason = decision
        autonomous_run = task.autonomous_run
        if task.task_mode == WorkspaceTaskMode.AUTONOMOUS and autonomous_run is not None:
            phase = AutonomousRunPhase.WORKING
            autonomous_run = autonomous_run.model_copy(
                update={
                    "phase": phase,
                    "status_summary": self._autonomous_phase_label(phase),
                    "active_session_ids": list(
                        dict.fromkeys([*autonomous_run.active_session_ids, target.id])
                    ),
                    "next_action": self._autonomous_next_action(phase),
                }
            )
        task = task.model_copy(
            update={
                "session_id": target.id,
                "clear_context": clear_context,
                "dispatch_reason": reason,
                "dispatch_pending": False,
                "autonomous_run": autonomous_run,
                "updated_at": _wm._now(),
            }
        )
        self.tasks[task.id] = task
        self._save_state()
        logger.info(
            "Workspace task id=%s queued for session_id=%s session_title=%r related_task_id=%s "
            "clear_context=%s reason=%r",
            task.id,
            target.id,
            target.title,
            task.related_task_id,
            clear_context,
            reason,
        )
        await self.dispatch_workspace(workspace.id)
        return self.tasks[task.id]

    async def _choose_dispatch_target(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        payload: StartTaskRequest,
    ) -> Optional[tuple[ManagedSession, bool, str]]:
        agents = self._workspace_agents(workspace.id, include_stopped=True)
        # The "Clear context" checkbox is stored on the task at creation
        # (task.clear_context) but a start call may also override it inline
        # (payload.clear_context). Resolve the explicit request once so every
        # dispatch branch below honors it: an inline payload value wins, then
        # the stored task flag, then None (let each branch fall back to its
        # default/heuristic). Without this fallback, continuity and related-task
        # branches dropped the stored flag and never sent /clear.
        requested_clear = (
            payload.clear_context if payload.clear_context is not None else task.clear_context
        )
        if payload.target_session_id:
            target = self.sessions.get(payload.target_session_id)
            if not target or target.workspace_id != workspace.id:
                raise KeyError(payload.target_session_id)
            if target.role != WorkspaceSessionRole.ORCHESTRATOR:
                raise RuntimeError("Tasks can only be assigned to workspace agents")
            if not self._can_assign_or_queue_to(target):
                if (
                    target.status == ManagedSessionStatus.STOPPED
                    or target.runtime_status == AgentRuntimeStatus.OFFLINE
                ):
                    raise RuntimeError("Offline workspace agents cannot accept tasks")
                if target.runtime_status == AgentRuntimeStatus.ATTENTION:
                    raise RuntimeError("Workspace agents waiting for input cannot accept new tasks")
                raise RuntimeError("Selected workspace agent cannot accept tasks yet")
            return (
                target,
                bool(requested_clear),
                "User selected target agent",
            )

        related_task_id = payload.related_task_id or task.related_task_id
        if related_task_id:
            related = self.tasks.get(related_task_id)
            target = None
            if related and related.session_id:
                target = self.sessions.get(related.session_id)
                if target and self._can_assign_or_queue_to(target):
                    logger.info(
                        "Dispatch target selected from related task: task_id=%s related_task_id=%s "
                        "target_session_id=%s related_status=%s",
                        task.id,
                        related_task_id,
                        target.id,
                        related.status,
                    )
                    return (
                        target,
                        bool(requested_clear),
                        f"Related to task {related_task_id}",
                    )
            logger.info(
                "Related task did not provide a dispatch target: task_id=%s related_task_id=%s "
                "related_exists=%s related_session_id=%s related_target_runtime=%s",
                task.id,
                related_task_id,
                bool(related),
                related.session_id if related else None,
                target.runtime_status if target else None,
            )

        if task.session_id:
            existing = self.sessions.get(task.session_id)
            if existing and self._can_assign_or_queue_to(existing):
                return (
                    existing,
                    bool(requested_clear),
                    "Continuing previous task assignment",
                )

        free_agents = [agent for agent in agents if self._can_dispatch_to(agent)]
        if len(free_agents) == 1:
            target = free_agents[0]
            should_clear = self._has_prior_task_history(target.id)
            return (
                target,
                requested_clear if requested_clear is not None else should_clear,
                "Only one workspace agent is available",
            )
        if len(free_agents) > 1:
            target = self._least_queued_agent(free_agents)
            should_clear = self._has_prior_task_history(target.id)
            return (
                target,
                requested_clear if requested_clear is not None else should_clear,
                "Selected least queued available workspace agent",
            )

        queueable_agents = [
            agent
            for agent in agents
            if agent.status != ManagedSessionStatus.STOPPED
            and (
                agent.runtime_status == AgentRuntimeStatus.WORKING
                or self._is_holding_unresolved_review_task(agent)
            )
        ]
        if queueable_agents:
            target = self._least_queued_agent(queueable_agents)
            should_clear = self._has_prior_task_history(target.id)
            return (
                target,
                requested_clear if requested_clear is not None else should_clear,
                "Queued behind existing workspace agent",
            )

        if agents:
            raise RuntimeError("No idle or working workspace agent is available")

        return None

    def _can_assign_or_queue_to(self, session: ManagedSession) -> bool:
        if session.role != WorkspaceSessionRole.ORCHESTRATOR:
            return False
        if session.status == ManagedSessionStatus.STOPPED:
            return False
        if session.runtime_status == AgentRuntimeStatus.WORKING:
            return True
        if self._is_holding_unresolved_review_task(session):
            return True
        return self._can_dispatch_to(session)

    def _least_queued_agent(self, agents: list[ManagedSession]) -> ManagedSession:
        def runtime_rank(agent: ManagedSession) -> int:
            if agent.runtime_status == AgentRuntimeStatus.IDLE:
                return 0
            if agent.runtime_status == AgentRuntimeStatus.OFFLINE:
                return 1
            return 2

        return sorted(
            agents,
            key=lambda agent: (
                self._queued_count(agent.id),
                runtime_rank(agent),
                1 if agent.current_task_id or agent.task_id else 0,
                agent.created_at,
            ),
        )[0]

    def _has_prior_task_history(self, session_id: str) -> bool:
        return any(
            task.session_id == session_id
            and task.status in {WorkspaceTaskStatus.REVIEW, WorkspaceTaskStatus.DONE}
            for task in self.tasks.values()
        )

    async def _request_dispatch_decision(self, workspace: Workspace, task: WorkspaceTask) -> None:
        dispatcher = await self.ensure_workspace_agent(
            workspace.id,
            EnsureWorkspaceAgentRequest(
                agent_type=task.agent_type,
                role=WorkspaceSessionRole.DISPATCHER,
                reuse_existing=True,
            ),
        )
        await self.send_session_message(
            dispatcher.id,
            self._build_dispatch_decision_prompt(workspace, task, dispatcher),
        )

    async def apply_dispatch_decision(
        self,
        task_id: str,
        payload: DispatchDecisionRequest,
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        target = self.sessions.get(payload.target_session_id)
        if not target or target.workspace_id != task.workspace_id:
            raise KeyError(payload.target_session_id)
        if target.role != WorkspaceSessionRole.ORCHESTRATOR:
            raise RuntimeError("Dispatch decisions must target a workspace agent")

        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.QUEUED,
                "session_id": target.id,
                # A user opt-in ("Clear context" checkbox -> task.clear_context)
                # must not be overridden by the dispatcher agent's discretion:
                # OR the stored flag so an explicit clear request always wins.
                "clear_context": bool(payload.clear_context) or bool(task.clear_context),
                "dispatch_reason": payload.reason or "Dispatcher selected target agent",
                "dispatch_pending": False,
                "queued_at": task.queued_at or _wm._now(),
                "updated_at": _wm._now(),
            }
        )
        self._save_state()
        await self.dispatch_workspace(task.workspace_id)
        return self.tasks[task.id]

    async def continue_task(
        self,
        task_id: str,
        payload: ContinueTaskRequest | None = None,
    ) -> WorkspaceTask:
        payload = payload or ContinueTaskRequest()
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        if task.status != WorkspaceTaskStatus.REVIEW:
            raise RuntimeError("Only review tasks can continue")
        if not task.session_id or task.session_id not in self.sessions:
            raise RuntimeError("Review task has no original agent")

        now = _wm._now()
        self._ensure_session_can_continue_task(self.sessions[task.session_id], task)
        session = await self._rename_session_for_task(
            self.sessions[task.session_id],
            task,
            updated_at=now,
        )
        autonomous_run = task.autonomous_run
        if task.task_mode == WorkspaceTaskMode.AUTONOMOUS:
            autonomous_run = autonomous_run or self._default_autonomous_run(
                task.id,
                task.autonomy_policy.max_iterations if task.autonomy_policy else 3,
            )
            phase = AutonomousRunPhase.WORKING
            autonomous_run = autonomous_run.model_copy(
                update={
                    "phase": phase,
                    "status_summary": self._autonomous_phase_label(phase),
                    "active_session_ids": list(
                        dict.fromkeys([*autonomous_run.active_session_ids, session.id])
                    ),
                    "next_action": self._autonomous_next_action(phase),
                }
            )
        # Opening a fresh work round: bump review_cycle so the worker's next
        # ready_for_review outranks the prior verdict (reviewed_cycle) and a
        # new reviewer is dispatched. Verdict timestamp fields are cleared too,
        # but cycle ordering — not timestamps — is what gates re-review.
        next_cycle = task.review_cycle + 1
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.WORKING,
                "started_at": now,
                "review_cycle": next_cycle,
                "review_skipped_at": None,
                "review_skip_reason": None,
                "human_acceptance_requested_at": None,
                "human_accepted_at": None,
                "autonomous_run": autonomous_run,
                "updated_at": now,
                "dispatch_pending": False,
                "review_completed_at": None,
                "reviewed_at": None,
                "review_requested_at": None,
            }
        )
        logger.info(
            "continue_task set status=WORKING and cleared stale goal-packet verdict fields "
            "task_id=%s review_completed_at_cleared=%s session_id=%s",
            task.id,
            task.review_completed_at is not None,
            task.session_id,
        )
        self.sessions[session.id] = session.model_copy(
            update={
                "task_id": task.id,
                "current_task_id": task.id,
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": 0,
                "last_auto_continue_at": None,
                "hard_recovery_task_id": None,
                "hard_recovery_attempts": 0,
                "last_hard_recovery_at": None,
                "prompt_retry_task_id": None,
                "prompt_retry_attempted_at": None,
                "updated_at": now,
                "last_activity_at": now,
            }
        )
        continue_report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=session.id,
            state=AgentReportState.WORKING,
            message=payload.message or "Task continued from review",
            changed_files=[],
            validation=None,
            risks=None,
            review_cycle=next_cycle,
            created_at=now,
        )
        self.reports[continue_report.id] = continue_report
        self._save_state()

        await self.send_session_message(
            session.id,
            self._build_continue_prompt(self.tasks[task.id], payload, session),
        )
        return self.tasks[task.id]

    def _ensure_session_can_continue_task(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
    ) -> None:
        if session.role != WorkspaceSessionRole.ORCHESTRATOR:
            raise RuntimeError("Review task original session is not a workspace agent")
        if session.status == ManagedSessionStatus.STOPPED:
            raise RuntimeError("Review task original agent is stopped")

        assigned_ids = {
            assigned_id for assigned_id in (session.task_id, session.current_task_id) if assigned_id
        }
        busy_ids = [assigned_id for assigned_id in assigned_ids if assigned_id != task.id]
        for busy_id in busy_ids:
            busy_task = self.tasks.get(busy_id)
            if not busy_task or busy_task.status != WorkspaceTaskStatus.DONE:
                raise RuntimeError(
                    "Review task original agent is busy with another task; "
                    "wait for that task to finish before requesting changes."
                )

    async def request_task_review(
        self,
        task_id: str,
        payload: RequestTaskReviewRequest | None = None,
    ) -> WorkspaceTask:
        payload = payload or RequestTaskReviewRequest()
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        if task.status == WorkspaceTaskStatus.DONE:
            raise RuntimeError("Done tasks cannot request review")
        if state_policy.review_in_flight(task.review_requested_at, task.review_completed_at):
            # Only suppress re-dispatch when an assigned reviewer is actively
            # working on this task. If the reviewer session is idle, stopped,
            # or missing entirely, the prior review dispatch got stuck and we
            # must allow re-dispatch so the task does not sit forever in
            # "Awaiting AI review".
            if self._reviewer_is_active(task):
                return task
            logger.info(
                "Re-requesting review for stuck task_id=%s (review_requested_at=%s "
                "review_session_id=%s)",
                task.id,
                task.review_requested_at,
                task.review_session_id,
            )
            self._release_stale_reviewer_for_task(task, updated_at=_wm._now())
        if not task.session_id:
            raise RuntimeError("Task has no implementation agent")

        now = _wm._now()
        message = (
            payload.message.strip()
            if payload.message and payload.message.strip()
            else "Human requested reviewer checks."
        )
        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=task.session_id,
            state=AgentReportState.READY_FOR_REVIEW,
            message=message,
            changed_files=[],
            validation=None,
            risks=None,
            review_decision=ReviewDecision.REQUEST,
            review_reason=message,
            risk_level=None,
            review_cycle=task.review_cycle,
            created_at=now,
        )
        self.reports[report.id] = report
        await self._request_task_review(task, report)
        return self.tasks[task.id]

    async def abort_task(
        self,
        task_id: str,
        payload: ManualTaskControlRequest,
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        if task.status not in {
            WorkspaceTaskStatus.QUEUED,
            WorkspaceTaskStatus.WORKING,
            WorkspaceTaskStatus.REVIEW,
        }:
            raise RuntimeError("Only queued, working, or review tasks can be manually aborted")

        reason = payload.reason.strip()
        if not reason:
            raise RuntimeError("Manual abort requires a reason")

        now = _wm._now()
        report_session_id = task.session_id or task.review_session_id or "manual-control"
        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=report_session_id,
            state=AgentReportState.BLOCKED,
            message=f"Task manually aborted by operator: {reason}",
            message_en=f"Task manually aborted by operator: {reason}",
            message_zh=f"操作员已手动终止任务：{reason}",
            changed_files=[],
            validation=None,
            risks="Task state was manually recovered; prior worker/reviewer output may be incomplete.",
            review_decision=ReviewDecision.SKIP,
            review_reason="Manual abort is an exceptional recovery action, not task completion.",
            risk_level="manual_control",
            review_cycle=task.review_cycle,
            created_at=now,
        )
        self.reports[report.id] = report
        task_before_release = task

        # Collect sessions to interrupt before we clear the session IDs on the
        # task object.  Only sessions actually assigned to THIS task are targeted
        # (worker via task.session_id, reviewers via task.review_session_id plus
        # stale REVIEWER-role sessions still pointing at this task) — we never
        # interrupt unrelated/idle sessions.  Interrupts are sent concurrently
        # and are best-effort: failures are logged inside _interrupt_session and
        # do not block the bookkeeping abort.
        sessions_to_interrupt: list[ManagedSession] = []
        if task_before_release.session_id:
            worker_session = self.sessions.get(task_before_release.session_id)
            if worker_session and (
                worker_session.task_id == task.id or worker_session.current_task_id == task.id
            ):
                sessions_to_interrupt.append(worker_session)
        reviewer_ids: set[str] = set()
        if task_before_release.review_session_id:
            reviewer_ids.add(task_before_release.review_session_id)
        reviewer_ids.update(
            s.id
            for s in self.sessions.values()
            if s.role == WorkspaceSessionRole.REVIEWER
            and (s.task_id == task.id or s.current_task_id == task.id)
        )
        for sid in reviewer_ids:
            reviewer_session = self.sessions.get(sid)
            if (
                reviewer_session
                and reviewer_session.role == WorkspaceSessionRole.REVIEWER
                and (
                    reviewer_session.task_id == task.id
                    or reviewer_session.current_task_id == task.id
                )
            ):
                sessions_to_interrupt.append(reviewer_session)
        if sessions_to_interrupt:
            await asyncio.gather(*(self._interrupt_session(s) for s in sessions_to_interrupt))

        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.TODO,
                "session_id": None,
                "clear_context": None,
                "dispatch_reason": f"Manually aborted: {reason}",
                "dispatch_pending": False,
                "review_session_id": None,
                "review_requested_at": None,
                "review_completed_at": None,
                "review_skipped_at": None,
                "review_skip_reason": None,
                "manual_aborted_at": now,
                "manual_abort_reason": reason,
                "human_acceptance_requested_at": None,
                "human_accepted_at": None,
                "queued_at": None,
                "started_at": None,
                "reviewed_at": None,
                "completed_at": None,
                "updated_at": now,
            }
        )
        self._release_task_session(task_before_release)
        await self._cleanup_reviewer_for_terminal_task(task_before_release, updated_at=now)
        self._save_state()
        await self.dispatch_workspace(task.workspace_id)
        return self.tasks[task.id]

    async def dispatch_workspace(
        self,
        workspace_id: str,
        *,
        refresh_sessions: bool = True,
    ) -> None:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)

        lock = self._dispatch_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            await self._dispatch_workspace_locked(
                workspace_id,
                refresh_sessions=refresh_sessions,
            )

    async def _dispatch_workspace_locked(
        self,
        workspace_id: str,
        *,
        refresh_sessions: bool,
    ) -> None:
        if refresh_sessions:
            await self._refresh_session_statuses(workspace_id)
        # Review dispatch has no retry mechanism of its own:
        # _request_task_review either succeeds or strands the task. Run the
        # stale-review reaper and stale-reviewer assignment cleanup before
        # every dispatch pass so a transient failure (prompt send error,
        # reviewer crash, reconciler-only REVIEW status) does not permanently
        # strand the task in "Awaiting AI review" with idle reviewers.
        if self._cleanup_stale_reviewer_assignments(workspace_id):
            self._save_state()
        await self._reap_stuck_reviews(workspace_id)
        # Drop managed terminal tabs left behind by removed sessions so leaked
        # reviewer tabs (visible in the tab bar but absent from Manage Agents)
        # do not accumulate.
        await self._prune_orphan_workspace_tabs(workspace_id)
        for session in self._workspace_agents(workspace_id, include_stopped=True):
            if not self._can_dispatch_to(session):
                logger.info(
                    "Skipping workspace session dispatch workspace_id=%s session_id=%s "
                    "runtime_status=%s task_id=%s current_task_id=%s",
                    workspace_id,
                    session.id,
                    session.runtime_status,
                    session.task_id,
                    session.current_task_id,
                )
                continue
            rebalanced_task = self._next_reassignable_queued_task(session.id, workspace_id)
            if rebalanced_task:
                logger.info(
                    "Reassigning queued workspace task id=%s from session_id=%s to free "
                    "session_id=%s",
                    rebalanced_task.id,
                    rebalanced_task.session_id,
                    session.id,
                )
                self.tasks[rebalanced_task.id] = rebalanced_task.model_copy(
                    update={
                        "session_id": session.id,
                        "clear_context": True,
                        "dispatch_reason": "Reassigned to newly available workspace agent",
                        "updated_at": _wm._now(),
                    }
                )
            next_task = self._next_queued_task(session.id)
            if not next_task:
                continue
            await self._dispatch_task_to_session(next_task, session)

    def _can_dispatch_to(self, session: ManagedSession) -> bool:
        if session.role != WorkspaceSessionRole.ORCHESTRATOR:
            return False
        if session.status == ManagedSessionStatus.STOPPED:
            return False
        if session.runtime_status != AgentRuntimeStatus.IDLE:
            return False
        if session.task_id or session.current_task_id:
            current_id = session.task_id or session.current_task_id
            current = self.tasks.get(current_id) if current_id else None
            # Hold the implementation agent for as long as its task lives on
            # the board. Only a DONE task (human-accepted) frees the agent,
            # so a REVIEW-state task — even after review_passed — keeps its
            # context locked for follow-up revisions or human rejection.
            if current and current.status != WorkspaceTaskStatus.DONE:
                return False
        return True

    def _is_review_passed(self, task: WorkspaceTask) -> bool:
        if task.review_completed_at is None:
            return False
        return self._latest_review_report_state(task.id) == AgentReportState.REVIEW_PASSED

    def _is_holding_unresolved_review_task(self, session: ManagedSession) -> bool:
        current_id = session.task_id or session.current_task_id
        if not current_id:
            return False
        current = self.tasks.get(current_id)
        if not current or current.status != WorkspaceTaskStatus.REVIEW:
            return False
        return True

    def _next_queued_task(self, session_id: str) -> Optional[WorkspaceTask]:
        tasks = [
            task
            for task in self.tasks.values()
            if task.session_id == session_id
            and task.status == WorkspaceTaskStatus.QUEUED
            and not task.dispatch_pending
        ]
        if not tasks:
            return None
        return sorted(tasks, key=_sort_time)[0]

    def _next_reassignable_queued_task(
        self,
        free_session_id: str,
        workspace_id: str,
    ) -> Optional[WorkspaceTask]:
        candidates: list[WorkspaceTask] = []
        for task in self.tasks.values():
            if task.workspace_id != workspace_id:
                continue
            if task.status != WorkspaceTaskStatus.QUEUED or task.dispatch_pending:
                continue
            if task.session_id == free_session_id:
                continue
            # Hard, explicit user pins wait for exactly the agent the operator
            # chose — never migrate them. Every other queued task (auto
            # queued-behind, related-task continuity, previous-assignment
            # continuity, or a prior reassignment) prefers its assigned agent
            # but may migrate when that agent is stuck. Preference is preserved
            # below: we only migrate when the assigned agent CANNOT dispatch, so
            # an available pinned agent still picks the task up itself via
            # _next_queued_task.
            if task.dispatch_reason in _NON_MIGRATABLE_DISPATCH_REASONS:
                continue
            assigned = self.sessions.get(task.session_id or "")
            # Migrate to the now-free agent whenever the currently-assigned
            # agent cannot take the task: genuinely busy WORKING, idle but
            # holding a non-DONE task parked in REVIEW (review_passed yet
            # awaiting human acceptance keeps the agent locked indefinitely),
            # or gone (STOPPED/OFFLINE). Without this, a continuity-pinned task
            # starves forever behind an agent that is idle on paper but will
            # never free up until a human resolves its review task, while other
            # agents sit idle. The migration block sets clear_context=True, so
            # the fresh agent starts clean rather than inheriting absent
            # related-task context.
            if not assigned or self._can_dispatch_to(assigned):
                continue
            candidates.append(task)
        if not candidates:
            return None
        return sorted(candidates, key=_sort_time)[0]

    async def _dispatch_task_to_session(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
    ) -> None:
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            raise KeyError(task.workspace_id)

        session = await self._rename_session_for_task(session, task)

        if task.clear_context:
            await self.send_session_message(session.id, "/clear")
            await asyncio.sleep(0.5)

        logger.info(
            "Dispatching workspace task id=%s title=%r to session_id=%s session_title=%r "
            "related_task_id=%s dispatch_reason=%r",
            task.id,
            task.title,
            session.id,
            session.title,
            task.related_task_id,
            task.dispatch_reason,
        )
        now = _wm._now()
        lesson_context = self._lesson_context_payload(
            workspace,
            f"{task.title}\n{task.prompt}",
        )
        await self.send_session_message(
            session.id,
            self._build_task_assignment_prompt(
                workspace,
                task,
                session,
                lesson_context=lesson_context,
            ),
        )

        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.WORKING,
                "started_at": now,
                "updated_at": now,
            }
        )
        self.sessions[session.id] = session.model_copy(
            update={
                "task_id": task.id,
                "current_task_id": task.id,
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": 0,
                "last_auto_continue_at": None,
                "hard_recovery_task_id": None,
                "hard_recovery_attempts": 0,
                "last_hard_recovery_at": None,
                "prompt_retry_task_id": None,
                "prompt_retry_attempted_at": None,
                "last_activity_at": now,
                "updated_at": now,
            }
        )
        self._save_state()
        logger.info(
            "Workspace task id=%s dispatched to session_id=%s and marked working",
            task.id,
            session.id,
        )
