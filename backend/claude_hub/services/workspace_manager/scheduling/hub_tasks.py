"""Scheduled-task implementation split from the compatibility mixin."""

from ...task_dependencies import require_task_dependencies
from .._constants import *  # noqa: F401,F403
from ._contract import _SchedulingHost


class _ScheduledHubTasksMixin(_SchedulingHost):
    @staticmethod
    def _is_scheduled_hub_task(task: Any) -> bool:
        return bool(
            getattr(task, "system_internal", False)
            and getattr(task, "internal_kind", None) == "scheduled"
        )

    def _hub_task_worker_is_dead(
        self,
        task: Any,
        session: Optional["ManagedSession"],
    ) -> bool:
        """A bound worker that can never ACK/report: stopped, offline, or gone.

        An OFFLINE runtime means the agent process is not running; STOPPED means
        the tmux/tab was torn down; ``None`` means the session row vanished. A
        merely WORKING/IDLE/ATTENTION session is alive and is left untouched, so
        a healthy long task (and one briefly at a prompt) is never reaped.
        """
        if session is None:
            return True
        if session.workspace_id != task.workspace_id:
            return True
        if session.status == ManagedSessionStatus.STOPPED:
            return True
        if session.runtime_status == AgentRuntimeStatus.OFFLINE:
            return True
        return False

    async def _converge_orphaned_hub_tasks(self, now: datetime) -> None:
        """Give WORKING scheduled hub_tasks with a dead worker a bounded exit.

        The normal recovery paths do not apply to a reviewed system task whose
        worker died before its first report: dispatch/recovery/migration require
        a QUEUED task, the failure reaper is subagent-only, an uncertain
        dispatch is fail-closed against an auto-resend to the SAME (dead)
        session, and the merged cold-wake only covers native ``chat_turn``. This
        sweep closes that hole.

        For each such task past an orphan grace, either bind a FRESH
        caller-owned ephemeral worker and redispatch (bounded by
        ``dispatch_attempt``), or — once the attempt budget is exhausted, or no
        replacement can be spawned — mark the task FAILED. In every branch the
        dead ephemeral session is cleaned up so neither a WORKING orphan nor a
        stopped session is left behind. A per-task lock prevents a double
        redispatch from overlapping ticks.
        """
        candidates = [
            task
            for task in self.tasks.values()
            if task.status == WorkspaceTaskStatus.WORKING
            and self._workspace_owns_task(task)
            and self._is_scheduled_hub_task(task)
        ]
        for task in candidates:
            bound_id = task.session_id
            if not bound_id:
                # Defensive: with the E1 binding fix a fired hub_task always has
                # a session_id. An unbound WORKING internal task is a legacy
                # artifact; leave it to manual recovery rather than guess.
                continue
            bound = self.sessions.get(bound_id)
            if not self._hub_task_worker_is_dead(task, bound):
                continue
            age = (now - task.updated_at).total_seconds()
            if age < HUBTASK_ORPHAN_GRACE_SECONDS:
                continue
            lock = self._hubtask_orphan_locks.setdefault(task.id, asyncio.Lock())
            try:
                await lock.acquire()
            except Exception:  # pragma: no cover - defensive
                continue
            try:
                await self._converge_one_orphaned_hub_task(task, now)
            except Exception:
                logger.exception(
                    "Scheduled hub_task orphan convergence failed task_id=%s",
                    task.id,
                )
            finally:
                lock.release()

    async def _converge_one_orphaned_hub_task(self, task: Any, now: datetime) -> None:
        # Re-read under the lock: an overlapping report / abort / dispatch may
        # have already moved the task off WORKING or changed its binding.
        live = self.tasks.get(task.id)
        if live is None or live.status != WorkspaceTaskStatus.WORKING:
            return
        if not self._workspace_owns_task(live):
            return
        if not self._is_scheduled_hub_task(live) or not live.session_id:
            return
        dead_session = self.sessions.get(live.session_id)
        if not self._hub_task_worker_is_dead(live, dead_session):
            return
        # Re-evaluate the grace against the authoritative live task under the
        # lock; the outer sweep's snapshot may predate a fresh progress update.
        if (now - live.updated_at).total_seconds() < HUBTASK_ORPHAN_GRACE_SECONDS:
            return

        if live.dispatch_attempt >= HUBTASK_ORPHAN_MAX_ATTEMPTS:
            await self._fail_orphaned_hub_task(
                live,
                dead_session,
                reason=(
                    "scheduled task worker died and the redispatch limit of "
                    f"{HUBTASK_ORPHAN_MAX_ATTEMPTS} attempts was exhausted"
                ),
                now=now,
            )
            return

        # Spawn a fresh throwaway worker BEFORE touching the dead one so a spawn
        # failure does not strand the task with no session at all.
        try:
            new_session = await self.ensure_workspace_agent(
                live.workspace_id,
                EnsureWorkspaceAgentRequest(
                    agent_type=live.agent_type,
                    ephemeral=True,
                    caller_owned_ephemeral=True,
                    role=WorkspaceSessionRole.ORCHESTRATOR,
                    reuse_existing=False,
                    # Carry the original launch preset onto the replacement so
                    # a redispatch does not silently fall back to the built-in
                    # agent default. A vanished session row leaves no preset
                    # record to recover.
                    env_preset=dead_session.env_preset if dead_session else None,
                ),
            )
        except Exception:
            logger.exception(
                "Could not spawn replacement worker for orphaned scheduled task "
                "task_id=%s; failing it",
                live.id,
            )
            await self._fail_orphaned_hub_task(
                live,
                dead_session,
                reason="scheduled task worker died and no replacement worker could be spawned",
                now=now,
            )
            return

        # Operator edits or a new binding during worker creation supersede
        # this recovery attempt. Do not overwrite or resume that newer contract.
        current = self.tasks.get(live.id)
        if (
            current is None
            or current.status != WorkspaceTaskStatus.WORKING
            or current.session_id != live.session_id
        ):
            await self._best_effort_delete_session(new_session.id)
            return
        live = current

        # Re-bind the task to the new worker and open a new dispatch attempt.
        # The fresh dispatch call_id (dispatch:{id}:{attempt}) has no uncertain
        # marker, so the fail-closed uncertain contract on the dead session is
        # never violated — we never auto-resend to that session.
        reattempt = live.dispatch_attempt + 1
        rebound = live.model_copy(
            update={
                "session_id": new_session.id,
                "status": WorkspaceTaskStatus.QUEUED,
                "queued_at": now,
                "dispatch_attempt": reattempt,
                "dispatch_reason": "scheduled worker died; redispatch to fresh worker",
                "dispatch_pending": False,
                "updated_at": now,
            }
        )
        self.tasks[live.id] = rebound
        self._save_state()
        logger.warning(
            "Redispatching orphaned scheduled task task_id=%s to fresh session_id=%s "
            "(dead session_id=%s, attempt=%s)",
            live.id,
            new_session.id,
            live.session_id,
            reattempt,
        )
        try:
            await self._dispatch_task_to_session(rebound, new_session)
        except Exception:
            logger.exception(
                "Redispatch of scheduled task task_id=%s to session_id=%s failed",
                live.id,
                new_session.id,
            )
            current = self.tasks.get(rebound.id)
            if (
                current is not None
                and current.status == WorkspaceTaskStatus.QUEUED
                and current.session_id == new_session.id
            ):
                self.tasks[current.id] = current.model_copy(
                    update={
                        "status": WorkspaceTaskStatus.FAILED,
                        "failure_reason": "scheduled task redispatch to a fresh worker failed",
                        "failed_at": self._runtime_now(),
                        "updated_at": self._runtime_now(),
                    }
                )
                self._save_state()
            await self._best_effort_delete_session(new_session.id)
            await self._best_effort_delete_session(live.session_id)
            return
        # Success: tear down the dead worker only after the task is canonically
        # bound to the live one, so delete_session sees no non-terminal referrer.
        await self._best_effort_delete_session(live.session_id)

    async def _fail_orphaned_hub_task(
        self,
        task: Any,
        dead_session: Optional["ManagedSession"],
        *,
        reason: str,
        now: datetime,
    ) -> None:
        current = self.tasks.get(task.id)
        if (
            current is None
            or current.status != WorkspaceTaskStatus.WORKING
            or current.session_id != task.session_id
        ):
            return
        task = current
        failed = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.FAILED,
                "failure_reason": reason,
                "failed_at": now,
                "human_acceptance_requested_at": now,
                "updated_at": now,
            }
        )
        self.tasks[task.id] = failed
        self._record_system_task_audit(
            task=failed,
            message=f"Scheduled task failed to converge: {reason}.",
            message_zh=f"定时任务无法收敛，已失败：{reason}。",
            validation=(
                f"dead_session_id={dead_session.id if dead_session else None}; "
                f"dispatch_attempt={task.dispatch_attempt}; orphan_grace="
                f"{HUBTASK_ORPHAN_GRACE_SECONDS}s"
            ),
            session_id=dead_session.id if dead_session else "system",
        )
        self._save_state()
        logger.warning(
            "Scheduled hub_task marked FAILED after orphan convergence task_id=%s reason=%s",
            task.id,
            reason,
        )
        if dead_session is not None:
            await self._best_effort_delete_session(dead_session.id)
