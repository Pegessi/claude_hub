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

# Returned by a guard when the wrapped step raised or was cancelled by the
# per-step timeout. Distinguishes a guarded failure from a step whose real
# result is ``None``.
_GUARD_STEP_FAILED: Any = object()

# Buckets that neither logged a failure nor flushed suppressed counts within
# this many error-log windows are pruned every round, so a persistent failure
# on a scope whose entity was then deleted (session/workspace/task id churn)
# cannot keep its bucket alive for the monitor's lifetime.
_ERROR_LOG_IDLE_RETENTION_WINDOWS = 8


class _StepErrorLogState:
    """Rate-limit state for identical background monitor step failures.

    Keyed by ``(step, scope, failure_type)``. Kept local to the monitor loop
    via this object; idle buckets are prunable so per-scope identifier churn
    (session/task/workspace ids that fail and then disappear) cannot grow the
    map without bound.
    """

    def __init__(self) -> None:
        # key -> (last log monotonic, hidden repeats since that log)
        self._entries: dict[tuple[str, str, str], tuple[float, int]] = {}

    def record_failure(
        self, step: str, scope: str, failure_type: str, now: float
    ) -> tuple[bool, int]:
        """Record one failure. Returns ``(emit_log, suppressed_count)``.

        ``emit_log`` is False when an identical failure was logged within the
        current window; its repeat is counted instead. Otherwise the caller
        logs and ``suppressed_count`` is the number of repeats hidden since
        the previous log (0 for a fresh failure).
        """
        key = (step, scope, failure_type)
        # Negative-infinite baseline so the first failure (even at a monotonic
        # near zero) always passes the window check.
        logged_at, suppressed = self._entries.get(key, (float("-inf"), 0))
        if now - logged_at < WORKSPACE_MONITOR_ERROR_LOG_INTERVAL_SECONDS:
            self._entries[key] = (logged_at, suppressed + 1)
            return False, 0
        self._entries[key] = (now, 0)
        return True, suppressed

    def clear_scope(self, step: str, scope: str) -> None:
        """Evict every failure-type bucket for one ``(step, scope)``.

        A successful run resets suppression: the next failure must log a fresh
        traceback instead of being hidden as a repeat of a recovered outage.
        Stranded repeat counts disappear with the recovered outage.
        """
        for key in [key for key in self._entries if key[0] == step and key[1] == scope]:
            del self._entries[key]

    def prune_idle(self, now: float, retention_seconds: float) -> None:
        """Drop buckets inactive for longer than ``retention_seconds``."""
        for key, (logged_at, _suppressed) in [(key, value) for key, value in self._entries.items()]:
            if now - logged_at > retention_seconds:
                del self._entries[key]


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
        # Rate-limits identical step failures so a persistently broken step
        # cannot flood the log once per monitor interval; state dies with the
        # monitor task and idle per-scope buckets are pruned every round.
        error_log_state = _StepErrorLogState()

        def _log_step_error(step: str, scope: str, failure_type: str, *, timed_out: bool) -> None:
            emit_log, suppressed = error_log_state.record_failure(
                step, scope, failure_type, time.monotonic()
            )
            if not emit_log:
                return
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

        async def _guard_awaitable(step: str, scope: str, awaitable: Any) -> Any:
            try:
                result = await asyncio.wait_for(
                    awaitable, timeout=WORKSPACE_MONITOR_STEP_TIMEOUT_SECONDS
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                _log_step_error(step, scope, "timeout", timed_out=True)
                return _GUARD_STEP_FAILED
            except Exception as exc:
                _log_step_error(step, scope, type(exc).__name__, timed_out=False)
                return _GUARD_STEP_FAILED
            error_log_state.clear_scope(step, scope)
            return result

        def _guard_call(step: str, scope: str, func: Any, *args: Any) -> Any:
            try:
                result = func(*args)
            except Exception as exc:
                _log_step_error(step, scope, type(exc).__name__, timed_out=False)
                return _GUARD_STEP_FAILED
            error_log_state.clear_scope(step, scope)
            return result

        while True:
            error_log_state.prune_idle(
                time.monotonic(),
                WORKSPACE_MONITOR_ERROR_LOG_INTERVAL_SECONDS * _ERROR_LOG_IDLE_RETENTION_WINDOWS,
            )
            try:
                _guard_call(
                    "schedule_pending_stream_cleanup",
                    "",
                    ttyd_manager.schedule_pending_stream_cleanup,
                )
                # Session refresh mirrors _refresh_session_statuses (same
                # ordering, arguments and save points) but with a timeout
                # budget per session instead of one over the whole round:
                # the batched status fetch is shared I/O guarded globally, and
                # a hung per-session tmux/API wait must not starve every later
                # session on stable iteration order.
                refresh_sessions = self._monitor_refresh_sessions(None)
                statuses = await _guard_awaitable(
                    "refresh_session_statuses",
                    "phase=collect_agent_statuses",
                    self._collect_session_agent_statuses(refresh_sessions),
                )
                # A failed shared fetch aborts the downstream phases for this
                # round, exactly as an exception inside
                # _refresh_session_statuses would.
                if statuses is not _GUARD_STEP_FAILED:
                    refresh_changed = False
                    for session in refresh_sessions:
                        item_changed = await _guard_awaitable(
                            "refresh_session_statuses",
                            f"session_id={session.id}",
                            self._refresh_one_session_status(
                                session,
                                statuses,
                                run_auto_continue=True,
                            ),
                        )
                        refresh_changed = item_changed is True or refresh_changed
                    failure_scan_now = self._runtime_now()
                    failures_changed = _guard_call(
                        "refresh_session_statuses",
                        "phase=runtime_failure_scan",
                        self._mark_runtime_task_failures,
                        None,
                        failure_scan_now,
                    )
                    if refresh_changed or failures_changed is True:
                        _guard_call(
                            "refresh_session_statuses",
                            "phase=save_state",
                            self._save_state,
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
                # Resident agents: iterate exactly like _tick_resident_agents
                # (one ``now`` snapshot, same due filter and arguments) but give
                # each workspace its own timeout budget.
                resident_now = self._runtime_now()
                for workspace_id in list(self.workspaces):
                    resident_workspace = self.workspaces.get(workspace_id)
                    if resident_workspace is None or not self._resident_agent_due(
                        resident_workspace, resident_now
                    ):
                        continue
                    await _guard_awaitable(
                        "tick_resident_agents",
                        f"workspace_id={workspace_id}",
                        self._run_resident_agent(resident_workspace),
                    )
                # Scheduled tasks: mirror _tick_scheduled_tasks phase by phase.
                # The global recovery/sweep phases keep global guards, while
                # each due scheduled task gets its own budget so one hung task
                # cannot cancel the rest of the tick.
                if self._scheduled_chat_recovery_pending:
                    recovery = await _guard_awaitable(
                        "tick_scheduled_tasks",
                        "phase=recover_scheduled_chat_runs",
                        self._recover_scheduled_chat_runs(),
                    )
                    # Same reset point as _tick_scheduled_tasks: the flag is
                    # cleared only after the recovery awaitable returns, so a
                    # guarded failure leaves the retry pending next round.
                    if recovery is not _GUARD_STEP_FAILED:
                        self._scheduled_chat_recovery_pending = False
                scheduled_now = self._runtime_now()
                _guard_call(
                    "tick_scheduled_tasks",
                    "phase=disable_deleted_chat_targets",
                    self._disable_deleted_chat_targets,
                    scheduled_now,
                )
                await _guard_awaitable(
                    "tick_scheduled_tasks",
                    "phase=reap_stale_scheduled_chat_runs",
                    self._reap_stale_scheduled_chat_runs(scheduled_now),
                )
                for scheduled_task_id in list(self.scheduled_tasks.keys()):
                    scheduled_task = self.scheduled_tasks.get(scheduled_task_id)
                    if scheduled_task is None or not scheduled_task.enabled:
                        continue
                    if (
                        scheduled_task.next_run_at is None
                        or scheduled_task.next_run_at > scheduled_now
                    ):
                        continue
                    await _guard_awaitable(
                        "tick_scheduled_tasks",
                        f"task_id={scheduled_task_id}",
                        self._fire_scheduled_task(scheduled_task, scheduled_now),
                    )
                await _guard_awaitable(
                    "tick_scheduled_tasks",
                    "phase=converge_orphaned_hub_tasks",
                    self._converge_orphaned_hub_tasks(scheduled_now),
                )
                await _guard_awaitable(
                    "tick_scheduled_tasks",
                    "phase=drain_scheduled_chat_runs",
                    self._drain_scheduled_chat_runs(),
                )
                _guard_call("kick_feedback_automation", "", self._kick_feedback_automation)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Workspace background monitor failed")
            await asyncio.sleep(WORKSPACE_MONITOR_INTERVAL_SECONDS)
