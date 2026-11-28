"""Task mailbox events, follow-up messaging, and control commands."""

import json
from typing import Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import merge_payload
from claude_hub.cli.output import emit
from claude_hub.cli.task_commands.group import task
from claude_hub.cli.task_commands.read import (
    ACK_FAILED_NOTE,
    _echo_call_id,
    _emit_task_events,
    _resolve_call_id,
    _resolve_ws_id,
)


@task.command("events")
@click.argument("task_id")
@click.option("--since-sequence", default=0, type=int, show_default=True)
@click.option("--subtree/--no-subtree", default=False, show_default=True)
@click.option(
    "--wait",
    "wait_mode",
    is_flag=True,
    default=False,
    help="Long-poll the Task mailbox until new events arrive or --timeout-seconds elapses.",
)
@click.option("--timeout-seconds", default=30.0, type=float, show_default=True)
@click.option(
    "--ack",
    is_flag=True,
    default=False,
    help="With --wait, POST /ack at max(sequence) after flushing non-empty events.",
)
@click.option("--workspace-id", default=None, help="Optional workspace id (skips lookup).")
@click.pass_context
def task_events(
    ctx: click.Context,
    task_id: str,
    since_sequence: int,
    subtree: bool,
    wait_mode: bool,
    timeout_seconds: float,
    ack: bool,
    workspace_id: Optional[str],
) -> None:
    """List or long-poll (--wait) Task mailbox events for ``task:<task_id>``."""
    if ack and not wait_mode:
        raise click.UsageError("--ack requires --wait (or run `task ack TASK_ID SEQUENCE`).")
    as_json = cli_main.as_json(ctx)
    try:
        with cli_main.get_client(ctx) as client:
            ws_id = workspace_id or _resolve_ws_id(client, task_id, None)
            if wait_mode:
                rows = client.wait_task_events(
                    ws_id,
                    task_id,
                    since_sequence=since_sequence,
                    subtree=subtree,
                    timeout_seconds=timeout_seconds,
                )
            else:
                rows = client.get_task_events(
                    ws_id,
                    task_id,
                    since_sequence=since_sequence,
                    subtree=subtree,
                )
            events = rows if isinstance(rows, list) else []
            _emit_task_events(events, as_json)
            if not ack or not events:
                return
            sequences = [
                int(item["sequence"])
                for item in events
                if isinstance(item, dict) and item.get("sequence") is not None
            ]
            if not sequences:
                raise click.ClickException("events missing sequence; not acknowledging")
            max_sequence = max(sequences)
            try:
                client.ack_task_events(ws_id, task_id, max_sequence)
            except HubError as e:
                click.echo(ACK_FAILED_NOTE, err=True)
                raise click.ClickException(str(e)) from e
    except HubError as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        click.echo(json.dumps({"acked_sequence": max_sequence}), err=True)
    else:
        click.echo(f"acked sequence {max_sequence}")


@task.command("ack")
@click.argument("task_id")
@click.argument("sequence", type=int)
@click.option("--workspace-id", default=None, help="Optional workspace id (skips lookup).")
@click.pass_context
def task_ack(
    ctx: click.Context,
    task_id: str,
    sequence: int,
    workspace_id: Optional[str],
) -> None:
    """ACK ``task:<task_id>`` mailbox cursor at SEQUENCE."""
    try:
        with cli_main.get_client(ctx) as client:
            ws_id = workspace_id or _resolve_ws_id(client, task_id, None)
            data = client.ack_task_events(ws_id, task_id, sequence)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("followup")
@click.argument("task_id")
@click.option("--message", required=True, help="Follow-up message for the Task inbox.")
@click.option("--call-id", default=None, help="Stable retry id. New UUID if omitted.")
@click.option("--workspace-id", default=None, help="Optional workspace id (skips lookup).")
@click.pass_context
def task_followup(
    ctx: click.Context,
    task_id: str,
    message: str,
    call_id: Optional[str],
    workspace_id: Optional[str],
) -> None:
    """Send a durable follow-up message to a Task."""
    resolved = _resolve_call_id(call_id)
    _echo_call_id(resolved)
    try:
        with cli_main.get_client(ctx) as client:
            ws_id = workspace_id or _resolve_ws_id(client, task_id, None)
            data = client.followup_task(
                ws_id,
                task_id,
                {"message": message, "call_id": resolved},
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("abort")
@click.argument("task_id")
@click.option("--reason", required=True, help="Reason for aborting.")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_abort(
    ctx: click.Context,
    task_id: str,
    reason: str,
    payload_json: Optional[str],
) -> None:
    """Abort a task."""
    body = merge_payload(payload_json, reason=reason)
    try:
        with cli_main.get_client(ctx) as client:
            data = client.abort_task(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("request-review")
@click.argument("task_id")
@click.option("--message", default=None, help="Optional note for the reviewer.")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_request_review(
    ctx: click.Context,
    task_id: str,
    message: Optional[str],
    payload_json: Optional[str],
) -> None:
    """Manually request reviewer checks for a task."""
    body = merge_payload(payload_json, message=message)
    try:
        with cli_main.get_client(ctx) as client:
            data = client.request_task_review(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("delete")
@click.argument("task_id")
@click.pass_context
def task_delete(ctx: click.Context, task_id: str) -> None:
    """Delete a task and its reports."""
    try:
        with cli_main.get_client(ctx) as client:
            client.delete_task(task_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit({"ok": True}, True)
    else:
        click.echo(f"deleted {task_id}")
