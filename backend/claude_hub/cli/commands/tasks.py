"""``task`` command group."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.output import emit, print_rows

TASK_COLUMNS = ["id", "title", "status", "agent_type", "task_mode"]


@click.group()
def task() -> None:
    """Manage workspace tasks."""


@task.command("list")
@click.argument("workspace_id")
@click.option("--status", default=None, help="Client-side filter on task status.")
@click.pass_context
def task_list(ctx: click.Context, workspace_id: str, status: Optional[str]) -> None:
    """List tasks for a workspace."""
    try:
        with cli_main.get_client(ctx) as client:
            board = client.get_board(workspace_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    tasks: List[dict] = board.get("tasks", []) if isinstance(board, dict) else []
    if status is not None:
        tasks = [t for t in tasks if t.get("status") == status]
    if cli_main.as_json(ctx):
        emit(tasks, True)
    else:
        print_rows(tasks, TASK_COLUMNS)


@task.command("create")
@click.argument("workspace_id")
@click.option("--title", required=True, help="Task title.")
@click.option("--prompt", required=True, help="Task prompt.")
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "cursor", "terminal"]),
    default="codex",
    help="Agent type.",
)
@click.option(
    "--task-mode",
    type=click.Choice(["direct", "reviewed", "autonomous"]),
    default="reviewed",
    help="Task automation mode.",
)
@click.option(
    "--execution-complexity",
    type=click.Choice(["auto", "simple", "complex"]),
    default="auto",
    help="Execution complexity hint.",
)
@click.option(
    "--review-profile",
    "review_profiles",
    multiple=True,
    type=click.Choice(["general", "code", "ui", "artifact", "delivery", "boundary"]),
    help="Review profile (repeatable).",
)
@click.pass_context
def task_create(
    ctx: click.Context,
    workspace_id: str,
    title: str,
    prompt: str,
    agent_type: str,
    task_mode: str,
    execution_complexity: str,
    review_profiles: tuple,
) -> None:
    """Create a task in a workspace."""
    body: Dict[str, Any] = {
        "title": title,
        "prompt": prompt,
        "agent_type": agent_type,
        "task_mode": task_mode,
        "execution_complexity": execution_complexity,
        "review_profiles": list(review_profiles),
    }
    try:
        with cli_main.get_client(ctx) as client:
            data = client.create_task(workspace_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("start")
@click.argument("task_id")
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "cursor", "terminal"]),
    default=None,
    help="Override the agent type.",
)
@click.option("--target-session-id", default=None, help="Target a specific session.")
@click.option(
    "--clear-context/--no-clear-context",
    default=None,
    help="Clear agent context before starting (omitted unless set).",
)
@click.option("--related-task-id", default=None, help="Related task id.")
@click.pass_context
def task_start(
    ctx: click.Context,
    task_id: str,
    agent_type: Optional[str],
    target_session_id: Optional[str],
    clear_context: Optional[bool],
    related_task_id: Optional[str],
) -> None:
    """Queue / start a task."""
    body: Dict[str, Any] = {}
    if agent_type is not None:
        body["agent_type"] = agent_type
    if target_session_id is not None:
        body["target_session_id"] = target_session_id
    if clear_context is not None:
        body["clear_context"] = clear_context
    if related_task_id is not None:
        body["related_task_id"] = related_task_id
    try:
        with cli_main.get_client(ctx) as client:
            data = client.start_task(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("continue")
@click.argument("task_id")
@click.option("--message", default=None, help="Message to send when continuing.")
@click.pass_context
def task_continue(ctx: click.Context, task_id: str, message: Optional[str]) -> None:
    """Continue a task from review with its original agent."""
    body: Dict[str, Any] = {"attachments": []}
    if message is not None:
        body["message"] = message
    try:
        with cli_main.get_client(ctx) as client:
            data = client.continue_task(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("send")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option("--message", required=True, help="Message to send to the task's agent.")
@click.pass_context
def task_send(ctx: click.Context, workspace_id: str, task_id: str, message: str) -> None:
    """Send a message to the agent session currently running a task.

    Convenience wrapper: resolves the task's session from the workspace board,
    then delegates to the session send endpoint. Errors clearly if the task has
    no active session (not started yet, or already finished) — in that case use
    ``session send`` directly, or ``task continue`` to resume from review.
    """
    try:
        with cli_main.get_client(ctx) as client:
            board = client.get_board(workspace_id)
            tasks: List[dict] = board.get("tasks", []) if isinstance(board, dict) else []
            match = next((t for t in tasks if t.get("id") == task_id), None)
            if match is None:
                raise click.ClickException(f"Task {task_id} not found in workspace {workspace_id}.")
            session_id = match.get("session_id")
            if not session_id:
                raise click.ClickException(
                    f"Task {task_id} has no active session "
                    f"(status: {match.get('status', '?')}). "
                    "Use `session send` directly, or `task continue` to resume from review."
                )
            client.send_session(session_id, {"message": message, "attachments": []})
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit({"ok": True, "task_id": task_id, "session_id": session_id}, True)
    else:
        click.echo(f"sent to session {session_id}")


@task.command("abort")
@click.argument("task_id")
@click.option("--reason", required=True, help="Reason for aborting.")
@click.pass_context
def task_abort(ctx: click.Context, task_id: str, reason: str) -> None:
    """Abort a task."""
    body: Dict[str, Any] = {"reason": reason}
    try:
        with cli_main.get_client(ctx) as client:
            data = client.abort_task(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
