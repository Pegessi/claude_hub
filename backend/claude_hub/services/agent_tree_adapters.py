"""Executor adapters for the Agent Tree coordination layer.

Each adapter wraps a different way of executing an agent run:
- ``ManagedTaskAdapter``: the existing workspace task/session/report flow.
- ``NativeSubagentAdapter``: future in-process subagent (stub with contract).
- ``ExternalJobAdapter``: future remote/third-party job (stub with contract).

The adapter interface is intentionally small: ``spawn``, ``send_message``,
``followup``, ``interrupt``, and ``get_status``. The Hub owns lifecycle
state; adapters translate Hub actions into executor-specific calls and
translate executor events back into ``AgentEvent`` entries.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from claude_hub.models.agent_tree import AgentRun, AgentRunStatus
    from claude_hub.services.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


class ExecutorAdapter(ABC):
    """Base interface for executing an agent run."""

    @abstractmethod
    async def spawn(self, run: "AgentRun", initial_message: str) -> str:
        """Start the executor for ``run`` with the initial task message.

        Returns the executor's context reference (e.g. workspace task id)
        which is stored on the run as ``context_ref``.
        """

    @abstractmethod
    async def send_message(self, run: "AgentRun", message: str) -> None:
        """Deliver a one-way message to the executor without waking it."""

    @abstractmethod
    async def followup(self, run: "AgentRun", message: str) -> None:
        """Deliver a message and resume the executor's turn."""

    @abstractmethod
    async def interrupt(self, run: "AgentRun", reason: Optional[str] = None) -> None:
        """Interrupt the executor, preserving context for later resume."""

    @abstractmethod
    def get_status(self, run: "AgentRun") -> "AgentRunStatus":
        """Return the current run status as observed by the executor."""


class ManagedTaskAdapter(ExecutorAdapter):
    """Adapter that drives the existing workspace task/session/report flow.

    The ``context_ref`` on the run is the workspace task id. The adapter
    translates:
    - spawn -> create task + start_task
    - send_message -> (no-op for managed tasks; messages go to mailbox only)
    - followup -> continue_task (or start_task if not yet started)
    - interrupt -> abort_task
    """

    def __init__(self, workspace_manager: "WorkspaceManager") -> None:
        self._wm = workspace_manager

    async def spawn(self, run: "AgentRun", initial_message: str) -> str:
        from claude_hub.models.schemas import (
            AgentType,
            WorkspaceTaskCreate,
            WorkspaceTaskMode,
            WorkspaceTaskStatus,
        )

        # Recoverability: if a previous spawn call already created a task for
        # this run (e.g. the process crashed after task creation but before
        # the run's context_ref was persisted), reuse it instead of creating
        # a duplicate.
        existing_task = next(
            (
                t
                for t in self._wm.tasks.values()
                if t.workspace_id == run.workspace_id and t.agent_run_id == run.id
            ),
            None,
        )
        if existing_task is not None:
            if existing_task.status == WorkspaceTaskStatus.TODO:
                await self._wm.start_task(existing_task.id)
            return str(existing_task.id)

        task = self._wm.create_task(
            run.workspace_id,
            WorkspaceTaskCreate(
                title=run.title or f"agent-run-{run.id[:8]}",
                prompt=initial_message,
                agent_type=AgentType.CLAUDE,
                task_mode=WorkspaceTaskMode.REVIEWED,
                agent_run_id=run.id,
            ),
        )
        # Start the task so it gets dispatched to a worker session.
        await self._wm.start_task(task.id)
        return str(task.id)

    async def send_message(self, run: "AgentRun", message: str) -> None:
        # Managed tasks don't have a passive mailbox; messages are recorded
        # in the event stream and the next followup will surface them.
        logger.debug(
            "send_message to managed task run_id=%s task_id=%s (mailbox-only)",
            run.id,
            run.context_ref,
        )

    async def followup(self, run: "AgentRun", message: str) -> None:
        from claude_hub.models.schemas import ContinueTaskRequest, WorkspaceTaskStatus

        task_id = run.context_ref
        if not task_id:
            raise RuntimeError(f"Run {run.id} has no managed task context_ref")
        task = self._wm.tasks.get(task_id)
        if task is None:
            # The task was deleted (e.g. by abort). Re-create it with the
            # same agent_run_id so the run's context_ref still points to a
            # valid task.
            from claude_hub.models.schemas import (
                AgentType,
                WorkspaceTaskCreate,
                WorkspaceTaskMode,
            )

            new_task = self._wm.create_task(
                run.workspace_id,
                WorkspaceTaskCreate(
                    title=run.title or f"agent-run-{run.id[:8]}",
                    prompt=message,
                    agent_type=AgentType.CLAUDE,
                    task_mode=WorkspaceTaskMode.REVIEWED,
                    agent_run_id=run.id,
                ),
            )
            await self._wm.start_task(new_task.id)
            run.context_ref = str(new_task.id)
            return

        # Resume the task based on its current status:
        # - TODO (including after abort): start it
        # - REVIEW / DONE: continue it back to working
        if task.status == WorkspaceTaskStatus.TODO:
            await self._wm.start_task(task_id)
        elif task.status in (
            WorkspaceTaskStatus.REVIEW,
            WorkspaceTaskStatus.DONE,
        ):
            await self._wm.continue_task(
                task_id,
                ContinueTaskRequest(message=message),
            )

    async def interrupt(self, run: "AgentRun", reason: Optional[str] = None) -> None:
        from claude_hub.models.schemas import ManualTaskControlRequest

        task_id = run.context_ref
        if not task_id:
            return
        try:
            await self._wm.abort_task(
                task_id,
                ManualTaskControlRequest(reason=reason or "interrupted by supervisor"),
            )
        except Exception:
            logger.exception("Failed to abort managed task %s for run %s", task_id, run.id)

    def get_status(self, run: "AgentRun") -> "AgentRunStatus":
        from claude_hub.models.agent_tree import AgentRunStatus
        from claude_hub.models.schemas import WorkspaceTaskStatus

        task_id = run.context_ref
        if not task_id:
            return AgentRunStatus.PENDING
        task = self._wm.tasks.get(task_id)
        if task is None:
            return AgentRunStatus.FAILED
        mapping = {
            WorkspaceTaskStatus.TODO: AgentRunStatus.PENDING,
            WorkspaceTaskStatus.QUEUED: AgentRunStatus.PENDING,
            WorkspaceTaskStatus.WORKING: AgentRunStatus.RUNNING,
            WorkspaceTaskStatus.REVIEW: AgentRunStatus.WAITING,
            WorkspaceTaskStatus.DONE: AgentRunStatus.COMPLETED,
        }
        return mapping.get(task.status, AgentRunStatus.RUNNING)


