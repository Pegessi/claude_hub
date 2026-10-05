"""One command surface for Chat-linked tasks and quiet recurring monitors."""

from __future__ import annotations

import json
from typing import Any, Callable, TypeVar
from urllib.parse import quote

import click

from claude_hub.cli.client import HubError

_F = TypeVar("_F", bound=Callable[..., Any])


def _tab_option(fn: _F) -> _F:
    return click.option(
        "--tab-id",
        envvar="CLAUDE_HUB_TAB_ID",
        required=True,
        help="Source Chat tab; defaults to CLAUDE_HUB_TAB_ID. Workers must pass the assignment's source tab explicitly.",
    )(fn)


def _call(
    ctx: click.Context,
    tab_id: str,
    method: str,
    work_id: str | None = None,
    body: Any = None,
    suffix: str = "",
) -> None:
    from claude_hub.cli.main import get_client

    path = f"/api/tabs/{quote(tab_id, safe='')}/work"
    if work_id:
        path += "/" + quote(work_id, safe="")
    try:
        with get_client(ctx) as client:
            result = client._request(method, path + suffix, json=body, timeout=120)
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except HubError as exc:
        raise click.ClickException(str(exc)) from None


@click.group()
def work() -> None:
    """Create and control work linked to the current Chat, with durable results."""


@work.command("list")
@_tab_option
@click.pass_context
def list_work(ctx: click.Context, tab_id: str) -> None:
    """List one card per linked work item (monitor checks are grouped)."""
    _call(ctx, tab_id, "GET")


@work.command("show")
@click.argument("work_id")
@_tab_option
@click.pass_context
def show_work(ctx: click.Context, work_id: str, tab_id: str) -> None:
    _call(ctx, tab_id, "GET", work_id)


@work.command("create")
@_tab_option
@click.option(
    "--request-key", required=True, help="Stable per-intent key; reuse unchanged on retry."
)
@click.option("--workspace-id", required=True)
@click.option("--title", required=True)
@click.option("--prompt", required=True)
@click.option("--kind", type=click.Choice(["task", "monitor"]), default="task")
@click.option("--interval", "interval_seconds", type=click.IntRange(min=60))
@click.option("--agent-type", type=click.Choice(["claude", "codex", "cursor", "traex"]))
@click.option(
    "--model", help="Explicit model (Claude/Codex); other providers reject this override."
)
@click.option("--cwd")
@click.option("--env-preset")
@click.option("--task-mode", type=click.Choice(["reviewed", "direct", "subagent", "autonomous"]))
@click.pass_context
def create_work(ctx: click.Context, tab_id: str, **kwargs: Any) -> None:
    """Start persistent work. Only create a monitor when recurring work was requested."""
    _call(ctx, tab_id, "POST", body={k: v for k, v in kwargs.items() if v is not None})


@work.command("update")
@click.argument("work_id")
@_tab_option
@click.option("--action", type=click.Choice(["pause", "resume", "stop"]))
@click.option("--interval", "interval_seconds", type=click.IntRange(min=60))
@click.option(
    "--prompt", help="Instructions for future checks; does not rewrite an active execution."
)
@click.pass_context
def update_work(ctx: click.Context, work_id: str, tab_id: str, **kwargs: Any) -> None:
    """Pause future checks, resume, or stop and request active execution interruption."""
    _call(ctx, tab_id, "PATCH", work_id, {k: v for k, v in kwargs.items() if v is not None})


@work.command("report")
@click.argument("work_id")
@_tab_option
@click.option("--task-id", required=True)
@click.option(
    "--session-id", help="Assigned worker session; writes the canonical task report before cleanup."
)
@click.option("--report-id", help="Alternatively classify an existing report from this execution.")
@click.option(
    "--call-id", help="Stable report idempotency key (default derived from this exact payload)."
)
@click.option(
    "--kind",
    required=True,
    type=click.Choice(["no_change", "progress", "anomaly", "completed", "decision"]),
)
@click.option("--summary", required=True)
@click.option("--validation")
@click.pass_context
def report_work(ctx: click.Context, work_id: str, tab_id: str, **kwargs: Any) -> None:
    """Report an execution. completed ends the whole monitor; no_change completes one unchanged check."""
    if not kwargs.get("session_id") and not kwargs.get("report_id"):
        raise click.UsageError("Provide --session-id or --report-id")
    _call(
        ctx,
        tab_id,
        "POST",
        work_id,
        {k: v for k, v in kwargs.items() if v is not None},
        suffix="/report",
    )
