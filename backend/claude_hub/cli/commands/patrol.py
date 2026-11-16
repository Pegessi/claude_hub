"""Demand radar controls; dry runs preview without model or notification writes."""

from typing import Any

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.output import emit


def _call(ctx: click.Context, method: str, path: str, **kwargs: Any) -> Any:
    try:
        with cli_main.get_client(ctx) as client:
            # Three diagnoses may each use the bounded 600-second provider turn.
            if path.endswith("/run"):
                kwargs["timeout"] = 12500.0
            return client.request(method, path, **kwargs)
    except HubError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def patrol() -> None:
    """Inspect or configure the daily demand and work-hours Feishu radar."""


@patrol.command("status")
@click.pass_context
def patrol_status(ctx: click.Context) -> None:
    emit(_call(ctx, "GET", "/api/patrol/status"), cli_main.as_json(ctx))


@patrol.command("run")
@click.option("--channel", type=click.Choice(["all", "feishu", "daily"]), default="all")
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
@click.option("--feishu/--no-feishu", "feishu_enabled", default=None)
@click.option("--daily/--no-daily", "daily_enabled", default=None)
@click.option(
    "--daily-slot", multiple=True, help="Replace daily slots (HH:MM); repeat for each."
)
@click.option("--feishu-interval", type=click.IntRange(1, 1440), default=None)
@click.option("--window-start", default=None)
@click.option("--window-end", default=None)
@click.pass_context
def patrol_config(
    ctx: click.Context,
    enabled: bool | None,
    feishu_enabled: bool | None,
    daily_enabled: bool | None,
    daily_slot: tuple[str, ...],
    feishu_interval: int | None,
    window_start: str | None,
    window_end: str | None,
) -> None:
    changes: dict[str, Any] = {}
    for name, value in (
        ("enabled", enabled),
        ("feishu_enabled", feishu_enabled),
        ("daily_enabled", daily_enabled),
        ("feishu_interval_minutes", feishu_interval),
        ("feishu_window_start", window_start),
        ("feishu_window_end", window_end),
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
