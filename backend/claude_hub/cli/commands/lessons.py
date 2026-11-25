"""``lessons`` command group."""

from __future__ import annotations

from typing import Any, Dict, List

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import merge_payload
from claude_hub.cli.output import emit, print_rows, truncate

LESSON_COLUMNS = ["id", "title", "summary", "hit_count"]


@click.group()
def lessons() -> None:
    """Browse reusable feedback lessons."""


@lessons.command("list")
@click.argument("workspace_id")
@click.option("--query", default="", help="Keyword search.")
@click.option(
    "--limit", default=20, type=click.IntRange(1, 50), help="Max lessons to return (1-50)."
)
@click.option("--include-inactive", is_flag=True, default=False, help="Include inactive lessons.")
@click.pass_context
def lessons_list(
    ctx: click.Context,
    workspace_id: str,
    query: str,
    limit: int,
    include_inactive: bool,
) -> None:
    """List or search feedback lessons for a workspace."""
    params: Dict[str, Any] = {
        "query": query,
        "limit": limit,
        "include_inactive": include_inactive,
    }
    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_lessons(workspace_id, params)
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
                "title": item.get("title", ""),
                "summary": truncate(item.get("summary", "")),
                "hit_count": item.get("hit_count", ""),
            }
        )
    print_rows(rows, LESSON_COLUMNS)


@lessons.command("get")
@click.argument("workspace_id")
@click.argument("lesson_id")
@click.pass_context
def lessons_get(ctx: click.Context, workspace_id: str, lesson_id: str) -> None:
    """Fetch a single feedback lesson."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_lesson(workspace_id, lesson_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@lessons.command("create")
@click.argument("workspace_id")
@click.option(
    "--summary",
    default=None,
    help="Lesson summary (auto-derived from --title and --do when omitted).",
)
@click.option("--title", default=None, help="Lesson title.")
@click.option("--fingerprint", default=None, help="Stable lesson fingerprint.")
@click.option("--applies-when", "applies_when", multiple=True, help="Applicability note.")
@click.option("--do", "do_text", default=None, help="Recommended behavior.")
@click.option("--avoid", default=None, help="Behavior to avoid.")
@click.option("--tag", "tags", multiple=True, help="Lesson tag (repeatable).")
@click.option(
    "--scope",
    type=click.Choice(["workspace", "family", "global"]),
    default=None,
    help="Lesson scope.",
)
@click.option("--source-draft-id", "source_draft_ids", multiple=True, help="Source draft id.")
@click.option("--source-record-id", "source_record_ids", multiple=True, help="Source record id.")
@click.option("--evidence-task-id", "evidence_task_ids", multiple=True, help="Evidence task id.")
@click.option("--confidence", type=float, default=None, help="Confidence score.")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def lessons_create(
    ctx: click.Context,
    workspace_id: str,
    summary: str | None,
    title: str | None,
    fingerprint: str | None,
    applies_when: tuple,
    do_text: str | None,
    avoid: str | None,
    tags: tuple,
    scope: str | None,
    source_draft_ids: tuple,
    source_record_ids: tuple,
    evidence_task_ids: tuple,
    confidence: float | None,
    payload_json: str | None,
) -> None:
    """Create or promote an active feedback lesson."""
    body = merge_payload(
        payload_json,
        summary=summary,
        title=title,
        fingerprint=fingerprint,
        applies_when=applies_when,
        do=do_text,
        avoid=avoid,
        tags=tags,
        scope=scope,
        source_draft_ids=source_draft_ids,
        source_record_ids=source_record_ids,
        evidence_task_ids=evidence_task_ids,
        confidence=confidence,
    )
    resolved_summary = str(body.get("summary") or "").strip()
    if not resolved_summary:
        resolved_title = str(body.get("title") or "").strip()
        resolved_do = str(body.get("do") or "").strip()
        if resolved_title and resolved_do:
            derived = f"{resolved_title.rstrip('.')} — {resolved_do}"
            body["summary"] = derived[:200]
        else:
            raise click.ClickException(
                "--summary is required unless both --title and --do are provided."
            )
    try:
        with cli_main.get_client(ctx) as client:
            data = client.create_lesson(workspace_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@lessons.command("summarize")
@click.argument("workspace_id")
@click.option(
    "--mode",
    type=click.Choice(["incremental", "full"]),
    default=None,
    help="Summary mode.",
)
@click.option("--limit", type=int, default=None, help="Task limit.")
@click.option("--force/--no-force", default=None, help="Force summary generation.")
@click.option(
    "--clear-context/--no-clear-context",
    default=None,
    help="Legacy flag accepted for compatibility (silent runs always use a fresh ephemeral turn).",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def lessons_summarize(
    ctx: click.Context,
    workspace_id: str,
    mode: str | None,
    limit: int | None,
    force: bool | None,
    clear_context: bool | None,
    payload_json: str | None,
) -> None:
    """Trigger silent background workspace lesson summarization."""
    body = merge_payload(
        payload_json,
        mode=mode,
        limit=limit,
        force=force,
        clear_context=clear_context,
    )
    try:
        with cli_main.get_client(ctx) as client:
            data = client.summarize_lessons(workspace_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@lessons.command("delete")
@click.argument("workspace_id")
@click.argument("lesson_id")
@click.pass_context
def lessons_delete(ctx: click.Context, workspace_id: str, lesson_id: str) -> None:
    """Archive (delete) a feedback lesson."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.delete_lesson(workspace_id, lesson_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if data is None:
        click.echo(f"deleted {lesson_id}")
    else:
        emit(data, cli_main.as_json(ctx))
