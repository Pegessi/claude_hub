"""Root Click group and shared CLI helpers for ``claude-hub``."""

from __future__ import annotations

from typing import Optional

import click

from claude_hub.cli.client import HubClient
from claude_hub.cli.config import DEFAULT_BASE_URL, DEFAULT_CONFIG_PATH, Settings, resolve_settings


def get_client(ctx: click.Context) -> HubClient:
    """Build a configured :class:`HubClient` from resolved CLI settings.

    Tests may monkeypatch this function to inject an ``httpx.MockTransport``.
    """
    settings: Settings = ctx.obj
    return HubClient(
        base_url=settings.base_url,
        token=settings.token,
        cookie=settings.cookie,
        verbose=settings.verbose,
    )


def as_json(ctx: click.Context) -> bool:
    """Return whether JSON output was requested for this invocation."""
    settings: Settings = ctx.obj
    return settings.json_output


@click.group()
@click.option(
    "--base-url",
    envvar="CLAUDE_HUB_URL",
    default=None,
    help=f"Claude Hub base URL (default {DEFAULT_BASE_URL}).",
)
@click.option(
    "--token",
    envvar="CLAUDE_HUB_TOKEN",
    default=None,
    help="Session token sent as the claude_hub_session cookie.",
)
@click.option(
    "--cookie",
    default=None,
    help='Raw cookie header string ("k=v; k2=v2"). Overridden by --token.',
)
@click.option("--json/--no-json", "json_output", default=False, help="Force JSON output.")
@click.option(
    "--config",
    envvar="CLAUDE_HUB_CONFIG",
    default=None,
    help=f"Path to TOML config (default {DEFAULT_CONFIG_PATH}).",
)
@click.option("-v", "--verbose", is_flag=True, default=False, help="Log requests to stderr.")
@click.pass_context
def cli(
    ctx: click.Context,
    base_url: Optional[str],
    token: Optional[str],
    cookie: Optional[str],
    json_output: bool,
    config: Optional[str],
    verbose: bool,
) -> None:
    """Claude Hub command-line interface."""
    ctx.obj = resolve_settings(
        base_url=base_url,
        token=token,
        cookie=cookie,
        json_output=json_output,
        verbose=verbose,
        config_path=config,
    )


def _register() -> None:
    """Attach subcommand groups. Imported here to avoid circular imports."""
    from claude_hub.cli.commands.agent_tree import agent_tree
    from claude_hub.cli.commands.feishu import feishu
    from claude_hub.cli.commands.lessons import lessons
    from claude_hub.cli.commands.rest import (
        api,
        auth,
        clipboard,
        filesystem,
        fs,
        remote,
        system,
        tab,
        terminal,
    )
    from claude_hub.cli.commands.sessions import session
    from claude_hub.cli.commands.tasks import task
    from claude_hub.cli.commands.workspaces import agent, workspace

    cli.add_command(auth)
    cli.add_command(system)
    cli.add_command(tab)
    cli.add_command(terminal)
    cli.add_command(filesystem)
    cli.add_command(fs, name="fs")
    cli.add_command(remote)
    cli.add_command(clipboard)
    cli.add_command(api)
    cli.add_command(workspace)
    cli.add_command(agent)
    cli.add_command(task)
    cli.add_command(session)
    cli.add_command(lessons)
    cli.add_command(feishu)
    cli.add_command(agent_tree)


_register()
