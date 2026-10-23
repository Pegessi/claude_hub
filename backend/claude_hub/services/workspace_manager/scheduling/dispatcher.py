"""Scheduled-task implementation split from the compatibility mixin."""

from claude_hub.services.task_transitions import apply_task_update

from ...task_dependencies import require_task_dependencies
from .._constants import *  # noqa: F401,F403
from ._contract import _SchedulingHost


class _ScheduleDispatcherMixin(_SchedulingHost):
    _FIRE_COOLDOWN = timedelta(seconds=1)

    async def _tick_scheduled_tasks(self) -> None:
        if self._scheduled_chat_recovery_pending:
            await self._recover_scheduled_chat_runs()
            self._scheduled_chat_recovery_pending = False
        now = self._runtime_now()
        self._disable_deleted_chat_targets(now)
        await self._reap_stale_scheduled_chat_runs(now)
        for task_id in list(self.scheduled_tasks.keys()):
            task = self.scheduled_tasks.get(task_id)
            if task is None or not task.enabled:
                continue
            if task.next_run_at is None or task.next_run_at > now:
                continue
            try:
                await self._fire_scheduled_task(task, now)
            except Exception:
                logger.exception("Scheduled task tick failed for task_id=%s", task_id)
        # Converge scheduled hub_tasks whose bound worker died before reporting
        # (stopped/offline/missing past the orphan grace): bounded redispatch to
        # a fresh throwaway worker, or FAILED + ephemeral cleanup. Runs after the
        # monitor's status refresh so session liveness is current.
        try:
            await self._converge_orphaned_hub_tasks(now)
        except Exception:
            logger.exception("Scheduled hub_task orphan convergence sweep failed")
        await self._drain_scheduled_chat_runs()

    def _disable_deleted_chat_targets(self, now: datetime) -> None:
        changed = False
        for task in self.scheduled_tasks.values():
            if (
                task.kind != ScheduledTaskKind.CHAT_TURN
                or not task.enabled
                or not task.tab_id
                or ttyd_manager.get_tab(task.tab_id) is not None
            ):
                continue
            task.enabled = False
            task.last_status = ScheduledTaskRunStatus.SKIPPED.value
            task.last_error = "target Chat was deleted"
            task.updated_at = now
            changed = True
        if changed:
            self._save_scheduled_tasks()

    async def _fire_scheduled_task(
        self, task: ScheduledTask, now: datetime, *, manual: bool = False
    ) -> Optional[ScheduledTaskRun]:
        lock = self._sched_fire_locks.setdefault(task.id, asyncio.Lock())
        async with lock:
            # Updates replace schedule objects while this caller waits.
            current = self.scheduled_tasks.get(task.id)
            if current is None:
                if manual:
                    raise KeyError(task.id)
                return None
            task = current

            # Re-check eligibility under the lock: a concurrent fire (the 5s
            # tick vs a manual run-now, or two run-now clicks) may have already
            # stamped this task. Without this re-check a one-shot could fire
            # twice and run_count could double-increment.
            #
            # The cooldown below is what prevents a *recurring* task from
            # double-firing on concurrent manual run-now calls: such a task
            # stays enabled after firing, so the ``enabled`` re-check does not
            # catch the second call, and the ``next_run_at`` re-check is
            # tick-only (it does not apply to the manual path).
            if task.last_run_at is not None and (now - task.last_run_at) < self._FIRE_COOLDOWN:
                # A concurrent fire advanced this task within the cooldown
                # window while we waited for the lock. Join it: don't re-fire.
                return (
                    self.scheduled_task_runs.get(task.last_run_id)
                    if task.kind == ScheduledTaskKind.CHAT_TURN and task.last_run_id
                    else None
                )
            if not task.enabled:
                if manual:
                    raise ValueError(f"Scheduled task '{task.id}' is disabled")
                return None
            if not manual and (task.next_run_at is None or task.next_run_at > now):
                # Already advanced by a concurrent manual fire.
                return None

            scheduled_for = now if manual else (task.next_run_at or now)
            # Stamp BEFORE the side effect (crash-idempotent, same pattern as
            # resident agents): persist last_run_at / run_count / next_run_at
            # first so a crash does not re-fire or respawn. One-shot tasks are
            # disabled after firing.
            task.last_run_at = now
            task.run_count += 1
            task.updated_at = now
            if task.run_at is not None:
                task.enabled = False
                task.next_run_at = None
            else:
                task.next_run_at = self._compute_next_run(task, now)
            chat_run: Optional[ScheduledTaskRun] = None
            if task.kind == ScheduledTaskKind.CHAT_TURN:
                chat_run = self._queue_scheduled_chat_run(task, scheduled_for, now)
            self._save_scheduled_tasks()

            try:
                if task.kind == ScheduledTaskKind.CHAT_TURN:
                    assert chat_run is not None
                    await self._drain_scheduled_chat_runs(task.tab_id)
                elif task.kind == ScheduledTaskKind.TAB_MESSAGE:
                    await self._send_tab_message(task.tab_id, task.message)
                elif task.kind == ScheduledTaskKind.NEW_SESSION:
                    await self._fire_new_session(task)
                elif task.kind == ScheduledTaskKind.HUB_TASK:
                    await self._fire_hub_task(task)
                if task.kind != ScheduledTaskKind.CHAT_TURN:
                    task.last_status = "ok"
                    task.last_error = None
            except Exception as exc:
                logger.exception("Scheduled task %s failed to fire", task.id)
                task.last_status = "error"
                task.last_error = str(exc)

            task.updated_at = self._runtime_now()
            self._save_scheduled_tasks()
            return chat_run

    async def _send_tab_message(self, tab_id: Optional[str], message: Optional[str]) -> None:
        """Type ``message`` into a terminal tab's pane and submit it (Enter).

        Used by the ``tab_message`` scheduled-task kind. Every tab (plain
        terminal or managed agent) owns a tmux session named
        ``claude-hub-<tab_id[:8]>``, so the same tmux paste path that
        delivers managed-session messages can target any tab.

        The fields are ``Optional`` because the durable schema allows nulls;
        create/update validation guarantees they are set for this kind, and
        the guards below re-check defensively.

        Delivery is best-effort: the message is pasted and submitted, but
        there is no worker-ACK state machine (unlike
        ``send_session_message``). A tab whose tmux session is gone raises,
        which the fire path records as ``last_status=error``.
        """
        from ...ttyd_manager import _tmux_session_name

        if not tab_id:
            raise ValueError("tab_id is required")
        if not message:
            raise ValueError("message is required")
        if ttyd_manager.get_tab(tab_id) is None:
            raise ValueError(f"Terminal tab '{tab_id}' not found")
        await self._send_tmux_message(_tmux_session_name(tab_id), message)

    async def _best_effort_delete_session(self, session_id: str | None) -> None:
        """Best-effort teardown of an ephemeral session; never raises."""
        if not session_id:
            return
        try:
            await self.delete_session(session_id)
        except Exception:
            logger.exception("Best-effort delete of scheduled-task session %s failed", session_id)

    def _resolve_fire_env_preset(self, task: ScheduledTask) -> Optional[str]:
        """Return the task's env preset, re-validating at fire time.

        The preset was resolved when the schedule was created or edited, but a
        custom preset can be deleted or corrupted afterwards. Fail the fire
        with a message that names the schedule and preset so ``last_error``
        explains why no worker was spawned.
        """
        if not task.env_preset:
            return task.env_preset
        from ...env_preset_resolver import (
            EnvPresetNotFoundError,
            EnvPresetParseError,
            resolve_env_preset,
        )

        try:
            resolve_env_preset(task.env_preset)
        except (EnvPresetNotFoundError, EnvPresetParseError) as exc:
            raise ValueError(
                f"scheduled task {task.name!r} references env preset "
                f"{task.env_preset!r} that is no longer available; edit the schedule "
                f"to choose a valid preset ({exc})"
            ) from None
        return task.env_preset

    async def _fire_new_session(self, task: ScheduledTask) -> None:
        """Create a new session in the workspace and send it the message."""
        env_preset = self._resolve_fire_env_preset(task)
        assert task.workspace_id is not None
        assert task.message is not None
        session = await self.ensure_workspace_agent(
            task.workspace_id,
            EnsureWorkspaceAgentRequest(
                agent_type=task.agent_type,
                ephemeral=True,
                caller_owned_ephemeral=True,
                role=WorkspaceSessionRole.ORCHESTRATOR,
                reuse_existing=False,
                env_preset=env_preset,
            ),
        )
        try:
            await self.send_session_message(session.id, task.message)
        except Exception:
            # The send failed; tear down the just-created ephemeral session so
            # a fire failure does not strand an idle orchestrator.
            await self._best_effort_delete_session(session.id)
            raise

    async def _fire_hub_task(self, task: ScheduledTask) -> None:
        """Publish a system-internal task on a caller-owned ephemeral orchestrator.

        The task runs through the normal reviewed-task flow; when the worker
        reports completion, ``_handle_internal_task_report`` marks it DONE
        (skipping human review) and the auto-cleanup hook deletes the ephemeral
        session so no agent / reviewer resources are held.
        """
        now = self._runtime_now()
        env_preset = self._resolve_fire_env_preset(task)
        # Validation at create/update time guarantees task_title and message are
        # set for hub_task; assert so the type checker sees non-Optional str.
        assert task.workspace_id is not None
        assert task.task_title is not None
        assert task.message is not None
        internal_task = self._create_task(
            task.workspace_id,
            WorkspaceTaskCreate(
                title=task.task_title,
                prompt=task.message,
                task_mode=WorkspaceTaskMode.REVIEWED,
                execution_complexity=WorkspaceTaskExecutionComplexity.AUTO,
                agent_type=task.agent_type,
            ),
            system_internal=True,
            internal_kind="scheduled",
        )
        session = await self.ensure_workspace_agent(
            task.workspace_id,
            EnsureWorkspaceAgentRequest(
                agent_type=task.agent_type,
                ephemeral=True,
                caller_owned_ephemeral=True,
                role=WorkspaceSessionRole.ORCHESTRATOR,
                reuse_existing=False,
                env_preset=env_preset,
            ),
        )
        try:
            # Agent creation yields while the task is still editable. Validate
            # and transition the current contract, never the pre-await copy.
            internal_task = self.tasks[internal_task.id]
            if internal_task.status != WorkspaceTaskStatus.TODO:
                raise RuntimeError("Scheduled task changed while its worker was being created")
            require_task_dependencies(self.tasks, internal_task)
            internal_task = apply_task_update(
                internal_task,
                {
                    "status": WorkspaceTaskStatus.QUEUED,
                    "queued_at": now,
                    "dispatch_attempt": internal_task.dispatch_attempt + 1,
                    "dispatch_reason": "scheduled",
                    "updated_at": now,
                },
                cause="scheduled",
            )
            self.tasks[internal_task.id] = internal_task
            await self._dispatch_task_to_session(internal_task, session)
        except Exception as exc:
            # Preserve edits made during async preparation/dispatch, including
            # dependencies. A concurrent terminal report or reassignment wins.
            try:
                current = self.tasks.get(internal_task.id)
                if (
                    current is not None
                    and current.status in (WorkspaceTaskStatus.TODO, WorkspaceTaskStatus.QUEUED)
                    and current.session_id in (None, session.id)
                ):
                    self.tasks[current.id] = apply_task_update(
                        current,
                        {
                            "status": WorkspaceTaskStatus.FAILED,
                            "failure_reason": str(exc),
                            "failed_at": self._runtime_now(),
                            "updated_at": self._runtime_now(),
                        },
                        cause="scheduled",
                    )
                    self._save_state()
            finally:
                # Persistence failure must not skip cleanup. delete_session's
                # non-terminal-reference guard protects a reassigned worker.
                await self._best_effort_delete_session(session.id)
            raise
