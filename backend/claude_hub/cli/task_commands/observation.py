"""Task event, messaging, and dispatch commands."""

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
    _emit_task_tree,
    _resolve_call_id,
)


@task.command("tree")
@click.argument("workspace_id")
@click.argument("task_id", required=False)
@click.pass_context
def task_tree(ctx: click.Context, workspace_id: str, task_id: Optional[str]) -> None:
    """List top-level Tasks, or the subtree rooted at TASK_ID."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_task_tree(workspace_id, task_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    rows = data if isinstance(data, list) else []
    _emit_task_tree(rows, cli_main.as_json(ctx))


@task.command("events")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option("--since-sequence", default=0, type=int, show_default=True)
@click.option("--subtree/--no-subtree", default=False, show_default=True)
@click.pass_context
def task_events(
    ctx: click.Context,
    workspace_id: str,
    task_id: str,
    since_sequence: int,
    subtree: bool,
) -> None:
    """List Task mailbox events for ``task:<task_id>``."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_task_events(
                workspace_id,
                task_id,
                since_sequence=since_sequence,
                subtree=subtree,
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    rows = data if isinstance(data, list) else []
    _emit_task_events(rows, cli_main.as_json(ctx))


@task.command("wait")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option("--since-sequence", default=0, type=int, show_default=True)
@click.option("--subtree/--no-subtree", default=False, show_default=True)
@click.option("--timeout-seconds", default=30.0, type=float, show_default=True)
@click.option(
    "--ack",
    is_flag=True,
    default=False,
    help="After a non-empty flushed event list, POST /ack at max(sequence).",
)
@click.pass_context
def task_wait(
    ctx: click.Context,
    workspace_id: str,
    task_id: str,
    since_sequence: int,
    subtree: bool,
    timeout_seconds: float,
    ack: bool,
) -> None:
    """Wait on the Task mailbox for ``task:<task_id>``."""
    as_json = cli_main.as_json(ctx)
    try:
        with cli_main.get_client(ctx) as client:
            rows = client.wait_task_events(
                workspace_id,
                task_id,
                since_sequence=since_sequence,
                subtree=subtree,
                timeout_seconds=timeout_seconds,
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
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
        raise click.ClickException("wait events missing sequence; not acknowledging")
    max_sequence = max(sequences)
    try:
        with cli_main.get_client(ctx) as client:
            client.ack_task_events(workspace_id, task_id, max_sequence)
    except HubError as e:
        click.echo(ACK_FAILED_NOTE, err=True)
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps({"acked_sequence": max_sequence}), err=True)
    else:
        click.echo(f"acked sequence {max_sequence}")


@task.command("ack")
@click.argument("workspace_id")
@click.argument("task_id")
@click.argument("sequence", type=int)
@click.pass_context
def task_ack(
    ctx: click.Context,
    workspace_id: str,
    task_id: str,
    sequence: int,
) -> None:
    """ACK ``task:<task_id>`` at SEQUENCE."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.ack_task_events(
                workspace_id,
                task_id,
                sequence,
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("followup")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option("--message", required=True, help="Follow-up message for the Task inbox.")
@click.option("--call-id", default=None, help="Stable retry id. New UUID if omitted.")
@click.pass_context
def task_followup(
    ctx: click.Context,
    workspace_id: str,
    task_id: str,
    message: str,
    call_id: Optional[str],
) -> None:
    """POST Task followup with a durable call_id on the Task Graph surface."""
    resolved = _resolve_call_id(call_id)
    _echo_call_id(resolved)
    try:
        with cli_main.get_client(ctx) as client:
            data = client.followup_task(
                workspace_id,
                task_id,
                {"message": message, "call_id": resolved},
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("send")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option("--message", required=True, help="Follow-up message for the Task inbox.")
@click.option("--call-id", default=None, help="Stable retry id. New UUID if omitted.")
@click.pass_context
def task_send(
    ctx: click.Context,
    workspace_id: str,
    task_id: str,
    message: str,
    call_id: Optional[str],
) -> None:
    """Compatibility alias for ``task followup``. Uses Task call_id, not session send."""
    ctx.forward(task_followup)


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


@task.command("spawn")
@click.argument("task_id")
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "cursor", "terminal"]),
    default=None,
    help="Worker agent type.",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_spawn(
    ctx: click.Context,
    task_id: str,
    agent_type: Optional[str],
    payload_json: Optional[str],
) -> None:
    """Spawn a worker session for a task."""
    body = merge_payload(payload_json, agent_type=agent_type)
    try:
        with cli_main.get_client(ctx) as client:
            data = client.spawn_worker(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("dispatch-decision")
@click.argument("task_id")
@click.option("--target-session-id", required=True, help="Target orchestrator session.")
@click.option(
    "--clear-context/--no-clear-context",
    default=False,
    help="Clear target agent context before dispatching.",
)
@click.option("--reason", default=None, help="Decision reason.")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_dispatch_decision(
    ctx: click.Context,
    task_id: str,
    target_session_id: str,
    clear_context: bool,
    reason: Optional[str],
    payload_json: Optional[str],
) -> None:
    """Apply a structured dispatcher decision."""
    body = merge_payload(
        payload_json,
        target_session_id=target_session_id,
        clear_context=clear_context,
        reason=reason,
    )
    try:
        with cli_main.get_client(ctx) as client:
            data = client.apply_dispatch_decision(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
