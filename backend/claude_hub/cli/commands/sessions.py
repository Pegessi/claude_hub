"""``session`` command group."""

from __future__ import annotations

from typing import Any, Dict, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.output import emit

REPORT_STATES = [
    "started",
    "working",
    "blocked",
    "needs_input",
    "ready_for_review",
    "completed",
    "review_started",
    "review_passed",
    "review_failed",
    "review_needs_input",
]


@click.group()
def session() -> None:
    """Interact with managed agent sessions."""


@session.command("send")
@click.argument("session_id")
@click.option("--message", required=True, help="Message to send to the session.")
@click.pass_context
def session_send(ctx: click.Context, session_id: str, message: str) -> None:
    """Send a message to a managed session."""
    body: Dict[str, Any] = {"message": message, "attachments": []}
    try:
        with cli_main.get_client(ctx) as client:
            client.send_session(session_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit({"ok": True}, True)
    else:
        click.echo("sent")


@session.command("report")
@click.argument("session_id")
@click.option(
    "--state",
    required=True,
    type=click.Choice(REPORT_STATES),
    help="Report state.",
)
@click.option("--message", required=True, help="Report message.")
@click.option("--task-id", default=None, help="Task id this report applies to.")
@click.option("--validation", default=None, help="Validation summary.")
@click.option("--risks", default=None, help="Known risks.")
@click.option(
    "--changed-file",
    "changed_files",
    multiple=True,
    help="Changed file path (repeatable).",
)
@click.pass_context
def session_report(
    ctx: click.Context,
    session_id: str,
    state: str,
    message: str,
    task_id: Optional[str],
    validation: Optional[str],
    risks: Optional[str],
    changed_files: tuple,
) -> None:
    """Append a progress report to a managed session."""
    body: Dict[str, Any] = {
        "state": state,
        "message": message,
        "changed_files": list(changed_files),
    }
    if task_id is not None:
        body["task_id"] = task_id
    if validation is not None:
        body["validation"] = validation
    if risks is not None:
        body["risks"] = risks
    try:
        with cli_main.get_client(ctx) as client:
            data = client.create_report(session_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
