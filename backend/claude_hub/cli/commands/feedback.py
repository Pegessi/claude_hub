"""Bounded feedback automation and explicit Chat corrections."""

from typing import Any

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.output import emit


def _call(ctx: click.Context, method: str, path: str, **kwargs: Any) -> Any:
    try:
        with cli_main.get_client(ctx) as client:
            return client.request(method, path, **kwargs)
    except HubError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def feedback() -> None:
    """Control automatic feedback; capture only explicit reusable corrections."""


@feedback.command("status")
@click.argument("workspace_id")
@click.pass_context
def feedback_status(ctx: click.Context, workspace_id: str) -> None:
    """Show enabled state, fresh-only watermark, cooldown and last outcome."""
    emit(
        _call(ctx, "GET", f"/api/workspaces/{workspace_id}/feedback/automation"),
        cli_main.as_json(ctx),
    )


@feedback.command("configure")
@click.argument("workspace_id")
@click.option("--enabled/--disabled", default=None)
@click.option(
    "--cooldown",
    type=click.IntRange(3600, 604800),
    default=None,
    help="Minimum seconds between model launches.",
)
@click.option("--max-records", type=click.IntRange(1, 10), default=None)
@click.pass_context
def feedback_configure(
    ctx: click.Context,
    workspace_id: str,
    enabled: bool | None,
    cooldown: int | None,
    max_records: int | None,
) -> None:
    """Set input/frequency limits (these are not hard token or billing caps)."""
    path = f"/api/workspaces/{workspace_id}/feedback/automation"
    body = dict(_call(ctx, "GET", path)["settings"])
    for key, value in (
        ("enabled", enabled),
        ("cooldown_seconds", cooldown),
        ("max_records", max_records),
    ):
        if value is not None:
            body[key] = value
    emit(_call(ctx, "PUT", path, json=body), cli_main.as_json(ctx))


@feedback.command("context")
@click.argument("workspace_id")
@click.option("--query", required=True)
@click.option("--limit", type=click.IntRange(1, 10), default=5)
@click.pass_context
def feedback_context(ctx: click.Context, workspace_id: str, query: str, limit: int) -> None:
    """Get relevant compact lesson indices; fetch a body only if needed."""
    emit(
        _call(
            ctx,
            "GET",
            f"/api/workspaces/{workspace_id}/feedback/context",
            params={"query": query, "limit": limit},
        ),
        cli_main.as_json(ctx),
    )


@feedback.command("sources")
@click.option("--tab-id", envvar="CLAUDE_HUB_TAB_ID", required=True)
@click.option("--limit", type=click.IntRange(1, 20), default=10)
@click.pass_context
def feedback_sources(ctx: click.Context, tab_id: str, limit: int) -> None:
    """Read bounded recent user-message IDs only when capturing a correction."""
    emit(
        _call(
            ctx, "GET", f"/api/workspaces/tabs/{tab_id}/feedback/sources", params={"limit": limit}
        ),
        cli_main.as_json(ctx),
    )


@feedback.command("capture")
@click.argument("workspace_id")
@click.option("--tab-id", envvar="CLAUDE_HUB_TAB_ID", required=True)
@click.option("--turn-id", required=True)
@click.option("--message-id", required=True)
@click.option(
    "--quote",
    required=True,
    help="Exact explicit correction from the persisted user message; max 1024 characters.",
)
@click.pass_context
def feedback_capture(
    ctx: click.Context, workspace_id: str, tab_id: str, turn_id: str, message_id: str, quote: str
) -> None:
    """Record a user correction as evidence, not as an automatically true rule."""
    body = {"tab_id": tab_id, "turn_id": turn_id, "message_id": message_id, "quote": quote}
    emit(
        _call(ctx, "POST", f"/api/workspaces/{workspace_id}/feedback/corrections", json=body),
        cli_main.as_json(ctx),
    )