class NativeSubagentAdapter(ExecutorAdapter):
    """Stub adapter for future in-process native subagents.

    Provides the contract and a deterministic in-memory implementation so
    the coordination layer can be tested end-to-end without a real
    subagent runtime.
    """

    def __init__(self) -> None:
        self._statuses: dict[str, "AgentRunStatus"] = {}

    async def spawn(self, run: "AgentRun", initial_message: str) -> str:
        from claude_hub.models.agent_tree import AgentRunStatus

        self._statuses[run.id] = AgentRunStatus.RUNNING
        # context_ref is the run id itself for native subagents.
        return run.id

    async def send_message(self, run: "AgentRun", message: str) -> None:
        pass

    async def followup(self, run: "AgentRun", message: str) -> None:
        from claude_hub.models.agent_tree import AgentRunStatus

        self._statuses[run.id] = AgentRunStatus.RUNNING

    async def interrupt(self, run: "AgentRun", reason: Optional[str] = None) -> None:
        from claude_hub.models.agent_tree import AgentRunStatus

        self._statuses[run.id] = AgentRunStatus.INTERRUPTED

    def get_status(self, run: "AgentRun") -> "AgentRunStatus":
        from claude_hub.models.agent_tree import AgentRunStatus

        return self._statuses.get(run.id, AgentRunStatus.PENDING)


class ExternalJobAdapter(ExecutorAdapter):
    """Stub adapter for future remote/third-party jobs.

    Provides the contract and a deterministic in-memory implementation.
    """

    def __init__(self) -> None:
        self._statuses: dict[str, "AgentRunStatus"] = {}

    async def spawn(self, run: "AgentRun", initial_message: str) -> str:
        from claude_hub.models.agent_tree import AgentRunStatus

        self._statuses[run.id] = AgentRunStatus.RUNNING
        return f"external-job-{run.id}"

    async def send_message(self, run: "AgentRun", message: str) -> None:
        pass

    async def followup(self, run: "AgentRun", message: str) -> None:
        from claude_hub.models.agent_tree import AgentRunStatus

        self._statuses[run.id] = AgentRunStatus.RUNNING

    async def interrupt(self, run: "AgentRun", reason: Optional[str] = None) -> None:
        from claude_hub.models.agent_tree import AgentRunStatus

        self._statuses[run.id] = AgentRunStatus.INTERRUPTED

    def get_status(self, run: "AgentRun") -> "AgentRunStatus":
        from claude_hub.models.agent_tree import AgentRunStatus

        return self._statuses.get(run.id, AgentRunStatus.PENDING)


class ResidentRootAdapter(ExecutorAdapter):
    """Adapter for the resident agent root run.

    The resident root run represents the resident agent itself. It is not
    spawned by the Hub (the resident session is created separately), so
    ``spawn`` and ``followup`` are no-ops: the resident picks up mailbox
    messages on its next periodic cycle.

    ``interrupt`` aborts the resident session (if any). ``get_status``
    returns RUNNING while the resident session exists.
    """

    def __init__(self, workspace_manager: "WorkspaceManager") -> None:
        self._wm = workspace_manager

    async def spawn(self, run: "AgentRun", initial_message: str) -> str:
        # The resident session is created outside the agent tree. The
        # context_ref is the resident session id, set by the workspace
        # manager after the session is created.
        return run.context_ref or run.id

    async def send_message(self, run: "AgentRun", message: str) -> None:
        # Messages are delivered to the resident's mailbox; the resident
        # reads them on its next cycle. No immediate side-effect needed.
        pass

    async def followup(self, run: "AgentRun", message: str) -> None:
        # The resident is always running; it will process the message on
        # its next cycle. No need to wake it explicitly.
        pass

    async def interrupt(self, run: "AgentRun", reason: Optional[str] = None) -> None:
        # Abort the resident session if it exists. The context_ref is the
        # resident session id.
        session_id = run.context_ref
        if not session_id:
            return
        try:
            session = self._wm.sessions.get(session_id)
            if session is not None:
                await self._wm.delete_session(session_id)
        except Exception:
            logger.exception("Failed to abort resident session %s for run %s", session_id, run.id)

    def get_status(self, run: "AgentRun") -> "AgentRunStatus":
        from claude_hub.models.agent_tree import AgentRunStatus

        session_id = run.context_ref
        if not session_id:
            return AgentRunStatus.PENDING
        session = self._wm.sessions.get(session_id)
        if session is None:
            return AgentRunStatus.FAILED
        return AgentRunStatus.RUNNING
