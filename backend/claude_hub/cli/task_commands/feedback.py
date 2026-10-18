"""Task feedback commands."""

from typing import Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import merge_payload, parse_json_object
from claude_hub.cli.output import emit
from claude_hub.cli.task_commands.group import task


@task.group("feedback")
def task_feedback() -> None:
    """Manage task feedback evidence."""


@task_feedback.command("reap")
@click.argument("task_id")
@click.option("--source", default=None, help="Feedback source.")
@click.option("--summary", default=None, help="Feedback summary.")
@click.option("--tag", "tags", multiple=True, help="Feedback tag (repeatable).")
@click.option(
    "--lesson-draft-json",
    "lesson_drafts",
    multiple=True,
    help="Lesson draft JSON object (repeatable).",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_feedback_reap(
    ctx: click.Context,
    task_id: str,
    source: Optional[str],
    summary: Optional[str],
    tags: tuple,
    lesson_drafts: tuple,
    payload_json: Optional[str],
) -> None:
    """Manually collect feedback evidence and optional lesson drafts."""
    body = merge_payload(payload_json, source=source, summary=summary)
    if tags:
        body["tags"] = list(tags)
    if lesson_drafts:
        body["lesson_drafts"] = [
            parse_json_object(value, "--lesson-draft-json") for value in lesson_drafts
        ]
    try:
        with cli_main.get_client(ctx) as client:
            data = client.reap_task_feedback(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
