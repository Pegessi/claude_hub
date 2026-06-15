"""``workspace`` and ``agent`` command groups."""

from __future__ import annotations

from typing import Any, Dict, List

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.output import emit, print_rows

WORKSPACE_COLUMNS = ["id", "name", "path", "default_branch"]
TASK_COLUMNS = ["id", "title", "status", "agent_type", "task_mode"]
SESSION_COLUMNS = ["id", "role", "agent_type", "status", "current_task_id"]


@click.group()
def workspace() -> None:
    """Manage agent workspaces."""


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
@click.pass_context
def workspace_create(
    ctx: click.Context,
    name: str,
    path: str,
    default_branch: str,
    session_prefix: str,
    target: str,
) -> None:
    """Create a workspace."""
    body: Dict[str, Any] = {
        "name": name,
        "path": path,
        "default_branch": default_branch,
        "target": target,
    }
    if session_prefix is not None:
        body["session_prefix"] = session_prefix
    try:
        with cli_main.get_client(ctx) as client:
            data = client.create_workspace(body)
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


@click.group()
def agent() -> None:
    """Manage resident workspace agent sessions."""


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
@click.option("--reuse-existing", is_flag=True, default=False, help="Reuse an existing session.")
@click.option(
    "--solo-mode/--no-solo-mode",
    default=True,
    help="Run the agent in solo mode (default on).",
)
@click.pass_context
def agent_create(
    ctx: click.Context,
    workspace_id: str,
    agent_type: str,
    title: str,
    role: str,
    reuse_existing: bool,
    solo_mode: bool,
) -> None:
    """Ensure a resident workspace agent session."""
    body: Dict[str, Any] = {
        "agent_type": agent_type,
        "role": role,
        "reuse_existing": reuse_existing,
        "solo_mode": solo_mode,
    }
    if title is not None:
        body["title"] = title
    try:
        with cli_main.get_client(ctx) as client:
            data = client.ensure_agent(workspace_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
