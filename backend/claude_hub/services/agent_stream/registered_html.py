"""Exact HTML artifact grants derived from current initiator Task snapshots."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claude_hub.models import (
    ExecutionTarget,
    ManagedSession,
    SessionKind,
    StreamAttachmentPreview,
    TerminalTab,
    Workspace,
    WorkspaceTask,
)
from claude_hub.models.schemas import TaskExecutionControl
from claude_hub.services.agent_stream.local_markdown import (
    MarkdownUnavailable,
    read_authorized_text_preview,
)


@dataclass(frozen=True)
class RegisteredHtmlGrant:
    """One read's immutable identity, compared with freshly resolved owners."""

    path: str
    task_id: str
    task_created_at: datetime
    workspace_id: str
    workspace_created_at: datetime
    workspace_path: str
    execution_epoch: int
    execution_ref: str
    progress_revision: int
    progress_call_id: str
    progress_reported_at: datetime
    source_tab_id: str
    source_created_at: datetime
    source_path: str
    source_session_id: str
    source_session_created_at: datetime
    source_session_path: str


def registered_html_path(raw_path: str) -> Path:
    """Accept exact absolute HTML filenames, without URL or traversal aliases."""
    if (
        not raw_path
        or len(raw_path) > 4096
        or "\\" in raw_path
        or any(ord(char) < 32 or ord(char) == 127 for char in raw_path)
    ):
        raise MarkdownUnavailable()
    path = Path(raw_path)
    if (
        not path.is_absolute()
        or raw_path.startswith("//")
        or ".." in path.parts
        or str(path) != raw_path
        or path.suffix.lower() not in {".html", ".htm"}
    ):
        raise MarkdownUnavailable()
    return path


def _utc(value: datetime) -> datetime:
    # Task/Workspace use datetime.now() (host-local, without an offset); Chat
    # timestamps can be offset-aware. astimezone interprets each correctly.
    return value.astimezone(timezone.utc)


def find_registered_html_grant(
    raw_path: str,
    *,
    tasks: Iterable[WorkspaceTask],
    workspaces: Mapping[str, Workspace],
    sessions: Mapping[str, ManagedSession],
    get_tab: Callable[[str], TerminalTab | None],
    scope: ManagedSession | None = None,
    task_id: str | None = None,
) -> RegisteredHtmlGrant:
    """Find a current exact Task reference, never a historical or directory grant.

    Source records must still exist with the same local identity. Archival or
    process inactivity does not withdraw a published artifact; deletion does.
    """
    path = registered_html_path(raw_path)
    for task in tasks:
        progress = task.latest_progress
        source = task.source
        if (
            (task_id is not None and task.id != task_id)
            or task.execution_control != TaskExecutionControl.INITIATOR
            or progress is None
            or progress.execution_epoch != task.execution_epoch
            or _utc(progress.reported_at) < _utc(task.created_at)
            or str(path) not in progress.artifact_refs
            or source is None
            or source.kind != "chat"
            or not source.tab_id
        ):
            continue
        workspace = workspaces.get(task.workspace_id)
        tab = get_tab(source.tab_id)
        if (
            workspace is None
            or workspace.id != task.workspace_id
            or workspace.target != ExecutionTarget.LOCAL
            or _utc(workspace.created_at) > _utc(task.created_at)
            or tab is None
            or tab.id != source.tab_id
            or tab.target != ExecutionTarget.LOCAL
            or tab.session_kind != SessionKind.CHAT
            or _utc(tab.created_at) > _utc(task.created_at)
            or not tab.cwd
        ):
            continue
        source_session_id = f"terminal-tab-{tab.id}"
        source_created_at = tab.created_at
        source_session_path = tab.cwd
        if tab.managed_session_id or tab.workspace_role is not None:
            session = sessions.get(tab.managed_session_id or "")
            if (
                session is None
                or session.id != tab.managed_session_id
                or session.target != ExecutionTarget.LOCAL
                or session.session_kind != SessionKind.CHAT
                or session.tab_id != tab.id
                or session.workspace_id != workspace.id
                or _utc(session.created_at) > _utc(task.created_at)
            ):
                continue
            source_session_id = session.id
            source_created_at = session.created_at
            source_session_path = session.workspace_path
        execution = task.execution_ref
        if execution is not None and execution.session_id not in {None, source_session_id}:
            continue
        if scope is not None and (
            scope.target != ExecutionTarget.LOCAL
            or scope.tab_id != tab.id
            or scope.id != source_session_id
            or scope.created_at != source_created_at
        ):
            continue
        return RegisteredHtmlGrant(
            path=str(path),
            task_id=task.id,
            task_created_at=task.created_at,
            workspace_id=workspace.id,
            workspace_created_at=workspace.created_at,
            workspace_path=workspace.path,
            execution_epoch=task.execution_epoch,
            execution_ref=execution.model_dump_json() if execution is not None else "",
            progress_revision=task.progress_revision,
            progress_call_id=progress.call_id,
            progress_reported_at=progress.reported_at,
            source_tab_id=tab.id,
            source_created_at=tab.created_at,
            source_path=tab.cwd,
            source_session_id=source_session_id,
            source_session_created_at=source_created_at,
            source_session_path=source_session_path,
        )
    raise MarkdownUnavailable()


def read_registered_html_preview(grant: RegisteredHtmlGrant) -> StreamAttachmentPreview:
    """Read only the exact granted file; the route must revalidate afterward."""
    preview = read_authorized_text_preview(registered_html_path(grant.path))
    return StreamAttachmentPreview(
        path=preview.path,
        filename=preview.filename,
        content=preview.content,
        size_bytes=preview.size_bytes,
        truncated=preview.truncated,
        kind="html",
        language="xml",
    )
