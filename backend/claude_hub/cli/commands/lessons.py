"""``lessons`` command group — workspace feedback lessons, context pull, and capture."""

from __future__ import annotations

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

LESSON_COLUMNS = ["id", "title", "summary", "hit_count"]


@click.group()
def lessons() -> None:
    """Manage reusable workspace lessons, compact context pull, and feedback capture."""


@lessons.command("context")
@click.argument("workspace_id", required=False, default=None)
@click.option("--query", required=True, help="Topic or task keywords to match.")
@click.option("--limit", type=click.IntRange(1, 10), default=5, show_default=True)
@click.pass_context
def lessons_context(
    ctx: click.Context,
    workspace_id: Optional[str],
    query: str,
    limit: int,
) -> None:
    """Pull compact confidence/relevance-filtered lesson indices for an active task."""
    try:
        with cli_main.get_client(ctx) as client:
            resolved_ws = resolve_workspace_id(client, workspace_id)
            data = client.request(
                "GET",
                f"/api/workspaces/{resolved_ws}/feedback/context",
                params={"query": query, "limit": limit},
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@lessons.command("list")
@click.argument("workspace_id", required=False, default=None)
@click.option("--query", default="", help="Keyword search.")
@click.option(
    "--limit",
    default=20,
    type=click.IntRange(1, 50),
    help="Max lessons to return (1-50).",
)
@click.option("--include-inactive", is_flag=True, default=False, help="Include inactive lessons.")
@click.pass_context
def lessons_list(
    ctx: click.Context,
    workspace_id: Optional[str],
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
            resolved_ws = resolve_workspace_id(client, workspace_id)
            data = client.list_lessons(resolved_ws, params)
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
@click.argument("lesson_id")
@click.option("--workspace-id", default=None, help="Workspace id (defaults to cwd match).")
@click.pass_context
def lessons_get(ctx: click.Context, lesson_id: str, workspace_id: Optional[str]) -> None:
    """Fetch a single feedback lesson in full."""
    try:
        with cli_main.get_client(ctx) as client:
            resolved_ws = resolve_workspace_id(client, workspace_id)
            data = client.get_lesson(resolved_ws, lesson_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@lessons.command("create")
@click.argument("workspace_id", required=False, default=None)
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
    workspace_id: Optional[str],
    summary: Optional[str],
    title: Optional[str],
    fingerprint: Optional[str],
    applies_when: tuple,
    do_text: Optional[str],
    avoid: Optional[str],
    tags: tuple,
    scope: Optional[str],
    source_draft_ids: tuple,
    source_record_ids: tuple,
    evidence_task_ids: tuple,
    confidence: Optional[float],
    payload_json: Optional[str],
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
            resolved_ws = resolve_workspace_id(client, workspace_id)
            data = client.create_lesson(resolved_ws, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@lessons.command("delete")
@click.argument("lesson_id")
@click.option("--workspace-id", default=None, help="Workspace id (defaults to cwd match).")
@click.pass_context
def lessons_delete(ctx: click.Context, lesson_id: str, workspace_id: Optional[str]) -> None:
    """Archive (delete) a feedback lesson."""
    try:
        with cli_main.get_client(ctx) as client:
            resolved_ws = resolve_workspace_id(client, workspace_id)
            data = client.delete_lesson(resolved_ws, lesson_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if data is None:
        click.echo(f"deleted {lesson_id}")
    else:
        emit(data, cli_main.as_json(ctx))


@lessons.command("sources")
@click.option("--tab-id", envvar="CLAUDE_HUB_TAB_ID", required=True)
@click.option("--limit", type=click.IntRange(1, 20), default=10, show_default=True)
@click.pass_context
def lessons_sources(ctx: click.Context, tab_id: str, limit: int) -> None:
    """Read bounded recent user-message IDs when capturing an explicit correction."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.request(
                "GET",
                f"/api/workspaces/tabs/{tab_id}/feedback/sources",
                params={"limit": limit},
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@lessons.command("capture")
@click.argument("workspace_id", required=False, default=None)
@click.option("--tab-id", envvar="CLAUDE_HUB_TAB_ID", required=True)
@click.option("--turn-id", required=True)
@click.option("--message-id", required=True)
@click.option(
    "--quote",
    required=True,
    help="Exact explicit correction from the persisted user message; max 1024 characters.",
)
@click.pass_context
def lessons_capture(
    ctx: click.Context,
    workspace_id: Optional[str],
    tab_id: str,
    turn_id: str,
    message_id: str,
    quote: str,
) -> None:
    """Record an explicit user correction as verifiable feedback evidence."""
    body = {
        "tab_id": tab_id,
        "turn_id": turn_id,
        "message_id": message_id,
        "quote": quote,
    }
    try:
        with cli_main.get_client(ctx) as client:
            resolved_ws = resolve_workspace_id(client, workspace_id)
            data = client.request(
                "POST",
                f"/api/workspaces/{resolved_ws}/feedback/corrections",
                json=body,
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@lessons.command("reap")
@click.argument("workspace_id", required=False, default=None)
@click.option("--task-id", default=None, help="Reap feedback evidence for one task.")
@click.option(
    "--workspace-id",
    "workspace_id_opt",
    default=None,
    help="Workspace id for batch lesson summarization.",
)
@click.option("--source", default=None, help="Feedback source (--task-id mode).")
@click.option("--summary", default=None, help="Feedback summary (--task-id mode).")
@click.option(
    "--summarize/--no-summarize",
    default=None,
    help="Trigger lesson summarization right after reaping task feedback (--task-id mode).",
)
@click.option("--tag", "tags", multiple=True, help="Feedback tag (--task-id mode, repeatable).")
@click.option(
    "--lesson-draft-json",
    "lesson_drafts",
    multiple=True,
    help="Lesson draft JSON object (--task-id mode, repeatable).",
)
@click.option(
    "--mode",
    type=click.Choice(["incremental", "full"]),
    default=None,
    help="Workspace summary mode.",
)
@click.option("--limit", type=int, default=None, help="Task limit (workspace mode).")
@click.option(
    "--force/--no-force",
    default=None,
    help="Force summary generation (workspace mode).",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def lessons_reap(
    ctx: click.Context,
    workspace_id: Optional[str],
    task_id: Optional[str],
    workspace_id_opt: Optional[str],
    source: Optional[str],
    summary: Optional[str],
    summarize: Optional[bool],
    tags: tuple,
    lesson_drafts: tuple,
    mode: Optional[str],
    limit: Optional[int],
    force: Optional[bool],
    payload_json: Optional[str],
) -> None:
    """Reap feedback evidence from one task (--task-id) or summarize workspace tasks."""
    if (
        workspace_id is not None
        and workspace_id_opt is not None
        and workspace_id != workspace_id_opt
    ):
        raise click.UsageError("Conflicting positional WORKSPACE_ID and --workspace-id.")
    effective_ws = workspace_id or workspace_id_opt
    if task_id is not None and effective_ws is not None:
        raise click.UsageError("Pass either --task-id or WORKSPACE_ID/--workspace-id, not both.")
    try:
        with cli_main.get_client(ctx) as client:
            if task_id is not None:
                body = merge_payload(
                    payload_json, source=source, summary=summary, summarize=summarize
                )
                if tags:
                    body["tags"] = list(tags)
                if lesson_drafts:
                    body["lesson_drafts"] = [
                        parse_json_object(value, "--lesson-draft-json") for value in lesson_drafts
                    ]
                data = client.reap_task_feedback(task_id, body)
            else:
                resolved_ws = resolve_workspace_id(client, effective_ws)
                body = merge_payload(payload_json, mode=mode, limit=limit, force=force)
                data = client.summarize_lessons(resolved_ws, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@lessons.command("config")
@click.argument("workspace_id", required=False, default=None)
@click.option("--enabled/--disabled", default=None, help="Enable or disable automatic feedback.")
@click.option(
    "--cooldown",
    type=click.IntRange(3600, 604800),
    default=None,
    help="Minimum seconds between model launches.",
)
@click.option("--max-records", type=click.IntRange(1, 10), default=None)
@click.pass_context
def lessons_config(
    ctx: click.Context,
    workspace_id: Optional[str],
    enabled: Optional[bool],
    cooldown: Optional[int],
    max_records: Optional[int],
) -> None:
    """Show or update bounded feedback automation settings for a workspace."""
    try:
        with cli_main.get_client(ctx) as client:
            resolved_ws = resolve_workspace_id(client, workspace_id)
            path = f"/api/workspaces/{resolved_ws}/feedback/automation"
            status_data = client.request("GET", path)
            if enabled is None and cooldown is None and max_records is None:
                data = status_data
            else:
                body = dict(status_data["settings"])
                for key, value in (
                    ("enabled", enabled),
                    ("cooldown_seconds", cooldown),
                    ("max_records", max_records),
                ):
                    if value is not None:
                        body[key] = value
                data = client.request("PUT", path, json=body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
