"""Scheduled-task implementation split from the compatibility mixin."""

from .._constants import *  # noqa: F401,F403
from ._contract import _SchedulingHost


class _ScheduledChatRunsMixin(_SchedulingHost):
    _SCHEDULED_CHAT_ACTIVE_RUN_LIMIT = 100
    _SCHEDULED_CHAT_COLD_MAX_ATTEMPTS = 12
    _SCHEDULED_RUN_STALE_GRACE = timedelta(minutes=10)
    _SCHEDULED_REAP_RECHECK = timedelta(seconds=60)

    @staticmethod
    def _chat_run_active(run: ScheduledTaskRun) -> bool:
        return run.status in {
            ScheduledTaskRunStatus.QUEUED,
            ScheduledTaskRunStatus.WAITING,
            ScheduledTaskRunStatus.DISPATCHING,
            ScheduledTaskRunStatus.RUNNING,
        }

    def _queue_scheduled_chat_run(
        self, task: ScheduledTask, scheduled_for: datetime, now: datetime
    ) -> ScheduledTaskRun:
        """Create one durable Chat run for one schedule occurrence."""
        assert task.tab_id is not None
        assert task.message is not None
        run_uuid = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"claude-hub:scheduled-chat:{task.id}:{scheduled_for.isoformat()}",
        )
        existing = self.scheduled_task_runs.get(str(run_uuid))
        if existing is not None:
            # A deterministic id makes replaying the exact same occurrence
            # idempotent without collapsing distinct future occurrences.
            task.last_run_id = existing.id
            task.last_status = existing.status.value
            task.last_error = existing.error or existing.waiting_reason
            return existing
        # Supersede blocked occurrences: a target Chat accepts one turn at a
        # time, so while it is busy/archived every interval occurrence just
        # accumulates in the FIFO and would replay as a burst of identical,
        # stale prompts once it frees up. Mark every earlier occurrence of the
        # SAME task that was never dispatched (queued/waiting) as auditable
        # SKIPPED and keep only the newest. A DISPATCHING/RUNNING occurrence
        # (or another task's run on the same tab) is always retained.
        new_key = (scheduled_for, now, str(run_uuid))
        superseded = [
            candidate
            for candidate in self.scheduled_task_runs.values()
            if candidate.scheduled_task_id == task.id
            and candidate.dispatched_at is None
            and candidate.status in {ScheduledTaskRunStatus.QUEUED, ScheduledTaskRunStatus.WAITING}
            and (candidate.scheduled_for, candidate.queued_at, candidate.id) < new_key
        ]
        for older in superseded:
            older.status = ScheduledTaskRunStatus.SKIPPED
            older.completed_at = now
            older.waiting_reason = None
            older.error = "superseded by a newer occurrence of this schedule"
        run = ScheduledTaskRun(
            id=str(run_uuid),
            scheduled_task_id=task.id,
            tab_id=task.tab_id,
            client_turn_id=f"scheduled-{run_uuid}",
            message=task.message,
            scheduled_for=scheduled_for,
            queued_at=now,
        )
        active_for_tab = sum(
            1
            for candidate in self.scheduled_task_runs.values()
            if candidate.tab_id == task.tab_id and self._chat_run_active(candidate)
        )
        if active_for_tab >= self._SCHEDULED_CHAT_ACTIVE_RUN_LIMIT:
            run.status = ScheduledTaskRunStatus.SKIPPED
            run.completed_at = now
            run.error = (
                "target Chat backlog limit reached "
                f"({self._SCHEDULED_CHAT_ACTIVE_RUN_LIMIT} active runs)"
            )
        self.scheduled_task_runs[run.id] = run
        task.last_run_id = run.id
        task.last_status = run.status.value
        task.last_error = run.error
        return run

    async def _scheduled_turn_lifecycle(self, run: ScheduledTaskRun) -> tuple[bool, Optional[str]]:
        """Return (turn_started, completion_status) from the durable transcript."""
        from ...agent_stream.store import AgentStreamStore

        store = AgentStreamStore("terminal-tabs", f"terminal-tab-{run.tab_id}")
        cursor = -1
        started = False
        while True:
            page = await store.read_since(cursor, limit=5000)
            for event in page.events:
                if event.turn_id != run.client_turn_id:
                    continue
                if event.type.value == "turn_started":
                    started = True
                elif event.type.value == "turn_completed":
                    return started, str(event.payload.get("status") or "failed")
                elif event.type.value == "error":
                    # Every persisted ERROR edge is terminal-by-construction
                    # (tailer publishes it immediately before turn_completed).
                    # If a non-terminal error event is ever introduced, this
                    # (and cold-start recovery) would fail the turn early.
                    return started, "failed"
            if not page.has_more:
                return started, None
            cursor = page.next_sequence

    async def _recover_scheduled_chat_runs(self) -> None:
        """Reconcile dispatch markers left by a previous backend process."""
        changed = False
        for run in self.scheduled_task_runs.values():
            if run.status not in {
                ScheduledTaskRunStatus.DISPATCHING,
                ScheduledTaskRunStatus.RUNNING,
            }:
                continue
            started, completion = await self._scheduled_turn_lifecycle(run)
            if completion is not None:
                self._complete_scheduled_chat_run(run, completion)
            elif started:
                run.status = ScheduledTaskRunStatus.UNCERTAIN
                run.waiting_reason = None
                run.error = "backend restarted while this Chat turn was in flight"
                run.completed_at = self._runtime_now()
                self._sync_task_from_run(run)
            else:
                run.status = ScheduledTaskRunStatus.QUEUED
                run.waiting_reason = "recovered before provider dispatch"
                run.error = None
                self._sync_task_from_run(run)
            changed = True
        if changed:
            self._save_scheduled_tasks()

    async def _reap_stale_scheduled_chat_runs(self, now: datetime) -> None:
        """Live-process safety net for a run whose terminal edge was missed.

        Cold restart reconciles every DISPATCHING/RUNNING marker once, but a
        turn can also die while the backend keeps running (the provider
        process exits and is respawned, an orphan is terminalized, etc.). The
        normal completion observer should always fire in that case; this is the
        backstop when it does not.

        A run older than the grace period is reconciled against the durable
        transcript first (authoritative completion evidence). If no terminal
        edge exists, the injected live liveness probe decides: the provider
        still owns the turn -> leave it alone; otherwise mark it uncertain so
        the FIFO drain can dispatch the next queued occurrence instead of
        blocking forever.
        """
        candidates = [
            run
            for run in self.scheduled_task_runs.values()
            if run.status in {ScheduledTaskRunStatus.DISPATCHING, ScheduledTaskRunStatus.RUNNING}
        ]
        if not candidates:
            return
        # Drop throttle stamps for runs that have since gone terminal.
        self._sched_reap_checked_at = {
            run_id: stamp
            for run_id, stamp in self._sched_reap_checked_at.items()
            if (run := self.scheduled_task_runs.get(run_id)) is not None
            and self._chat_run_active(run)
        }
        changed = False
        affected_tabs: set[str] = set()
        for run in candidates:
            stamp = run.dispatched_at or run.queued_at
            if now - stamp < self._SCHEDULED_RUN_STALE_GRACE:
                continue
            last_check = self._sched_reap_checked_at.get(run.id)
            if last_check is not None and now - last_check < self._SCHEDULED_REAP_RECHECK:
                continue
            self._sched_reap_checked_at[run.id] = now
            # Scan the durable transcript WITHOUT the per-tab lock: a full
            # history can span many 5000-event pages, and the same lock gates
            # completion observers and dispatch for that tab. Re-check run
            # state under the lock before acting on the result.
            try:
                started, completion = await self._scheduled_turn_lifecycle(run)
            except Exception:
                logger.exception("Stale scheduled run %s transcript reconciliation failed", run.id)
                continue
            if completion is not None:
                async with self._scheduled_chat_tab_locks.setdefault(run.tab_id, asyncio.Lock()):
                    if run.status not in {
                        ScheduledTaskRunStatus.DISPATCHING,
                        ScheduledTaskRunStatus.RUNNING,
                    }:
                        continue
                    self._complete_scheduled_chat_run(run, completion)
                    changed = True
                    affected_tabs.add(run.tab_id)
                continue
            # Live probe (tailer send-lock, not the scheduler tab lock).
            liveness = await self._probe_scheduled_turn_liveness(run)
            if liveness in {"active", "terminalized", "unknown"}:
                # active: a turn owns the provider guard (this scheduled turn
                # or a newer unrelated manual turn — both mean "do not reap").
                # terminalized: the orphan was just repaired; its completion
                # observer is in flight. unknown: no tailer / a different
                # orphan open / probe error — fail safe and wait.
                continue
            # Only "dead" reaches here: provider idle, no open orphan.
            async with self._scheduled_chat_tab_locks.setdefault(run.tab_id, asyncio.Lock()):
                # Status may have changed during the scan/probe awaits.
                if run.status not in {
                    ScheduledTaskRunStatus.DISPATCHING,
                    ScheduledTaskRunStatus.RUNNING,
                }:
                    continue
                if started:
                    run.status = ScheduledTaskRunStatus.UNCERTAIN
                    run.error = "Chat turn stopped reporting while it was in flight"
                    run.completed_at = now
                else:
                    # Never started and the provider is idle: requeue so the
                    # drain delivers it again.
                    run.status = ScheduledTaskRunStatus.QUEUED
                    run.waiting_reason = "redelivered after a stalled dispatch"
                    run.error = None
                    run.completed_at = None
                self._sync_task_from_run(run)
                changed = True
                affected_tabs.add(run.tab_id)
        if changed:
            self._save_scheduled_tasks()
            for tab_id in affected_tabs:
                await self._drain_scheduled_chat_runs(tab_id)

    async def _probe_scheduled_turn_liveness(self, run: ScheduledTaskRun) -> str:
        """Ask the live Chat transport whether ``run``'s turn is still owned.

        Returns ``"active"`` (the provider guard is raised — by this turn or a
        newer unrelated one), ``"dead"`` (provider idle, no open orphan),
        ``"terminalized"`` (this run's orphan was just closed), or
        ``"unknown"`` (no probe / different orphan open / error), in which case
        the caller must be conservative and leave the run alone.
        """
        probe = self._scheduled_chat_liveness
        if probe is None:
            return "unknown"
        try:
            result = await probe(run.tab_id, run.client_turn_id)
        except Exception:
            logger.exception("Scheduled run liveness probe failed for tab=%s", run.tab_id)
            return "unknown"
        return result if result in {"active", "dead", "terminalized"} else "unknown"

    def configure_scheduled_chat_liveness(self, callback: ScheduledChatLiveness) -> None:
        """Inject the live Chat turn-liveness probe without importing the API."""
        self._scheduled_chat_liveness = callback

    def _cancel_scheduled_task_runs(
        self,
        task_id: str,
        *,
        statuses: set[ScheduledTaskRunStatus],
        reason: str,
        now: datetime,
    ) -> int:
        """Move matching active runs of one task to CANCELLED. Returns count."""
        count = 0
        for run in self.scheduled_task_runs.values():
            if run.scheduled_task_id == task_id and run.status in statuses:
                run.status = ScheduledTaskRunStatus.CANCELLED
                run.error = reason
                run.waiting_reason = None
                run.completed_at = now
                count += 1
        return count

    def _sync_task_from_run(self, run: ScheduledTaskRun) -> None:
        task = self.scheduled_tasks.get(run.scheduled_task_id)
        if task is None:
            return
        current = (
            self.scheduled_task_runs.get(task.last_run_id) if task.last_run_id is not None else None
        )
        if current is not None and current.id != run.id:
            current_key = (current.scheduled_for, current.queued_at, current.id)
            run_key = (run.scheduled_for, run.queued_at, run.id)
            if current_key > run_key:
                # An older FIFO entry can keep transitioning after a newer
                # occurrence is queued. Keep task-level "last" fields tied
                # to the newest occurrence; its own run record still captures
                # every transition.
                return
        task.last_run_id = run.id
        task.last_status = run.status.value
        task.last_error = run.error or run.waiting_reason
        task.updated_at = self._runtime_now()

    def _complete_scheduled_chat_run(self, run: ScheduledTaskRun, status: str) -> None:
        if status in {"completed", "success", "ok"}:
            run.status = ScheduledTaskRunStatus.COMPLETED
            run.error = None
        elif status == "cancelled":
            # The turn (or its backend runtime) was stopped/interrupted. This
            # is distinct from a model-side failure: surface it as cancelled
            # rather than a red "failed" fire.
            run.status = ScheduledTaskRunStatus.CANCELLED
            run.error = "Chat turn was cancelled or its backend runtime was lost"
        else:
            run.status = ScheduledTaskRunStatus.FAILED
            run.error = f"Chat turn completed with status: {status}"
        run.completed_at = self._runtime_now()
        run.waiting_reason = None
        self._sync_task_from_run(run)

    async def on_scheduled_chat_turn_completed(
        self, tab_id: str, turn_id: str, status: str
    ) -> None:
        """Advance a scheduled run after its persisted Chat completion edge."""
        lock = self._scheduled_chat_tab_locks.setdefault(tab_id, asyncio.Lock())
        async with lock:
            run = next(
                (
                    candidate
                    for candidate in self.scheduled_task_runs.values()
                    if candidate.tab_id == tab_id and candidate.client_turn_id == turn_id
                ),
                None,
            )
            if run is None or not self._chat_run_active(run):
                return
            self._complete_scheduled_chat_run(run, status)
            self._save_scheduled_tasks()
        await self._drain_scheduled_chat_runs(tab_id)

    async def _drain_scheduled_chat_runs(self, tab_id: Optional[str] = None) -> None:
        tab_ids = (
            [tab_id]
            if tab_id is not None
            else sorted(
                {
                    run.tab_id
                    for run in self.scheduled_task_runs.values()
                    if self._chat_run_active(run)
                }
            )
        )
        for current_tab_id in tab_ids:
            await self._drain_scheduled_chat_tab(current_tab_id)

    async def _drain_scheduled_chat_tab(self, tab_id: str) -> None:
        lock = self._scheduled_chat_tab_locks.setdefault(tab_id, asyncio.Lock())
        async with lock:
            cancelled_any = False
            while True:
                active = sorted(
                    (
                        run
                        for run in self.scheduled_task_runs.values()
                        if run.tab_id == tab_id and self._chat_run_active(run)
                    ),
                    key=lambda run: (run.scheduled_for, run.queued_at, run.id),
                )
                if not active:
                    if cancelled_any:
                        self._save_scheduled_tasks()
                    return
                if any(
                    run.status
                    in {ScheduledTaskRunStatus.DISPATCHING, ScheduledTaskRunStatus.RUNNING}
                    for run in active
                ):
                    if cancelled_any:
                        self._save_scheduled_tasks()
                    return
                run = active[0]
                task = self.scheduled_tasks.get(run.scheduled_task_id)
                if task is None:
                    run.status = ScheduledTaskRunStatus.CANCELLED
                    run.error = "scheduled task was deleted"
                    run.completed_at = self._runtime_now()
                    cancelled_any = True
                    continue
                if not task.enabled:
                    # Disabling an automation is a Stop: queued occurrences must
                    # not fire when the queue drains (e.g. once a previously
                    # running turn finally ends). Nothing is DISPATCHING/RUNNING
                    # here (early return above), so all active runs are pending.
                    self._cancel_scheduled_task_runs(
                        task.id,
                        statuses={
                            ScheduledTaskRunStatus.QUEUED,
                            ScheduledTaskRunStatus.WAITING,
                        },
                        reason="automation is disabled",
                        now=self._runtime_now(),
                    )
                    cancelled_any = True
                    continue
                break
            tab = ttyd_manager.get_tab(tab_id)
            if tab is None:
                reason = "target Chat was deleted"
                run.status = ScheduledTaskRunStatus.SKIPPED
                run.error = reason
                run.completed_at = self._runtime_now()
                task.enabled = False
                # The target is gone: every queued occurrence is unfulfillable.
                self._cancel_scheduled_task_runs(
                    task.id,
                    statuses={
                        ScheduledTaskRunStatus.QUEUED,
                        ScheduledTaskRunStatus.WAITING,
                        ScheduledTaskRunStatus.DISPATCHING,
                        ScheduledTaskRunStatus.RUNNING,
                    },
                    reason=reason,
                    now=self._runtime_now(),
                )
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()
                return
            if tab.session_kind != SessionKind.CHAT or tab.workspace_role is not None:
                reason = "target is no longer a top-level Chat session"
                run.status = ScheduledTaskRunStatus.SKIPPED
                run.error = reason
                run.completed_at = self._runtime_now()
                task.enabled = False
                self._cancel_scheduled_task_runs(
                    task.id,
                    statuses={ScheduledTaskRunStatus.QUEUED, ScheduledTaskRunStatus.WAITING},
                    reason=reason,
                    now=self._runtime_now(),
                )
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()
                return
            if tab.agent_type != task.agent_type:
                reason = "target Chat backend changed; edit the automation to confirm it"
                run.status = ScheduledTaskRunStatus.SKIPPED
                run.error = reason
                run.completed_at = self._runtime_now()
                task.enabled = False
                # Re-confirmation required: do not replay the queued backlog
                # against the new backend after the user edits the task.
                self._cancel_scheduled_task_runs(
                    task.id,
                    statuses={ScheduledTaskRunStatus.QUEUED, ScheduledTaskRunStatus.WAITING},
                    reason=reason,
                    now=self._runtime_now(),
                )
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()
                return
            if tab.archived:
                run.status = ScheduledTaskRunStatus.WAITING
                run.waiting_reason = "target Chat is archived"
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()
                return

            from ...goal_run import get_goal_admission_lock, get_goal_manager

            async with get_goal_admission_lock(tab_id):
                goal = get_goal_manager().current(tab_id)
                if goal is not None and (
                    goal.status.value == "active" or goal.dispatch_state.value != "idle"
                ):
                    run.status = ScheduledTaskRunStatus.WAITING
                    run.waiting_reason = "waiting for the active Goal"
                    self._sync_task_from_run(run)
                    self._save_scheduled_tasks()
                    return

                dispatch = self._scheduled_chat_dispatch
                if dispatch is None:
                    run.status = ScheduledTaskRunStatus.WAITING
                    run.waiting_reason = "Chat dispatcher is starting"
                    self._sync_task_from_run(run)
                    self._save_scheduled_tasks()
                    return
                # Cold-runtime gate. A Chat whose native provider has never been
                # spawned (or was idle-reaped) has no ready runtime; delivering
                # into that state attaches the turn to a subprocess that cannot
                # produce it and the tailer later terminalizes it as
                # "backend runtime was no longer available". Ensure the runtime
                # is up first; if it is still starting, park the run WAITING with
                # a bounded redrain instead of cancelling it.
                ready = await self._ensure_scheduled_chat_runtime_ready(tab_id)
                if not ready:
                    if run.dispatch_attempts >= self._SCHEDULED_CHAT_COLD_MAX_ATTEMPTS:
                        run.status = ScheduledTaskRunStatus.FAILED
                        run.waiting_reason = None
                        run.error = (
                            "Chat runtime did not become ready "
                            f"after {self._SCHEDULED_CHAT_COLD_MAX_ATTEMPTS} cold-start attempts"
                        )
                        run.completed_at = self._runtime_now()
                        self._sync_task_from_run(run)
                        self._save_scheduled_tasks()
                        return
                    run.dispatch_attempts += 1
                    run.status = ScheduledTaskRunStatus.WAITING
                    run.waiting_reason = "waiting for the Chat runtime to start"
                    run.error = None
                    self._sync_task_from_run(run)
                    self._save_scheduled_tasks()
                    self._schedule_chat_redrain(tab_id)
                    return
                run.status = ScheduledTaskRunStatus.DISPATCHING
                run.waiting_reason = None
                run.error = None
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()
                try:
                    await dispatch(run, task)
                except RuntimeError as exc:
                    if "turn is already in flight" in str(exc):
                        run.status = ScheduledTaskRunStatus.WAITING
                        run.waiting_reason = "waiting for the current Chat response"
                        run.error = None
                    else:
                        run.status = ScheduledTaskRunStatus.FAILED
                        run.error = str(exc)
                        run.completed_at = self._runtime_now()
                except Exception as exc:
                    run.status = ScheduledTaskRunStatus.FAILED
                    run.error = str(exc)
                    run.completed_at = self._runtime_now()
                else:
                    # A very fast provider may complete before the send
                    # acknowledgement returns. Preserve that terminal evidence.
                    if run.status == ScheduledTaskRunStatus.DISPATCHING:
                        run.status = ScheduledTaskRunStatus.RUNNING
                        run.dispatched_at = self._runtime_now()
                self._sync_task_from_run(run)
                self._save_scheduled_tasks()
