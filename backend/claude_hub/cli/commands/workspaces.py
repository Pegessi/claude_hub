"""``workspace`` and ``agent`` command groups."""

from __future__ import annotations

from typing import Any, Dict, List

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import merge_payload, parse_kv_pairs
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
@click.option("--remote-profile-id", default=None, help="Remote profile for remote workspaces.")
@click.option("--remote-cwd", default=None, help="Remote working directory.")
@click.option(
    "--remote-reconnect/--no-remote-reconnect",
    default=None,
    help="Reconnect remote sessions automatically.",
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
    if remote_profile_id is not None:
        body["remote_profile_id"] = remote_profile_id
    if remote_cwd is not None:
        body["remote_cwd"] = remote_cwd
    if remote_reconnect is not None:
        body["remote_reconnect"] = remote_reconnect
    try:
        with cli_main.get_client(ctx) as client:
            data = client.create_workspace(body)
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
        path=path,
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
@click.option("--ephemeral", is_flag=True, default=False, help="Create an ephemeral session.")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def agent_create(
    ctx: click.Context,
    workspace_id: str,
    agent_type: str,
    title: str,
    role: str,
    reuse_existing: bool,
    cwd: str,
    solo_mode: bool,
    target: str,
    remote_profile_id: str,
    remote_cwd: str,
    remote_reconnect: bool,
    env_values: tuple,
    ephemeral: bool,
    payload_json: str,
) -> None:
    """Ensure a resident workspace agent session."""
    body: Dict[str, Any] = merge_payload(
        payload_json,
        agent_type=agent_type,
        role=role,
        reuse_existing=reuse_existing,
        cwd=cwd,
        solo_mode=solo_mode,
        target=target,
        remote_profile_id=remote_profile_id,
        remote_cwd=remote_cwd,
        remote_reconnect=remote_reconnect,
        ephemeral=ephemeral,
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
    emit(data, cli_main.as_json(ctx))
