"""Compatibility mixin and prompt exports for workspace lifecycle."""

import time

from claude_hub.services.workspace_manager._base import _WorkspaceManagerBase
from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403
from claude_hub.services.workspace_manager.workspaces.cleanup import _WorkspaceCleanupMixin
from claude_hub.services.workspace_manager.workspaces.lifecycle import _WorkspaceLifecycleMixin
from claude_hub.services.workspace_manager.workspaces.prompts import (
    _build_resident_master_prompt as _build_resident_master_prompt,
)
from claude_hub.services.workspace_manager.workspaces.prompts import (
    _build_task_graph_block as _build_task_graph_block,
)
from claude_hub.services.workspace_manager.workspaces.prompts import (
    _render_periodic_tasks_block as _render_periodic_tasks_block,
)
from claude_hub.services.workspace_manager.workspaces.prompts import (
    build_resident_agent_prompt as build_resident_agent_prompt,
)
from claude_hub.services.workspace_manager.workspaces.residency import _ResidentAgentMixin


class _WorkspacesMixin(
    _WorkspaceLifecycleMixin, _ResidentAgentMixin, _WorkspaceCleanupMixin, _WorkspaceManagerBase
):
    _monitor_task: "asyncio.Task[None] | None"
    tasks: "dict[str, WorkspaceTask]"
    sessions: "dict[str, ManagedSession]"
    reports: "dict[str, AgentReport]"

    def list_workspaces(self) -> list[Workspace]:
        def _activity_key(item: Workspace) -> tuple[float, str]:
            latest_task_at = max(
                (task.updated_at for task in self.tasks.values() if task.workspace_id == item.id),
                default=item.updated_at,
            )
            activity_at = max(item.updated_at, latest_task_at)
            return (-activity_at.timestamp(), item.id)

        return sorted(self.workspaces.values(), key=_activity_key)

    def start_background_monitor(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.create_task(self._background_monitor_loop())

    async def stop_background_monitor(self) -> None:
        if not self._monitor_task:
            await self._stop_feedback_automation()
            return
        self._monitor_task.cancel()
        try:
            await self._monitor_task
        except asyncio.CancelledError:
            pass
        finally:
            self._monitor_task = None
            await self._stop_monitor_jobs()
            await self._stop_feedback_automation()

    async def _stop_monitor_jobs(self) -> None:
        """Cancel notice delivery started by the monitor."""

        jobs = list(self._task_notice_jobs.values())
        self._task_notice_jobs.clear()
        for job in jobs:
            job.cancel()
        for job in jobs:
            try:
                await job
            except BaseException:
                pass

    async def _background_monitor_loop(self) -> None:
        # (step, scope, failure_type) -> (last log monotonic, hidden repeats).
        # Kept local to the loop so a persistently broken step cannot flood the
        # log once per monitor interval; state dies with the monitor task.
        error_log_state: dict[tuple[str, str, str], tuple[float, int]] = {}

        def _log_step_error(step: str, scope: str, failure_type: str, *, timed_out: bool) -> None:
            now = time.monotonic()
            key = (step, scope, failure_type)
            logged_at, suppressed = error_log_state.get(key, (0.0, 0))
            if now - logged_at < WORKSPACE_MONITOR_ERROR_LOG_INTERVAL_SECONDS:
                error_log_state[key] = (logged_at, suppressed + 1)
                return
            error_log_state[key] = (now, 0)
            scope_suffix = f" {scope}" if scope else ""
            detail = (
                f" ({suppressed} prior identical failure log(s) suppressed)" if suppressed else ""
            )
            if timed_out:
                logger.warning(
                    "Workspace background monitor step %s%s timed out after %s seconds%s",
                    step,
                    scope_suffix,
                    WORKSPACE_MONITOR_STEP_TIMEOUT_SECONDS,
                    detail,
                )
            else:
                logger.exception(
                    "Workspace background monitor step %s%s failed%s",
                    step,
                    scope_suffix,
                    detail,
                )

        async def _guard_awaitable(step: str, scope: str, awaitable: Any) -> None:
            try:
                await asyncio.wait_for(awaitable, timeout=WORKSPACE_MONITOR_STEP_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                _log_step_error(step, scope, "timeout", timed_out=True)
            except Exception as exc:
                _log_step_error(step, scope, type(exc).__name__, timed_out=False)

        def _guard_call(step: str, scope: str, func: Any, *args: Any) -> None:
            try:
                func(*args)
            except Exception as exc:
                _log_step_error(step, scope, type(exc).__name__, timed_out=False)

        while True:
            try:
                _guard_call(
                    "schedule_pending_stream_cleanup",
                    "",
                    ttyd_manager.schedule_pending_stream_cleanup,
                )
                await _guard_awaitable(
                    "refresh_session_statuses",
                    "",
                    self._refresh_session_statuses(run_auto_continue=True),
                )
                # Expire stale processing call_ids for sessions whose tmux
                # inbox is gone (STOPPED). A call_id in processing means the
                # pump successfully sent it to tmux; for live sessions the
                # message is already in the tmux input buffer and will be
                # ACKed by the worker, so we must NOT re-deliver it (that
                # would produce a duplicate turn). Only when the tmux session
                # itself is destroyed is the input buffer lost, at which
                # point expiry moves the stranded call_ids back to pending
                # for re-delivery.
                for session_id in list(self.sessions):
                    _guard_call(
                        "expire_processing_leases",
                        f"session_id={session_id}",
                        self._expire_processing_leases,
                        session_id,
                    )
                # Receipt-based cold recovery for live sessions: query the
                # tmux-server receipt for each processing call_id. If the
                # receipt is absent, the paste never ran and we can safely
                # return the call_id to pending. If the session is gone,
                # move to uncertain.
                for session_id in list(self.sessions):
                    sess = self.sessions.get(session_id)
                    if sess is None or sess.status == ManagedSessionStatus.STOPPED:
                        continue
                    if not sess.processing_call_ids:
                        continue
                    await _guard_awaitable(
                        "recover_processing_via_receipt",
                        f"session_id={session_id}",
                        self._recover_processing_via_receipt(session_id),
                    )
                # Pump any pending call_ids that were not yet delivered to
                # tmux (e.g. the pump failed on a previous cycle, or a new
                # message was enqueued). This is the live counterpart to the
                # cold-recovery pump in recover_pending_runs.
                for session_id in list(self.sessions):
                    sess = self.sessions.get(session_id)
                    if sess is None or not sess.pending_call_ids:
                        continue
                    await _guard_awaitable(
                        "pump_session_messages",
                        f"session_id={session_id}",
                        self._pump_session_messages(session_id),
                    )
                for workspace_id in list(self.workspaces):
                    # dispatch and lifecycle are guarded independently: a
                    # failing dispatch for one workspace must skip neither its
                    # lifecycle observation nor any other workspace.
                    await _guard_awaitable(
                        "dispatch_workspace",
                        f"workspace_id={workspace_id}",
                        self.dispatch_workspace(workspace_id, refresh_sessions=False),
                    )
                    await _guard_awaitable(
                        "tick_task_lifecycles",
                        f"workspace_id={workspace_id}",
                        self._tick_task_lifecycles(workspace_id),
                    )
                await _guard_awaitable("tick_resident_agents", "", self._tick_resident_agents())
                await _guard_awaitable("tick_scheduled_tasks", "", self._tick_scheduled_tasks())
                _guard_call("kick_feedback_automation", "", self._kick_feedback_automation)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Workspace background monitor failed")
            await asyncio.sleep(WORKSPACE_MONITOR_INTERVAL_SECONDS)
