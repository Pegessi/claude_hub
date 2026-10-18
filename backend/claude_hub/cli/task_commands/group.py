"""Root Click group for task commands."""

import click

from claude_hub.cli.commands.common import lifecycle_group_help


@click.group(help=lifecycle_group_help("Manage workspace tasks."))
def task() -> None:
    pass
