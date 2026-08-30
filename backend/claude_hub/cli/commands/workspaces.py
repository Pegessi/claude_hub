"""``workspace`` and ``agent`` command groups."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import (
    lifecycle_group_help,
    merge_payload,
    parse_kv_pairs,
    redact_session_env_payload,
    resolve_agent_reuse,
    resolve_cli_local_path,
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
DOCUMENT_COLUMNS = ["source", "label", "path", "task_id", "updated_at"]


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
    workspace = board.get("workspace", {}) if isinstance(board, dict) else {}
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
        "workspace": workspace,
        "task_counts": _counts(tasks, "status"),
        "session_counts": _counts(sessions, "runtime_status"),
        "active_tasks": task_rows,
        "active_sessions": session_rows,
        "latest_reports": list(reports.values()),
        "markdown_document_count": len(documents),
        "snapshot_path": board.get("snapshot_path") if isinstance(board, dict) else None,
    }


def _print_workspace_summary(workspace_id: str, summary: Dict[str, Any]) -> None:
    workspace = summary.get("workspace", {})
    click.echo(f"Workspace: {_workspace_label(workspace, workspace_id)} ({workspace_id})")
    if isinstance(workspace, dict):
        if workspace.get("path"):
            click.echo(f"path: {workspace.get('path')}")
        if workspace.get("default_branch"):
            click.echo(f"default_branch: {workspace.get('default_branch')}")
    click.echo(f"task_counts: {summary.get('task_counts', {})}")
    click.echo(f"session_counts: {summary.get('session_counts', {})}")
    click.echo(f"markdown_documents: {summary.get('markdown_document_count', 0)}")
    if summary.get("snapshot_path"):
        click.echo(f"snapshot: {summary['snapshot_path']}")
    click.echo("\nActive tasks:")
    print_rows(
        summary.get("active_tasks", []),
        ["id", "title", "status", "session_id", "latest_report_state", "latest_report_message"],
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


@workspace.command("create")
@click.option("--name", required=True, help="Workspace name.")
@click.option("--path", required=True, help="Local repository path.")
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
    "--allow-duplicate",
    is_flag=True,
    default=False,
    help="Allow creating a workspace when the repo identity already exists.",
)
@click.pass_context
def workspace_create(
    ctx: click.Context,
    name: str,
    path: str,
    default_branch: str,
    session_prefix: str,
    target: str,
    remote_profile_id: str,
    remote_cwd: str,
    remote_reconnect: bool,
    allow_duplicate: bool,
) -> None:
    """Create a workspace (refuses duplicate repo identity unless --allow-duplicate)."""
    body: Dict[str, Any] = {
        "name": name,
        "path": resolve_cli_local_path(path),
        "default_branch": default_branch,
        "target": target,
    }
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
            data = client.create_workspace(body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@workspace.command("ensure")
@click.option(
    "--name",
    default=None,
    required=False,
    help="Workspace name when creating (defaults to canonical primary repo basename).",
)
@click.option("--path", required=True, help="Local repository path or worktree.")
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
    session_prefix: str,
    target: str,
    remote_profile_id: str,
    remote_cwd: str,
    remote_reconnect: bool,
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


@workspace.command("update")
@click.argument("workspace_id")
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
    workspace_id: str,
    name: str,
    path: str,
    default_branch: str,
    remote_cwd: str,
    remote_reconnect: bool,
    payload_json: str,
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
            data = client.update_workspace(workspace_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@workspace.command("board")
@click.argument("workspace_id")
@click.pass_context
def workspace_board(ctx: click.Context, workspace_id: str) -> None:
    """Show the board (tasks + sessions) for a workspace.

    Also available as ``workspace status``.
    """
    try:
        with cli_main.get_client(ctx) as client:
            board = client.get_board(workspace_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e

    if cli_main.as_json(ctx):
        emit(board, True)
        return

    ws = board.get("workspace", {}) if isinstance(board, dict) else {}
    click.echo(f"Workspace: {ws.get('name', '')} ({ws.get('id', workspace_id)})")
    click.echo("\nTasks:")
    print_rows(board.get("tasks", []), TASK_COLUMNS)
    click.echo("\nSessions:")
    print_rows(board.get("sessions", []), SESSION_COLUMNS)


# Register the board command under the alias ``status`` as well.
workspace.add_command(workspace_board, name="status")


@workspace.command("summary")
@click.argument("workspace_id")
@click.pass_context
def workspace_summary(ctx: click.Context, workspace_id: str) -> None:
    """Show a concise typed summary of backend workspace state."""
    try:
        with cli_main.get_client(ctx) as client:
            board = client.get_board(workspace_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    summary = _workspace_summary(workspace_id, board)
    if cli_main.as_json(ctx):
        emit(summary, True)
    else:
        _print_workspace_summary(workspace_id, summary)


@workspace.command("docs")
@click.argument("workspace_id")
@click.pass_context
def workspace_docs(ctx: click.Context, workspace_id: str) -> None:
    """List board-discoverable Markdown documents and the snapshot path."""
    try:
        with cli_main.get_client(ctx) as client:
            board = client.get_board(workspace_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    documents = _items(board, "markdown_documents")
    payload = {
        "workspace_id": workspace_id,
        "snapshot_path": board.get("snapshot_path") if isinstance(board, dict) else None,
        "markdown_documents": documents,
    }
    if cli_main.as_json(ctx):
        emit(payload, True)
        return
    click.echo(f"Workspace: {workspace_id}")
    click.echo(f"snapshot: {payload['snapshot_path'] or '(none)'}")
    click.echo("\nMarkdown documents:")
    print_rows(documents, DOCUMENT_COLUMNS)


@workspace.command("dispatch")
@click.argument("workspace_id")
@click.pass_context
def workspace_dispatch(ctx: click.Context, workspace_id: str) -> None:
    """Manually dispatch queued tasks for a workspace."""
    try:
        with cli_main.get_client(ctx) as client:
            client.dispatch_workspace(workspace_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit({"ok": True}, True)
    else:
        click.echo("dispatched")


@workspace.command("artifact-preview")
@click.argument("workspace_id")
@click.option("--path", "artifact_path", required=True, help="Artifact path to preview.")
@click.option("--report-id", default=None, help="Optional report id for relative artifacts.")
@click.pass_context
def workspace_artifact_preview(
    ctx: click.Context,
    workspace_id: str,
    artifact_path: str,
    report_id: str,
) -> None:
    """Preview a workspace artifact."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.preview_workspace_artifact(
                workspace_id,
                artifact_path,
                report_id=report_id,
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@workspace.command("attachment-get")
@click.argument("attachment_id")
@click.option("--output", "output_path", default=None, help="Write attachment bytes to this file.")
@click.pass_context
def workspace_attachment_get(
    ctx: click.Context,
    attachment_id: str,
    output_path: str,
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


@click.group(help=lifecycle_group_help("Manage resident workspace agent sessions."))
def agent() -> None:
    pass


@agent.command("list")
@click.argument("workspace_id")
@click.pass_context
def agent_list(ctx: click.Context, workspace_id: str) -> None:
    """List managed agent sessions for a workspace."""
    try:
        with cli_main.get_client(ctx) as client:
            board = client.get_board(workspace_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    sessions: List[dict] = board.get("sessions", []) if isinstance(board, dict) else []
    if cli_main.as_json(ctx):
        emit(sessions, True)
    else:
        print_rows(sessions, SESSION_COLUMNS)


@agent.command("status")
@click.argument("workspace_id")
@click.option("--role", default=None, help="Client-side filter on session role.")
@click.pass_context
def agent_status(ctx: click.Context, workspace_id: str, role: Optional[str]) -> None:
    """Show resident agent/session runtime state for a workspace."""
    try:
        with cli_main.get_client(ctx) as client:
            board = client.get_board(workspace_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    sessions = _items(board, "sessions")
    if role is not None:
        sessions = [s for s in sessions if s.get("role") == role]
    rows = [
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
        for session in sessions
    ]
    if cli_main.as_json(ctx):
        emit(rows, True)
    else:
        print_rows(rows, SESSION_STATUS_COLUMNS)


@agent.command("create")
@click.argument("workspace_id")
@click.option("--agent-type", default="codex", help="Agent type to run.")
@click.option("--title", default=None, help="Optional session title.")
@click.option(
    "--role",
    type=click.Choice(["worker", "orchestrator", "reviewer", "dispatcher"]),
    default="orchestrator",
    help="Session role.",
)
@click.option(
    "--reuse-existing",
    is_flag=True,
    default=False,
    help=(
        "Request best-effort reuse of a compatible idle session "
        "(default when neither flag is set)."
    ),
)
@click.option(
    "--no-reuse-existing",
    is_flag=True,
    default=False,
    help="Always create a new session.",
)
@click.option("--cwd", default=None, help="Session working directory override.")
@click.option(
    "--solo-mode/--no-solo-mode",
    default=True,
    help="Run the agent in solo mode (default on).",
)
@click.option(
    "--target",
    type=click.Choice(["local", "remote"]),
    default=None,
    help="Execution target override.",
)
@click.option("--remote-profile-id", default=None, help="Remote profile for remote agents.")
@click.option("--remote-cwd", default=None, help="Remote working directory.")
@click.option(
    "--remote-reconnect/--no-remote-reconnect",
    default=None,
    help="Reconnect remote sessions automatically.",
)
@click.option("--env", "env_values", multiple=True, help="Environment variable KEY=VALUE.")
@click.option(
    "--env-preset",
    default=None,
    help="Any built-in or saved custom env preset, selected by id or name.",
)
@click.option(
    "--ephemeral", is_flag=True, default=False, help="Create a task-scoped ephemeral session."
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def agent_create(
    ctx: click.Context,
    workspace_id: str,
    agent_type: str,
    title: str,
    role: str,
    reuse_existing: bool,
    no_reuse_existing: bool,
    cwd: str,
    solo_mode: bool,
    target: str,
    remote_profile_id: str,
    remote_cwd: str,
    remote_reconnect: bool,
    env_values: tuple,
    env_preset: str,
    ephemeral: bool,
    payload_json: str,
) -> None:
    """Ensure a resident workspace agent session."""
    effective_reuse = resolve_agent_reuse(ephemeral, reuse_existing, no_reuse_existing)
    settings = ctx.obj
    resolved_preset = (
        env_preset
        or settings.env_preset_for_agent_type(agent_type)
        or getattr(settings, "default_env_preset", None)
    )
    resolved_cwd = resolve_cli_local_path(cwd) if cwd is not None else None
    body: Dict[str, Any] = merge_payload(
        payload_json,
        agent_type=agent_type,
        role=role,
        reuse_existing=effective_reuse,
        cwd=resolved_cwd,
        solo_mode=solo_mode,
        target=target,
        remote_profile_id=remote_profile_id,
        remote_cwd=remote_cwd,
        remote_reconnect=remote_reconnect,
        ephemeral=ephemeral,
        caller_owned_ephemeral=ephemeral if ephemeral else False,
        env_preset=resolved_preset,
    )
    if title is not None:
        body["title"] = title
    if env_values:
        body["env"] = parse_kv_pairs(env_values, "--env")
    try:
        with cli_main.get_client(ctx) as client:
            data = client.ensure_agent(workspace_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    data = redact_session_env_payload(data)
    emit(data, cli_main.as_json(ctx))
