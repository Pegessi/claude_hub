import asyncio
import json
import logging
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..models import (
    AgentReport,
    AgentReportCreate,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    ManagedSession,
    ManagedSessionStatus,
    TerminalAgentStatus,
    Workspace,
    WorkspaceBoard,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskStatus,
)
from .ttyd_manager import ttyd_manager

logger = logging.getLogger(__name__)

STATE_FILE = Path.home() / ".claude_hub" / "workspaces.json"
WORKSPACE_ROOT = Path.home() / ".claude_hub" / "projects"


def _now() -> datetime:
    return datetime.now()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "workspace"


def _load_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class WorkspaceManager:
    """Human-orchestrated workspace/task/session layer above TTYDManager."""

    def __init__(self) -> None:
        self.workspaces: dict[str, Workspace] = {}
        self.tasks: dict[str, WorkspaceTask] = {}
        self.sessions: dict[str, ManagedSession] = {}
        self.reports: dict[str, AgentReport] = {}
        self._load_state()

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            self.workspaces = {item["id"]: Workspace(**item) for item in data.get("workspaces", [])}
            self.tasks = {item["id"]: WorkspaceTask(**item) for item in data.get("tasks", [])}
            self.sessions = {
                item["id"]: ManagedSession(**item) for item in data.get("sessions", [])
            }
            self.reports = {item["id"]: AgentReport(**item) for item in data.get("reports", [])}
        except Exception as e:
            logger.error(f"Failed to load workspace state: {e}")

    def _save_state(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "workspaces": [item.model_dump(mode="json") for item in self.workspaces.values()],
            "tasks": [item.model_dump(mode="json") for item in self.tasks.values()],
            "sessions": [item.model_dump(mode="json") for item in self.sessions.values()],
            "reports": [item.model_dump(mode="json") for item in self.reports.values()],
        }
        STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def list_workspaces(self) -> list[Workspace]:
        return sorted(self.workspaces.values(), key=lambda item: item.created_at)

    def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        return self.workspaces.get(workspace_id)

    def create_workspace(self, payload: WorkspaceCreate) -> Workspace:
        source_path = Path(payload.path).expanduser().resolve()
        if not source_path.exists() or not source_path.is_dir():
            raise ValueError(f"Workspace path does not exist: {source_path}")

        workspace_id = str(uuid.uuid4())
        now = _now()
        prefix = payload.session_prefix or _slug(payload.name)
        workspace = Workspace(
            id=workspace_id,
            name=payload.name,
            path=str(source_path),
            default_branch=payload.default_branch,
            session_prefix=prefix,
            agent_session_id=None,
            created_at=now,
            updated_at=now,
        )
        self.workspaces[workspace_id] = workspace
        self._save_state()
        return workspace

    def create_task(self, workspace_id: str, payload: WorkspaceTaskCreate) -> WorkspaceTask:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        task_id = str(uuid.uuid4())
        now = _now()
        task = WorkspaceTask(
            id=task_id,
            workspace_id=workspace_id,
            title=payload.title,
            prompt=payload.prompt,
            agent_type=payload.agent_type,
            status=WorkspaceTaskStatus.TODO,
            created_at=now,
            updated_at=now,
        )
        self.tasks[task_id] = task
        self._save_state()
        return task

    def update_task_status(self, task_id: str, status: WorkspaceTaskStatus) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        updated = task.model_copy(update={"status": status, "updated_at": _now()})
        self.tasks[task_id] = updated
        self._save_state()
        return updated

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
            if session.task_id == task_id:
                self.sessions[session_id] = session.model_copy(
                    update={"task_id": None, "updated_at": _now()}
                )
        self._save_state()

    async def ensure_workspace_agent(
        self,
        workspace_id: str,
        agent_type: AgentType = AgentType.CODEX,
    ) -> ManagedSession:
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            raise KeyError(workspace_id)

        if workspace.agent_session_id:
            existing = self.sessions.get(workspace.agent_session_id)
            if existing and existing.status != ManagedSessionStatus.STOPPED:
                self._sync_session_tab_metadata(existing)
                return existing

        for session in self.sessions_for_workspace(workspace_id):
            if (
                session.role == WorkspaceSessionRole.ORCHESTRATOR
                and session.status != ManagedSessionStatus.STOPPED
            ):
                self.workspaces[workspace_id] = workspace.model_copy(
                    update={"agent_session_id": session.id, "updated_at": _now()}
                )
                self._save_state()
                self._sync_session_tab_metadata(session)
                return session

        session_id = f"{workspace.session_prefix}-agent"
        tab = await ttyd_manager.create_tab(
            name=f"{workspace.name} Agent",
            cwd=workspace.path,
            solo_mode=True,
            agent_type=agent_type,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            workspace_role=WorkspaceSessionRole.ORCHESTRATOR,
        )
        now = _now()
        session = ManagedSession(
            id=session_id,
            workspace_id=workspace.id,
            task_id=None,
            tab_id=tab.id,
            role=WorkspaceSessionRole.ORCHESTRATOR,
            agent_type=agent_type,
            status=ManagedSessionStatus.SPAWNING,
            title=f"{workspace.name} Agent",
            branch=None,
            workspace_path=workspace.path,
            tmux_session=f"claude-hub-{tab.id[:8]}",
            created_at=now,
            updated_at=now,
        )
        self.sessions[session.id] = session
        self.workspaces[workspace.id] = workspace.model_copy(
            update={"agent_session_id": session.id, "updated_at": now}
        )
        self._save_state()

        await self.send_session_message(
            session.id, self._build_workspace_agent_prompt(workspace, session)
        )
        return session

    async def start_task(
        self, task_id: str, agent_type: Optional[AgentType] = None
    ) -> ManagedSession:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            raise KeyError(task.workspace_id)

        session = await self.ensure_workspace_agent(workspace.id, agent_type or task.agent_type)
        now = _now()
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.ASSIGNED,
                "session_id": session.id,
                "updated_at": now,
            }
        )
        self._save_state()
        await self.send_session_message(
            session.id,
            self._build_task_assignment_prompt(workspace, self.tasks[task.id], session),
        )
        return session

    async def spawn_worker(
        self,
        task_id: str,
        agent_type: Optional[AgentType] = None,
    ) -> ManagedSession:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            raise KeyError(task.workspace_id)
        if task.session_id:
            existing = self.sessions.get(task.session_id)
            if existing:
                return existing

        session_id = (
            f"{workspace.session_prefix}-{len(self.sessions_for_workspace(workspace.id)) + 1}"
        )
        selected_agent = agent_type or task.agent_type
        branch = f"chub/{_slug(task.title)[:36]}-{session_id}"
        worktree_path = await self._create_worktree(workspace, session_id, branch)

        tab = await ttyd_manager.create_tab(
            name=task.title,
            cwd=str(worktree_path),
            solo_mode=True,
            agent_type=selected_agent,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            workspace_role=WorkspaceSessionRole.WORKER,
        )

        now = _now()
        session = ManagedSession(
            id=session_id,
            workspace_id=workspace.id,
            task_id=task.id,
            tab_id=tab.id,
            role=WorkspaceSessionRole.WORKER,
            agent_type=selected_agent,
            status=ManagedSessionStatus.SPAWNING,
            title=task.title,
            branch=branch,
            workspace_path=str(worktree_path),
            tmux_session=f"claude-hub-{tab.id[:8]}",
            created_at=now,
            updated_at=now,
        )
        self.sessions[session.id] = session
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.WORKING,
                "session_id": session.id,
                "updated_at": now,
            }
        )
        self._save_state()

        await self.send_session_message(
            session.id, self._build_worker_prompt(workspace, task, session)
        )
        return session

    async def _create_worktree(self, workspace: Workspace, session_id: str, branch: str) -> Path:
        project_dir = WORKSPACE_ROOT / workspace.id / "worktrees"
        project_dir.mkdir(parents=True, exist_ok=True)
        worktree_path = project_dir / session_id
        if worktree_path.exists():
            return worktree_path

        source_path = Path(workspace.path)
        if not (source_path / ".git").exists():
            raise ValueError(f"Workspace path is not a git repository: {source_path}")

        proc = await asyncio.create_subprocess_exec(
            "git",
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_path),
            workspace.default_branch,
            cwd=str(source_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or "git worktree add failed")
        return worktree_path

    def _build_worker_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
    ) -> str:
        return (
            f"You are a worker session in Claude Hub Agent Workspace.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Task: {task.title}\n"
            f"Session: {session.id}\n"
            f"Branch: {session.branch}\n"
            f"Worktree: {session.workspace_path}\n\n"
            f"Instructions:\n{task.prompt}\n\n"
            "Work only inside this worktree.\n\n"
            "Report progress back to Claude Hub when you start, get blocked, need input, "
            "are ready for review, or complete the work. Use this local endpoint:\n"
            f"curl -sS -X POST http://localhost:8173/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            '-d \'{"state":"working","message":"Started implementation"}\'\n\n'
            "Allowed report states: started, working, blocked, needs_input, "
            "ready_for_review, completed. Final reports should include message, "
            "changed_files, validation, and risks."
        )

    def _build_workspace_agent_prompt(self, workspace: Workspace, session: ManagedSession) -> str:
        return (
            "You are the resident Claude Hub workspace agent.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Session: {session.id}\n"
            f"Repository path: {workspace.path}\n\n"
            "Stay in this terminal and wait for assigned tasks. Do not start unrelated work. "
            "When a task is assigned, execute it in this repository, report progress to Claude Hub, "
            "and keep the terminal available so the user can inspect progress at any time."
        )

    def _build_task_assignment_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        session: ManagedSession,
    ) -> str:
        return (
            "New Claude Hub task assigned.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Repository path: {workspace.path}\n\n"
            f"Task description:\n{task.prompt}\n\n"
            "Start by reporting state started, then report working as you make progress. "
            "If blocked, report blocked or needs_input. When ready for human review, report "
            "ready_for_review or completed.\n\n"
            "Report endpoint example:\n"
            f"curl -sS -X POST http://localhost:8173/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"started",'
            '"message":"Started task"}\'\n\n'
            "Final reports should include task_id, state, message, changed_files, validation, and risks."
        )

    async def send_session_message(self, session_id: str, message: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)
        await self._send_tmux_message(session.tmux_session, message)

    def create_report(self, session_id: str, payload: AgentReportCreate) -> AgentReport:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        now = _now()
        task_id = payload.task_id or session.task_id
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
        self.sessions[session.id] = session.model_copy(
            update={
                "status": session_status,
                "last_activity_at": now,
                "updated_at": now,
            }
        )
        if task_id and task_id in self.tasks:
            task_status = self._task_status_from_report(payload.state)
            if task_status:
                self.tasks[task_id] = self.tasks[task_id].model_copy(
                    update={"status": task_status, "updated_at": now}
                )

        self._save_state()
        return report

    def reports_for_workspace(self, workspace_id: str) -> list[AgentReport]:
        return sorted(
            [report for report in self.reports.values() if report.workspace_id == workspace_id],
            key=lambda report: report.created_at,
        )

    async def _send_tmux_message(self, tmux_session: str, message: str) -> None:
        clear_proc = await asyncio.create_subprocess_exec(
            "tmux",
            "send-keys",
            "-t",
            tmux_session,
            "C-u",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await clear_proc.wait()
        await asyncio.sleep(0.2)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(message)
            tmp_path = tmp.name
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "load-buffer",
                tmp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode("utf-8", errors="ignore").strip())
            paste_proc = await asyncio.create_subprocess_exec(
                "tmux",
                "paste-buffer",
                "-t",
                tmux_session,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await paste_proc.wait()
            await asyncio.sleep(0.1)
            enter_proc = await asyncio.create_subprocess_exec(
                "tmux",
                "send-keys",
                "-t",
                tmux_session,
                "Enter",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await enter_proc.wait()
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def sessions_for_workspace(self, workspace_id: str) -> list[ManagedSession]:
        return [
            session for session in self.sessions.values() if session.workspace_id == workspace_id
        ]

    async def get_board(self, workspace_id: str) -> WorkspaceBoard:
        workspace = self.workspaces.get(workspace_id)
        if not workspace:
            raise KeyError(workspace_id)

        await self._refresh_session_statuses()
        self._sync_workspace_tab_metadata(workspace_id)
        tasks = [task for task in self.tasks.values() if task.workspace_id == workspace_id]
        sessions = self.sessions_for_workspace(workspace_id)
        reports = self.reports_for_workspace(workspace_id)
        return WorkspaceBoard(workspace=workspace, tasks=tasks, sessions=sessions, reports=reports)

    def _sync_workspace_tab_metadata(self, workspace_id: str) -> None:
        for session in self.sessions_for_workspace(workspace_id):
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
            if next_status == ManagedSessionStatus.STOPPED and self._is_spawn_grace_period(session):
                continue
            update: dict[str, Any] = {
                "status": next_status,
                "updated_at": status.sampled_at,
            }
            if status.last_changed_at:
                update["last_activity_at"] = status.last_changed_at
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


workspace_manager = WorkspaceManager()
