"""``schedule`` command group — manage scheduled tasks.

Four kinds are supported:
* ``chat_turn`` — queue a native turn in an existing Chat conversation.
* ``tab_message`` — type a message into an existing terminal tab's pane.
* ``new_session`` — create a new session in a workspace and send it a message.
* ``hub_task`` — publish a Hub-native task that runs in an isolated session.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import (
    merge_payload,
    parse_json_object,
    resolve_workspace_id,
)
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

SCHEDULE_KINDS = ["chat_turn", "tab_message", "new_session", "hub_task"]
RUN_COLUMNS = ["scheduled_for", "status", "id", "error"]


def _schedule_body(
    payload_json: Optional[str],
    *,
    name: Optional[str],
    kind: Optional[str],
    enabled: Optional[bool],
    run_at: Optional[str],
    cron: Optional[str],
    interval: Optional[int],
    tab_id: Optional[str],
    workspace_id: Optional[str],
    agent_type: Optional[str],
    message: Optional[str],
    task_title: Optional[str],
    env_preset: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a scheduled-task body, validating the schedule spec locally."""
    schedule_fields = [run_at, cron, interval]
    provided = [f for f in schedule_fields if f is not None]
    if len(provided) > 1:
        raise click.ClickException("Only one of --run-at, --cron, or --interval may be set.")
    return merge_payload(
        payload_json,
        name=name,
        kind=kind,
        enabled=enabled,
        run_at=run_at,
        cron=cron,
        interval_seconds=interval,
        tab_id=tab_id,
        workspace_id=workspace_id,
        agent_type=agent_type,
        message=message,
        task_title=task_title,
        env_preset=env_preset,
    )


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
@click.argument("schedule_id")
@click.pass_context
def schedule_get(ctx: click.Context, schedule_id: str) -> None:
    """Fetch a single scheduled task."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_scheduled_task(schedule_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@schedule.command("create")
@click.option("--name", required=True, help="Human-readable schedule name.")
@click.option(
    "--kind",
    required=True,
    type=click.Choice(SCHEDULE_KINDS),
    help="How the schedule fires when due.",
)
@click.option("--run-at", default=None, help="One-shot fire time (ISO 8601).")
@click.option("--cron", default=None, help="5-field cron expression (e.g. '30 9 * * *').")
@click.option("--interval", type=int, default=None, help="Repeat every N seconds.")
@click.option(
    "--tab-id",
    default=None,
    help="Target Chat/terminal tab (defaults to $CLAUDE_HUB_TAB_ID for chat_turn/tab_message).",
)
@click.option(
    "--workspace-id",
    default=None,
    help="Workspace id (defaults to cwd match for new_session/hub_task).",
)
@click.option(
    "--agent-type",
    default=None,
    help="Agent type for new_session / hub_task (default claude).",
)
@click.option("--message", default=None, help="Message to send / task prompt.")
@click.option("--task-title", default=None, help="Task title (hub_task kind).")
@click.option(
    "--env-preset",
    default=None,
    help="Env preset id/name for the spawned session (new_session / hub_task).",
)
@click.option("--enabled/--disabled", default=True, help="Create enabled (default) or disabled.")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def schedule_create(
    ctx: click.Context,
    name: str,
    kind: str,
    run_at: Optional[str],
    cron: Optional[str],
    interval: Optional[int],
    tab_id: Optional[str],
    workspace_id: Optional[str],
    agent_type: Optional[str],
    message: Optional[str],
    task_title: Optional[str],
    env_preset: Optional[str],
    enabled: bool,
    payload_json: Optional[str],
) -> None:
    """Create a scheduled task. Exactly one of --run-at / --cron / --interval."""
    raw_payload = parse_json_object(payload_json)
    effective_tab = tab_id
    if (
        effective_tab is None
        and "tab_id" not in raw_payload
        and kind in ("chat_turn", "tab_message")
    ):
        env_tab = os.environ.get("CLAUDE_HUB_TAB_ID", "").strip()
        if env_tab:
            effective_tab = env_tab

    try:
        with cli_main.get_client(ctx) as client:
            effective_ws = workspace_id
            if (
                effective_ws is None
                and "workspace_id" not in raw_payload
                and kind in ("new_session", "hub_task")
            ):
                effective_ws = resolve_workspace_id(client, None)
            body = _schedule_body(
                payload_json,
                name=name,
                kind=kind,
                enabled=enabled,
                run_at=run_at,
                cron=cron,
                interval=interval,
                tab_id=effective_tab,
                workspace_id=effective_ws,
                agent_type=agent_type,
                message=message,
                task_title=task_title,
                env_preset=env_preset,
            )
            data = client.create_scheduled_task(body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@schedule.command("update")
@click.argument("schedule_id")
@click.option("--name", default=None, help="New schedule name.")
@click.option("--enabled/--disabled", default=None, help="Enable or disable the schedule.")
@click.option("--run-at", default=None, help="One-shot fire time (ISO 8601).")
@click.option("--cron", default=None, help="5-field cron expression.")
@click.option("--interval", type=int, default=None, help="Repeat every N seconds.")
@click.option("--tab-id", default=None, help="Target Chat/terminal tab (chat_turn/tab_message).")
@click.option("--workspace-id", default=None, help="Workspace (new_session / hub_task kinds).")
@click.option("--agent-type", default=None, help="Agent type.")
@click.option("--message", default=None, help="Message to send / task prompt.")
@click.option("--task-title", default=None, help="Task title (hub_task kind).")
@click.option(
    "--env-preset",
    default=None,
    help="Env preset id/name for the spawned session (new_session / hub_task).",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def schedule_update(
    ctx: click.Context,
    schedule_id: str,
    name: Optional[str],
    enabled: Optional[bool],
    run_at: Optional[str],
    cron: Optional[str],
    interval: Optional[int],
    tab_id: Optional[str],
    workspace_id: Optional[str],
    agent_type: Optional[str],
    message: Optional[str],
    task_title: Optional[str],
    env_preset: Optional[str],
    payload_json: Optional[str],
) -> None:
    """Update fields (including --enabled/--disabled) of a scheduled task."""
    body = _schedule_body(
        payload_json,
        name=name,
        kind=None,
        enabled=enabled,
        run_at=run_at,
        cron=cron,
        interval=interval,
        tab_id=tab_id,
        workspace_id=workspace_id,
        agent_type=agent_type,
        message=message,
        task_title=task_title,
        env_preset=env_preset,
    )
    try:
        with cli_main.get_client(ctx) as client:
            data = client.update_scheduled_task(schedule_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@schedule.command("delete")
@click.argument("schedule_id")
@click.pass_context
def schedule_delete(ctx: click.Context, schedule_id: str) -> None:
    """Delete a scheduled task."""
    try:
        with cli_main.get_client(ctx) as client:
            client.delete_scheduled_task(schedule_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit({"ok": True, "schedule_id": schedule_id}, True)
    else:
        click.echo(f"deleted {schedule_id}")


@schedule.command("run")
@click.argument("schedule_id")
@click.pass_context
def schedule_run(ctx: click.Context, schedule_id: str) -> None:
    """Fire a scheduled task immediately."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.run_scheduled_task(schedule_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@schedule.command("runs")
