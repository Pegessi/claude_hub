"""``lessons`` command group."""

from __future__ import annotations

from typing import Any, Dict, List

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
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
