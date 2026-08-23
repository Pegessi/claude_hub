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
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional

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

        parent_task_id, _actor_session_id = self._wm._managed_spawn_parent_assignment(run)
        task = self._wm.create_task(
            run.workspace_id,
            WorkspaceTaskCreate(
                title=run.title or f"agent-run-{run.id[:8]}",
                prompt=initial_message,
                agent_type=config.agent_type,
                task_mode=WorkspaceTaskMode.REVIEWED,
                agent_run_id=run.id,
                session_id=session.id,
                parent_task_id=parent_task_id,
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
        self.prepare_run(run)
        try:
            task = self._wm._resolve_task_for_compat_run(run.workspace_id, run)
        except (KeyError, ValueError) as exc:
            raise RuntimeError(f"Run {run.id} has no linked Task") from exc

        from claude_hub.models.task_mailbox import TaskActorRole

        await self._wm.followup_task(
            run.workspace_id,
            task.id,
            message,
            call_id or str(uuid.uuid4()),
            actor_role=TaskActorRole.SUPERVISOR,
            actor_session_id=None,
        )

    async def interrupt(self, run: "AgentRun", reason: Optional[str] = None) -> None:
        from claude_hub.models.schemas import ManualTaskControlRequest
        from claude_hub.models.task_mailbox import TaskActorRole

        task = self._wm._resolve_task_for_compat_run(run.workspace_id, run)
        await self._wm.abort_task(
            task.id,
            ManualTaskControlRequest(reason=reason or "interrupted by supervisor"),
            workspace_id=run.workspace_id,
            actor_role=TaskActorRole.SUPERVISOR,
            compat_author_run_id=run.supervisor_id,
        )

    def get_status(self, run: "AgentRun") -> "AgentRunStatus":
        return self._wm._projected_agent_run_status(run)


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
