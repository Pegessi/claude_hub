"""Executor adapters for the Agent Tree coordination layer.

Each adapter wraps a different way of executing an agent run:
- ``ManagedTaskAdapter``: the existing workspace task/session/report flow.
- ``NativeSubagentAdapter``: unavailable in-memory simulator used by tests.
- ``ExternalJobAdapter``: unavailable in-memory simulator used by tests.

The adapter interface is intentionally small: ``spawn``, ``send_message``,
``followup``, ``interrupt``, and ``get_status``. The Hub owns lifecycle
state; adapters translate Hub actions into executor-specific calls and
translate executor events back into ``AgentEvent`` entries.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

from claude_hub.models.agent_tree import ExecutorCapabilities, ManagedExecutorConfig
from claude_hub.models.schemas import AgentType, ExecutionTarget

if TYPE_CHECKING:
    from claude_hub.models.agent_tree import AgentRun, AgentRunStatus
    from claude_hub.models.schemas import ManagedSession
    from claude_hub.services.workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)


class ExecutorUnavailableError(RuntimeError):
    """Raised when a caller selects an executor with no real runtime."""


class ExecutorAdapter(ABC):
    """Base interface for executing an agent run."""

    @abstractmethod
    def capabilities(self) -> ExecutorCapabilities:
        """Return the adapter's public, serializable capability snapshot."""

    def prepare_run(self, run: "AgentRun") -> None:
        """Validate configuration and attach a durable capability snapshot.

        The manager calls this before persisting a new run.  Keeping the
        snapshot on ``AgentRun`` makes availability and supported operations
        survive process restarts.  Availability is enforced separately at
        the public API boundary so internal unit tests may still use explicit
        simulators without advertising them as production executors.
        """

        capability = self.capabilities()
        run.executor_capabilities = capability.model_copy(deep=True)

    def require_available(self) -> None:
        """Fail clearly when this adapter has no production runtime."""

        capability = self.capabilities()
        if not capability.available:
            raise ExecutorUnavailableError(
                capability.unavailable_reason or "The selected executor is unavailable"
            )

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

        ``call_id`` is the delivery key. Delivery is **fail-closed** with
        at-most-once paste per call_id per tmux session lifetime:

        - The sender persists the call_id in ``pending_call_ids`` before
          delivery. The receiver pump claims it (``pending → processing``)
          and sends it to tmux.
        - The call_id stays in ``processing_call_ids`` until the worker
          ACKs it (lists it in ``acked_call_ids`` of its report). Only the
          worker's ACK moves it to ``delivered_call_ids`` — the ACK is the
          durable commit.
        - A crash after the tmux send leaves the call_id in
          ``processing_call_ids``. Cold recovery does NOT blindly
          re-deliver: the monitor reconciles against the tmux
          ``@receipt_<sha16(call_id)>`` session option (set atomically with
          the paste):
            * receipt present on a LIVE session → keep processing (the
              paste definitely happened; no repaste).
            * receipt absent on a LIVE session → move back to pending for
              one re-delivery (the paste definitely did not happen).
            * session STOPPED/gone/unqueryable → move to ``uncertain``
              (fail closed; explicit operator retry required via
              ``retry_uncertain_delivery``).
        - The sender skips call_ids already in ``delivered_call_ids``
          (worker-ACKed) or ``processing_call_ids`` (in-flight).
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

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            available=True,
            supports_spawn=True,
            supports_send=True,
            supports_followup=True,
            supports_interrupt=True,
            durable_status=True,
            supported_agent_types=[
                AgentType.CLAUDE,
                AgentType.CODEX,
                AgentType.CURSOR,
            ],
            # Claude and Codex have verified model launch paths in ttyd_manager.
            # Cursor model selection is intentionally not claimed here.
            model_configurable_agent_types=[AgentType.CLAUDE, AgentType.CODEX],
        )

    def prepare_run(self, run: "AgentRun") -> None:
        config = run.executor_config or ManagedExecutorConfig()
        workspace = self._wm.workspaces.get(run.workspace_id)
        if workspace is None:
            raise ValueError(f"Workspace {run.workspace_id} not found")
        target = config.target or workspace.target
        updates: dict[str, object] = {"target": target}
        if target == ExecutionTarget.REMOTE:
            updates.update(
                {
                    "remote_profile_id": (config.remote_profile_id or workspace.remote_profile_id),
                    "remote_cwd": config.remote_cwd or workspace.remote_cwd,
                    "remote_reconnect": (
                        config.remote_reconnect
                        if config.remote_reconnect is not None
                        else workspace.remote_reconnect
                    ),
                }
            )
        run.executor_config = config.model_copy(update=updates)
        super().prepare_run(run)
        self._launch_env(run.executor_config)

    @staticmethod
    def _config(run: "AgentRun") -> ManagedExecutorConfig:
        config = run.executor_config
        if config is None:
            raise RuntimeError("Managed executor config was not prepared")
        if config.target is None:
            raise RuntimeError("Managed executor target was not resolved")
        return config

    @staticmethod
    def _launch_env(config: ManagedExecutorConfig) -> dict[str, str]:
        """Return the exact persisted launch environment for ``config``.

        Agent type selects a known CLI integration.  No arbitrary executable
        or shell fragment is accepted.  Model names are passed through the
        existing Claude launch variable or the Codex launch variable consumed
        by ``TTYDProcess``.
        """

        if config.agent_type == AgentType.TERMINAL:
            raise ValueError("managed_task does not support the terminal executor")
        if config.agent_type not in {AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR}:
            raise ValueError(f"Unsupported managed_task agent_type: {config.agent_type.value}")

        launch_env = dict(config.env)
        model = config.model.strip() if config.model else None
        if config.model is not None and not model:
            raise ValueError("executor_config.model must not be blank")
        if not model:
            return launch_env

        if config.agent_type == AgentType.CLAUDE:
            key = "ANTHROPIC_MODEL"
        elif config.agent_type == AgentType.CODEX:
            key = "CODEX_MODEL"
        else:
            raise ValueError("Cursor executor does not support an explicit model override")
        configured = launch_env.get(key)
        if configured is not None and configured != model:
            raise ValueError(f"executor_config.model conflicts with executor_config.env[{key!r}]")
        launch_env[key] = model
        return launch_env

    @staticmethod
    def config_from_session(session: "ManagedSession") -> ManagedExecutorConfig:
        """Recover an explicit managed config from an existing Hub session.

        Legacy resident-master spawn calls pass only ``session_id``.  Deriving
        the config from that session preserves the actual CLI/model/target
        instead of silently relabelling a Codex or remote worker as Claude.
        """

        env = dict(session.env)
        model = None
        if session.agent_type == AgentType.CLAUDE:
            model = env.pop("ANTHROPIC_MODEL", None)
        elif session.agent_type == AgentType.CODEX:
            model = env.pop("CODEX_MODEL", None)
        return ManagedExecutorConfig(
            agent_type=session.agent_type,
            model=model,
            env=env,
            solo_mode=session.solo_mode,
            target=session.target,
            cwd=(session.workspace_path if session.target == ExecutionTarget.LOCAL else None),
            remote_profile_id=session.remote_profile_id,
            remote_cwd=session.remote_cwd,
            remote_reconnect=session.remote_reconnect,
        )

    def validate_session(self, run: "AgentRun", session: "ManagedSession") -> None:
        """Reject an explicit session that does not match the persisted spec."""

        self.prepare_run(run)
        config = self._config(run)
        expected_env = self._launch_env(config)
        mismatches = []
        if session.workspace_id != run.workspace_id:
            mismatches.append("workspace_id")
        if session.agent_type != config.agent_type:
            mismatches.append("agent_type")
        if session.solo_mode != config.solo_mode:
            mismatches.append("solo_mode")
        if session.target != config.target:
            mismatches.append("target")
        if session.env != expected_env:
            mismatches.append("env/model")
        if config.cwd is not None and session.workspace_path != config.cwd:
            mismatches.append("cwd")
        if config.target == ExecutionTarget.REMOTE:
            if session.remote_profile_id != config.remote_profile_id:
                mismatches.append("remote_profile_id")
            if session.remote_cwd != config.remote_cwd:
                mismatches.append("remote_cwd")
            if session.remote_reconnect != config.remote_reconnect:
                mismatches.append("remote_reconnect")
        if mismatches:
            raise ValueError(
                f"Session {session.id} does not match executor_config: " + ", ".join(mismatches)
            )

    def _compatible_session(
        self, run: "AgentRun", launch_env: dict[str, str]
    ) -> Optional["ManagedSession"]:
        """Find a real managed session with the exact requested CLI config."""

        from claude_hub.models.schemas import (
            AgentRuntimeStatus,
            ManagedSessionStatus,
            WorkspaceSessionRole,
        )

        config = self._config(run)
        candidates = [
            session
            for session in self._wm.sessions.values()
            if session.workspace_id == run.workspace_id
            and session.role == WorkspaceSessionRole.ORCHESTRATOR
            and session.status != ManagedSessionStatus.STOPPED
            and session.runtime_status in {AgentRuntimeStatus.IDLE, AgentRuntimeStatus.WORKING}
            and session.agent_type == config.agent_type
            and session.solo_mode == config.solo_mode
            and session.target == config.target
            and session.env == launch_env
            and (
                config.cwd is None
                or (
                    session.target == ExecutionTarget.LOCAL and session.workspace_path == config.cwd
                )
            )
            and (
                config.target != ExecutionTarget.REMOTE
                or (
                    session.remote_profile_id == config.remote_profile_id
                    and session.remote_cwd == config.remote_cwd
                    and session.remote_reconnect == config.remote_reconnect
                )
            )
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: (item.queued_count, item.created_at))[0]

    async def _ensure_executor_session(self, run: "AgentRun") -> "ManagedSession":
        from claude_hub.models.schemas import (
            EnsureWorkspaceAgentRequest,
            WorkspaceSessionRole,
        )

        config = self._config(run)
        launch_env = self._launch_env(config)
        compatible = self._compatible_session(run, launch_env)
        if compatible is not None:
            return compatible
        return await self._wm.ensure_workspace_agent(
            run.workspace_id,
            EnsureWorkspaceAgentRequest(
                agent_type=config.agent_type,
                title=(run.title or f"agent-run-{run.id[:8]}") + f" ({config.agent_type.value})",
                role=WorkspaceSessionRole.ORCHESTRATOR,
                reuse_existing=False,
                cwd=config.cwd,
                solo_mode=config.solo_mode,
                target=config.target,
                remote_profile_id=config.remote_profile_id,
                remote_cwd=config.remote_cwd,
                remote_reconnect=config.remote_reconnect,
                ephemeral=True,
                env=launch_env,
            ),
        )

    async def spawn(self, run: "AgentRun", initial_message: str) -> str:
        from claude_hub.models.schemas import (
            StartTaskRequest,
            WorkspaceTaskCreate,
            WorkspaceTaskMode,
            WorkspaceTaskStatus,
        )

        self.prepare_run(run)
        config = self._config(run)

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
                await self._wm.start_task(
                    existing_task.id,
                    StartTaskRequest(
                        agent_type=existing_task.agent_type,
                        target_session_id=existing_task.session_id,
                    ),
                )
            return str(existing_task.id)

        session = await self._ensure_executor_session(run)

        task = self._wm.create_task(
            run.workspace_id,
            WorkspaceTaskCreate(
                title=run.title or f"agent-run-{run.id[:8]}",
                prompt=initial_message,
                agent_type=config.agent_type,
                task_mode=WorkspaceTaskMode.REVIEWED,
                agent_run_id=run.id,
                session_id=session.id,
            ),
        )
        # Pin the start to the exact session whose persisted CLI/model/env
        # configuration was selected above.  The generic dispatcher does not
        # filter sessions by AgentType, so omitting this would allow a Codex
        # child task to land on an arbitrary Claude worker.
        await self._wm.start_task(
            task.id,
            StartTaskRequest(
                agent_type=config.agent_type,
                target_session_id=session.id,
            ),
        )
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

        self.prepare_run(run)
        config = self._config(run)

        task_id = run.context_ref
        if not task_id:
            raise RuntimeError(f"Run {run.id} has no managed task context_ref")
        task = self._wm.tasks.get(task_id)
        if task is None:
            # The task was deleted (e.g. by abort). Re-create it with the
            # same agent_run_id so the run's context_ref still points to a
            # valid task. The followup message becomes the new task's prompt.
            from claude_hub.models.schemas import (
                StartTaskRequest,
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
                    await self._wm.start_task(
                        existing_task.id,
                        StartTaskRequest(
                            agent_type=existing_task.agent_type,
                            target_session_id=existing_task.session_id,
                        ),
                    )
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
            session = await self._ensure_executor_session(run)
            new_task = self._wm.create_task(
                run.workspace_id,
                WorkspaceTaskCreate(
                    title=run.title or f"agent-run-{run.id[:8]}",
                    prompt=prompt_text,
                    agent_type=config.agent_type,
                    task_mode=WorkspaceTaskMode.REVIEWED,
                    agent_run_id=run.id,
                    session_id=session.id,
                ),
            )
            # Persist the new context_ref BEFORE dispatching. If we crash
            # after start_task but before persisting context_ref, the run
            # would still point at the deleted task and a retry would
            # create a duplicate. Persisting first makes the new task the
            # durable context; start_task is idempotent on the task id.
            run.context_ref = str(new_task.id)
            self._wm._save_state()
            await self._wm.start_task(
                new_task.id,
                StartTaskRequest(
                    agent_type=config.agent_type,
                    target_session_id=session.id,
                ),
            )
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
        # pending_call_ids (not yet sent to tmux) WILL be pumped on retry —
        # the pump sends it to tmux with at-most-once paste per call_id per
        # tmux session lifetime (enforced by the tmux @receipt_<sha16>
        # session option), and cold recovery reconciles processing call_ids
        # against that receipt.
        if call_id and call_id in task.delivered_call_ids:
            logger.debug(
                "followup call_id=%s already ACKed by task %s; skipping",
                call_id,
                task_id,
            )
            return

        # --- Crash-safe outbox (persist-intent-before-side-effect) ---
        # Persist the call_id as pending BEFORE delivery. This is the durable
        # sender record: if we crash after this point, the pump will see the
        # call_id in pending_call_ids and send it to tmux. The call_id stays
        # in pending_call_ids until the pump claims it (pending → processing)
        # and sends it to tmux; it stays in processing_call_ids until the
        # worker ACKs it (lists it in acked_call_ids of its report). The
        # worker ACK is the durable commit to delivered_call_ids.
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
            # send_session_message persists the call_id as pending before
            # sending. The pump claims it (pending → processing) and sends
            # it to tmux. The call_id stays in processing_call_ids until
            # the worker ACKs it (via acked_call_ids in its report). The
            # Hub does NOT re-send a processing call_id to a LIVE tmux
            # session (at-most-once paste per call_id per tmux session
            # lifetime, enforced by the tmux @receipt_<sha16(call_id)>
            # session option). On cold restart the monitor reconciles
            # processing call_ids against the receipt: receipt present →
            # keep processing; receipt absent on a LIVE session → move
            # back to pending for one re-delivery; session gone/STOPPED →
            # move to uncertain (fail closed). The [call_id:<id>] marker
            # in the message lets the worker correlate its ACK to the
            # call_id.
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
        # the pump claims it and sends it to tmux (processing_call_ids), and
        # then until the worker lists it in acked_call_ids of its report (the
        # dispatch call_id is ACKed automatically). Moving it here would
        # suppress delivery if the worker never received the message.
        #
        # For TODO/QUEUED the followup text (with its [call_id:<id>] marker)
        # is durably stored in the task prompt, so a re-delivery is a no-op
        # (the prompt-append is idempotent). For WORKING the message goes
        # through send_session_message which tracks the call_id at the
        # session level; the pump sends it to the tmux inbox exactly once.
        # For REVIEW/DONE the call_id is passed to continue_task, which
        # embeds it in the continue prompt.

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
    """Explicitly unavailable simulator for future native subagents.

    The deterministic in-memory behavior remains for coordination unit tests,
    but ``capabilities().available`` is false and the public API must call
    ``require_available`` before dispatch.  This prevents a simulator context
    id from being mistaken for a running model.
    """

    def __init__(self) -> None:
        self._statuses: dict[str, "AgentRunStatus"] = {}

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            available=False,
            unavailable_reason=(
                "native_subagent is not connected to a runtime; use managed_task "
                "with executor_config.agent_type instead"
            ),
        )

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
    """Explicitly unavailable simulator for future external jobs.

    See ``NativeSubagentAdapter``: public callers must be rejected until a
    durable external-job runtime is wired in.
    """

    def __init__(self) -> None:
        self._statuses: dict[str, "AgentRunStatus"] = {}

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            available=False,
            unavailable_reason="external_job is not connected to a durable job runtime",
        )

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

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            available=True,
            supports_send=True,
            supports_followup=True,
            supports_interrupt=True,
            durable_status=True,
        )

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
