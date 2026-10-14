"""``dot`` command group — Dot's memory, focus, work and delegation.

Dot (the persistent personal agent) runs these from its own Chat; people can
use them too. Everything goes through ``/api/dot``.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.output import emit, print_rows, truncate

CARD_KINDS = ["user", "work", "knowledge", "concept"]
AGENT_TYPES = ["codex", "claude", "cursor", "traex"]
TASK_MODES = ["direct", "reviewed", "autonomous"]


def _call(
    ctx: click.Context,
    method: str,
    path: str,
    *,
    json: Any = None,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    try:
        with cli_main.get_client(ctx) as client:
            return client.request(method, f"/api/dot{path}", json=json, params=params)
    except HubError as e:
        raise click.ClickException(str(e)) from e


def _rows(ctx: click.Context, data: Any, columns: List[str]) -> None:
    if cli_main.as_json(ctx):
        emit(data, True)
        return
    rows = [{column: truncate(item.get(column, "")) for column in columns} for item in data or []]
    print_rows(rows, columns)


@click.group()
def dot() -> None:
    """Dot: persistent personal agent memory, focus and delegation."""


@dot.command("setup")
@click.option(
    "--env-preset",
    default=None,
    help="Provider env preset for Dot's Chat tab (default: the Claude default preset).",
)
@click.pass_context
def dot_setup(ctx: click.Context, env_preset: Optional[str]) -> None:
    """Create Dot's memory home, Chat tab and schedules (safe to repeat)."""
    settings = ctx.obj
    preset = (
        env_preset
        or settings.env_preset_for_agent_type("claude")
        or getattr(settings, "default_env_preset", None)
    )
    emit(_call(ctx, "POST", "/setup", json={"env_preset": preset}), cli_main.as_json(ctx))


@dot.command("status")
@click.pass_context
def dot_status(ctx: click.Context) -> None:
    """Show Dot's tab, session and memory counts."""
    emit(_call(ctx, "GET", "/status"), cli_main.as_json(ctx))


@dot.command("health")
@click.pass_context
def dot_health(ctx: click.Context) -> None:
    """Run deterministic memory health checks."""
    data = _call(ctx, "GET", "/health")
    if cli_main.as_json(ctx):
        emit(data, True)
        return
    for check in data.get("checks", []):
        mark = "ok  " if check.get("ok") else "WARN"
        click.echo(f"{mark} {check.get('name')}: {check.get('detail')}")


@dot.command("rotate")
@click.pass_context
def dot_rotate(ctx: click.Context) -> None:
    """Start a fresh Dot session on the next turn."""
    emit(_call(ctx, "POST", "/rotate"), cli_main.as_json(ctx))


# ── memory ──────────────────────────────────────────────────────────────


@dot.command("recall")
@click.argument("query")
@click.option("--kind", type=click.Choice(CARD_KINDS), default=None)
@click.option("--limit", type=int, default=8, show_default=True)
@click.option("--archived", is_flag=True, help="Include archived cards.")
@click.pass_context
def dot_recall(
    ctx: click.Context, query: str, kind: Optional[str], limit: int, archived: bool
) -> None:
    """Search memory cards."""
    params: Dict[str, Any] = {"q": query, "limit": limit, "include_archived": archived}
    if kind:
        params["kind"] = kind
    _rows(ctx, _call(ctx, "GET", "/recall", params=params), ["id", "kind", "title", "summary"])


@dot.command("cards")
@click.option("--kind", type=click.Choice(CARD_KINDS), default=None)
@click.option("--archived", is_flag=True, help="Include archived cards.")
@click.pass_context
def dot_cards(ctx: click.Context, kind: Optional[str], archived: bool) -> None:
    """List memory cards (newest first)."""
    params: Dict[str, Any] = {"include_archived": archived}
    if kind:
        params["kind"] = kind
    _rows(
        ctx,
        _call(ctx, "GET", "/cards", params=params),
        ["id", "kind", "importance", "title", "summary"],
    )


@dot.command("show")
@click.argument("card_id")
@click.pass_context
def dot_show(ctx: click.Context, card_id: str) -> None:
    """Print one card in full."""
    card = _call(ctx, "GET", f"/cards/{quote(card_id, safe='')}")
    if cli_main.as_json(ctx):
        emit(card, True)
        return
    click.echo(
        f"{card['title']}  [{card['id']} · {card['kind']} · importance {card['importance']}]"
    )
    if card.get("summary"):
        click.echo(f"summary: {card['summary']}")
    for key in ("tags", "links", "files"):
        if card.get(key):
            click.echo(f"{key}: {', '.join(card[key])}")
    if card.get("source"):
        click.echo(f"source: {card['source']}")
    if card.get("body"):
        click.echo("")
        click.echo(card["body"])


