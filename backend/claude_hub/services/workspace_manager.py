import asyncio
import base64
import binascii
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
    RequestTaskReviewRequest,
    ReviewDecision,
    StartTaskRequest,
    TerminalAgentStatus,
    Workspace,
    WorkspaceAttachment,
    WorkspaceAttachmentCreate,
    WorkspaceBoard,
    WorkspaceCreate,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskCreate,
    WorkspaceTaskStatus,
    WorkspaceTaskUpdate,
    WorkspaceUpdate,
)
from . import workspace_state_policy as state_policy
from .remote_profiles import remote_profile_manager
from .ttyd_manager import ttyd_manager

logger = logging.getLogger(__name__)

STATE_ROOT = Path.home() / ".claude_hub" / "workspaces"
INDEX_FILE = STATE_ROOT / "index.json"
LEGACY_STATE_FILE = Path.home() / ".claude_hub" / "workspaces.json"
REMOTE_FORWARD_PORT_BASE = 18173
TMUX_SUBMIT_ATTEMPTS = 3
TMUX_PASTE_SETTLE_SECONDS = 0.35
TMUX_SUBMIT_SETTLE_SECONDS = 0.7
AUTO_CONTINUE_MAX_ATTEMPTS = 10
AUTO_CONTINUE_MIN_INTERVAL_SECONDS = 15
AUTO_CONTINUE_IDLE_GRACE_SECONDS = 20
REVIEW_RUNTIME_REOPEN_GRACE_SECONDS = 20
MAX_AUTOMATED_REVIEW_FAILURES = 3
WORKSPACE_MONITOR_INTERVAL_SECONDS = 5
AUTO_CONTINUE_MESSAGE = (
    "Please inspect the current task state. If the task was interrupted or is unfinished, "
    "continue from the last actionable step. If the task is already complete and only missed "
    "the workspace report, immediately POST a ready_for_review or completed report instead of "
    "doing more work."
)
AUTO_REPORT_MISSING_MESSAGE = (
    "The task appears complete but no workspace report was recorded. Please immediately POST "
    "the final ready_for_review or completed report with changed_files, validation, risks, "
    "the stored Goal Packet if it has not been reported yet, and acceptance_check evidence; "
    "only continue work if you find it is actually unfinished."
)
ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024
IMAGE_ATTACHMENT_TYPES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _now() -> datetime:
    return datetime.now()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "workspace"


def _sort_time(task: WorkspaceTask) -> datetime:
    return task.queued_at or task.created_at


def _safe_attachment_filename(value: str, suffix: str) -> str:
    stem = Path(value or "attachment").stem
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip(".-")
    return f"{slug or 'attachment'}{suffix}"


