"""``workspace`` command group."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import (
    lifecycle_group_help,
    merge_payload,
    resolve_cli_local_path,
    resolve_workspace_id,
)
from claude_hub.cli.output import emit, print_rows

WORKSPACE_COLUMNS = ["id", "name", "path", "default_branch"]
TASK_COLUMNS = ["id", "title", "status", "agent_type", "task_mode"]
SESSION_COLUMNS = ["id", "role", "agent_type", "status", "current_task_id"]
SESSION_STATUS_COLUMNS = [
    "id",
    "role",
    "agent_type",
    "status",
    "runtime_status",
    "current_task_id",
    "tab_id",
    "last_activity_at",
]


def _items(board: Any, key: str) -> List[dict]:
    """Return dict items from a board list, ignoring malformed rows."""
    if not isinstance(board, dict):
        return []
    value = board.get(key, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _counts(items: List[dict], key: str) -> Dict[str, int]:
    """Build stable string counts for a board field."""
    return dict(sorted(Counter(str(item.get(key) or "unknown") for item in items).items()))


def _latest_reports(board: Any) -> Dict[str, dict]:
    """Index board latest reports by task id."""
    reports: Dict[str, dict] = {}
    for report in _items(board, "reports"):
        task_id = report.get("task_id")
        if task_id:
            reports[str(task_id)] = report
    return reports


def _report_message(report: Optional[dict]) -> str:
    """Return the most useful one-line report message."""
    if not report:
        return ""
    return str(report.get("message_zh") or report.get("message_en") or report.get("message") or "")


def _workspace_label(workspace: Any, fallback_id: str) -> str:
    if isinstance(workspace, dict):
        return str(workspace.get("name") or workspace.get("id") or fallback_id)
    return fallback_id


def _workspace_summary(workspace_id: str, board: Any) -> Dict[str, Any]:
    """Derive a typed, scrape-free summary from the workspace board."""
    workspace_obj = board.get("workspace", {}) if isinstance(board, dict) else {}
    tasks = _items(board, "tasks")
    sessions = _items(board, "sessions")
    documents = _items(board, "markdown_documents")
    reports = _latest_reports(board)
    active_statuses = {"todo", "queued", "working", "review"}
    active_tasks = [task for task in tasks if str(task.get("status")) in active_statuses]
    active_sessions = [
        session
        for session in sessions
        if str(session.get("runtime_status") or session.get("status") or "").lower()
        not in {"", "idle", "offline"}
    ]
    task_rows = []
    for task in active_tasks[:10]:
        latest = reports.get(str(task.get("id")))
        task_rows.append(
            {
                "id": task.get("id"),
                "title": task.get("title"),
                "status": task.get("status"),
                "session_id": task.get("session_id"),
                "latest_report_state": latest.get("state") if latest else "",
                "latest_report_message": _report_message(latest),
            }
        )
    session_rows = [
        {
            "id": session.get("id"),
            "role": session.get("role"),
            "agent_type": session.get("agent_type"),
            "status": session.get("status"),
            "runtime_status": session.get("runtime_status"),
            "current_task_id": session.get("current_task_id"),
            "tab_id": session.get("tab_id"),
            "last_activity_at": session.get("last_activity_at"),
        }
        for session in active_sessions[:10]
    ]
    return {
        "workspace": workspace_obj,
        "task_counts": _counts(tasks, "status"),
        "session_counts": _counts(sessions, "runtime_status"),
        "active_tasks": task_rows,
        "active_sessions": session_rows,
        "latest_reports": list(reports.values()),
        "markdown_document_count": len(documents),
        "markdown_documents": documents,
        "snapshot_path": board.get("snapshot_path") if isinstance(board, dict) else None,
    }


def _print_workspace_summary(workspace_id: str, summary: Dict[str, Any]) -> None:
    workspace_obj = summary.get("workspace", {})
    click.echo(f"Workspace: {_workspace_label(workspace_obj, workspace_id)} ({workspace_id})")
    if isinstance(workspace_obj, dict):
        if workspace_obj.get("path"):
            click.echo(f"path: {workspace_obj.get('path')}")
        if workspace_obj.get("default_branch"):
            click.echo(f"default_branch: {workspace_obj.get('default_branch')}")
    click.echo(f"task_counts: {summary.get('task_counts', {})}")
    click.echo(f"session_counts: {summary.get('session_counts', {})}")
    click.echo(f"markdown_documents: {summary.get('markdown_document_count', 0)}")
    if summary.get("snapshot_path"):
        click.echo(f"snapshot: {summary['snapshot_path']}")
    click.echo("\nActive tasks:")
    print_rows(
        summary.get("active_tasks", []),
        [
            "id",
            "title",
            "status",
            "session_id",
            "latest_report_state",
            "latest_report_message",
        ],
    )
    click.echo("\nActive sessions:")
    print_rows(summary.get("active_sessions", []), SESSION_STATUS_COLUMNS)


@click.group(help=lifecycle_group_help("Manage agent workspaces."))
def workspace() -> None:
    pass


@workspace.command("list")
@click.pass_context
def workspace_list(ctx: click.Context) -> None:
    """List all workspaces."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_workspaces()
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit(data, True)
    else:
        print_rows(data or [], WORKSPACE_COLUMNS)


