"""Tmux send/run, session queries, reaper, and board."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ._constants import *  # noqa: F401,F403


class _TmuxQueriesMixin:
    def reports_for_workspace(self, workspace_id: str) -> list[AgentReport]:
        return sorted(
            [report for report in self.reports.values() if report.workspace_id == workspace_id],
            key=lambda report: report.created_at,
        )

    def latest_reports_per_task_for_workspace(self, workspace_id: str) -> list[AgentReport]:
        """Latest report per ``task_id`` for the board.

        The board only renders the most recent report per task card; the full
        per-task history is fetched on demand by the detail panel. Trimming here
        keeps the board payload an order of magnitude smaller (the full history
        can be thousands of reports) without changing what any card shows.
        """
        latest: dict[Optional[str], AgentReport] = {}
        for report in self.reports_for_workspace(workspace_id):  # asc by created_at
            latest[report.task_id] = report  # later (newer) overwrites
        return sorted(latest.values(), key=lambda report: report.created_at)

    def reports_for_task(self, workspace_id: str, task_id: str) -> list[AgentReport]:
        """Full report history for a single task, sorted ascending by created_at."""
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        return [
            report
            for report in self.reports_for_workspace(workspace_id)
            if report.task_id == task_id
        ]

    async def _send_tmux_message(self, tmux_session: str, message: str) -> None:
        logger.info(
            "Sending workspace message to tmux_session=%s message_length=%s",
            tmux_session,
            len(message),
        )
        await self._run_tmux("send-keys", "-t", tmux_session, "C-u")
        await asyncio.sleep(0.2)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(message)
            tmp_path = tmp.name
        try:
            await self._run_tmux("load-buffer", tmp_path)
            await self._run_tmux("paste-buffer", "-t", tmux_session)
            await asyncio.sleep(TMUX_PASTE_SETTLE_SECONDS)
            await self._submit_tmux_message(tmux_session, message)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    async def _submit_tmux_message(self, tmux_session: str, message: str) -> None:
        for attempt in range(1, TMUX_SUBMIT_ATTEMPTS + 1):
            await self._run_tmux("send-keys", "-t", tmux_session, "C-m")
            await asyncio.sleep(TMUX_SUBMIT_SETTLE_SECONDS)
            try:
                output = await self._capture_tmux_output(tmux_session)
            except RuntimeError as exc:
                logger.warning(
                    "Could not verify workspace message submit for tmux_session=%s: %s",
                    tmux_session,
                    exc,
                )
                return
            if not self._message_still_in_input(output, message):
                if attempt > 1:
                    logger.info(
                        "Workspace message submit succeeded after retry tmux_session=%s attempts=%s",
                        tmux_session,
                        attempt,
                    )
                return
            logger.warning(
                "Workspace message still appears pending after submit attempt %s/%s "
                "tmux_session=%s output_tail=%r",
                attempt,
                TMUX_SUBMIT_ATTEMPTS,
                tmux_session,
                output[-240:],
            )

        raise RuntimeError("Failed to submit workspace agent message; input still appears pending")

    def _message_still_in_input(self, output: str, message: str) -> bool:
        lines = [line.rstrip() for line in output.splitlines()]
        while lines and not lines[-1].strip():
            lines.pop()
        first_line = message.strip().splitlines()[0][:80] if message.strip() else ""
        is_slash_command = message.strip().startswith("/")
        prompt_markers = ("›", ">", "❯", "→")
        tail_start = max(0, len(lines) - 16)
        tail = lines[tail_start:]
        for index, line in enumerate(tail):
            stripped = line.strip()
            if not stripped.startswith(prompt_markers):
                continue
            has_pasted_placeholder = "[Pasted Content" in stripped or "[Pasted text" in stripped
            has_message_prefix = bool(first_line and first_line in stripped)
            if not has_pasted_placeholder and not has_message_prefix:
                continue
            following_lines = tail[index + 1 :]
            if any(next_line.strip().startswith(prompt_markers) for next_line in following_lines):
                continue
            following = "\n".join(following_lines[:5]).lstrip()
            if following.startswith(("•", "⏺", "●")):
                continue
            if following.startswith("⎿") and (is_slash_command or not has_pasted_placeholder):
                continue
            return True
        return False

    async def _run_tmux(self, *args: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux {' '.join(args)} failed with code {proc.returncode}")

    async def _interrupt_session(self, session: ManagedSession) -> None:
        """Send Escape then a single Ctrl-C to interrupt a running Claude Code process.

        Sequence: Escape dismisses any open dialog/prompt, a short settle allows the
        TUI to return to its main loop, then one Ctrl-C raises KeyboardInterrupt in
        the agent.  We deliberately send exactly one Ctrl-C to avoid the double-tap
        that exits Claude Code entirely.  Errors are logged but not raised so that
        bookkeeping abort can still proceed.
        """
        tmux_name = session.tmux_session
        try:
            await self._run_tmux("send-keys", "-t", tmux_name, "Escape")
            await asyncio.sleep(0.3)
            await self._run_tmux("send-keys", "-t", tmux_name, "C-c")
            logger.info(
                "Sent interrupt (Escape + single C-c) to session_id=%s tmux=%s role=%s",
                session.id,
                tmux_name,
                session.role.value,
            )
        except Exception:
            logger.exception(
                "Failed to send interrupt keys to session_id=%s tmux=%s",
                session.id,
                tmux_name,
            )

    async def _rename_session_for_task(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        *,
        updated_at: datetime | None = None,
    ) -> ManagedSession:
        title = task.title
        if session.title == title:
            return session

        try:
            updated_tab = await ttyd_manager.update_tab(session.tab_id, name=title)
        except Exception:
            logger.exception(
                "Failed to rename workspace terminal tab_id=%s session_id=%s task_id=%s",
                session.tab_id,
                session.id,
                task.id,
            )
        else:
            if not updated_tab:
                logger.warning(
                    "Could not rename missing workspace terminal tab_id=%s session_id=%s task_id=%s",
                    session.tab_id,
                    session.id,
                    task.id,
                )

        updated_session = session.model_copy(
            update={
                "title": title,
                "updated_at": updated_at or _wm._now(),
            }
        )
        self.sessions[session.id] = updated_session
        return updated_session

    def sessions_for_workspace(self, workspace_id: str) -> list[ManagedSession]:
        sessions = self._sessions_for_workspace_raw(workspace_id)
        return [self._with_assignment_summary(session) for session in sessions]

    def _sessions_for_workspace_raw(self, workspace_id: str) -> list[ManagedSession]:
        return [
            session for session in self.sessions.values() if session.workspace_id == workspace_id
        ]

    def _workspace_agents(
        self,
        workspace_id: str,
        include_stopped: bool = False,
    ) -> list[ManagedSession]:
        return [
            session
            for session in self._sessions_for_workspace_raw(workspace_id)
            if session.role == WorkspaceSessionRole.ORCHESTRATOR
            and (include_stopped or session.status != ManagedSessionStatus.STOPPED)
        ]

    def _dispatcher_session(self, workspace: Workspace) -> Optional[ManagedSession]:
        if workspace.dispatcher_session_id:
            session = self.sessions.get(workspace.dispatcher_session_id)
            if session and session.status != ManagedSessionStatus.STOPPED:
                return session
        for session in self._sessions_for_workspace_raw(workspace.id):
            if (
                session.role == WorkspaceSessionRole.DISPATCHER
                and session.status != ManagedSessionStatus.STOPPED
            ):
                self.workspaces[workspace.id] = workspace.model_copy(
                    update={"dispatcher_session_id": session.id, "updated_at": _wm._now()}
                )
                self._save_state()
                return session
        return None

    def _first_available_workspace_agent(self, workspace_id: str) -> Optional[ManagedSession]:
        agents = self._workspace_agents(workspace_id)
        return agents[0] if agents else None

    def _reviewer_is_active(self, task: WorkspaceTask) -> bool:
        """Return True when the task has a reviewer session that is actively
        working on its review. A missing, idle, or stopped reviewer means the
        prior dispatch got stuck and re-dispatch is allowed."""
        if not task.review_session_id:
            return False
        reviewer = self.sessions.get(task.review_session_id)
        if not reviewer:
            return False
        if reviewer.status == ManagedSessionStatus.STOPPED:
            return False
        if reviewer.runtime_status == AgentRuntimeStatus.IDLE:
            return False
        if reviewer.task_id != task.id and reviewer.current_task_id != task.id:
            return False
        return True

    def _reviewer_has_active_task_binding(self, session: ManagedSession) -> bool:
        return any(
            task.workspace_id == session.workspace_id
            and task.review_session_id == session.id
            and task.status in {WorkspaceTaskStatus.WORKING, WorkspaceTaskStatus.REVIEW}
            for task in self.tasks.values()
        )

    def _release_stale_reviewer_for_task(
        self, task: WorkspaceTask, *, updated_at: datetime
    ) -> None:
        """Clear the task's stale review_session_id reference and reset any
        reviewer sessions whose task_id/current_task_id still point at this
        task but are not actively working on it. Used to unstick review
        dispatch after a prompt send failure or reviewer crash."""
        # Release any reviewer session that still carries this task's id.
        self._release_reviewer_session(
            task,
            status=ManagedSessionStatus.IDLE,
            runtime_status=AgentRuntimeStatus.IDLE,
            updated_at=updated_at,
            include_stale_assignments=True,
        )
        # Clear the task's own stale reference so future dispatch succeeds.
        current = self.tasks.get(task.id)
        if not current:
            return
        if current.review_session_id and not self._reviewer_is_active(current):
            self.tasks[current.id] = current.model_copy(
                update={
                    "review_session_id": None,
                    "updated_at": updated_at,
                }
            )

    def _cleanup_stale_reviewer_assignments(self, workspace_id: str) -> bool:
        """Drop stale task_id/current_task_id values from REVIEWER sessions
        where the referenced task no longer exists, is not awaiting review,
        or does not list the session as its review_session_id. Returns True
        if any session was modified."""
        changed = False
        now = _wm._now()
        for session in self._sessions_for_workspace_raw(workspace_id):
            if session.role != WorkspaceSessionRole.REVIEWER:
                continue
            if not session.task_id and not session.current_task_id:
                continue
            candidate_id = session.task_id or session.current_task_id
            if not candidate_id:
                continue
            task = self.tasks.get(candidate_id)
            should_reset = False
            if not task or task.workspace_id != workspace_id:
                should_reset = True
            elif task.status == WorkspaceTaskStatus.DONE:
                should_reset = True
            elif task.review_session_id != session.id:
                # Session claims the task but the task does not reference this
                # session as its reviewer — the assignment is stale.
                should_reset = True
            elif (
                session.runtime_status == AgentRuntimeStatus.IDLE
                and session.status == ManagedSessionStatus.IDLE
            ):
                # An idle reviewer may still be intentionally bound to a task
                # after a terminal verdict. Only clear the fields when the task
                # itself is no longer in a protected working/review phase.
                should_reset = task.status not in {
                    WorkspaceTaskStatus.WORKING,
                    WorkspaceTaskStatus.REVIEW,
                }
            if not should_reset:
                continue
            logger.info(
                "Cleaning stale reviewer assignment session_id=%s task_id=%s",
                session.id,
                candidate_id,
            )
            self.sessions[session.id] = session.model_copy(
                update={
                    "task_id": None,
                    "current_task_id": None,
                    "status": ManagedSessionStatus.IDLE,
                    "runtime_status": AgentRuntimeStatus.IDLE,
                    "updated_at": now,
                    "last_activity_at": now,
                }
            )
            changed = True
        return changed

    def _review_dispatch_in_reaper_grace(self, task: WorkspaceTask, *, now: datetime) -> bool:
        """Return True when a review dispatch is recent enough that the
        reviewer may simply be slow to emit first tokens. The fallback
        reaper should not redispatch in this window even when
        ``_reviewer_is_active`` reports False, because the terminal
        classifier briefly reports IDLE between bursts of model output.

        We grant the grace based on whichever of the two timestamps is
        more recent: when the review was last requested, or when the
        assigned reviewer last had terminal activity recorded.
        """
        candidates: list[datetime] = []
        if task.review_requested_at:
            candidates.append(task.review_requested_at)
        if task.review_session_id:
            reviewer = self.sessions.get(task.review_session_id)
            if reviewer and reviewer.last_activity_at:
                candidates.append(reviewer.last_activity_at)
        if not candidates:
            return False
        latest = max(candidates)
        return (now - latest).total_seconds() < REVIEW_REAPER_DISPATCH_GRACE_SECONDS

    async def _reap_stuck_reviews(self, workspace_id: str) -> int:
        """Fallback reaper: find tasks whose review dispatch appears stuck
        (review-requested or REVIEW status with no active reviewer) and
        trigger a fresh review dispatch. Called from the periodic
        dispatch_workspace loop so transient failures do not permanently
        strand tasks in "Awaiting AI review".

        Returns the number of tasks that were re-dispatched."""
        reaped = 0
        for task in list(self.tasks.values()):
            if task.workspace_id != workspace_id:
                continue
            if task.status == WorkspaceTaskStatus.DONE:
                continue
            if task.system_internal:
                continue
            needs_review_dispatch = False
            if state_policy.review_in_flight(
                task.review_requested_at, task.review_completed_at
            ) and not self._reviewer_is_active(task):
                # Review was requested but the assigned reviewer is idle,
                # stopped, missing, or reassigned.
                needs_review_dispatch = True
            elif (
                task.status == WorkspaceTaskStatus.REVIEW
                and not task.review_completed_at
                and not task.human_acceptance_requested_at
                and not self._reviewer_is_active(task)
            ):
                # Task is in REVIEW state with no reviewer progress — a
                # reconciler or manual status transition set REVIEW without
                # actually dispatching a reviewer.
                needs_review_dispatch = True
            if not needs_review_dispatch:
                continue
            if not task.session_id:
                continue
            now = _wm._now()
            if self._review_dispatch_in_reaper_grace(task, now=now):
                # Reviewer was just dispatched (or has very recent terminal
                # activity). The terminal classifier can briefly report IDLE
                # while the model produces its first tokens — wait the
                # configured grace before declaring the dispatch stuck.
                logger.debug(
                    "Skipping fallback reaper for task_id=%s within dispatch grace",
                    task.id,
                )
                continue
            latest_state = self._latest_report_state(task.id)
            trigger_state = (
                latest_state
                if latest_state
                in {
                    AgentReportState.READY_FOR_REVIEW,
                    AgentReportState.COMPLETED,
                    AgentReportState.BLOCKED,
                    AgentReportState.NEEDS_INPUT,
                }
                else AgentReportState.READY_FOR_REVIEW
            )
            self._release_stale_reviewer_for_task(task, updated_at=now)
            trigger_report = AgentReport(
                id=str(uuid.uuid4()),
                workspace_id=task.workspace_id,
                task_id=task.id,
                session_id=task.session_id,
                state=trigger_state,
                message=(
                    "Re-dispatching stuck review task (fallback reaper); "
                    "prior reviewer dispatch did not complete."
                ),
                message_en=(
                    "Re-dispatching stuck review task (fallback reaper); "
                    "prior reviewer dispatch did not complete."
                ),
                message_zh=(
                    "重新分派卡住的 review 任务（fallback reaper）；" "之前的 reviewer 分派未完成。"
                ),
                changed_files=[],
                validation=None,
                risks=None,
                review_decision=ReviewDecision.REQUEST,
                review_reason="Stuck review recovered by background dispatcher.",
                risk_level=None,
                review_cycle=task.review_cycle,
                created_at=now,
            )
            self.reports[trigger_report.id] = trigger_report
            logger.info(
                "Reaping stuck review task_id=%s trigger_state=%s",
                task.id,
                trigger_state.value,
            )
            try:
                await self._request_task_review(self.tasks[task.id], trigger_report)
                reaped += 1
            except Exception:
                logger.exception("Failed to reap stuck review task_id=%s", task.id)
        return reaped

    def _first_available_reviewer(self, workspace_id: str) -> Optional[ManagedSession]:
        reviewers = [
            session
            for session in self._sessions_for_workspace_raw(workspace_id)
            if session.role == WorkspaceSessionRole.REVIEWER
            and session.status != ManagedSessionStatus.STOPPED
            and session.runtime_status == AgentRuntimeStatus.IDLE
            and not session.task_id
            and not session.current_task_id
            and not self._reviewer_has_active_task_binding(session)
        ]
        if not reviewers:
            return None
        return sorted(reviewers, key=lambda session: (session.ephemeral, session.created_at))[0]

    def _queued_count(self, session_id: str) -> int:
        return len(
            [
                task
                for task in self.tasks.values()
                if task.session_id == session_id
                and task.status == WorkspaceTaskStatus.QUEUED
                and not task.system_internal
            ]
        )

    def _with_assignment_summary(self, session: ManagedSession) -> ManagedSession:
        current_task_id = session.current_task_id or session.task_id
        current_task = self.tasks.get(current_task_id or "")
        visible_current_task_id = (
            None if current_task and current_task.system_internal else current_task_id
        )
        return session.model_copy(
            update={
                "task_id": (
                    None if current_task and current_task.system_internal else session.task_id
                ),
                "current_task_id": visible_current_task_id,
                "queued_count": self._queued_count(session.id),
            }
        )

    async def get_board(self, workspace_id: str) -> WorkspaceBoard:
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            raise KeyError(workspace_id)

        await self._refresh_session_statuses(workspace_id)
        self._reconcile_task_report_statuses(workspace_id)
        self._sync_workspace_tab_metadata(workspace_id)
        tasks = [
            task
            for task in self.tasks.values()
            if task.workspace_id == workspace_id and not task.system_internal
        ]
        sessions = self.sessions_for_workspace(workspace_id)
        reports = self.latest_reports_per_task_for_workspace(workspace_id)
        return WorkspaceBoard(
            workspace=self.workspaces[workspace_id],
            tasks=tasks,
            sessions=sessions,
            reports=reports,
            markdown_documents=self.markdown_documents_for_workspace(workspace_id),
            snapshot_path=str(self.snapshot_path(workspace_id)),
        )

    def _sync_workspace_tab_metadata(self, workspace_id: str) -> None:
        for session in self._sessions_for_workspace_raw(workspace_id):
            self._sync_session_tab_metadata(session)

    def _sync_session_tab_metadata(self, session: ManagedSession) -> None:
        workspace = self.workspaces.get(session.workspace_id)
        if not workspace:
            return
        ttyd_manager.set_tab_workspace_metadata(
            tab_id=session.tab_id,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            workspace_role=session.role,
        )
