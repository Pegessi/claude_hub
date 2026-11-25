"""Demand radar controls; dry runs preview without model or notification writes."""

from typing import Any

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.output import emit


def _call(ctx: click.Context, method: str, path: str, **kwargs: Any) -> Any:
    try:
        with cli_main.get_client(ctx) as client:
            # Daily digest collection and model analysis can take several minutes.
            if path.endswith("/run"):
                kwargs["timeout"] = 12500.0
            return client.request(method, path, **kwargs)
    except HubError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def patrol() -> None:
    """Inspect or configure the daily demand digest."""


@patrol.command("status")
@click.pass_context
def patrol_status(ctx: click.Context) -> None:
    emit(_call(ctx, "GET", "/api/patrol/status"), cli_main.as_json(ctx))


@patrol.command("run")
@click.option("--channel", type=click.Choice(["all", "daily"]), default="all")
@click.option("--dry-run", is_flag=True)
@click.option("--notify/--no-notify", default=True)
@click.pass_context
def patrol_run(ctx: click.Context, channel: str, dry_run: bool, notify: bool) -> None:
    """Run now; --dry-run only reads sources and previews deterministic findings."""
    emit(
        _call(
            ctx,
            "POST",
            "/api/patrol/run",
            json={
                "channel": channel,
                "dry_run": dry_run,
                "notify": notify,
            },
        ),
        cli_main.as_json(ctx),
    )


@patrol.command("config")
@click.option("--enable/--disable", "enabled", default=None)
@click.option("--daily/--no-daily", "daily_enabled", default=None)
@click.option("--daily-slot", multiple=True, help="Replace daily slots (HH:MM); repeat for each.")
@click.option("--workspace-id", default=None)
@click.pass_context
def patrol_config(
    ctx: click.Context,
    enabled: bool | None,
    daily_enabled: bool | None,
    daily_slot: tuple[str, ...],
    workspace_id: str | None,
) -> None:
    changes: dict[str, Any] = {}
    for name, value in (
        ("enabled", enabled),
        ("daily_enabled", daily_enabled),
        ("workspace_id", workspace_id),
    ):
        if value is not None:
            changes[name] = value
    if daily_slot:
        changes["daily_slots"] = list(daily_slot)
    if changes:
        result = _call(ctx, "PUT", "/api/patrol/config", json=changes)
    else:
        result = _call(ctx, "GET", "/api/patrol/status")["config"]
    emit(result, cli_main.as_json(ctx))