class WorkspaceManager:
    """Human-orchestrated workspace/task/session layer above TTYDManager."""

    def __init__(self) -> None:
        self.workspaces: dict[str, Workspace] = {}
        self.tasks: dict[str, WorkspaceTask] = {}
        self.sessions: dict[str, ManagedSession] = {}
        self.reports: dict[str, AgentReport] = {}
        self._dispatch_locks: dict[str, asyncio.Lock] = {}
        self._monitor_task: asyncio.Task[None] | None = None
        self._load_state()

    def _workspace_dir(self, workspace_id: str) -> Path:
        return STATE_ROOT / workspace_id

    def _workspace_state_file(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "state.json"

    def _workspace_task_records_dir(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "task_records"

    def _workspace_attachments_dir(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "attachments"

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
                    report = AgentReport(**self._normalize_report_item(item))
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
                item["id"]: AgentReport(**self._normalize_report_item(item))
                for item in data.get("reports", [])
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
        normalized.setdefault("attachments", [])
        normalized.setdefault("clear_context", None)
        normalized.setdefault("dispatch_reason", None)
        normalized.setdefault("dispatch_pending", False)
        normalized.setdefault("review_session_id", None)
        normalized.setdefault("review_attempts", 0)
        normalized.setdefault("review_requested_at", None)
        normalized.setdefault("review_completed_at", None)
        normalized.setdefault("review_skipped_at", None)
        normalized.setdefault("review_skip_reason", None)
        normalized.setdefault("human_acceptance_requested_at", None)
        normalized.setdefault("human_accepted_at", None)
        normalized.setdefault("queued_at", None)
        normalized.setdefault("started_at", None)
        normalized.setdefault("reviewed_at", None)
        normalized.setdefault("completed_at", None)
        normalized["goal_packet"] = self._normalize_goal_packet(normalized.get("goal_packet"))
        return normalized

    def _normalize_report_item(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        normalized["acceptance_check"] = self._normalize_acceptance_check(
            normalized.get("acceptance_check")
        )
        return normalized

    def _normalize_goal_packet(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        objective = value.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            return None
        normalized = dict(value)
        normalized["objective"] = objective.strip()
        for field in (
            "acceptance_criteria",
            "validation_plan",
            "assumptions",
            "out_of_scope",
            "handoff_requirements",
        ):
            items = normalized.get(field)
            if isinstance(items, list):
                normalized[field] = [str(item) for item in items if str(item).strip()]
            elif isinstance(items, str) and items.strip():
                normalized[field] = [items.strip()]
            else:
                normalized[field] = []
        normalized.setdefault("source", "agent_generated")
        if normalized.get("status") not in {"draft", "frozen", "superseded"}:
            normalized["status"] = "draft"
        normalized.setdefault("created_at", None)
        normalized.setdefault("updated_at", None)
        return normalized

    def _normalize_acceptance_check(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            criterion = item.get("criterion")
            evidence = item.get("evidence")
            if not isinstance(criterion, str) or not criterion.strip():
                continue
            if not isinstance(evidence, str) or not evidence.strip():
                evidence = "No evidence provided."
            status = item.get("status", "not_checked")
            if status not in {"passed", "failed", "partial", "not_checked"}:
                status = "not_checked"
            normalized.append(
                {
                    "criterion": criterion.strip(),
                    "status": status,
                    "evidence": evidence.strip(),
                }
            )
        return normalized

    def _normalize_session_item(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        normalized.setdefault("runtime_status", self._runtime_from_managed_status(normalized))
        normalized.setdefault("current_task_id", normalized.get("task_id"))
        normalized.setdefault("queued_count", 0)
        normalized.setdefault(
            "target",
            (
                ExecutionTarget.REMOTE.value
                if normalized.get("remote_forward_port")
                else ExecutionTarget.LOCAL.value
            ),
        )
        normalized.setdefault("remote_profile_id", None)
        normalized.setdefault("remote_cwd", None)
        normalized.setdefault("remote_reconnect", True)
        normalized.setdefault("solo_mode", True)
        normalized.setdefault("ephemeral", False)
        normalized.setdefault("remote_forward_port", None)
        normalized.setdefault("auto_continue_task_id", None)
        normalized.setdefault("auto_continue_attempts", 0)
        normalized.setdefault("last_auto_continue_at", None)
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
                "- Reviewer agents are independent quality gates; dispatcher agents are reserved for future smart assignment flows.",
            ]
        )
        self.snapshot_path(workspace_id).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def list_workspaces(self) -> list[Workspace]:
        return sorted(self.workspaces.values(), key=lambda item: item.created_at)

    def start_background_monitor(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.create_task(self._background_monitor_loop())

    async def stop_background_monitor(self) -> None:
        if not self._monitor_task:
            return
        self._monitor_task.cancel()
        try:
            await self._monitor_task
        except asyncio.CancelledError:
            pass
        finally:
            self._monitor_task = None

    async def _background_monitor_loop(self) -> None:
        while True:
            try:
                await self._refresh_session_statuses(run_auto_continue=True)
                for workspace_id in list(self.workspaces):
                    await self.dispatch_workspace(workspace_id, refresh_sessions=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Workspace background monitor failed")
            await asyncio.sleep(WORKSPACE_MONITOR_INTERVAL_SECONDS)

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

    def update_workspace(self, workspace_id: str, payload: WorkspaceUpdate) -> Workspace:
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)

        update_kwargs: dict[str, Any] = {}
        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                raise ValueError("Workspace name cannot be empty")
            update_kwargs["name"] = name
        if payload.path is not None:
            new_path = payload.path.strip()
            if not new_path:
                raise ValueError("Local workspace dir cannot be empty")
            resolved = Path(new_path).expanduser().resolve()
            if not resolved.exists() or not resolved.is_dir():
                raise ValueError(f"Local workspace dir does not exist: {resolved}")
            update_kwargs["path"] = str(resolved)
        if payload.default_branch is not None:
            branch = payload.default_branch.strip()
            if not branch:
                raise ValueError("Default branch cannot be empty")
            update_kwargs["default_branch"] = branch
        if payload.remote_cwd is not None:
            value = payload.remote_cwd.strip()
            update_kwargs["remote_cwd"] = value or None
        if payload.remote_reconnect is not None:
            update_kwargs["remote_reconnect"] = payload.remote_reconnect

        if not update_kwargs:
            return workspace

        updated = workspace.model_copy(update={**update_kwargs, "updated_at": _now()})
        self.workspaces[workspace_id] = updated
        self._save_state()
        return updated

    def create_task(self, workspace_id: str, payload: WorkspaceTaskCreate) -> WorkspaceTask:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)
        if payload.related_task_id and payload.related_task_id not in self.tasks:
            raise KeyError(payload.related_task_id)

        task_id = str(uuid.uuid4())
        now = _now()
        attachments = self._persist_attachments(workspace_id, task_id, payload.attachments)
        task = WorkspaceTask(
            id=task_id,
            workspace_id=workspace_id,
            title=payload.title,
            prompt=payload.prompt,
            attachments=attachments,
            goal_packet=payload.goal_packet,
            agent_type=payload.agent_type,
            status=WorkspaceTaskStatus.TODO,
            related_task_id=payload.related_task_id,
            created_at=now,
            updated_at=now,
        )
        self.tasks[task_id] = task
        self._save_state()
        logger.info(
            "Created workspace task id=%s workspace_id=%s title=%r related_task_id=%s agent_type=%s",
            task.id,
            workspace_id,
            task.title,
            task.related_task_id,
            task.agent_type,
        )
        return task

    def _persist_attachments(
        self,
        workspace_id: str,
        owner_id: str,
        attachments: list[WorkspaceAttachmentCreate],
    ) -> list[WorkspaceAttachment]:
        persisted: list[WorkspaceAttachment] = []
        if not attachments:
            return persisted

        owner_dir = self._workspace_attachments_dir(workspace_id) / owner_id
        owner_dir.mkdir(parents=True, exist_ok=True)
        for item in attachments:
            mime_type = item.mime_type.strip().lower()
            suffix = IMAGE_ATTACHMENT_TYPES.get(mime_type)
            if not suffix:
                raise ValueError(f"Unsupported attachment type: {item.mime_type}")
            header = f"data:{mime_type};base64,"
            if not item.data_url.startswith(header):
                raise ValueError("Attachment data must be a matching base64 data URL")
            try:
                content = base64.b64decode(item.data_url[len(header) :], validate=True)
            except binascii.Error as exc:
                raise ValueError("Invalid attachment data") from exc
            if not content:
                raise ValueError("Attachment data is empty")
            if len(content) > ATTACHMENT_MAX_BYTES:
                raise ValueError("Attachment exceeds the 8 MB limit")

            attachment_id = uuid.uuid4().hex
            filename = _safe_attachment_filename(item.filename, suffix)
            path = owner_dir / f"{attachment_id}-{filename}"
            path.write_bytes(content)
            persisted.append(
                WorkspaceAttachment(
                    id=attachment_id,
                    filename=filename,
                    mime_type=mime_type,
                    path=str(path),
                    size_bytes=len(content),
                )
            )
        return persisted

    def _attachment_prompt_block(self, attachments: list[WorkspaceAttachment]) -> str:
        if not attachments:
            return ""
        lines = ["Attachments:"]
        for attachment in attachments:
            lines.append(
                f"- {attachment.filename} ({attachment.mime_type}, {attachment.size_bytes} bytes): "
                f"{attachment.path}"
            )
        return "\n".join(lines)

    def _append_attachment_block(self, message: str, attachments: list[WorkspaceAttachment]) -> str:
        block = self._attachment_prompt_block(attachments)
        if not block:
            return message
        if not message.strip():
            return block
        return f"{message.rstrip()}\n\n{block}"

    def get_attachment(self, attachment_id: str) -> WorkspaceAttachment:
        for task in self.tasks.values():
            for attachment in task.attachments:
                if attachment.id == attachment_id:
                    return attachment
        raise KeyError(attachment_id)

    async def update_task_status(
        self,
        task_id: str,
        status: WorkspaceTaskStatus,
    ) -> WorkspaceTask:
        return await self.update_task(task_id, WorkspaceTaskUpdate(status=status))

    async def update_task(
        self,
        task_id: str,
        payload: WorkspaceTaskUpdate,
    ) -> WorkspaceTask:
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)

        now = _now()
        update: dict[str, Any] = {"updated_at": now}
        if payload.goal_packet is not None:
            update["goal_packet"] = payload.goal_packet
        status = payload.status
        if status is not None:
            update["status"] = status
            if status == WorkspaceTaskStatus.QUEUED:
                update["queued_at"] = task.queued_at or now
            elif status == WorkspaceTaskStatus.WORKING:
                update["started_at"] = task.started_at or now
                update["human_acceptance_requested_at"] = None
                update["human_accepted_at"] = None
            elif status == WorkspaceTaskStatus.REVIEW:
                update["reviewed_at"] = now
                update["human_acceptance_requested_at"] = task.human_acceptance_requested_at or now
            elif status == WorkspaceTaskStatus.DONE:
                update["completed_at"] = now
                update["human_accepted_at"] = now

        self.tasks[task.id] = task.model_copy(update=update)
        if status == WorkspaceTaskStatus.DONE:
            self._write_task_record(self.tasks[task.id])
            self._release_task_session(self.tasks[task.id])
        elif status == WorkspaceTaskStatus.WORKING and task.session_id:
            self._assign_current_task(task.session_id, task.id)

        self._save_state()
        if status is not None:
            await self.dispatch_workspace(task.workspace_id)
        return self.tasks[task.id]

    def _write_task_record(self, task: WorkspaceTask) -> None:
        completed_at = task.completed_at or _now()
        record_dir = self._workspace_task_records_dir(task.workspace_id)
        record_dir.mkdir(parents=True, exist_ok=True)
        timestamp = completed_at.isoformat(timespec="seconds").replace(":", "-")
        record_path = record_dir / f"{timestamp}-{task.id}.json"
        task_reports = [
            report
            for report in self.reports_for_workspace(task.workspace_id)
            if report.task_id == task.id
        ]
        session = self.sessions.get(task.session_id or "")
        payload = {
            "schema_version": 1,
            "archived_at": _now().isoformat(),
            "workspace_id": task.workspace_id,
            "task": task.model_dump(mode="json"),
            "session": session.model_dump(mode="json") if session else None,
            "reports": [report.model_dump(mode="json") for report in task_reports],
            "timeline": self._build_task_record_timeline(task, task_reports),
            "artifacts": self._build_task_record_artifacts(task_reports),
            "final_summary": self._task_record_final_summary(task_reports),
        }
        record_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _build_task_record_timeline(
        self,
        task: WorkspaceTask,
        reports: list[AgentReport],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = [
            {"at": task.created_at.isoformat(), "type": "task_created", "title": task.title}
        ]
        for field, event_type in (
            ("queued_at", "task_queued"),
            ("started_at", "task_started"),
            ("reviewed_at", "task_reviewed"),
            ("completed_at", "task_completed"),
        ):
            value = getattr(task, field)
            if value:
                events.append({"at": value.isoformat(), "type": event_type})
        for report in reports:
            events.append(
                {
                    "at": report.created_at.isoformat(),
                    "type": "agent_report",
                    "state": report.state.value,
                    "session_id": report.session_id,
                    "message": report.message,
                    "review_decision": report.review_decision.value,
                    "review_reason": report.review_reason,
                }
            )
        return sorted(events, key=lambda item: item["at"])

    def _build_task_record_artifacts(self, reports: list[AgentReport]) -> dict[str, Any]:
        changed_files: list[str] = []
        validations: list[str] = []
        risks: list[str] = []
        for report in reports:
            for file_path in report.changed_files:
                if file_path not in changed_files:
                    changed_files.append(file_path)
            if report.validation:
                validations.append(report.validation)
            if report.risks:
                risks.append(report.risks)
        return {
            "changed_files": changed_files,
            "commits": [],
            "validation": validations,
            "risks": risks,
        }

    def _task_record_final_summary(self, reports: list[AgentReport]) -> str:
        for report in reversed(reports):
            if report.state in {
                AgentReportState.COMPLETED,
                AgentReportState.READY_FOR_REVIEW,
            }:
                return report.message
        return reports[-1].message if reports else ""

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
        elif payload.role == WorkspaceSessionRole.REVIEWER and payload.reuse_existing:
            existing = self._first_available_reviewer(workspace.id)
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
            [
                session
                for session in self._sessions_for_workspace_raw(workspace.id)
                if session.role == role
            ]
        )
        if role == WorkspaceSessionRole.DISPATCHER:
            session_id = f"{workspace.session_prefix}-dispatcher"
            title = payload.title or f"{workspace.name} Dispatcher"
        elif role == WorkspaceSessionRole.REVIEWER:
            session_id = f"{workspace.session_prefix}-reviewer-{role_count + 1}"
            title = payload.title or f"{workspace.name} Reviewer {role_count + 1}"
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
        session_workspace_path = (
            (remote_cwd or local_cwd) if session_target == ExecutionTarget.REMOTE else local_cwd
        )
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
            ephemeral=payload.ephemeral,
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
            if (task.session_id == session_id or task.review_session_id == session_id)
            and task.status != WorkspaceTaskStatus.DONE
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

        logger.info(
            "Starting workspace task id=%s workspace_id=%s title=%r payload_target_session_id=%s "
            "payload_related_task_id=%s stored_related_task_id=%s current_session_id=%s status=%s",
            task.id,
            workspace.id,
            task.title,
            payload.target_session_id,
            payload.related_task_id,
            task.related_task_id,
            task.session_id,
            task.status,
        )
        await self._refresh_session_statuses(workspace.id)

        if not self._workspace_agents(workspace.id, include_stopped=True):
            logger.info(
                "No workspace agents found for workspace_id=%s; creating default agent for task id=%s",
                workspace.id,
                task.id,
            )
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
            logger.info(
                "Workspace task id=%s is waiting for dispatcher decision related_task_id=%s",
                task.id,
                task.related_task_id,
            )
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
        logger.info(
            "Workspace task id=%s queued for session_id=%s session_title=%r related_task_id=%s "
            "clear_context=%s reason=%r",
            task.id,
            target.id,
            target.title,
            task.related_task_id,
            clear_context,
            reason,
        )
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
            if not self._can_assign_or_queue_to(target):
                if (
                    target.status == ManagedSessionStatus.STOPPED
                    or target.runtime_status == AgentRuntimeStatus.OFFLINE
                ):
                    raise RuntimeError("Offline workspace agents cannot accept tasks")
                if target.runtime_status == AgentRuntimeStatus.ATTENTION:
                    raise RuntimeError("Workspace agents waiting for input cannot accept new tasks")
                raise RuntimeError("Selected workspace agent cannot accept tasks yet")
            return (
                target,
                bool(payload.clear_context),
                "User selected target agent",
            )

        related_task_id = payload.related_task_id or task.related_task_id
        if related_task_id:
            related = self.tasks.get(related_task_id)
            target = None
            if related and related.session_id:
                target = self.sessions.get(related.session_id)
                if target and self._can_assign_or_queue_to(target):
                    logger.info(
                        "Dispatch target selected from related task: task_id=%s related_task_id=%s "
                        "target_session_id=%s related_status=%s",
                        task.id,
                        related_task_id,
                        target.id,
                        related.status,
                    )
                    return (
                        target,
                        False,
                        f"Related to task {related_task_id}",
                    )
            logger.info(
                "Related task did not provide a dispatch target: task_id=%s related_task_id=%s "
                "related_exists=%s related_session_id=%s related_target_runtime=%s",
                task.id,
                related_task_id,
                bool(related),
                related.session_id if related else None,
                target.runtime_status if target else None,
            )

        if task.session_id:
            existing = self.sessions.get(task.session_id)
            if existing and self._can_assign_or_queue_to(existing):
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

        queueable_agents = [
            agent
            for agent in agents
            if agent.status != ManagedSessionStatus.STOPPED
            and (
                agent.runtime_status == AgentRuntimeStatus.WORKING
                or self._is_holding_unresolved_review_task(agent)
            )
        ]
        if queueable_agents:
            target = self._least_queued_agent(queueable_agents)
            should_clear = self._has_prior_task_history(target.id)
            return (
                target,
                payload.clear_context if payload.clear_context is not None else should_clear,
                "Queued behind existing workspace agent",
            )

        if agents:
            raise RuntimeError("No idle or working workspace agent is available")

        return None

    def _can_assign_or_queue_to(self, session: ManagedSession) -> bool:
        if session.role != WorkspaceSessionRole.ORCHESTRATOR:
            return False
        if session.status == ManagedSessionStatus.STOPPED:
            return False
        if session.runtime_status == AgentRuntimeStatus.WORKING:
            return True
        if self._is_holding_unresolved_review_task(session):
            return True
        return self._can_dispatch_to(session)

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

        now = _now()
        self._ensure_session_can_continue_task(self.sessions[task.session_id], task)
        session = await self._rename_session_for_task(
            self.sessions[task.session_id],
            task,
            updated_at=now,
        )
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.WORKING,
                "started_at": now,
                "review_skipped_at": None,
                "review_skip_reason": None,
                "human_acceptance_requested_at": None,
                "human_accepted_at": None,
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
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": 0,
                "last_auto_continue_at": None,
                "updated_at": now,
                "last_activity_at": now,
            }
        )
        continue_report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=session.id,
            state=AgentReportState.WORKING,
            message=payload.message or "Task continued from review",
            changed_files=[],
            validation=None,
            risks=None,
            created_at=now,
        )
        self.reports[continue_report.id] = continue_report
        self._save_state()

        await self.send_session_message(
            session.id,
            self._build_continue_prompt(self.tasks[task.id], payload),
        )
        return self.tasks[task.id]

    def _ensure_session_can_continue_task(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
    ) -> None:
        if session.role != WorkspaceSessionRole.ORCHESTRATOR:
            raise RuntimeError("Review task original session is not a workspace agent")
        if session.status == ManagedSessionStatus.STOPPED:
            raise RuntimeError("Review task original agent is stopped")

        assigned_ids = {
            assigned_id for assigned_id in (session.task_id, session.current_task_id) if assigned_id
        }
        busy_ids = [assigned_id for assigned_id in assigned_ids if assigned_id != task.id]
        for busy_id in busy_ids:
            busy_task = self.tasks.get(busy_id)
            if not busy_task or busy_task.status != WorkspaceTaskStatus.DONE:
                raise RuntimeError(
                    "Review task original agent is busy with another task; "
                    "wait for that task to finish before requesting changes."
                )

    async def request_task_review(
        self,
        task_id: str,
        payload: RequestTaskReviewRequest | None = None,
    ) -> WorkspaceTask:
        payload = payload or RequestTaskReviewRequest()
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        if task.status == WorkspaceTaskStatus.DONE:
            raise RuntimeError("Done tasks cannot request review")
        if task.review_requested_at and not task.review_completed_at:
            return task
        if not task.session_id:
            raise RuntimeError("Task has no implementation agent")

        now = _now()
        message = (
            payload.message.strip()
            if payload.message and payload.message.strip()
            else "Human requested reviewer checks."
        )
        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=task.workspace_id,
            task_id=task.id,
            session_id=task.session_id,
            state=AgentReportState.READY_FOR_REVIEW,
            message=message,
            changed_files=[],
            validation=None,
            risks=None,
            review_decision=ReviewDecision.REQUEST,
            review_reason=message,
            risk_level=None,
            created_at=now,
        )
        self.reports[report.id] = report
        await self._request_task_review(task, report)
        return self.tasks[task.id]

    async def dispatch_workspace(
        self,
        workspace_id: str,
        *,
        refresh_sessions: bool = True,
    ) -> None:
        if workspace_id not in self.workspaces:
            raise KeyError(workspace_id)

        lock = self._dispatch_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            await self._dispatch_workspace_locked(
                workspace_id,
                refresh_sessions=refresh_sessions,
            )

    async def _dispatch_workspace_locked(
        self,
        workspace_id: str,
        *,
        refresh_sessions: bool,
    ) -> None:
        if refresh_sessions:
            await self._refresh_session_statuses(workspace_id)
        for session in self._workspace_agents(workspace_id, include_stopped=True):
            if not self._can_dispatch_to(session):
                logger.info(
                    "Skipping workspace session dispatch workspace_id=%s session_id=%s "
                    "runtime_status=%s task_id=%s current_task_id=%s",
                    workspace_id,
                    session.id,
                    session.runtime_status,
                    session.task_id,
                    session.current_task_id,
                )
                continue
            next_task = self._next_queued_task(session.id)
            if not next_task:
                continue
            await self._dispatch_task_to_session(next_task, session)

    def _can_dispatch_to(self, session: ManagedSession) -> bool:
        if session.role != WorkspaceSessionRole.ORCHESTRATOR:
            return False
        if session.status == ManagedSessionStatus.STOPPED:
            return False
        if session.runtime_status != AgentRuntimeStatus.IDLE:
            return False
        if session.task_id or session.current_task_id:
            current_id = session.task_id or session.current_task_id
            current = self.tasks.get(current_id) if current_id else None
            if current and current.status not in {
                WorkspaceTaskStatus.DONE,
                WorkspaceTaskStatus.REVIEW,
            }:
                return False
            # While a task is in REVIEW we hold the original agent so that
            # reviewer-failure feedback can re-engage the same context. Once
            # the reviewer has approved the work, free the agent so the queue
            # can advance without waiting for a manual "done" click.
            if current and current.status == WorkspaceTaskStatus.REVIEW:
                if not self._is_review_passed(current):
                    return False
        return True

    def _is_review_passed(self, task: WorkspaceTask) -> bool:
        if task.review_completed_at is None:
            return False
        return self._latest_review_report_state(task.id) == AgentReportState.REVIEW_PASSED

    def _is_holding_unresolved_review_task(self, session: ManagedSession) -> bool:
        current_id = session.task_id or session.current_task_id
        if not current_id:
            return False
        current = self.tasks.get(current_id)
        if not current or current.status != WorkspaceTaskStatus.REVIEW:
            return False
        return not self._is_review_passed(current)

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

        session = await self._rename_session_for_task(session, task)

        if task.clear_context:
            await self.send_session_message(session.id, "/clear")
            await asyncio.sleep(0.5)

        logger.info(
            "Dispatching workspace task id=%s title=%r to session_id=%s session_title=%r "
            "related_task_id=%s dispatch_reason=%r",
            task.id,
            task.title,
            session.id,
            session.title,
            task.related_task_id,
            task.dispatch_reason,
        )
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
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": 0,
                "last_auto_continue_at": None,
                "last_activity_at": now,
                "updated_at": now,
            }
        )
        self._save_state()
        logger.info(
            "Workspace task id=%s dispatched to session_id=%s and marked working",
            task.id,
            session.id,
        )

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
        if session.role == WorkspaceSessionRole.REVIEWER:
            return self._build_reviewer_bootstrap_prompt(workspace, session)
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
            "When you report completed, the workspace may assign an independent reviewer. "
            "If reviewer feedback is sent back to you, continue from that feedback and report "
            "completed again when the fixes are done. "
            "Final reports may include review_decision: auto, request, or skip. This only controls "
            "whether an independent AI reviewer is requested; every completed task still waits for "
            "human acceptance before it is done. Use request when independent reviewer checks are "
            "needed, skip only for low-risk no-change analysis or manual follow-up that does not "
            "need AI reviewer checks, and include review_reason. "
            "Report progress to the workspace coordinator only after you receive a task, "
            "when you start, get blocked, need input, are ready for review, or complete the work. "
            "Every report should include both message_en (concise English) and message_zh "
            "(concise 中文) so the workspace UI can render either language; keep the legacy "
            "message field as a short fallback (English is fine).\n\n"
            "Report endpoint for assigned tasks:\n"
            f"curl -sS -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            '-d \'{"task_id":"TASK_ID","state":"working","message":"Progress update",'
            '"message_en":"Progress update","message_zh":"进度更新"}\''
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
            "Return decisions only by calling the provided local API endpoint. "
            "This dispatcher path is a reserved smart-assignment extension point and is "
            "independent from reviewer workflow decisions."
        )

    def _build_reviewer_bootstrap_prompt(
        self,
        workspace: Workspace,
        session: ManagedSession,
    ) -> str:
        return (
            "You are an independent reviewer agent for this workspace.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Session: {session.id}\n"
            f"{self._session_environment_lines(workspace, session)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            "Wait for explicit review assignments. Do not implement, refactor, format, or edit files.\n\n"
            "Reviewer operating contract:\n"
            "- Derive concrete acceptance criteria from the task description, user intent, "
            "recent task reports, changed files, and repository conventions.\n"
            "- Review against those criteria plus regression risk, integration fit, validation quality, "
            "and whether the implementation stayed within scope.\n"
            "- Treat reported validation as evidence to evaluate, not proof. Inspect enough code and "
            "state to decide whether it is adequate.\n"
            "- Report review_started when you begin.\n"
            "- Finish by reporting exactly one of review_passed, review_failed, or review_needs_input.\n\n"
            "Review exit rules:\n"
            "- Use review_passed only when all acceptance criteria are met, no blocking defects remain, "
            "validation is adequate for the risk, and residual risks are acceptable for final human acceptance.\n"
            "- Use review_failed when the implementation agent can fix concrete defects or missing checks. "
            "Include required fixes specific enough for the implementation agent to follow.\n"
            "- Use review_needs_input only when a product, credential, environment, or requirement decision "
            "is genuinely required before review can finish.\n\n"
            "Final review message format:\n"
            "Verdict: review_passed | review_failed | review_needs_input\n"
            "Acceptance criteria checked:\n"
            "- ...\n"
            "Findings:\n"
            "- Severity, file/area, issue, evidence, required fix or reason non-blocking\n"
            "Validation reviewed:\n"
            "- Commands/evidence reviewed, gaps, and whether gaps block acceptance\n"
            "Risks:\n"
            "- Residual risks or none\n\n"
            "Report endpoint for assigned reviews:\n"
            f"curl -sS -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            '-d \'{"task_id":"TASK_ID","state":"review_started","message":"Started review"}\''
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
            '-d \'{"target_session_id":"AGENT_ID","clear_context":false,'
            '"reason":"why this agent is best"}\''
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
        attachment_note = (
            f"{self._attachment_prompt_block(task.attachments)}\n\n" if task.attachments else ""
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
            f"{attachment_note}"
            "Start by reading the state snapshot. This workspace may contain many projects; "
            "use the task description to choose the correct directory before editing. "
            "Check for uncommitted file changes. "
            "Before substantive implementation, derive a Goal Packet from the original task "
            "prompt and include it in your first working report. The Goal Packet must preserve "
            "the user's requested outcome, record assumptions instead of silently narrowing "
            "ambiguous scope, and include concrete reviewer-checkable acceptance criteria, "
            "a validation plan, out-of-scope boundaries, and final handoff requirements.\n\n"
            "Report state started, then report working as you make progress. "
            "If blocked or waiting for user input, report blocked or needs_input. "
            "When ready for human review, report ready_for_review. When you believe the task is "
            "fully complete, report completed. The task is not finally done until a human accepts it.\n\n"
            "For completed reports, decide reviewer routing explicitly:\n"
            "- review_decision=request when this should go to an independent AI reviewer before human acceptance.\n"
            "- review_decision=skip only for low-risk no-change analysis or manual follow-up "
            "where AI reviewer checks are unnecessary; this still requires human acceptance.\n"
            "- review_decision=auto to use the workspace default reviewer policy.\n"
            "Always include review_reason when choosing request or skip. The backend may still "
            "force review for changed files, failed review follow-ups, blocked input, runtime "
            "attention, or other higher-risk work.\n\n"
            "Report endpoint example:\n"
            f"curl -sS -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"started",'
            '"message":"Started task","message_en":"Started task","message_zh":"已开始任务"}\'\n\n'
            "Goal Packet report example:\n"
            f"curl -sS -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"working",'
            '"message":"Goal packet created; starting implementation.",'
            '"message_en":"Goal packet created; starting implementation.",'
            '"message_zh":"已创建目标包，开始实现。",'
            '"goal_packet":{"objective":"Concrete task objective in your words.",'
            '"acceptance_criteria":["Specific reviewer-checkable condition."],'
            '"validation_plan":["Command, manual check, or evidence source."],'
            '"assumptions":["Assumption made from ambiguity."],'
            '"out_of_scope":["Explicitly excluded work."],'
            '"handoff_requirements":["What final report must include."]}}\'\n\n'
            "Every report should include both message_en (concise English) and message_zh "
            "(concise 中文); keep the legacy message field as a short fallback. "
            "Final reports should include task_id, state, message, message_en, message_zh, "
            "changed_files, validation, risks, acceptance_check, review_decision, review_reason, "
            "and risk_level. acceptance_check should map each Goal Packet acceptance criterion "
            "to status passed, failed, partial, or not_checked with evidence."
        )

    def _build_review_prompt(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
        reviewer: ManagedSession,
        trigger_report: AgentReport,
    ) -> str:
        task_reports = [
            report
            for report in self.reports_for_workspace(task.workspace_id)
            if report.task_id == task.id
        ][-12:]
        report_payload = [
            {
                "state": report.state.value,
                "session_id": report.session_id,
                "message": report.message,
                "changed_files": report.changed_files,
                "validation": report.validation,
                "risks": report.risks,
                "acceptance_check": [
                    item.model_dump(mode="json") for item in report.acceptance_check
                ],
                "review_decision": report.review_decision.value,
                "review_reason": report.review_reason,
                "risk_level": report.risk_level,
                "created_at": report.created_at.isoformat(),
            }
            for report in task_reports
        ]
        return (
            "Review workspace task.\n\n"
            f"Workspace: {workspace.name}\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Implementation agent session: {task.session_id or 'unknown'}\n"
            f"Reviewer session: {reviewer.id}\n"
            f"{self._session_environment_lines(workspace, reviewer)}\n"
            f"State snapshot: {self.snapshot_path(workspace.id)}\n\n"
            "Task description:\n"
            f"{task.prompt}\n\n"
            "Stored Goal Packet JSON:\n"
            f"{task.goal_packet.model_dump_json() if task.goal_packet else 'null'}\n\n"
            "Review workflow:\n"
            "1. Stay read-only. Do not edit files, run formatters that write changes, or revert work.\n"
            "2. Check whether the stored Goal Packet faithfully preserves the original task prompt. "
            "Fail the review if the packet narrowed or distorted the user's requested outcome.\n"
            "3. Derive a task-specific acceptance checklist before judging the implementation. Use:\n"
            "   - the task title and description,\n"
            "   - the stored Goal Packet objective, acceptance criteria, validation plan, assumptions, "
            "out-of-scope boundaries, and handoff requirements,\n"
            "   - explicit user requirements and attachments,\n"
            "   - changed_files, validation, risks, and acceptance_check evidence from the implementation reports,\n"
            "   - repository conventions and nearby behavior,\n"
            "   - any blocked/needs_input context from the trigger report.\n"
            "4. Inspect changed files and related code paths enough to verify correctness and scope.\n"
            "5. Evaluate validation evidence. Decide whether missing tests/checks are acceptable or blocking.\n"
            "6. Produce one final verdict using the exit criteria below.\n\n"
            "Acceptance standards:\n"
            "- Goal fidelity: the Goal Packet preserves the original prompt and does not hide ambiguous scope.\n"
            "- Functional correctness: the requested behavior is implemented end to end.\n"
            "- Scope control: changes are limited to the task and do not introduce unrelated churn.\n"
            "- Integration fit: code follows local architecture, state flow, API contracts, and UI conventions.\n"
            "- Regression safety: existing user flows, persistence, concurrency, and error paths are not broken.\n"
            "- Validation quality: reported checks match the risk level; missing checks are called out clearly.\n"
            "- Handoff quality: changed_files, validation, and risks are understandable for a human reviewer.\n\n"
            "Review exit criteria:\n"
            "- review_passed: every acceptance criterion is satisfied; no blocking defects remain; validation is "
            "adequate or any gaps are explicitly non-blocking; residual risks are acceptable for final human acceptance.\n"
            "- review_failed: at least one blocking defect, regression, scope issue, or missing required validation "
            "can be fixed by the implementation agent. Include a Required fixes section.\n"
            "- review_needs_input: review cannot finish without user/product clarification, credentials, unavailable "
            "environment, or another decision the implementation agent cannot safely infer.\n\n"
            "Required final report format:\n"
            "Verdict: review_passed | review_failed | review_needs_input\n"
            "Acceptance criteria checked:\n"
            "- [pass/fail/unclear] criterion and evidence\n"
            "Findings:\n"
            "- Severity, file/area, issue, evidence, required fix or why non-blocking\n"
            "Validation reviewed:\n"
            "- Commands/evidence reviewed; missing or weak checks; whether gaps block acceptance\n"
            "Required fixes:\n"
            "- Only for review_failed; concrete steps for the implementation agent\n"
            "Risks:\n"
            "- Residual risks or none\n\n"
            "Trigger report JSON:\n"
            f"{trigger_report.model_dump_json()}\n\n"
            "Recent task reports JSON:\n"
            f"{json.dumps(report_payload, indent=2)}\n\n"
            "First report review_started, then finish with exactly one final review report:\n"
            f"curl -sS -X POST {self._report_base_url(reviewer)}/api/workspaces/sessions/{reviewer.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"review_passed",'
            '"message":"Verdict, acceptance criteria checked, findings, validation reviewed, '
            'required fixes if any, and risks","validation":"Checks reviewed",'
            '"risks":"Residual risk or none"}}\'\n\n'
            "Use review_failed when fixes are required. Use review_needs_input only for genuine blockers "
            "outside the implementation agent's control."
        )

    def _build_continue_prompt(self, task: WorkspaceTask, payload: ContinueTaskRequest) -> str:
        message = payload.message.strip() if payload.message else ""
        attachments = self._persist_attachments(
            task.workspace_id,
            f"{task.id}-continue-{uuid.uuid4().hex[:8]}",
            payload.attachments,
        )
        follow_up = self._append_attachment_block(
            message or "Continue addressing the review feedback.",
            attachments,
        )
        return (
            "Continue workspace task from review.\n\n"
            f"Task ID: {task.id}\n"
            f"Task title: {task.title}\n"
            f"Follow-up instructions:\n{follow_up}\n\n"
            "The task is back in working state. Report progress with the same task_id."
        )

    async def send_session_message(
        self,
        session_id: str,
        message: str,
        attachments: list[WorkspaceAttachmentCreate] | None = None,
    ) -> None:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)
        persisted = self._persist_attachments(
            session.workspace_id,
            f"{session.id}-message-{uuid.uuid4().hex[:8]}",
            attachments or [],
        )
        await self._ensure_session_ready_for_send(session)
        await self._send_tmux_message(
            session.tmux_session,
            self._append_attachment_block(message, persisted),
        )

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

    async def create_report(self, session_id: str, payload: AgentReportCreate) -> AgentReport:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(session_id)

        now = _now()
        task: WorkspaceTask | None = None
        task_id = payload.task_id or session.task_id or session.current_task_id
        if task_id:
            task = self.tasks.get(task_id)
            if not task or task.workspace_id != session.workspace_id:
                raise KeyError(task_id)
            session = await self._rename_session_for_task(session, task, updated_at=now)

        report = AgentReport(
            id=str(uuid.uuid4()),
            workspace_id=session.workspace_id,
            task_id=task_id,
            session_id=session.id,
            state=payload.state,
            message=payload.message,
            message_en=payload.message_en,
            message_zh=payload.message_zh,
            changed_files=payload.changed_files,
            validation=payload.validation,
            risks=payload.risks,
            acceptance_check=payload.acceptance_check,
            review_decision=payload.review_decision,
            review_reason=payload.review_reason,
            risk_level=payload.risk_level,
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
        if task:
            session_update["title"] = session.title
        self.sessions[session.id] = session.model_copy(update=session_update)

        if task_id and task_id in self.tasks:
            task_status = self._task_status_from_report(payload.state)
            task_update: dict[str, Any] = {}
            if payload.goal_packet is not None:
                task_update["goal_packet"] = payload.goal_packet
            if task_status:
                task_update.update({"status": task_status, "updated_at": now})
                if task_status == WorkspaceTaskStatus.WORKING:
                    task_update["started_at"] = self.tasks[task_id].started_at or now
                if task_status == WorkspaceTaskStatus.REVIEW:
                    task_update["reviewed_at"] = now
            elif task_update:
                task_update["updated_at"] = now
            if task_update:
                self.tasks[task_id] = self.tasks[task_id].model_copy(update=task_update)

        self._save_state()
        if task_id and task_id in self.tasks:
            await self._after_report_recorded(
                self.tasks[task_id], self.sessions[session.id], report
            )
        return report

    async def _after_report_recorded(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        report: AgentReport,
    ) -> None:
        if session.role == WorkspaceSessionRole.REVIEWER:
            await self._handle_review_report(task, session, report)
            return
        if session.role != WorkspaceSessionRole.ORCHESTRATOR:
            return
        if not state_policy.is_review_gate_state(report.state):
            return
        if task.review_requested_at and not task.review_completed_at:
            return
        evidence_gaps = self._completion_evidence_gaps(task, report)
        if report.review_decision == ReviewDecision.SKIP and evidence_gaps:
            await self._request_goal_packet_supplement(task, session, report, evidence_gaps)
            return
        should_review = await self._should_request_task_review(
            task,
            report,
            trigger_kind="agent_report",
        )
        if should_review:
            await self._request_task_review(task, report)
            return
        self._mark_task_review_skipped(task, report)

    async def _should_request_task_review(
        self,
        task: WorkspaceTask,
        report: AgentReport,
        *,
        trigger_kind: str,
    ) -> bool:
        if trigger_kind != "agent_report":
            return True
        can_skip_review = False
        if (
            report.review_decision == ReviewDecision.SKIP
            and report.state == AgentReportState.COMPLETED
        ):
            can_skip_review = await self._can_skip_task_review(task, report)
        return state_policy.should_request_task_review(
            trigger_kind=trigger_kind,
            report_state=report.state,
            review_decision=report.review_decision,
            can_skip_review=can_skip_review,
        )

    async def _can_skip_task_review(self, task: WorkspaceTask, report: AgentReport) -> bool:
        return state_policy.can_skip_task_review(
            state_policy.ReviewSkipContext(
                report_state=report.state,
                evidence_gaps=self._completion_evidence_gaps(task, report),
                changed_files=report.changed_files,
                risk_level=report.risk_level,
                latest_review_state=self._latest_review_report_state(task.id),
                workspace_has_tracked_changes=await self._workspace_has_tracked_changes(
                    task.workspace_id
                ),
            )
        )

    def _completion_evidence_gaps(
        self,
        task: WorkspaceTask,
        report: AgentReport,
    ) -> list[str]:
        return state_policy.completion_evidence_gaps(
            report.state,
            has_goal_packet=task.goal_packet is not None,
            has_acceptance_check=bool(report.acceptance_check),
        )

    async def _request_goal_packet_supplement(
        self,
        task: WorkspaceTask,
        session: ManagedSession,
        report: AgentReport,
        gaps: list[str],
    ) -> None:
        now = _now()
        gap_text = ", ".join(gaps)
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.WORKING,
                "reviewed_at": None,
                "updated_at": now,
            }
        )
        self.sessions[session.id] = session.model_copy(
            update={
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "task_id": task.id,
                "current_task_id": task.id,
                "updated_at": now,
                "last_activity_at": now,
            }
        )
        self._save_state()
        message = (
            "Your latest completion-style workspace report is missing required Goal Packet "
            f"audit evidence: {gap_text}.\n\n"
            "Please supplement the task before review or review-skip can proceed. If a Goal "
            "Packet has not been stored yet, include goal_packet with objective, "
            "acceptance_criteria, validation_plan, assumptions, out_of_scope, and "
            "handoff_requirements. Include acceptance_check mapping each acceptance criterion "
            "to status passed, failed, partial, or not_checked with evidence. Then POST a new "
            "ready_for_review or completed report.\n\n"
            "Supplement report example:\n"
            f"curl -sS -X POST {self._report_base_url(session)}/api/workspaces/sessions/{session.id}/reports "
            "-H 'Content-Type: application/json' "
            f'-d \'{{"task_id":"{task.id}","state":"completed",'
            '"message":"Supplemented Goal Packet evidence.",'
            '"message_en":"Supplemented Goal Packet evidence.",'
            '"message_zh":"已补充目标包验收证据。",'
            '"goal_packet":{"objective":"Concrete task objective.",'
            '"acceptance_criteria":["Reviewer-checkable criterion."],'
            '"validation_plan":["Command or manual check."],'
            '"assumptions":[],"out_of_scope":[],"handoff_requirements":[]},'
            '"acceptance_check":[{"criterion":"Reviewer-checkable criterion.",'
            '"status":"passed","evidence":"Command, file, or manual check evidence."}],'
            '"changed_files":[],"validation":"Checks run.",'
            '"risks":"Residual risk or none",'
            '"review_decision":"request","review_reason":"Goal Packet evidence supplemented.",'
            '"risk_level":"low"}\''
        )
        logger.info(
            "Requesting Goal Packet supplement session_id=%s task_id=%s report_id=%s gaps=%s",
            session.id,
            task.id,
            report.id,
            gap_text,
        )
        await self.send_session_message(session.id, message)

    async def _workspace_has_tracked_changes(self, workspace_id: str) -> bool:
        workspace = self.workspaces.get(workspace_id)
        if not workspace or workspace.target != ExecutionTarget.LOCAL:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                workspace.path,
                "status",
                "--porcelain",
                "--untracked-files=no",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return True
        except OSError:
            return True
        if proc.returncode != 0:
            return False
        return bool(stdout.strip())

    def _mark_task_review_skipped(self, task: WorkspaceTask, report: AgentReport) -> None:
        now = _now()
        reason = report.review_reason or "Agent completed the task without requesting review."
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.REVIEW,
                "review_session_id": None,
                "review_requested_at": None,
                "review_completed_at": None,
                "review_skipped_at": now,
                "review_skip_reason": reason,
                "reviewed_at": now,
                "completed_at": None,
                "human_acceptance_requested_at": now,
                "human_accepted_at": None,
                "updated_at": now,
            }
        )
        self._save_state()

    async def _request_task_review(
        self,
        task: WorkspaceTask,
        trigger_report: AgentReport,
    ) -> None:
        workspace = self.workspaces.get(task.workspace_id)
        if not workspace:
            raise KeyError(task.workspace_id)
        reviewer = await self._select_or_create_reviewer(workspace, task)
        now = _now()
        reviewer = await self._rename_session_for_task(reviewer, task, updated_at=now)
        self.tasks[task.id] = task.model_copy(
            update={
                "status": WorkspaceTaskStatus.WORKING,
                "review_session_id": reviewer.id,
                "review_attempts": task.review_attempts + 1,
                "review_requested_at": now,
                "review_completed_at": None,
                "review_skipped_at": None,
                "review_skip_reason": None,
                "completed_at": None,
                "human_acceptance_requested_at": None,
                "human_accepted_at": None,
                "updated_at": now,
            }
        )
        self.sessions[reviewer.id] = reviewer.model_copy(
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
        await self.send_session_message(
            reviewer.id,
            self._build_review_prompt(
                workspace,
                self.tasks[task.id],
                self.sessions[reviewer.id],
                trigger_report,
            ),
        )

    async def _select_or_create_reviewer(
        self,
        workspace: Workspace,
        task: WorkspaceTask,
    ) -> ManagedSession:
        reviewer = self._first_available_reviewer(workspace.id)
        if reviewer:
            return reviewer
        return await self.ensure_workspace_agent(
            workspace.id,
            EnsureWorkspaceAgentRequest(
                agent_type=task.agent_type,
                title=f"{workspace.name} Temporary Reviewer",
                role=WorkspaceSessionRole.REVIEWER,
                reuse_existing=False,
                cwd=workspace.path,
                target=workspace.target,
                remote_profile_id=workspace.remote_profile_id,
                remote_cwd=workspace.remote_cwd,
                remote_reconnect=workspace.remote_reconnect,
                ephemeral=True,
            ),
        )

    async def _handle_review_report(
        self,
        task: WorkspaceTask,
        reviewer: ManagedSession,
        report: AgentReport,
    ) -> None:
        if report.state == AgentReportState.REVIEW_STARTED:
            return
        if report.state not in {
            AgentReportState.REVIEW_PASSED,
            AgentReportState.REVIEW_FAILED,
            AgentReportState.REVIEW_NEEDS_INPUT,
        }:
            return

        now = _now()
        reviewer_status = (
            ManagedSessionStatus.NEEDS_INPUT
            if report.state == AgentReportState.REVIEW_NEEDS_INPUT
            else ManagedSessionStatus.IDLE
        )
        reviewer_runtime_status = (
            AgentRuntimeStatus.ATTENTION
            if report.state == AgentReportState.REVIEW_NEEDS_INPUT
            else AgentRuntimeStatus.IDLE
        )
        task_with_reviewer = task.model_copy(update={"review_session_id": reviewer.id})
        self._release_reviewer_session(
            task_with_reviewer,
            status=reviewer_status,
            runtime_status=reviewer_runtime_status,
            updated_at=now,
            include_stale_assignments=report.state != AgentReportState.REVIEW_NEEDS_INPUT,
        )

        task_update = {
            "review_session_id": reviewer.id,
            "review_completed_at": now,
            "review_skipped_at": None,
            "review_skip_reason": None,
            "reviewed_at": task.reviewed_at or now,
            "completed_at": None,
            "human_acceptance_requested_at": (
                now if report.state == AgentReportState.REVIEW_PASSED else None
            ),
            "human_accepted_at": None,
            "updated_at": now,
        }
        self.tasks[task.id] = task.model_copy(
            update={
                **task_update,
                "status": WorkspaceTaskStatus.REVIEW,
            }
        )
        self._save_state()

        if report.state != AgentReportState.REVIEW_FAILED:
            return
        updated_task = self.tasks[task.id]
        if updated_task.review_attempts > MAX_AUTOMATED_REVIEW_FAILURES:
            return
        feedback = (
            "Reviewer requested changes.\n\n"
            f"Reviewer session: {reviewer.id}\n"
            f"Review attempt: {updated_task.review_attempts}\n\n"
            f"{report.message}\n\n"
            "Address the required fixes, rerun appropriate validation, and report completed again."
        )
        await self.continue_task(updated_task.id, ContinueTaskRequest(message=feedback))

    def reports_for_workspace(self, workspace_id: str) -> list[AgentReport]:
        return sorted(
            [report for report in self.reports.values() if report.workspace_id == workspace_id],
            key=lambda report: report.created_at,
        )

    async def _send_tmux_message(self, tmux_session: str, message: str) -> None:
        logger.info(
            "Sending workspace message to tmux_session=%s message_length=%s",
            tmux_session,
            len(message),
        )
        await self._run_tmux("send-keys", "-t", tmux_session, "C-u")
        await asyncio.sleep(0.2)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(message)
            tmp_path = tmp.name
        try:
            await self._run_tmux("load-buffer", tmp_path)
            await self._run_tmux("paste-buffer", "-t", tmux_session)
            await asyncio.sleep(TMUX_PASTE_SETTLE_SECONDS)
            await self._submit_tmux_message(tmux_session, message)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    async def _submit_tmux_message(self, tmux_session: str, message: str) -> None:
        for attempt in range(1, TMUX_SUBMIT_ATTEMPTS + 1):
            await self._run_tmux("send-keys", "-t", tmux_session, "C-m")
            await asyncio.sleep(TMUX_SUBMIT_SETTLE_SECONDS)
            try:
                output = await self._capture_tmux_output(tmux_session)
            except RuntimeError as exc:
                logger.warning(
                    "Could not verify workspace message submit for tmux_session=%s: %s",
                    tmux_session,
                    exc,
                )
                return
            if not self._message_still_in_input(output, message):
                if attempt > 1:
                    logger.info(
                        "Workspace message submit succeeded after retry tmux_session=%s attempts=%s",
                        tmux_session,
                        attempt,
                    )
                return
            logger.warning(
                "Workspace message still appears pending after submit attempt %s/%s "
                "tmux_session=%s output_tail=%r",
                attempt,
                TMUX_SUBMIT_ATTEMPTS,
                tmux_session,
                output[-240:],
            )

        raise RuntimeError("Failed to submit workspace agent message; input still appears pending")

    def _message_still_in_input(self, output: str, message: str) -> bool:
        lines = [line.rstrip() for line in output.splitlines()]
        first_line = message.strip().splitlines()[0][:80] if message.strip() else ""
        is_slash_command = message.strip().startswith("/")
        tail_start = max(0, len(lines) - 16)
        tail = lines[tail_start:]
        for index, line in enumerate(tail):
            stripped = line.strip()
            if not stripped.startswith(("›", ">", "❯")):
                continue
            has_pasted_placeholder = "[Pasted Content" in stripped
            has_message_prefix = bool(first_line and first_line in stripped)
            if not has_pasted_placeholder and not has_message_prefix:
                continue
            following_lines = tail[index + 1 :]
            if any(next_line.strip().startswith(("›", ">", "❯")) for next_line in following_lines):
                continue
            following = "\n".join(following_lines[:5]).lstrip()
            if following.startswith(("•", "⏺", "●")):
                continue
            if following.startswith("⎿") and (is_slash_command or not has_pasted_placeholder):
                continue
            return True
        return False

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

    async def _rename_session_for_task(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        *,
        updated_at: datetime | None = None,
    ) -> ManagedSession:
        title = task.title
        if session.title == title:
            return session

        try:
            updated_tab = await ttyd_manager.update_tab(session.tab_id, name=title)
        except Exception:
            logger.exception(
                "Failed to rename workspace terminal tab_id=%s session_id=%s task_id=%s",
                session.tab_id,
                session.id,
                task.id,
            )
        else:
            if not updated_tab:
                logger.warning(
                    "Could not rename missing workspace terminal tab_id=%s session_id=%s task_id=%s",
                    session.tab_id,
                    session.id,
                    task.id,
                )

        updated_session = session.model_copy(
            update={
                "title": title,
                "updated_at": updated_at or _now(),
            }
        )
        self.sessions[session.id] = updated_session
        return updated_session

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

    def _first_available_reviewer(self, workspace_id: str) -> Optional[ManagedSession]:
        reviewers = [
            session
            for session in self._sessions_for_workspace_raw(workspace_id)
            if session.role == WorkspaceSessionRole.REVIEWER
            and session.status != ManagedSessionStatus.STOPPED
            and session.runtime_status == AgentRuntimeStatus.IDLE
            and not session.task_id
            and not session.current_task_id
        ]
        if not reviewers:
            return None
        return sorted(reviewers, key=lambda session: (session.ephemeral, session.created_at))[0]

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

        await self._refresh_session_statuses(workspace_id)
        self._reconcile_task_report_statuses(workspace_id)
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

    async def _refresh_session_statuses(
        self,
        workspace_id: Optional[str] = None,
        *,
        run_auto_continue: bool = False,
    ) -> None:
        sessions = [
            session
            for session in self.sessions.values()
            if workspace_id is None or session.workspace_id == workspace_id
        ]
        tab_ids = [session.tab_id for session in sessions]
        statuses = {
            status.tab_id: status
            for status in await ttyd_manager.list_tab_agent_statuses(tab_ids=tab_ids)
        }
        changed = False
        for session in sessions:
            session_id = session.id
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
                    runtime_status == AgentRuntimeStatus.IDLE
                    and task
                    and task.status == WorkspaceTaskStatus.REVIEW
                    and self._is_review_passed(task)
                ):
                    update.update(
                        {
                            "task_id": None,
                            "current_task_id": None,
                            "status": ManagedSessionStatus.IDLE,
                            "runtime_status": AgentRuntimeStatus.IDLE,
                            "auto_continue_task_id": None,
                            "auto_continue_attempts": 0,
                            "last_auto_continue_at": None,
                        }
                    )
                    current_task_id = None
                    changed = True
                if (
                    run_auto_continue
                    and runtime_status == AgentRuntimeStatus.IDLE
                    and task
                    and task.status == WorkspaceTaskStatus.WORKING
                ):
                    auto_continue_update = await self._auto_continue_stopped_task(
                        session,
                        task,
                        status.sampled_at,
                    )
                    if auto_continue_update:
                        update.update(auto_continue_update)
                        next_status = update["status"]
                        runtime_status = update["runtime_status"]
                        changed = True
                if (
                    runtime_status == AgentRuntimeStatus.WORKING
                    and task
                    and task.status == WorkspaceTaskStatus.REVIEW
                    and current_task_id is not None
                    and status.last_changed_at
                    and task.reviewed_at
                    and (status.last_changed_at - task.reviewed_at).total_seconds()
                    > REVIEW_RUNTIME_REOPEN_GRACE_SECONDS
                    and self._latest_report_state(current_task_id)
                    in {
                        AgentReportState.READY_FOR_REVIEW,
                        AgentReportState.COMPLETED,
                    }
                ):
                    self.tasks[current_task_id] = task.model_copy(
                        update={
                            "status": WorkspaceTaskStatus.WORKING,
                            "started_at": status.last_changed_at,
                            "updated_at": status.last_changed_at,
                        }
                    )
                    update["task_id"] = current_task_id
                    changed = True
                if (
                    runtime_status == AgentRuntimeStatus.ATTENTION
                    and task
                    and task.status == WorkspaceTaskStatus.WORKING
                ):
                    if not (task.review_requested_at and not task.review_completed_at):
                        report = AgentReport(
                            id=str(uuid.uuid4()),
                            workspace_id=task.workspace_id,
                            task_id=task.id,
                            session_id=session.id,
                            state=AgentReportState.NEEDS_INPUT,
                            message=status.detail
                            or "Agent runtime is waiting for input; reviewer diagnosis requested.",
                            changed_files=[],
                            validation=None,
                            risks=None,
                            created_at=status.sampled_at,
                        )
                        self.reports[report.id] = report
                        await self._request_task_review(task, report)
                    update["task_id"] = current_task_id
                    changed = True

            self.sessions[session_id] = session.model_copy(update=update)
            changed = True
        if changed:
            self._save_state()

    async def _auto_continue_stopped_task(
        self,
        session: ManagedSession,
        task: WorkspaceTask,
        sampled_at: datetime,
    ) -> dict[str, Any] | None:
        if task.review_requested_at and not task.review_completed_at:
            return None
        latest_state = self._latest_report_state(task.id)
        if latest_state in {
            AgentReportState.READY_FOR_REVIEW,
            AgentReportState.COMPLETED,
            AgentReportState.BLOCKED,
            AgentReportState.NEEDS_INPUT,
        }:
            return None
        try:
            output = await self._capture_tmux_output(session.tmux_session)
        except RuntimeError as exc:
            logger.warning(
                "Could not inspect workspace agent output for auto-continue session_id=%s: %s",
                session.id,
                exc,
            )
            return None

        if self._auto_continue_output_looks_busy(output):
            return {
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": (
                    session.auto_continue_attempts
                    if session.auto_continue_task_id == task.id
                    else 0
                ),
                "updated_at": sampled_at,
            }

        last_activity_at = session.last_activity_at
        if (
            last_activity_at
            and (sampled_at - last_activity_at).total_seconds() < AUTO_CONTINUE_IDLE_GRACE_SECONDS
        ):
            return None

        interruption_reason = self._auto_continue_interruption_reason(output)
        completion_reason = None
        if not interruption_reason:
            completion_reason = self._auto_continue_completion_reason(output)
        if not interruption_reason and not completion_reason:
            return None

        attempts = session.auto_continue_attempts if session.auto_continue_task_id == task.id else 0
        if (
            session.auto_continue_task_id == task.id
            and session.last_auto_continue_at
            and (sampled_at - session.last_auto_continue_at).total_seconds()
            < AUTO_CONTINUE_MIN_INTERVAL_SECONDS
        ):
            return {
                "status": ManagedSessionStatus.WORKING,
                "runtime_status": AgentRuntimeStatus.WORKING,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": attempts,
                "updated_at": sampled_at,
            }
        if attempts >= AUTO_CONTINUE_MAX_ATTEMPTS:
            self.tasks[task.id] = task.model_copy(
                update={
                    "status": WorkspaceTaskStatus.REVIEW,
                    "reviewed_at": sampled_at,
                    "updated_at": sampled_at,
                }
            )
            logger.warning(
                "Workspace agent auto-continue limit reached session_id=%s task_id=%s attempts=%s",
                session.id,
                task.id,
                attempts,
            )
            return {
                "status": ManagedSessionStatus.NEEDS_INPUT,
                "runtime_status": AgentRuntimeStatus.ATTENTION,
                "auto_continue_task_id": task.id,
                "auto_continue_attempts": attempts,
                "updated_at": sampled_at,
            }

        message = AUTO_CONTINUE_MESSAGE if interruption_reason else AUTO_REPORT_MISSING_MESSAGE
        await self._send_tmux_message(session.tmux_session, message)
        attempts += 1
        logger.info(
            "Auto-prompted idle workspace agent session_id=%s task_id=%s "
            "attempt=%s/%s action=%s reason=%s",
            session.id,
            task.id,
            attempts,
            AUTO_CONTINUE_MAX_ATTEMPTS,
            "continue" if interruption_reason else "report_missing",
            interruption_reason or completion_reason,
        )
        return {
            "status": ManagedSessionStatus.WORKING,
            "runtime_status": AgentRuntimeStatus.WORKING,
            "auto_continue_task_id": task.id,
            "auto_continue_attempts": attempts,
            "last_auto_continue_at": sampled_at,
            "last_activity_at": sampled_at,
            "updated_at": sampled_at,
        }

    def _auto_continue_completion_reason(self, output: str) -> str | None:
        return state_policy.auto_continue_completion_reason(output)

    def _auto_continue_interruption_reason(self, output: str) -> str | None:
        return state_policy.auto_continue_interruption_reason(output)

    def _auto_continue_recent_output_segment(self, output: str) -> str:
        return state_policy.auto_continue_recent_output_segment(output)

    def _auto_continue_output_looks_busy(self, output: str) -> bool:
        return state_policy.auto_continue_output_looks_busy(output)

    def _reconcile_task_report_statuses(self, workspace_id: str) -> None:
        changed = False
        reports_by_task: dict[str, AgentReport] = {}
        for report in self.reports_for_workspace(workspace_id):
            if report.task_id:
                reports_by_task[report.task_id] = report

        for task_id, report in reports_by_task.items():
            task = self.tasks.get(task_id)
            if (
                not task
                or task.workspace_id != workspace_id
                or task.status == WorkspaceTaskStatus.DONE
            ):
                continue
            if report.state not in {
                AgentReportState.READY_FOR_REVIEW,
                AgentReportState.COMPLETED,
                AgentReportState.REVIEW_PASSED,
            }:
                continue
            if report.state == AgentReportState.REVIEW_PASSED:
                reviewed_task = task.model_copy(
                    update={
                        "status": WorkspaceTaskStatus.REVIEW,
                        "review_session_id": report.session_id,
                        "review_completed_at": task.review_completed_at or report.created_at,
                        "reviewed_at": task.reviewed_at or report.created_at,
                        "completed_at": None,
                        "human_acceptance_requested_at": (
                            task.human_acceptance_requested_at or report.created_at
                        ),
                        "human_accepted_at": None,
                        "updated_at": report.created_at,
                    }
                )
                self.tasks[task_id] = reviewed_task
                self._release_reviewer_session(
                    reviewed_task,
                    status=ManagedSessionStatus.IDLE,
                    runtime_status=AgentRuntimeStatus.IDLE,
                    updated_at=report.created_at,
                    include_stale_assignments=True,
                )
                changed = True
                continue
            if task.status == WorkspaceTaskStatus.REVIEW:
                continue
            if task.reviewed_at != report.created_at:
                continue
            if (
                task.updated_at > report.created_at
                and task.started_at
                and task.started_at > report.created_at
            ):
                continue

            self.tasks[task_id] = task.model_copy(
                update={
                    "status": WorkspaceTaskStatus.REVIEW,
                    "reviewed_at": report.created_at,
                    "updated_at": report.created_at,
                }
            )
            changed = True

        if changed:
            self._save_state()

    def _latest_report_state(self, task_id: str) -> AgentReportState | None:
        reports = sorted(
            [report for report in self.reports.values() if report.task_id == task_id],
            key=lambda report: report.created_at,
        )
        return reports[-1].state if reports else None

    def _latest_review_report_state(self, task_id: str) -> AgentReportState | None:
        reports = sorted(
            [
                report
                for report in self.reports.values()
                if report.task_id == task_id and report.state.value.startswith("review_")
            ],
            key=lambda report: report.created_at,
        )
        return reports[-1].state if reports else None

    def _map_runtime_status(self, status: TerminalAgentStatus) -> ManagedSessionStatus:
        return state_policy.managed_status_from_runtime(status.status)

    def _is_spawn_grace_period(self, session: ManagedSession) -> bool:
        if session.status != ManagedSessionStatus.SPAWNING:
            return False
        return (_now() - session.created_at).total_seconds() < 90

    def _status_from_report(
        self,
        state: AgentReportState,
        session: ManagedSession,
    ) -> ManagedSessionStatus:
        return state_policy.managed_status_from_report(state, session.role, session.status)

    def _runtime_from_report(
        self,
        state: AgentReportState,
        session: ManagedSession,
    ) -> AgentRuntimeStatus:
        return state_policy.runtime_status_from_report(state, session.runtime_status)

    def _task_status_from_report(self, state: AgentReportState) -> Optional[WorkspaceTaskStatus]:
        return state_policy.task_status_from_report(state)

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
                    "auto_continue_task_id": None,
                    "auto_continue_attempts": 0,
                    "last_auto_continue_at": None,
                    "updated_at": _now(),
                }
            )

    def _release_reviewer_session(
        self,
        task: WorkspaceTask,
        *,
        status: ManagedSessionStatus,
        runtime_status: AgentRuntimeStatus,
        updated_at: datetime,
        include_stale_assignments: bool = False,
    ) -> None:
        session_ids: set[str] = set()
        if task.review_session_id:
            session_ids.add(task.review_session_id)
        if include_stale_assignments:
            session_ids.update(
                session.id
                for session in self.sessions.values()
                if session.role == WorkspaceSessionRole.REVIEWER
                and (session.task_id == task.id or session.current_task_id == task.id)
            )

        for session_id in session_ids:
            session = self.sessions.get(session_id)
            if (
                not session
                or session.role != WorkspaceSessionRole.REVIEWER
                or (session.task_id != task.id and session.current_task_id != task.id)
            ):
                continue
            self.sessions[session.id] = session.model_copy(
                update={
                    "task_id": None,
                    "current_task_id": None,
                    "status": status,
                    "runtime_status": runtime_status,
                    "updated_at": updated_at,
                    "last_activity_at": updated_at,
                }
            )

    def _assign_current_task(self, session_id: str, task_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            return
        task = self.tasks.get(task_id)
        title = task.title.strip() if task and task.title.strip() else session.title
        if task:
            renamed = ttyd_manager.rename_tab(session.tab_id, title)
            if not renamed:
                logger.warning(
                    "Could not rename workspace session tab for task session_id=%s tab_id=%s task_id=%s",
                    session.id,
                    session.tab_id,
                    task.id,
                )
        self.sessions[session_id] = session.model_copy(
            update={
                "task_id": task_id,
                "current_task_id": task_id,
                "title": title,
                "auto_continue_task_id": task_id,
                "auto_continue_attempts": 0,
                "last_auto_continue_at": None,
                "updated_at": _now(),
            }
        )


workspace_manager = WorkspaceManager()