def _upload(ctx: click.Context, path: str) -> str:
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise click.ClickException(f"Not a file: {path}")
    data = base64.b64encode(file_path.read_bytes()).decode("ascii")
    result = _call(ctx, "POST", "/files", json={"filename": file_path.name, "data_base64": data})
    return str(result["path"])


@dot.command("remember")
@click.option("--id", "card_id", default=None, help="Update this card instead of creating one.")
@click.option("--kind", type=click.Choice(CARD_KINDS), default=None, help="Default: knowledge.")
@click.option("--title", default=None)
@click.option("--summary", default=None, help="One line. Shown in context packs.")
@click.option("--body", default=None, help="Full text. Use '-' to read stdin.")
@click.option("--tag", "tags", multiple=True)
@click.option("--importance", type=click.IntRange(1, 5), default=None)
@click.option("--link", "links", multiple=True, help="Related card id (repeatable).")
@click.option(
    "--file", "files", multiple=True, help="Local file to store with the card (repeatable)."
)
@click.option("--source", default=None, help="Where this came from (URL, task id, ...).")
@click.pass_context
def dot_remember(
    ctx: click.Context,
    card_id: Optional[str],
    kind: Optional[str],
    title: Optional[str],
    summary: Optional[str],
    body: Optional[str],
    tags: Tuple[str, ...],
    importance: Optional[int],
    links: Tuple[str, ...],
    files: Tuple[str, ...],
    source: Optional[str],
) -> None:
    """Save a memory card (or update one with --id; unset fields are kept)."""
    if body == "-":
        body = click.get_text_stream("stdin").read()
    card: Dict[str, Any] = {}
    if card_id:
        card = _call(ctx, "GET", f"/cards/{quote(card_id, safe='')}")
        card = {
            key: card.get(key)
            for key in (
                "id",
                "kind",
                "title",
                "summary",
                "body",
                "tags",
                "importance",
                "links",
                "files",
                "source",
            )
        }
    elif not title:
        raise click.ClickException("--title is required for a new card.")
    stored = [_upload(ctx, path) for path in files]
    updates: Dict[str, Any] = {
        "kind": kind,
        "title": title,
        "summary": summary,
        "body": body,
        "importance": importance,
        "source": source,
    }
    card.update({key: value for key, value in updates.items() if value is not None})
    card.setdefault("kind", "knowledge")
    if tags:
        card["tags"] = sorted(set(card.get("tags") or []) | set(tags))
    if links:
        card["links"] = list(dict.fromkeys([*(card.get("links") or []), *links]))
    if stored:
        card["files"] = list(dict.fromkeys([*(card.get("files") or []), *stored]))
    saved = _call(ctx, "POST", "/cards", json=card)
    if cli_main.as_json(ctx):
        emit(saved, True)
    else:
        click.echo(f"saved {saved['id']} ({saved['kind']}): {saved['title']}")


@dot.command("forget")
@click.argument("card_id")
@click.option("--hard", is_flag=True, help="Delete the file instead of archiving it.")
@click.pass_context
def dot_forget(ctx: click.Context, card_id: str, hard: bool) -> None:
    """Archive (or delete) a memory card. Git history keeps old versions."""
    data = _call(ctx, "DELETE", f"/cards/{quote(card_id, safe='')}", params={"hard": hard})
    emit(data, cli_main.as_json(ctx))


@dot.command("journal")
@click.option("--day", default=None, help="YYYY-MM-DD (default: latest).")
@click.pass_context
def dot_journal(ctx: click.Context, day: Optional[str]) -> None:
    """Print one day of the raw talk log."""
    data = _call(ctx, "GET", "/journal", params={"day": day} if day else None)
    if cli_main.as_json(ctx):
        emit(data, True)
        return
    click.echo(data.get("text") or "(empty)")


# ── focus ───────────────────────────────────────────────────────────────


@dot.group("focus")
def dot_focus() -> None:
    """Dot's short list of what matters now (at most 7)."""


FOCUS_COLUMNS = ["id", "priority", "status", "title", "why"]


@dot_focus.command("list")
@click.option("--all", "include_all", is_flag=True, help="Include done and dropped items.")
@click.pass_context
def dot_focus_list(ctx: click.Context, include_all: bool) -> None:
    _rows(ctx, _call(ctx, "GET", "/focus", params={"all": include_all}), FOCUS_COLUMNS)