@workspace.command("ensure")
@click.option(
    "--name",
    default=None,
    required=False,
    help="Workspace name when creating (defaults to canonical primary repo basename).",
)
@click.option(
    "--path",
    default=".",
    show_default=True,
    help="Local repository path or linked worktree.",
)
@click.option("--default-branch", default="main", help="Default git branch.")
@click.option("--session-prefix", default=None, help="Prefix for managed session names.")
@click.option(
    "--target",
    type=click.Choice(["local", "remote"]),
    default="local",
    help="Execution target.",
)
@click.option("--remote-profile-id", default=None, help="Remote profile for remote workspaces.")
@click.option("--remote-cwd", default=None, help="Remote working directory.")
@click.option(
    "--remote-reconnect/--no-remote-reconnect",
    default=None,
    help="Reconnect remote sessions automatically.",
)
@click.option(
    "--create/--no-create",
    "create_if_missing",
    default=True,
    help="Create the workspace when no identity match exists.",
)
@click.option(
    "--allow-duplicate",
    is_flag=True,
    default=False,
    help="Allow create when duplicate identity would otherwise fail.",
)
@click.pass_context
def workspace_ensure(
    ctx: click.Context,
    name: Optional[str],
    path: str,
    default_branch: str,
    session_prefix: Optional[str],
    target: str,
    remote_profile_id: Optional[str],
    remote_cwd: Optional[str],
    remote_reconnect: Optional[bool],
    create_if_missing: bool,
    allow_duplicate: bool,
) -> None:
    """Ensure the canonical Hub Workspace for a repo identity (reuse or create)."""
    body: Dict[str, Any] = {
        "path": resolve_cli_local_path(path),
        "default_branch": default_branch,
        "target": target,
        "create_if_missing": create_if_missing,
    }
    if name is not None:
        body["name"] = name
    if session_prefix is not None:
        body["session_prefix"] = session_prefix
    if remote_profile_id is not None:
        body["remote_profile_id"] = remote_profile_id
    if remote_cwd is not None:
        body["remote_cwd"] = remote_cwd
    if remote_reconnect is not None:
        body["remote_reconnect"] = remote_reconnect
    if allow_duplicate:
        body["allow_duplicate"] = True
    try:
        with cli_main.get_client(ctx) as client:
            data = client.ensure_workspace(body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@workspace.command("status")
@click.argument("workspace_id", required=False, default=None)
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help="Show the full workspace board (all tasks, sessions, reports, and lifecycles).",
)
@click.option(
    "--include-archived",
    is_flag=True,
    default=False,
    help="Include archived sessions in the board.",
)
@click.pass_context
def workspace_status(
    ctx: click.Context,
    workspace_id: Optional[str],
    full: bool,
    include_archived: bool,
) -> None:
    """Show concise workspace status (or full board with --full).

    Defaults to the workspace matching the current directory when WORKSPACE_ID
    is omitted.
    """
    try:
        with cli_main.get_client(ctx) as client:
            resolved_id = resolve_workspace_id(client, workspace_id)
            board = client.get_board(resolved_id, include_archived=include_archived)
    except HubError as e:
        raise click.ClickException(str(e)) from e

    if full:
        if cli_main.as_json(ctx):
            emit(board, True)
            return
        ws = board.get("workspace", {}) if isinstance(board, dict) else {}
        click.echo(f"Workspace: {ws.get('name', '')} ({ws.get('id', resolved_id)})")
        click.echo("\nTasks:")
        print_rows(board.get("tasks", []) if isinstance(board, dict) else [], TASK_COLUMNS)
        click.echo("\nSessions:")
        print_rows(
            board.get("sessions", []) if isinstance(board, dict) else [],
            SESSION_COLUMNS,
        )
        return

    summary = _workspace_summary(resolved_id, board)
    if cli_main.as_json(ctx):
        emit(summary, True)
    else:
        _print_workspace_summary(resolved_id, summary)


@workspace.command("update")
@click.argument("workspace_id", required=False, default=None)
@click.option("--name", default=None, help="Workspace name.")
@click.option("--path", default=None, help="Local repository path.")
@click.option("--default-branch", default=None, help="Default git branch.")
@click.option("--remote-cwd", default=None, help="Remote working directory.")
@click.option(
    "--remote-reconnect/--no-remote-reconnect",
    default=None,
    help="Reconnect remote sessions automatically.",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def workspace_update(
    ctx: click.Context,
    workspace_id: Optional[str],
    name: Optional[str],
    path: Optional[str],
    default_branch: Optional[str],
    remote_cwd: Optional[str],
    remote_reconnect: Optional[bool],
    payload_json: Optional[str],
) -> None:
    """Update editable workspace fields."""
    body = merge_payload(
        payload_json,
        name=name,
        path=resolve_cli_local_path(path) if path is not None else None,
        default_branch=default_branch,
        remote_cwd=remote_cwd,
        remote_reconnect=remote_reconnect,
    )
    try:
        with cli_main.get_client(ctx) as client:
            resolved_id = resolve_workspace_id(client, workspace_id)
            data = client.update_workspace(resolved_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@workspace.command("delete")
@click.argument("workspace_id")
@click.pass_context
def workspace_delete(ctx: click.Context, workspace_id: str) -> None:
    """Delete a workspace record."""
    try:
        with cli_main.get_client(ctx) as client:
            client.delete_workspace(workspace_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit({"ok": True, "workspace_id": workspace_id}, True)
    else:
        click.echo(f"deleted {workspace_id}")


@workspace.command("attachment-get")
@click.argument("attachment_id")
@click.option("--output", "output_path", default=None, help="Write attachment bytes to this file.")
@click.pass_context
def workspace_attachment_get(
    ctx: click.Context,
    attachment_id: str,
    output_path: Optional[str],
) -> None:
    """Download a persisted task attachment."""
    try:
        with cli_main.get_client(ctx) as client:
            response = client.get_attachment_response(attachment_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e

    if output_path:
        with open(output_path, "wb") as f:
            f.write(response.content)
        if cli_main.as_json(ctx):
            emit({"attachment_id": attachment_id, "output": output_path}, True)
        else:
            click.echo(output_path)
        return
    click.echo(response.text)
