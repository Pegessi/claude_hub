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
    async def followup(self, run: "AgentRun", message: str, call_id: Optional[str] = None) -> None:
        """Deliver a message and resume the executor's turn.

        ``call_id`` is the deduplication key. The sender guarantees
        at-least-once delivery (a crash between send and the delivered-call_id
        persist causes a re-send). Adapters should record delivered call_ids
        and skip duplicates on the sender side; the ``[call_id:<id>]`` marker
        embedded in the message lets the receiving executor dedupe any
        duplicate that slips through.
        """

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

    async def followup(self, run: "AgentRun", message: str, call_id: Optional[str] = None) -> None:
        from claude_hub.models.schemas import ContinueTaskRequest, WorkspaceTaskStatus

        task_id = run.context_ref
        if not task_id:
            raise RuntimeError(f"Run {run.id} has no managed task context_ref")
        task = self._wm.tasks.get(task_id)
        if task is None:
            # The task was deleted (e.g. by abort). Re-create it with the
            # same agent_run_id so the run's context_ref still points to a
            # valid task. The followup message becomes the new task's prompt.
            from claude_hub.models.schemas import (
                AgentType,
                WorkspaceTaskCreate,
                WorkspaceTaskMode,
                WorkspaceTaskStatus,
            )

            # Crash-idempotency: if a previous followup call already created
            # a task for this run (e.g. the process crashed after create_task
            # but before run.context_ref was persisted), reuse it instead of
            # creating a duplicate. This is the same agent_run_id lookup
            # that spawn() uses.
            existing_task = next(
                (
                    t
                    for t in self._wm.tasks.values()
                    if t.workspace_id == run.workspace_id and t.agent_run_id == run.id
                ),
                None,
            )
            if existing_task is not None:
                # Reuse the existing task. Update its prompt with the
                # followup message if not already present, then ensure it
                # is started. start_task is idempotent on a started task.
                #
                # The call_id is embedded in the prompt as a [call_id:<id>]
                # marker so the worker can ACK it. The call_id stays in
                # task.pending_call_ids until ACKed.
                followup_text = message
                if call_id:
                    followup_text = f"[call_id:{call_id}]\n{message}"
                if f"[followup] {followup_text}" not in existing_task.prompt:
                    self._wm.tasks[existing_task.id] = existing_task.model_copy(
                        update={"prompt": f"{existing_task.prompt}\n\n[followup] {followup_text}"}
                    )
                run.context_ref = str(existing_task.id)
                self._wm._save_state()
                if existing_task.status == WorkspaceTaskStatus.TODO:
                    await self._wm.start_task(existing_task.id)
                # The call_id stays in pending_call_ids until the worker ACKs
                # it. It is embedded in the prompt so the worker can list it
                # in acked_call_ids.
                if call_id:
                    reused = self._wm.tasks.get(existing_task.id)
                    if reused is not None and call_id not in reused.pending_call_ids:
                        self._wm.tasks[existing_task.id] = reused.model_copy(
                            update={"pending_call_ids": reused.pending_call_ids + [call_id]}
                        )
                self._wm._save_state()
                return

            # The followup message becomes the new task's prompt. Embed the
            # call_id as a [call_id:<id>] marker so the worker can ACK it.
            prompt_text = message
            if call_id:
                prompt_text = f"[call_id:{call_id}]\n{message}"
            new_task = self._wm.create_task(
                run.workspace_id,
                WorkspaceTaskCreate(
                    title=run.title or f"agent-run-{run.id[:8]}",
                    prompt=prompt_text,
                    agent_type=AgentType.CLAUDE,
                    task_mode=WorkspaceTaskMode.REVIEWED,
                    agent_run_id=run.id,
                ),
            )
            # Persist the new context_ref BEFORE dispatching. If we crash
            # after start_task but before persisting context_ref, the run
            # would still point at the deleted task and a retry would
            # create a duplicate. Persisting first makes the new task the
            # durable context; start_task is idempotent on the task id.
            run.context_ref = str(new_task.id)
            self._wm._save_state()
            await self._wm.start_task(new_task.id)
            # The call_id stays in pending_call_ids until the worker ACKs it.
            # It is embedded in the prompt (above) so the worker can list it
            # in acked_call_ids of its report.
            if call_id:
                recreated = self._wm.tasks.get(new_task.id)
                if recreated is not None and call_id not in recreated.pending_call_ids:
                    self._wm.tasks[new_task.id] = recreated.model_copy(
                        update={"pending_call_ids": recreated.pending_call_ids + [call_id]}
                    )
            self._wm._save_state()
            return

        # Sender-side dedup: if this call_id was already ACKed by the worker
        # (i.e. it is in task.delivered_call_ids), skip. The delivered_call_ids
        # list is persisted with the task so a restarted adapter does not
        # re-deliver an already-processed followup. A call_id still in
        # pending_call_ids (sent but not yet ACKed) WILL be re-delivered on
        # retry — this is the at-least-once crash-recovery path.
        if call_id and call_id in task.delivered_call_ids:
            logger.debug(
                "followup call_id=%s already ACKed by task %s; skipping",
                call_id,
                task_id,
            )
            return

        # --- Crash-safe outbox (at-least-once) ---
        # Persist the call_id as pending BEFORE delivery. This is the durable
        # receipt: if we crash after this point, a retry will see the call_id
        # in pending_call_ids and re-deliver (the receiver dedupes via the
        # [call_id:<id>] marker). The call_id stays in pending_call_ids until
        # the worker ACKs it (lists it in acked_call_ids of its report).
        if call_id:
            current = self._wm.tasks.get(task_id)
            if current is not None and call_id not in current.pending_call_ids:
                self._wm.tasks[task_id] = current.model_copy(
                    update={"pending_call_ids": current.pending_call_ids + [call_id]}
                )
                self._wm._save_state()

        # Re-fetch the task in case it changed (e.g. dispatched to a worker
        # between the top check and now). Each branch is idempotent: it
        # checks the current status before acting, so a re-delivery after a
        # crash (receipt persisted as pending) is a no-op or safe repeat.
        task = self._wm.tasks.get(task_id)
        if task is None:
            return

        if task.status == WorkspaceTaskStatus.TODO:
            # The task hasn't started yet. Append the followup message to
            # the prompt so the worker sees it when dispatched, then start
            # the task. Idempotent: if the task is no longer TODO (e.g.
            # started by a concurrent dispatch), skip.
            #
            # The call_id is embedded in the prompt as a [call_id:<id>]
            # marker so the worker can ACK it (list it in acked_call_ids of
            # its report). Until ACKed, the call_id stays in
            # task.pending_call_ids.
            followup_text = message
            if call_id:
                followup_text = f"[call_id:{call_id}]\n{message}"
            if f"[followup] {followup_text}" not in task.prompt:
                updated = task.model_copy(
                    update={"prompt": f"{task.prompt}\n\n[followup] {followup_text}"}
                )
                self._wm.tasks[task_id] = updated
            await self._wm.start_task(task_id)
        elif task.status == WorkspaceTaskStatus.QUEUED:
            # The task is waiting for a worker. Append the followup message
            # to the prompt so the worker picks it up when dispatched.
            # Idempotent: skip if the message is already in the prompt.
            followup_text = message
            if call_id:
                followup_text = f"[call_id:{call_id}]\n{message}"
            if f"[followup] {followup_text}" not in task.prompt:
                updated = task.model_copy(
                    update={"prompt": f"{task.prompt}\n\n[followup] {followup_text}"}
                )
                self._wm.tasks[task_id] = updated
        elif task.status == WorkspaceTaskStatus.WORKING:
            # The agent is actively running. Send the followup message
            # directly to its session so it processes it immediately.
            # Delivery is at-least-once: send_session_message persists the
            # call_id as pending before sending. The call_id stays in
            # pending_call_ids until the worker ACKs it (via acked_call_ids
            # in its report). A crash after send but before ACK leaves the
            # call_id in pending; a followup retry re-sends it. The
            # [call_id:<id>] marker in the message lets the receiving
            # executor dedupe any duplicate.
            if task.session_id:
                await self._wm.send_session_message(task.session_id, message, call_id=call_id)
        elif task.status in (
            WorkspaceTaskStatus.REVIEW,
            WorkspaceTaskStatus.DONE,
        ):
            # The task is in review or done. Reopen it with the followup
            # message as the continue prompt. The call_id is passed to
            # continue_task so it is embedded in the continue prompt (via
            # send_session_message) and the worker can ACK it.
            await self._wm.continue_task(
                task_id,
                ContinueTaskRequest(message=message),
                call_id=call_id,
            )

        # NOTE: We intentionally do NOT move the call_id to delivered_call_ids
        # here. delivered_call_ids only contains call_ids the worker has
        # ACKed (processed). The call_id stays in task.pending_call_ids until
        # the worker lists it in acked_call_ids of its report (the dispatch
        # call_id is ACKed automatically). Moving it here would suppress the
        # at-least-once re-delivery if the worker crashed after receiving but
        # before processing.
        #
        # For TODO/QUEUED the followup text (with its [call_id:<id>] marker)
        # is durably stored in the task prompt, so a re-delivery is a no-op
        # (the prompt-append is idempotent). For WORKING the message goes
        # through send_session_message which tracks the call_id at the
        # session level; a retry re-sends and the worker dedupes via the
        # marker. For REVIEW/DONE the call_id is passed to continue_task,
        # which embeds it in the continue prompt.

    async def interrupt(self, run: "AgentRun", reason: Optional[str] = None) -> None:
        from claude_hub.models.schemas import ManualTaskControlRequest

        task_id = run.context_ref
        if not task_id:
            return
        # Do NOT swallow exceptions: the caller (AgentTreeManager.interrupt)
        # relies on the exception to know the interrupt did not complete.
        # The INTERRUPTED intent event is already persisted, so recovery
        # will retry the adapter call on restart.
        await self._wm.abort_task(
            task_id,
            ManualTaskControlRequest(reason=reason or "interrupted by supervisor"),
        )

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

    async def followup(self, run: "AgentRun", message: str, call_id: Optional[str] = None) -> None:
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

    async def followup(self, run: "AgentRun", message: str, call_id: Optional[str] = None) -> None:
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

    async def followup(self, run: "AgentRun", message: str, call_id: Optional[str] = None) -> None:
        # The resident runs on a periodic cycle, but a followup should wake
        # it immediately so it processes the message without waiting for the
        # next tick. request_resident_run stamps a flag that the monitor
        # loop consumes on the next tick (bypassing the interval gate).
        try:
            self._wm.request_resident_run(run.workspace_id)
        except ValueError:
            # Resident not enabled for this workspace; the message stays in
            # the mailbox and will be picked up if/when the resident is
            # enabled. No error to the caller.
            logger.debug(
                "followup to resident run %s but resident not enabled in "
                "workspace %s; message stays in mailbox",
                run.id,
                run.workspace_id,
            )

    async def interrupt(self, run: "AgentRun", reason: Optional[str] = None) -> None:
        # Abort the resident session if it exists. The context_ref is the
        # resident session id. Do NOT swallow exceptions: the caller relies
        # on them to know the interrupt did not complete.
        session_id = run.context_ref
        if not session_id:
            return
        session = self._wm.sessions.get(session_id)
        if session is not None:
            await self._wm.delete_session(session_id)

    def get_status(self, run: "AgentRun") -> "AgentRunStatus":
        from claude_hub.models.agent_tree import AgentRunStatus

        session_id = run.context_ref
        if not session_id:
            return AgentRunStatus.PENDING
        session = self._wm.sessions.get(session_id)
        if session is None:
            return AgentRunStatus.FAILED
        return AgentRunStatus.RUNNING