@dot_focus.command("add")
@click.argument("title")
@click.option("--why", default="")
@click.option("--priority", type=click.IntRange(1, 5), default=3, show_default=True)
@click.pass_context
def dot_focus_add(ctx: click.Context, title: str, why: str, priority: int) -> None:
    item = _call(ctx, "POST", "/focus", json={"title": title, "why": why, "priority": priority})
    emit(item, cli_main.as_json(ctx))


def _set_focus(ctx: click.Context, focus_id: str, **changes: Any) -> None:
    body = {key: value for key, value in changes.items() if value is not None}
    item = _call(ctx, "PATCH", f"/focus/{quote(focus_id, safe='')}", json=body)
    emit(item, cli_main.as_json(ctx))


@dot_focus.command("update")
@click.argument("focus_id")
@click.option("--title", default=None)
@click.option("--why", default=None)
@click.option("--priority", type=click.IntRange(1, 5), default=None)
@click.pass_context
def dot_focus_update(
    ctx: click.Context,
    focus_id: str,
    title: Optional[str],
    why: Optional[str],
    priority: Optional[int],
) -> None:
    _set_focus(ctx, focus_id, title=title, why=why, priority=priority)


@dot_focus.command("done")
@click.argument("focus_id")
@click.pass_context
def dot_focus_done(ctx: click.Context, focus_id: str) -> None:
    _set_focus(ctx, focus_id, status="done")


@dot_focus.command("drop")
@click.argument("focus_id")
@click.pass_context
def dot_focus_drop(ctx: click.Context, focus_id: str) -> None:
    _set_focus(ctx, focus_id, status="dropped")


# ── work ────────────────────────────────────────────────────────────────


@dot.group("work")
def dot_work() -> None:
    """Hub Tasks that Dot delegated and tracks."""


@dot_work.command("list")
@click.option("--all", "include_all", is_flag=True, help="Include closed items.")
@click.pass_context
def dot_work_list(ctx: click.Context, include_all: bool) -> None:
    _rows(
        ctx,
        _call(ctx, "GET", "/work", params={"all": include_all}),
        ["task_id", "status", "workspace_id", "title", "result_card"],
    )


@dot.command("delegate")
@click.option("--workspace", "workspace_id", required=True, help="Workspace id.")
@click.option("--title", required=True)
@click.option("--goal", required=True, help="What outcome is wanted, and why.")
@click.option("--done-when", required=True, help="Concrete, checkable finish line.")
@click.option("--context", default="", help="Facts, paths and links the worker needs.")
@click.option("--constraints", default="", help="Limits: scope, style, things not to touch.")
@click.option("--agent", "agent_type", type=click.Choice(AGENT_TYPES), default="codex")
@click.option("--mode", "task_mode", type=click.Choice(TASK_MODES), default=None)
@click.option("--no-start", is_flag=True, help="Register only; do not dispatch.")
@click.option("--request-key", default=None, help="Idempotency key for safe retries.")
@click.pass_context
def dot_delegate(
    ctx: click.Context,
    workspace_id: str,
    title: str,
    goal: str,
    done_when: str,
    context: str,
    constraints: str,
    agent_type: str,
    task_mode: Optional[str],
    no_start: bool,
    request_key: Optional[str],
) -> None:
    """Hand a job to a Hub Task with a full brief. Dot is woken when it ends."""
    body: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "title": title,
        "goal": goal,
        "done_when": done_when,
        "context": context,
        "constraints": constraints,
        "agent_type": agent_type,
        "start": not no_start,
    }
    if task_mode:
        body["task_mode"] = task_mode
    if request_key:
        body["request_key"] = request_key
    item = _call(ctx, "POST", "/delegate", json=body)
    if cli_main.as_json(ctx):
        emit(item, True)
    else:
        click.echo(f"delegated {item['task_id']} ({item.get('status')}): {item.get('title')}")


# ── inbox ───────────────────────────────────────────────────────────────


@dot.group("inbox", invoke_without_command=True)
@click.option("--all", "include_all", is_flag=True, help="Include read events.")
@click.pass_context
def dot_inbox(ctx: click.Context, include_all: bool) -> None:
    """Task events for Dot (shown once in its next turn)."""
    if ctx.invoked_subcommand is None:
        _rows(
            ctx,
            _call(ctx, "GET", "/inbox", params={"all": include_all}),
            ["id", "at", "kind", "read", "text"],
        )


@dot_inbox.command("ack")
@click.argument("event_ids", nargs=-1)
@click.pass_context
def dot_inbox_ack(ctx: click.Context, event_ids: Tuple[str, ...]) -> None:
    """Mark events read (all unread when no ids are given)."""
    body = {"ids": list(event_ids)} if event_ids else {}
    emit(_call(ctx, "POST", "/inbox/ack", json=body), cli_main.as_json(ctx))
