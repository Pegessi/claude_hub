import asyncio
import json
import logging
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from ..models import (
    AgentReport,
    AgentReportCreate,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ContinueTaskRequest,
    DispatchDecisionRequest,
    EnsureWorkspaceAgentRequest,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    StartTaskRequest,
    TerminalAgentStatus,
    Workspace,
    WorkspaceBoard,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskStatus,
)
from .remote_profiles import remote_profile_manager
from .ttyd_manager import ttyd_manager

logger = logging.getLogger(__name__)

STATE_ROOT = Path.home() / ".claude_hub" / "workspaces"
INDEX_FILE = STATE_ROOT / "index.json"
LEGACY_STATE_FILE = Path.home() / ".claude_hub" / "workspaces.json"
REMOTE_FORWARD_PORT_BASE = 18173


def _now() -> datetime:
    return datetime.now()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "workspace"


def _sort_time(task: WorkspaceTask) -> datetime:
    return task.queued_at or task.created_at


class WorkspaceManager:
    """Human-orchestrated workspace/task/session layer above TTYDManager."""

    def __init__(self) -> None:
        self.workspaces: dict[str, Workspace] = {}
        self.tasks: dict[str, WorkspaceTask] = {}
        self.sessions: dict[str, ManagedSession] = {}
        self.reports: dict[str, AgentReport] = {}
        self._load_state()

    def _workspace_dir(self, workspace_id: str) -> Path:
        return STATE_ROOT / workspace_id

    def _workspace_state_file(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "state.json"

    def snapshot_path(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "snapshot.md"

    def _load_state(self) -> None:
        if INDEX_FILE.exists():
            self._load_nested_state()
        elif LEGACY_STATE_FILE.exists():
            self._load_legacy_state()

        for session_id, session in list(self.sessions.items()):
            if session.current_task_id is None and session.task_id is not None:
                self.sessions[session_id] = session.model_copy(
                    update={"current_task_id": session.task_id}
                )

    def _load_nested_state(self) -> None:
        try:
            index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            self.workspaces = {
                item["id"]: Workspace(**self._normalize_workspace_item(item))
                for item in index.get("workspaces", [])
            }
            for workspace_id in self.workspaces:
                state_file = self._workspace_state_file(workspace_id)
                if not state_file.exists():
                    continue
                data = json.loads(state_file.read_text(encoding="utf-8"))
                for item in data.get("tasks", []):
                    task = WorkspaceTask(**self._normalize_task_item(item))
                    self.tasks[task.id] = task
                for item in data.get("sessions", []):
                    session = ManagedSession(**self._normalize_session_item(item))
                    self.sessions[session.id] = session
                for item in data.get("reports", []):
                    report = AgentReport(**item)
                    self.reports[report.id] = report
        except Exception as e:
            logger.error(f"Failed to load nested workspace state: {e}")

    def _load_legacy_state(self) -> None:
        try:
            data = json.loads(LEGACY_STATE_FILE.read_text(encoding="utf-8"))
            self.workspaces = {
                item["id"]: Workspace(**self._normalize_workspace_item(item))
                for item in data.get("workspaces", [])
            }
            self.tasks = {
                item["id"]: WorkspaceTask(**self._normalize_task_item(item))
                for item in data.get("tasks", [])
            }
            self.sessions = {
                item["id"]: ManagedSession(**self._normalize_session_item(item))
                for item in data.get("sessions", [])
            }
            self.reports = {
                item["id"]: AgentReport(**item) for item in data.get("reports", [])
            }
            self._save_state()
        except Exception as e:
            logger.error(f"Failed to load legacy workspace state: {e}")

    def _normalize_workspace_item(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        normalized.pop("agent_session_id", None)
        normalized.setdefault("dispatcher_session_id", None)
        normalized.setdefault("target", ExecutionTarget.LOCAL.value)
        normalized.setdefault("remote_profile_id", None)
        normalized.setdefault("remote_cwd", None)
        normalized.setdefault("remote_reconnect", True)
        return normalized

    def _normalize_task_item(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        if normalized.get("status") == "assigned":
            normalized["status"] = WorkspaceTaskStatus.QUEUED.value
            normalized.setdefault("queued_at", normalized.get("updated_at"))
        normalized.setdefault("related_task_id", None)
        normalized.setdefault("clear_context", None)
        normalized.setdefault("dispatch_reason", None)
        normalized.setdefault("dispatch_pending", False)
        normalized.setdefault("queued_at", None)
        normalized.setdefault("started_at", None)
        normalized.setdefault("reviewed_at", None)
        normalized.setdefault("completed_at", None)
        return normalized

    def _normalize_session_item(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        normalized.setdefault("runtime_status", self._runtime_from_managed_status(normalized))
        normalized.setdefault("current_task_id", normalized.get("task_id"))
        normalized.setdefault("queued_count", 0)
        normalized.setdefault(
            "target",
            ExecutionTarget.REMOTE.value
            if normalized.get("remote_forward_port")
            else ExecutionTarget.LOCAL.value,
        )
        normalized.setdefault("remote_profile_id", None)
        normalized.setdefault("remote_cwd", None)
        normalized.setdefault("remote_reconnect", True)
        normalized.setdefault("solo_mode", True)
        normalized.setdefault("remote_forward_port", None)
        return normalized

    def _runtime_from_managed_status(self, item: dict[str, Any]) -> str:
        status = item.get("status")
        if status == ManagedSessionStatus.WORKING.value:
            return AgentRuntimeStatus.WORKING.value
        if status == ManagedSessionStatus.NEEDS_INPUT.value:
            return AgentRuntimeStatus.ATTENTION.value
        if status == ManagedSessionStatus.STOPPED.value:
            return AgentRuntimeStatus.OFFLINE.value
        return AgentRuntimeStatus.IDLE.value

    def _save_state(self) -> None:
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        index_payload = {
            "workspaces": [item.model_dump(mode="json") for item in self.workspaces.values()]
        }
        INDEX_FILE.write_text(json.dumps(index_payload, indent=2), encoding="utf-8")

        for workspace in self.workspaces.values():
            workspace_dir = self._workspace_dir(workspace.id)
            workspace_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "tasks": [
                    item.model_dump(mode="json")
                    for item in self.tasks.values()
                    if item.workspace_id == workspace.id
                ],
                "sessions": [
                    item.model_dump(mode="json")
                    for item in self.sessions.values()
                    if item.workspace_id == workspace.id
                ],
                "reports": [
                    item.model_dump(mode="json")
                    for item in self.reports.values()
                    if item.workspace_id == workspace.id
                ],
            }
            self._workspace_state_file(workspace.id).write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
            self._write_snapshot(workspace.id)

    def _write_snapshot(self, workspace_id: str) -> None:
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            return

        sessions = self._sessions_for_workspace_raw(workspace_id)
        tasks = [task for task in self.tasks.values() if task.workspace_id == workspace_id]
        lines = [
            "# Claude Hub Workspace State",
            "",
            f"Generated: {_now().isoformat(timespec='seconds')}",
            f"Workspace: {workspace.name}",
            f"Target: {workspace.target.value}",
            f"Local workspace dir: {workspace.path}",
            f"Remote profile: {workspace.remote_profile_id or 'none'}",
            f"Remote start dir: {self._workspace_remote_cwd(workspace) if workspace.target == ExecutionTarget.REMOTE else 'n/a'}",
            f"Default branch: {workspace.default_branch}",
            "",
            "## Agents",
        ]
        if not sessions:
            lines.append("- No managed agents yet.")
        for session in sorted(sessions, key=lambda item: item.created_at):
            current = session.current_task_id or session.task_id or "none"
            lines.append(
                "- "
                f"{session.id}: role={session.role.value}, type={session.agent_type.value}, "
                f"target={session.target.value}, runtime={session.runtime_status.value}, current_task={current}, "
                f"queued={self._queued_count(session.id)}, path={session.workspace_path}"
            )

        lines.extend(["", "## Tasks"])
        if not tasks:
            lines.append("- No tasks yet.")
        for task in sorted(tasks, key=lambda item: item.created_at):
            target = task.session_id or "unassigned"
            lines.append(
                "- "
                f"{task.id}: status={task.status.value}, title={task.title}, "
                f"target_agent={target}, pending_dispatch={task.dispatch_pending}"
            )

        lines.extend(
            [
                "",
                "## Coordination Notes",
                "- Only work on the task explicitly assigned to your agent session.",
                "- The workspace is an environment, not necessarily a single repository.",
                "- Use the task instructions to choose the correct local or remote project directory.",
                "- Check for existing file changes before editing; do not overwrite work from another agent.",
                "- If your terminal asks for human input, stop and let the task move to review.",
            ]
        )
        self.snapshot_path(workspace_id).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def list_workspaces(self) -> list[Workspace]:
        return sorted(self.workspaces.values(), key=lambda item: item.created_at)

    def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        return self.workspaces.get(workspace_id)

    def create_workspace(self, payload: WorkspaceCreate) -> Workspace:
        source_path = Path(payload.path).expanduser().resolve()
        if not source_path.exists() or not source_path.is_dir():
            raise ValueError(f"Local workspace dir does not exist: {source_path}")
        if payload.target == ExecutionTarget.REMOTE:
            if not payload.remote_profile_id:
                raise ValueError("Remote workspace requires remote_profile_id")
            if not remote_profile_manager.get_profile(payload.remote_profile_id):
                raise ValueError(f"Remote profile not found: {payload.remote_profile_id}")

        workspace_id = str(uuid.uuid4())
        now = _now()
        prefix = payload.session_prefix or _slug(payload.name)
        workspace = Workspace(
            id=workspace_id,
            name=payload.name,
            path=str(source_path),
            default_branch=payload.default_branch,
            session_prefix=prefix,
            dispatcher_session_id=None,
            target=payload.target,
            remote_profile_id=payload.remote_profile_id,
            remote_cwd=payload.remote_cwd,
            remote_reconnect=payload.remote_reconnect,
            created_at=now,
            updated_at=now,
        )
        self.workspaces[workspace_id] = workspace
        self._save_state()
        return workspace

    def create_task(self, workspace_id: str, payload: WorkspaceTaskCreate) -> WorkspaceTask:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        if payload.related_task_id and payload.related_task_id not in self.tasks:
            raise KeyError(payload.related_task_id)

        task_id = str(uuid.uuid4())
        now = _now()
        task = WorkspaceTask(
            id=task_id,
            workspace_id=workspace_id,
            title=payload.title,
            prompt=payload.prompt,
            agent_type=payload.agent_type,
            status=WorkspaceTaskStatus.TODO,
            related_task_id=payload.related_task_id,
            created_at=now,
            updated_at=now,
        )
        self.tasks[task_id] = task
        self._save_state()
        return task

    async def update_task_status(
        self,
        task_id: str,
        status: WorkspaceTaskStatus,
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)

        now = _now()
        update: dict[str, Any] = {"status": status, "updated_at": now}
        if status == WorkspaceTaskStatus.QUEUED:
            update["queued_at"] = task.queued_at or now
        elif status == WorkspaceTaskStatus.WORKING:
            update["started_at"] = task.started_at or now
        elif status == WorkspaceTaskStatus.REVIEW:
            update["reviewed_at"] = now
        elif status == WorkspaceTaskStatus.DONE:
            update["completed_at"] = now

        self.tasks[task.id] = task.model_copy(update=update)
        if status == WorkspaceTaskStatus.DONE:
            self._release_task_session(self.tasks[task.id])
        elif status == WorkspaceTaskStatus.WORKING and task.session_id:
            self._assign_current_task(task.session_id, task.id)

        self._save_state()
        await self.dispatch_workspace(task.workspace_id)
        return self.tasks[task.id]

    def delete_task(self, task_id: str) -> None:
        task = self.tasks.pop(task_id, None)
        if not task:
            raise KeyError(task_id)

        self.reports = {
            report_id: report
            for report_id, report in self.reports.items()
            if report.task_id != task_id
        }
        for session_id, session in list(self.sessions.items()):
            if session.task_id == task_id or session.current_task_id == task_id:
                self.sessions[session_id] = session.model_copy(
                    update={"task_id": None, "current_task_id": None, "updated_at": _now()}
                )
        self._save_state()

    async def ensure_workspace_agent(
        self,
        workspace_id: str,
        payload_or_agent_type: EnsureWorkspaceAgentRequest | AgentType = AgentType.CODEX,
    ) -> ManagedSession:
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            raise KeyError(workspace_id)

        if isinstance(payload_or_agent_type, EnsureWorkspaceAgentRequest):
            payload = payload_or_agent_type
        else:
            payload = EnsureWorkspaceAgentRequest(
                agent_type=payload_or_agent_type,
                reuse_existing=True,
            )

        if payload.role == WorkspaceSessionRole.DISPATCHER:
            existing = self._dispatcher_session(workspace)
            if existing:
                self._sync_session_tab_metadata(existing)
                return existing
        elif payload.reuse_existing:
            existing = self._first_available_workspace_agent(workspace.id)
            if existing:
                self._sync_session_tab_metadata(existing)
                return existing

        session = await self._create_managed_session(workspace, payload)
        await self.send_session_message(
            session.id,
            self._build_session_bootstrap_prompt(workspace, session),
        )
        return session

    async def _create_managed_session(
        self,
        workspace: Workspace,
        payload: EnsureWorkspaceAgentRequest,
    ) -> ManagedSession:
        role = payload.role
        role_count = len(
            [session for session in self._sessions_for_workspace_raw(workspace.id) if session.role == role]
        )
        if role == WorkspaceSessionRole.DISPATCHER:
            session_id = f"{workspace.session_prefix}-dispatcher"
            title = payload.title or f"{workspace.name} Dispatcher"
        else:
            session_id = f"{workspace.session_prefix}-agent-{role_count + 1}"
            title = payload.title or f"{workspace.name} Agent {role_count + 1}"

        if session_id in self.sessions:
            session_id = f"{session_id}-{uuid.uuid4().hex[:6]}"

        session_target = payload.target or ExecutionTarget.LOCAL
        local_cwd = payload.cwd or workspace.path
        remote_profile_id: str | None = None
        remote_cwd: str | None = None
        remote_reconnect = (
            payload.remote_reconnect
            if payload.remote_reconnect is not None
            else workspace.remote_reconnect
        )
        if session_target == ExecutionTarget.REMOTE:
            remote_profile_id = payload.remote_profile_id or workspace.remote_profile_id
            if not remote_profile_id:
                raise ValueError("Remote agent requires remote_profile_id")
            if not remote_profile_manager.get_profile(remote_profile_id):
                raise ValueError(f"Remote profile not found: {remote_profile_id}")
            remote_cwd = self._resolve_remote_cwd(
                profile_id=remote_profile_id,
                requested_cwd=payload.remote_cwd,
                workspace_cwd=workspace.remote_cwd,
            )

        remote_forward_port = (
            self._next_remote_forward_port() if session_target == ExecutionTarget.REMOTE else None
        )
        session_workspace_path = remote_cwd if session_target == ExecutionTarget.REMOTE else local_cwd
        tab = await ttyd_manager.create_tab(
            name=title,
            cwd=local_cwd if session_target == ExecutionTarget.LOCAL else None,
            solo_mode=payload.solo_mode,
            agent_type=payload.agent_type,
            target=session_target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            remote_forward_port=remote_forward_port,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            workspace_role=role,
        )
        now = _now()
        session = ManagedSession(
            id=session_id,
            workspace_id=workspace.id,
            task_id=None,
            tab_id=tab.id,
            role=role,
            agent_type=payload.agent_type,
            status=ManagedSessionStatus.SPAWNING,
            runtime_status=AgentRuntimeStatus.IDLE,
            current_task_id=None,
            queued_count=0,
            title=title,
            branch=None,
            workspace_path=session_workspace_path,
            tmux_session=f"claude-hub-{tab.id[:8]}",
            target=session_target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            solo_mode=payload.solo_mode,
            remote_forward_port=remote_forward_port,
            created_at=now,
            updated_at=now,
        )
        self.sessions[session.id] = session
        if role == WorkspaceSessionRole.DISPATCHER:
            self.workspaces[workspace.id] = workspace.model_copy(
                update={"dispatcher_session_id": session.id, "updated_at": now}
            )
        self._save_state()
        return session

    def _workspace_remote_cwd(self, workspace: Workspace) -> str:
        return self._resolve_remote_cwd(
            profile_id=workspace.remote_profile_id,
            requested_cwd=workspace.remote_cwd,
            workspace_cwd=None,
        )

    def _resolve_remote_cwd(
        self,
        profile_id: str | None,
        requested_cwd: str | None,
        workspace_cwd: str | None,
    ) -> str:
        if requested_cwd:
            return requested_cwd
        if workspace_cwd:
            return workspace_cwd
        if profile_id:
            profile = remote_profile_manager.get_profile(profile_id)
            if profile and profile.default_cwd:
                return profile.default_cwd
        return "~"

    def _next_remote_forward_port(self) -> int:
        used_ports = {
            session.remote_forward_port
            for session in self.sessions.values()
            if session.remote_forward_port is not None
        }
        port = REMOTE_FORWARD_PORT_BASE
        while port in used_ports:
            port += 1
        return port

    async def delete_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        blocking = [
            task
            for task in self.tasks.values()
            if task.session_id == session_id and task.status != WorkspaceTaskStatus.DONE
        ]
        if blocking:
            raise RuntimeError("Cannot delete an agent with queued, working, or review tasks")

        await ttyd_manager.delete_tab(session.tab_id)
        self.sessions.pop(session_id, None)
        workspace = self.workspaces.get(session.workspace_id)
        if workspace and workspace.dispatcher_session_id == session_id:
            self.workspaces[workspace.id] = workspace.model_copy(
                update={"dispatcher_session_id": None, "updated_at": _now()}
            )
        self._save_state()

    async def start_task(
        self,
        task_id: str,
        payload: StartTaskRequest | None = None,
    ) -> WorkspaceTask:
        payload = payload or StartTaskRequest()
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            raise KeyError(task.workspace_id)
        if task.status == WorkspaceTaskStatus.DONE:
            raise RuntimeError("Done tasks cannot be started")

        await self._refresh_session_statuses()

        if not self._workspace_agents(workspace.id, include_stopped=True):
            await self.ensure_workspace_agent(
                workspace.id,
                EnsureWorkspaceAgentRequest(
                    agent_type=payload.agent_type or task.agent_type,
                    reuse_existing=False,
                ),
            )

        base_update: dict[str, Any] = {
            "status": WorkspaceTaskStatus.QUEUED,
            "queued_at": task.queued_at or _now(),
            "updated_at": _now(),
            "dispatch_pending": False,
        }
        if payload.agent_type:
            base_update["agent_type"] = payload.agent_type
        if payload.related_task_id:
            if payload.related_task_id not in self.tasks:
                raise KeyError(payload.related_task_id)
            base_update["related_task_id"] = payload.related_task_id

        task = task.model_copy(update=base_update)
        decision = await self._choose_dispatch_target(workspace, task, payload)
        if decision is None:
            task = task.model_copy(
                update={
                    "session_id": None,
                    "clear_context": payload.clear_context,
                    "dispatch_reason": "Waiting for dispatcher agent decision",
                    "dispatch_pending": True,
                    "updated_at": _now(),
                }
            )
            self.tasks[task.id] = task
            self._save_state()
            await self._request_dispatch_decision(workspace, task)
            return self.tasks[task.id]

        target, clear_context, reason = decision
        task = task.model_copy(
            update={
                "session_id": target.id,
                "clear_context": clear_context,
                "dispatch_reason": reason,
                "dispatch_pending": False,
                "updated_at": _now(),
            }
        )
        self.tasks[task.id] = task
        self._save_state()
        await self.dispatch_workspace(workspace.id)
        return self.tasks[task.id]

    async def _choose_dispatch_target(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        payload: StartTaskRequest,
    ) -> Optional[tuple[ManagedSession, bool, str]]:
        agents = self._workspace_agents(workspace.id, include_stopped=True)
        if payload.target_session_id:
            target = self.sessions.get(payload.target_session_id)
            if not target or target.workspace_id != workspace.id:
                raise KeyError(payload.target_session_id)
            if target.role != WorkspaceSessionRole.ORCHESTRATOR:
                raise RuntimeError("Tasks can only be assigned to workspace agents")
            return (
                target,
                bool(payload.clear_context),
                "User selected target agent",
            )

        related_task_id = payload.related_task_id or task.related_task_id
        if related_task_id:
            related = self.tasks.get(related_task_id)
            if related and related.session_id:
                target = self.sessions.get(related.session_id)
                if target and target.role == WorkspaceSessionRole.ORCHESTRATOR:
                    return (
                        target,
                        False,
                        f"Related to task {related_task_id}",
                    )

        if task.session_id:
            existing = self.sessions.get(task.session_id)
            if existing and existing.role == WorkspaceSessionRole.ORCHESTRATOR:
                return existing, False, "Continuing previous task assignment"

        free_agents = [agent for agent in agents if self._can_dispatch_to(agent)]
        if len(free_agents) == 1:
            target = free_agents[0]
            should_clear = self._has_prior_task_history(target.id)
            return (
                target,
                payload.clear_context if payload.clear_context is not None else should_clear,
                "Only one workspace agent is available",
            )
        if len(free_agents) > 1:
            target = self._least_queued_agent(free_agents)
            should_clear = self._has_prior_task_history(target.id)
            return (
                target,
                payload.clear_context if payload.clear_context is not None else should_clear,
                "Selected least queued available workspace agent",
            )

        if agents:
            target = self._least_queued_agent(agents)
            should_clear = self._has_prior_task_history(target.id)
            return (
                target,
                payload.clear_context if payload.clear_context is not None else should_clear,
                "Queued behind existing workspace agent",
            )

        return None

    def _least_queued_agent(self, agents: list[ManagedSession]) -> ManagedSession:
        def runtime_rank(agent: ManagedSession) -> int:
            if agent.runtime_status == AgentRuntimeStatus.IDLE:
                return 0
            if agent.runtime_status == AgentRuntimeStatus.OFFLINE:
                return 1
            return 2

        return sorted(
            agents,
            key=lambda agent: (
                self._queued_count(agent.id),
                runtime_rank(agent),
                1 if agent.current_task_id or agent.task_id else 0,
                agent.created_at,
            ),
        )[0]

    def _has_prior_task_history(self, session_id: str) -> bool:
        return any(
            task.session_id == session_id
            and task.status in {WorkspaceTaskStatus.REVIEW, WorkspaceTaskStatus.DONE}
            for task in self.tasks.values()
        )

    async def _request_dispatch_decision(self, workspace: Workspace, task: WorkspaceTask) -> None:
        dispatcher = await self.ensure_workspace_agent(
            workspace.id,
            EnsureWorkspaceAgentRequest(
                agent_type=task.agent_type,
                role=WorkspaceSessionRole.DISPATCHER,
                reuse_existing=True,
            ),
        )
        await self.send_session_message(
            dispatcher.id,
            self._build_dispatch_decision_prompt(workspace, task, dispatcher),
        )

    async def apply_dispatch_decision(
        self,
        task_id: str,
        payload: DispatchDecisionRequest,
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        target = self.sessions.get(payload.target_session_id)
        if not target or target.workspace_id != task.workspace_id:
            raise KeyError(payload.target_session_id)
        if target.role != WorkspaceSessionRole.ORCHESTRATOR:
            raise RuntimeError("Dispatch decisions must target a workspace agent")

        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.QUEUED,
                "session_id": target.id,
                "clear_context": payload.clear_context,
                "dispatch_reason": payload.reason or "Dispatcher selected target agent",
                "dispatch_pending": False,
                "queued_at": task.queued_at or _now(),
                "updated_at": _now(),
            }
        )
        self._save_state()
        await self.dispatch_workspace(task.workspace_id)
        return self.tasks[task.id]

    async def continue_task(
        self,
        task_id: str,
        payload: ContinueTaskRequest | None = None,
    ) -> WorkspaceTask:
        payload = payload or ContinueTaskRequest()
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        if task.status != WorkspaceTaskStatus.REVIEW:
            raise RuntimeError("Only review tasks can continue")
        if not task.session_id or task.session_id not in self.sessions:
            raise RuntimeError("Review task has no original agent")

        session = self.sessions[task.session_id]
        await self.send_session_message(
            session.id,
            self._build_continue_prompt(task, payload),
        )

        now = _now()
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.WORKING,
                "started_at": now,
                "updated_at": now,
                "dispatch_pending": False,
            }
        )
        self.sessions[session.id] = session.model_copy(
            update={
                "task_id": task.id,
                "current_task_id": task.id,
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "updated_at": now,
                "last_activity_at": now,
            }
        )
        self._save_state()
        return self.tasks[task.id]

    async def dispatch_workspace(self, workspace_id: str) -> None:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)

        await self._refresh_session_statuses()
        for session in self._workspace_agents(workspace_id, include_stopped=True):
            if not self._can_dispatch_to(session):
                continue
            next_task = self._next_queued_task(session.id)
            if not next_task:
                continue
            await self._dispatch_task_to_session(next_task, session)

    def _can_dispatch_to(self, session: ManagedSession) -> bool:
        if session.role != WorkspaceSessionRole.ORCHESTRATOR:
            return False
        if session.runtime_status not in {
            AgentRuntimeStatus.IDLE,
            AgentRuntimeStatus.OFFLINE,
        }:
            return False
        if session.task_id or session.current_task_id:
            current_id = session.task_id or session.current_task_id
            current = self.tasks.get(current_id) if current_id else None
            if current and current.status != WorkspaceTaskStatus.DONE:
                return False
        return True

    def _next_queued_task(self, session_id: str) -> Optional[WorkspaceTask]:
        tasks = [
            task
            for task in self.tasks.values()
            if task.session_id == session_id
            and task.status == WorkspaceTaskStatus.QUEUED
            and not task.dispatch_pending
        ]
        if not tasks:
            return None
        return sorted(tasks, key=_sort_time)[0]

    async def _dispatch_task_to_session(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
    ) -> None:
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            raise KeyError(task.workspace_id)

        if task.clear_context:
            await self.send_session_message(session.id, "/clear")
            await asyncio.sleep(0.5)

        await self.send_session_message(
            session.id,
            self._build_task_assignment_prompt(workspace, task, session),
        )

        now = _now()
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.WORKING,
                "started_at": now,
                "updated_at": now,
            }
        )
        self.sessions[session.id] = session.model_copy(
            update={
                "task_id": task.id,
                "current_task_id": task.id,
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "last_activity_at": now,
                "updated_at": now,
            }
        )
        self._save_state()

    async def spawn_worker(
        self,
        task_id: str,
        agent_type: Optional[AgentType] = None,
    ) -> ManagedSession:
        del agent_type
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        if task.workspace_id not in self.workspaces:
            raise KeyError(task.workspace_id)
        raise RuntimeError(
            "Worker spawning is disabled. Add a workspace agent and start the task instead."
        )

    def _build_session_bootstrap_prompt(
        self,
        workspace: Workspace,
        session: ManagedSession,
    ) -> str:
        if session.role == WorkspaceSessionRole.DISPATCHER:
            return self._build_dispatcher_bootstrap_prompt(workspace, session)
        return self._build_workspace_agent_prompt(workspace, session)

    def _report_base_url(self, session: ManagedSession) -> str:
        if session.remote_forward_port:
            return f"http://127.0.0.1:{session.remote_forward_port}"
        return f"http://localhost:{settings.port}"

    def _remote_target_label(self, session: ManagedSession) -> str:
        if not session.remote_profile_id:
            return "unknown remote host"
        profile = remote_profile_manager.get_profile(session.remote_profile_id)
        if not profile:
            return session.remote_profile_id
        host = f"{profile.user}@{profile.ssh_host}" if profile.user else profile.ssh_host
        if profile.port != 22:
            host = f"{host}:{profile.port}"
        if profile.name and profile.name != profile.id:
            return f"{profile.name} ({host})"
        return host

    def _session_environment_lines(self, workspace: Workspace, session: ManagedSession) -> str:
        lines = [
            f"Runtime target: {session.target.value}",
            f"Local workspace dir: {workspace.path}",
        ]
        if session.target == ExecutionTarget.REMOTE:
            lines.extend(
                [
                    f"SSH development target: {self._remote_target_label(session)}",
                    f"Remote working directory: {session.workspace_path}",
                ]
            )
        else:
            lines.append(f"Default working directory: {session.workspace_path}")
        return "\n".join(lines)

    def _build_workspace_agent_prompt(self, workspace: Workspace, session: ManagedSession) -> str:
        return (
            "You are a resident workspace agent.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Session: {session.id}\n"
            f"{self._session_environment_lines(workspace, session)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            "Stay in this terminal and wait for assigned tasks. Do not start unrelated work. "
            "This workspace is an environment, not necessarily a single repository. "
            "Do not inspect repositories, run git status, edit files, or report working until "
            "a task is explicitly assigned. Use each task to choose the correct project "
            "directory before editing. "
            "Before editing, read the state snapshot and check for local file changes. "
            "If another agent modified files you need, avoid overwriting them and ask for review. "
            "Report progress to the workspace coordinator only after you receive a task, "
            "when you start, get blocked, need input, are ready for review, or complete the work.\n\n"
            "Report endpoint for assigned tasks:\n"
            f"curl -sS -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            '-d \'{"task_id":"TASK_ID","state":"working","message":"Progress update"}\''
        )

    def _build_dispatcher_bootstrap_prompt(
        self,
        workspace: Workspace,
        session: ManagedSession,
    ) -> str:
        return (
            "You are the dispatcher agent for this workspace.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Session: {session.id}\n"
            f"{self._session_environment_lines(workspace, session)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            "When asked for a dispatch decision, choose the best workspace agent "
            "for context continuity and decide whether the target should clear context. "
            "Return decisions only by calling the provided local API endpoint."
        )

    def _build_dispatch_decision_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        dispatcher: ManagedSession,
    ) -> str:
        agents = [
            {
                "id": session.id,
                "title": session.title,
                "agent_type": session.agent_type.value,
                "target": session.target.value,
                "workspace_path": session.workspace_path,
                "runtime": session.runtime_status.value,
                "current_task_id": session.current_task_id,
                "queued_count": self._queued_count(session.id),
            }
            for session in self._workspace_agents(workspace.id)
        ]
        recent_tasks = [
            {
                "id": item.id,
                "title": item.title,
                "status": item.status.value,
                "agent": item.session_id,
            }
            for item in sorted(
                [item for item in self.tasks.values() if item.workspace_id == workspace.id],
                key=lambda item: item.updated_at,
                reverse=True,
            )[:12]
        ]
        return (
            "Dispatch decision needed.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Task description:\n{task.prompt}\n\n"
            f"Available agents JSON:\n{json.dumps(agents, indent=2)}\n\n"
            f"Recent tasks JSON:\n{json.dumps(recent_tasks, indent=2)}\n\n"
            "Choose a target_agent_id and whether to clear context. Prefer context continuity "
            "for related work. If the best related agent is busy, still choose that agent so "
            "the workspace queues the task behind its current work.\n\n"
            "Call this endpoint with your decision:\n"
            f"curl -sS -X POST {self._report_base_url(dispatcher)}/api/workspaces/tasks/{task.id}/dispatch-decision "
            "-H 'Content-Type: application/json' "
            "-d '{\"target_session_id\":\"AGENT_ID\",\"clear_context\":false,"
            "\"reason\":\"why this agent is best\"}'"
        )

    def _build_task_assignment_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
    ) -> str:
        clear_note = (
            "This task is unrelated to prior work. Treat prior conversation context as stale.\n\n"
            if task.clear_context
            else ""
        )
        return (
            "New workspace task assigned.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"{self._session_environment_lines(workspace, session)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n"
            f"Dispatch reason: {task.dispatch_reason or 'not specified'}\n\n"
            f"{clear_note}"
            f"Task description:\n{task.prompt}\n\n"
            "Start by reading the state snapshot. This workspace may contain many projects; "
            "use the task description to choose the correct directory before editing. "
            "Check for uncommitted file changes. "
            "Report state started, then report working as you make progress. "
            "If blocked or waiting for user input, report blocked or needs_input. "
            "When ready for human review, report ready_for_review or completed.\n\n"
            "Report endpoint example:\n"
            f"curl -sS -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"started",'
            '"message":"Started task"}\'\n\n'
            "Final reports should include task_id, state, message, changed_files, validation, and risks."
        )

    def _build_continue_prompt(self, task: WorkspaceTask, payload: ContinueTaskRequest) -> str:
        message = payload.message.strip() if payload.message else ""
        return (
            "Continue workspace task from review.\n\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Follow-up instructions:\n{message or 'Continue addressing the review feedback.'}\n\n"
            "The task is back in working state. Report progress with the same task_id."
        )

    async def send_session_message(self, session_id: str, message: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)
        await self._ensure_session_ready_for_send(session)
        await self._send_tmux_message(session.tmux_session, message)

    async def _ensure_session_ready_for_send(self, session: ManagedSession) -> None:
        created = await ttyd_manager.ensure_tab_tmux_session(session.tab_id)
        if not created:
            return

        deadline = asyncio.get_running_loop().time() + 12
        last_output = ""
        while asyncio.get_running_loop().time() < deadline:
            try:
                last_output = await self._capture_tmux_output(session.tmux_session)
            except RuntimeError:
                await asyncio.sleep(0.3)
                continue
            if self._agent_input_ready(last_output):
                return
            await asyncio.sleep(0.5)

        logger.warning(
            "Timed out waiting for workspace agent input prompt before sending to %s. "
            "Sending anyway. Last output tail: %s",
            session.id,
            last_output[-200:],
        )

    async def _capture_tmux_output(self, tmux_session: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "capture-pane",
            "-p",
            "-S",
            "-120",
            "-t",
            tmux_session,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux capture-pane failed with code {proc.returncode}")
        return stdout.decode("utf-8", errors="ignore")

    def _agent_input_ready(self, output: str) -> bool:
        lower = output.lower()
        return any(
            marker in lower
            for marker in (
                "implement {feature}",
                "? for shortcuts",
                "/ for commands",
                "openai codex",
                "claude code",
                "permissions:",
            )
        )

    def create_report(self, session_id: str, payload: AgentReportCreate) -> AgentReport:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        now = _now()
        task_id = payload.task_id or session.task_id or session.current_task_id
        if task_id:
            task = self.tasks.get(task_id)
            if not task or task.workspace_id != session.workspace_id:
                raise KeyError(task_id)

        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=session.workspace_id,
            task_id=task_id,
            session_id=session.id,
            state=payload.state,
            message=payload.message,
            changed_files=payload.changed_files,
            validation=payload.validation,
            risks=payload.risks,
            created_at=now,
        )
        self.reports[report.id] = report

        session_status = self._status_from_report(payload.state, session)
        session_update: dict[str, Any] = {
            "status": session_status,
            "runtime_status": self._runtime_from_report(payload.state, session),
            "last_activity_at": now,
            "updated_at": now,
        }
        if task_id:
            session_update["task_id"] = task_id
            session_update["current_task_id"] = task_id
        self.sessions[session.id] = session.model_copy(update=session_update)

        if task_id and task_id in self.tasks:
            task_status = self._task_status_from_report(payload.state)
            if task_status:
                task_update: dict[str, Any] = {"status": task_status, "updated_at": now}
                if task_status == WorkspaceTaskStatus.WORKING:
                    task_update["started_at"] = self.tasks[task_id].started_at or now
                if task_status == WorkspaceTaskStatus.REVIEW:
                    task_update["reviewed_at"] = now
                self.tasks[task_id] = self.tasks[task_id].model_copy(update=task_update)

        self._save_state()
        return report

    def reports_for_workspace(self, workspace_id: str) -> list[AgentReport]:
        return sorted(
            [report for report in self.reports.values() if report.workspace_id == workspace_id],
            key=lambda report: report.created_at,
        )

    async def _send_tmux_message(self, tmux_session: str, message: str) -> None:
        await self._run_tmux("send-keys", "-t", tmux_session, "C-u")
        await asyncio.sleep(0.2)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(message)
            tmp_path = tmp.name
        try:
            await self._run_tmux("load-buffer", tmp_path)
            await self._run_tmux("paste-buffer", "-t", tmux_session)
            await asyncio.sleep(0.1)
            await self._run_tmux("send-keys", "-t", tmux_session, "Enter")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    async def _run_tmux(self, *args: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux {' '.join(args)} failed with code {proc.returncode}")

    def sessions_for_workspace(self, workspace_id: str) -> list[ManagedSession]:
        sessions = self._sessions_for_workspace_raw(workspace_id)
        return [self._with_assignment_summary(session) for session in sessions]

    def _sessions_for_workspace_raw(self, workspace_id: str) -> list[ManagedSession]:
        return [
            session for session in self.sessions.values() if session.workspace_id == workspace_id
        ]

    def _workspace_agents(
        self,
        workspace_id: str,
        include_stopped: bool = False,
    ) -> list[ManagedSession]:
        return [
            session
            for session in self._sessions_for_workspace_raw(workspace_id)
            if session.role == WorkspaceSessionRole.ORCHESTRATOR
            and (include_stopped or session.status != ManagedSessionStatus.STOPPED)
        ]

    def _dispatcher_session(self, workspace: Workspace) -> Optional[ManagedSession]:
        if workspace.dispatcher_session_id:
            session = self.sessions.get(workspace.dispatcher_session_id)
            if session and session.status != ManagedSessionStatus.STOPPED:
                return session
        for session in self._sessions_for_workspace_raw(workspace.id):
            if (
                session.role == WorkspaceSessionRole.DISPATCHER
                and session.status != ManagedSessionStatus.STOPPED
            ):
                self.workspaces[workspace.id] = workspace.model_copy(
                    update={"dispatcher_session_id": session.id, "updated_at": _now()}
                )
                self._save_state()
                return session
        return None

    def _first_available_workspace_agent(self, workspace_id: str) -> Optional[ManagedSession]:
        agents = self._workspace_agents(workspace_id)
        return agents[0] if agents else None

    def _queued_count(self, session_id: str) -> int:
        return len(
            [
                task
                for task in self.tasks.values()
                if task.session_id == session_id and task.status == WorkspaceTaskStatus.QUEUED
            ]
        )

    def _with_assignment_summary(self, session: ManagedSession) -> ManagedSession:
        current_task_id = session.current_task_id or session.task_id
        return session.model_copy(
            update={
                "current_task_id": current_task_id,
                "queued_count": self._queued_count(session.id),
            }
        )

    async def get_board(self, workspace_id: str) -> WorkspaceBoard:
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            raise KeyError(workspace_id)

        await self._refresh_session_statuses()
        self._sync_workspace_tab_metadata(workspace_id)
        tasks = [task for task in self.tasks.values() if task.workspace_id == workspace_id]
        sessions = self.sessions_for_workspace(workspace_id)
        reports = self.reports_for_workspace(workspace_id)
        return WorkspaceBoard(
            workspace=self.workspaces[workspace_id],
            tasks=tasks,
            sessions=sessions,
            reports=reports,
            snapshot_path=str(self.snapshot_path(workspace_id)),
        )

    def _sync_workspace_tab_metadata(self, workspace_id: str) -> None:
        for session in self._sessions_for_workspace_raw(workspace_id):
            self._sync_session_tab_metadata(session)

    def _sync_session_tab_metadata(self, session: ManagedSession) -> None:
        workspace = self.workspaces.get(session.workspace_id)
        if not workspace:
            return
        ttyd_manager.set_tab_workspace_metadata(
            tab_id=session.tab_id,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            workspace_role=session.role,
        )

    async def _refresh_session_statuses(self) -> None:
        statuses = {
            status.tab_id: status for status in await ttyd_manager.list_tab_agent_statuses()
        }
        changed = False
        for session_id, session in list(self.sessions.items()):
            if session.status in {ManagedSessionStatus.DONE, ManagedSessionStatus.ERROR}:
                continue
            status = statuses.get(session.tab_id)
            if not status:
                continue
            next_status = self._map_runtime_status(status)
            runtime_status = status.status
            if next_status == ManagedSessionStatus.STOPPED and self._is_spawn_grace_period(session):
                continue

            current_task_id = session.current_task_id or session.task_id
            update: dict[str, Any] = {
                "status": next_status,
                "runtime_status": runtime_status,
                "current_task_id": current_task_id,
                "updated_at": status.sampled_at,
            }
            if status.last_changed_at:
                update["last_activity_at"] = status.last_changed_at

            if current_task_id:
                task = self.tasks.get(current_task_id)
                if (
                    runtime_status == AgentRuntimeStatus.ATTENTION
                    and task
                    and task.status == WorkspaceTaskStatus.WORKING
                ):
                    self.tasks[current_task_id] = task.model_copy(
                        update={
                            "status": WorkspaceTaskStatus.REVIEW,
                            "reviewed_at": status.sampled_at,
                            "updated_at": status.sampled_at,
                        }
                    )
                    update["task_id"] = current_task_id
                    changed = True
                elif (
                    runtime_status == AgentRuntimeStatus.WORKING
                    and task
                    and task.status == WorkspaceTaskStatus.REVIEW
                ):
                    self.tasks[current_task_id] = task.model_copy(
                        update={
                            "status": WorkspaceTaskStatus.WORKING,
                            "started_at": task.started_at or status.sampled_at,
                            "updated_at": status.sampled_at,
                        }
                    )
                    update["task_id"] = current_task_id
                    changed = True

            self.sessions[session_id] = session.model_copy(update=update)
            changed = True
        if changed:
            self._save_state()

    def _map_runtime_status(self, status: TerminalAgentStatus) -> ManagedSessionStatus:
        if status.status == AgentRuntimeStatus.ATTENTION:
            return ManagedSessionStatus.NEEDS_INPUT
        if status.status == AgentRuntimeStatus.WORKING:
            return ManagedSessionStatus.WORKING
        if status.status == AgentRuntimeStatus.OFFLINE:
            return ManagedSessionStatus.STOPPED
        return ManagedSessionStatus.IDLE

    def _is_spawn_grace_period(self, session: ManagedSession) -> bool:
        if session.status != ManagedSessionStatus.SPAWNING:
            return False
        return (_now() - session.created_at).total_seconds() < 90

    def _status_from_report(
        self,
        state: AgentReportState,
        session: ManagedSession,
    ) -> ManagedSessionStatus:
        if session.role == WorkspaceSessionRole.ORCHESTRATOR:
            if state in {AgentReportState.COMPLETED, AgentReportState.READY_FOR_REVIEW}:
                return ManagedSessionStatus.IDLE
            if state == AgentReportState.BLOCKED:
                return ManagedSessionStatus.NEEDS_INPUT
        if state == AgentReportState.BLOCKED:
            return ManagedSessionStatus.ERROR
        if state == AgentReportState.NEEDS_INPUT:
            return ManagedSessionStatus.NEEDS_INPUT
        if state == AgentReportState.COMPLETED:
            return ManagedSessionStatus.DONE
        if state in {
            AgentReportState.STARTED,
            AgentReportState.WORKING,
            AgentReportState.READY_FOR_REVIEW,
        }:
            return ManagedSessionStatus.WORKING
        return session.status

    def _runtime_from_report(
        self,
        state: AgentReportState,
        session: ManagedSession,
    ) -> AgentRuntimeStatus:
        if state in {AgentReportState.BLOCKED, AgentReportState.NEEDS_INPUT}:
            return AgentRuntimeStatus.ATTENTION
        if state in {AgentReportState.STARTED, AgentReportState.WORKING}:
            return AgentRuntimeStatus.WORKING
        if state in {AgentReportState.READY_FOR_REVIEW, AgentReportState.COMPLETED}:
            return AgentRuntimeStatus.IDLE
        return session.runtime_status

    def _task_status_from_report(self, state: AgentReportState) -> Optional[WorkspaceTaskStatus]:
        if state == AgentReportState.COMPLETED:
            return WorkspaceTaskStatus.REVIEW
        if state == AgentReportState.READY_FOR_REVIEW:
            return WorkspaceTaskStatus.REVIEW
        if state in {
            AgentReportState.STARTED,
            AgentReportState.WORKING,
            AgentReportState.BLOCKED,
            AgentReportState.NEEDS_INPUT,
        }:
            return WorkspaceTaskStatus.WORKING
        return None

    def _release_task_session(self, task: WorkspaceTask) -> None:
        if not task.session_id:
            return
        session = self.sessions.get(task.session_id)
        if not session:
            return
        if session.task_id == task.id or session.current_task_id == task.id:
            self.sessions[session.id] = session.model_copy(
                update={
                    "task_id": None,
                    "current_task_id": None,
                    "status": ManagedSessionStatus.IDLE,
                    "runtime_status": AgentRuntimeStatus.IDLE,
                    "updated_at": _now(),
                }
            )

    def _assign_current_task(self, session_id: str, task_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            return
        self.sessions[session_id] = session.model_copy(
            update={
                "task_id": task_id,
                "current_task_id": task_id,
                "updated_at": _now(),
            }
        )


workspace_manager = WorkspaceManager()
