"""``schedule`` command group — manage scheduled tasks.

Scheduled tasks fire an action on a cron / interval / one-off basis. Three
kinds are supported:

* ``session_message`` — send a message to an existing managed session. This is
  the agent self-scheduling primitive: an agent calls this command (which hits
  the scheduling API) to register a schedule that re-messages its own session.
* ``new_session`` — create a new session in a workspace and send it a message.
* ``hub_task`` — publish a Hub-native task that runs on a throwaway ephemeral
  session and auto-cleans when done (no agent / reviewer resources held).
"""

from __future__ import annotations

from typing import Any, Dict, List

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import merge_payload
from claude_hub.cli.output import emit, print_rows, truncate

SCHEDULE_COLUMNS = [
    "id",
    "name",
    "kind",
    "enabled",
    "next_run_at",
    "last_run_at",
    "last_status",
    "run_count",
]

SCHEDULE_KINDS = ["session_message", "new_session", "hub_task"]


def _schedule_body(
    payload_json: str | None,
    *,
    name: str | None,
    kind: str | None,
    enabled: bool | None,
    run_at: str | None,
    cron: str | None,
    interval: int | None,
    session_id: str | None,
    workspace_id: str | None,
    agent_type: str | None,
    message: str | None,
    task_title: str | None,
) -> Dict[str, Any]:
    """Build a scheduled-task body, validating the schedule spec locally."""
    schedule_fields = [run_at, cron, interval]
    provided = [f for f in schedule_fields if f is not None]
    if len(provided) > 1:
        raise click.ClickException("Only one of --run-at, --cron, or --interval may be set.")
    body = merge_payload(
        payload_json,
        name=name,
        kind=kind,
        enabled=enabled,
        run_at=run_at,
        cron=cron,
        interval_seconds=interval,
        session_id=session_id,
        workspace_id=workspace_id,
        agent_type=agent_type,
        message=message,
        task_title=task_title,
    )
    return body


@click.group()
def schedule() -> None:
    """Manage scheduled tasks (cron / interval / one-off)."""


@schedule.command("list")
@click.pass_context
def schedule_list(ctx: click.Context) -> None:
    """List all scheduled tasks."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_scheduled_tasks()
    except HubError as e:
        raise click.ClickException(str(e)) from e

    if cli_main.as_json(ctx):
        emit(data, True)
        return

    rows: List[dict] = []
    for item in data or []:
        rows.append(
            {
                "id": item.get("id", ""),
                "name": truncate(item.get("name", "")),
                "kind": item.get("kind", ""),
                "enabled": item.get("enabled", ""),
                "next_run_at": item.get("next_run_at", ""),
                "last_run_at": item.get("last_run_at", ""),
                "last_status": item.get("last_status", ""),
                "run_count": item.get("run_count", ""),
            }
        )
    print_rows(rows, SCHEDULE_COLUMNS)


@schedule.command("get")
@click.argument("task_id")
@click.pass_context
def schedule_get(ctx: click.Context, task_id: str) -> None:
    """Fetch a single scheduled task."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_scheduled_task(task_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@schedule.command("create")
@click.option("--name", required=True, help="Human-readable task name.")
@click.option(
    "--kind",
    required=True,
    type=click.Choice(SCHEDULE_KINDS),
    help="How the task fires when due.",
)
@click.option("--run-at", default=None, help="One-shot fire time (ISO 8601).")
@click.option("--cron", default=None, help="5-field cron expression (e.g. '30 9 * * *').")
@click.option("--interval", type=int, default=None, help="Repeat every N seconds.")
@click.option("--session-id", default=None, help="Target session (session_message kind).")
@click.option("--workspace-id", default=None, help="Workspace (new_session / hub_task kinds).")
@click.option(
    "--agent-type",
    default=None,
    help="Agent type for new_session / hub_task (default claude).",
)
@click.option("--message", default=None, help="Message to send / task prompt.")
@click.option("--task-title", default=None, help="Task title (hub_task kind).")
@click.option("--enabled/--disabled", default=True, help="Create enabled (default) or disabled.")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def schedule_create(
    ctx: click.Context,
    name: str,
    kind: str,
    run_at: str | None,
    cron: str | None,
    interval: int | None,
    session_id: str | None,
    workspace_id: str | None,
    agent_type: str | None,
    message: str | None,
    task_title: str | None,
    enabled: bool,
    payload_json: str | None,
) -> None:
    """Create a scheduled task. Exactly one of --run-at / --cron / --interval."""
    body = _schedule_body(
        payload_json,
        name=name,
        kind=kind,
        enabled=enabled,
        run_at=run_at,
        cron=cron,
        interval=interval,
        session_id=session_id,
        workspace_id=workspace_id,
        agent_type=agent_type,
        message=message,
        task_title=task_title,
    )
    try:
        with cli_main.get_client(ctx) as client:
            data = client.create_scheduled_task(body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@schedule.command("update")
@click.argument("task_id")
@click.option("--name", default=None, help="New task name.")
@click.option("--run-at", default=None, help="One-shot fire time (ISO 8601).")
@click.option("--cron", default=None, help="5-field cron expression.")
@click.option("--interval", type=int, default=None, help="Repeat every N seconds.")
@click.option("--session-id", default=None, help="Target session (session_message kind).")
@click.option("--workspace-id", default=None, help="Workspace (new_session / hub_task kinds).")
@click.option("--agent-type", default=None, help="Agent type.")
@click.option("--message", default=None, help="Message to send / task prompt.")
@click.option("--task-title", default=None, help="Task title (hub_task kind).")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def schedule_update(
    ctx: click.Context,
    task_id: str,
    name: str | None,
    run_at: str | None,
    cron: str | None,
    interval: int | None,
    session_id: str | None,
    workspace_id: str | None,
    agent_type: str | None,
    message: str | None,
    task_title: str | None,
    payload_json: str | None,
) -> None:
    """Update fields of a scheduled task (kind is immutable).

    When any schedule field (--run-at / --cron / --interval) is supplied the
    next-run time is recomputed.
    """
    body = _schedule_body(
        payload_json,
        name=name,
        kind=None,
        enabled=None,
        run_at=run_at,
        cron=cron,
        interval=interval,
        session_id=session_id,
        workspace_id=workspace_id,
        agent_type=agent_type,
        message=message,
        task_title=task_title,
    )
    try:
        with cli_main.get_client(ctx) as client:
            data = client.update_scheduled_task(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@schedule.command("delete")
@click.argument("task_id")
@click.pass_context
def schedule_delete(ctx: click.Context, task_id: str) -> None:
    """Delete a scheduled task."""
    try:
        with cli_main.get_client(ctx) as client:
            client.delete_scheduled_task(task_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"deleted {task_id}")


@schedule.command("enable")
@click.argument("task_id")
@click.pass_context
def schedule_enable(ctx: click.Context, task_id: str) -> None:
    """Enable a scheduled task."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.update_scheduled_task(task_id, {"enabled": True})
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@schedule.command("disable")
@click.argument("task_id")
@click.pass_context
def schedule_disable(ctx: click.Context, task_id: str) -> None:
    """Disable a scheduled task."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.update_scheduled_task(task_id, {"enabled": False})
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@schedule.command("run")
@click.argument("task_id")
@click.pass_context
def schedule_run(ctx: click.Context, task_id: str) -> None:
    """Fire a scheduled task immediately.

    Stamps and advances the schedule like a tick fire (a one-shot is disabled
    after firing). Exits non-zero if the fire side-effect fails.
    """
    try:
        with cli_main.get_client(ctx) as client:
            data = client.run_scheduled_task(task_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