@click.argument("schedule_id", required=False, default=None)
@click.option(
    "--cancel",
    "cancel_run_id",
    default=None,
    help="Cancel one scheduled run by RUN_ID.",
)
@click.option(
    "--clear",
    is_flag=True,
    default=False,
    help="Cancel all queued/waiting occurrences of SCHEDULE_ID.",
)
@click.pass_context
def schedule_runs(
    ctx: click.Context,
    schedule_id: Optional[str],
    cancel_run_id: Optional[str],
    clear: bool,
) -> None:
    """List durable runs of a schedule, cancel one run (--cancel), or clear backlog (--clear)."""
    if cancel_run_id and clear:
        raise click.UsageError("Pass either --cancel RUN_ID or --clear, not both.")
    if cancel_run_id:
        try:
            with cli_main.get_client(ctx) as client:
                data = client.cancel_scheduled_task_run(cancel_run_id)
        except HubError as e:
            raise click.ClickException(str(e)) from e
        emit(data, cli_main.as_json(ctx))
        return

    if not schedule_id:
        raise click.UsageError("SCHEDULE_ID is required unless --cancel RUN_ID is passed.")

    if clear:
        try:
            with cli_main.get_client(ctx) as client:
                data = client.clear_scheduled_task_backlog(schedule_id)
        except HubError as e:
            raise click.ClickException(str(e)) from e
        emit(data, cli_main.as_json(ctx))
        if not cli_main.as_json(ctx):
            click.echo(f"cancelled {data.get('cancelled', 0)} queued run(s)")
        return

    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_scheduled_task_runs(schedule_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit(data, True)
        return
    rows = [
        {
            "scheduled_for": item.get("scheduled_for", ""),
            "status": item.get("status", ""),
            "id": item.get("id", ""),
            "error": truncate(item.get("error") or item.get("waiting_reason") or "", 60),
        }
        for item in data or []
    ]
    print_rows(rows, RUN_COLUMNS)
