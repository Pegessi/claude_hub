"""Typed cross-responsibility contract for scheduling mixins."""

from abc import abstractmethod

from .._base import _WorkspaceManagerBase
from .._constants import *  # noqa: F401,F403


class _SchedulingHost(_WorkspaceManagerBase):
    @abstractmethod
    def _save_scheduled_tasks(self) -> None: ...

    @staticmethod
    @abstractmethod
    def _chat_run_active(run: ScheduledTaskRun) -> bool: ...

    @abstractmethod
    async def _drain_scheduled_chat_runs(self, tab_id: Optional[str] = None) -> None: ...

    @abstractmethod
    def _cancel_scheduled_task_runs(
        self,
        task_id: str,
        *,
        statuses: set[ScheduledTaskRunStatus],
        reason: str,
        now: datetime,
    ) -> int: ...

    @abstractmethod
    def _sync_task_from_run(self, run: ScheduledTaskRun) -> None: ...

    @abstractmethod
    async def _fire_scheduled_task(
        self, task: ScheduledTask, now: datetime, *, manual: bool = False
    ) -> Optional[ScheduledTaskRun]: ...

    @abstractmethod
    def _compute_next_run(self, task: ScheduledTask, after: datetime) -> Optional[datetime]: ...

    @abstractmethod
    def _queue_scheduled_chat_run(
        self, task: ScheduledTask, scheduled_for: datetime, now: datetime
    ) -> ScheduledTaskRun: ...

    @abstractmethod
    async def _recover_scheduled_chat_runs(self) -> None: ...

    @abstractmethod
    async def _reap_stale_scheduled_chat_runs(self, now: datetime) -> None: ...

    @abstractmethod
    async def _converge_orphaned_hub_tasks(self, now: datetime) -> None: ...

    @abstractmethod
    async def _ensure_scheduled_chat_runtime_ready(self, tab_id: str) -> bool: ...

    @abstractmethod
    def _schedule_chat_redrain(self, tab_id: str) -> None: ...

    @abstractmethod
    async def _best_effort_delete_session(self, session_id: str | None) -> None: ...
