"""Root Click group and shared CLI helpers for ``claude-hub``."""

from __future__ import annotations

import json
import sys
from contextvars import ContextVar
from typing import Any, Optional, Sequence

import click

from claude_hub.cli.client import HubClient
from claude_hub.cli.commands.common import lifecycle_group_help
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


_JSON_OUTPUT: ContextVar[bool] = ContextVar("claude_hub_cli_json_output", default=False)
_CAPTURE_EXPLICIT_EXIT: ContextVar[bool] = ContextVar(
    "claude_hub_cli_capture_explicit_exit", default=False
)


class _ExplicitClickExit(Exception):
    """Carry an explicit ``ctx.exit`` through Click's non-standalone main."""

    def __init__(self, exit_code: int) -> None:
        super().__init__(exit_code)
        self.exit_code = exit_code


class _MachineReadableGroup(click.Group):
    """Keep JSON-mode failures parseable without changing Click exit semantics."""

    def _parse_root_json_output(
        self,
        args: Sequence[str],
        prog_name: Optional[str],
        extra: dict[str, Any],
    ) -> bool:
        """Resolve the root JSON option with Click's parser in resilient mode."""
        parse_extra = dict(extra)
        parse_extra["resilient_parsing"] = True
        try:
            with self.make_context(prog_name or "claude-hub", list(args), **parse_extra) as ctx:
                return bool(ctx.params.get("json_output", False))
        except click.ClickException:
            return False

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.exceptions.Exit as exc:
            if _CAPTURE_EXPLICIT_EXIT.get():
                raise _ExplicitClickExit(exc.exit_code) from exc
            raise

    def main(
        self,
        args: Optional[Sequence[str]] = None,
        prog_name: Optional[str] = None,
        complete_var: Optional[str] = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        if not standalone_mode:
            return super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )

        raw_args = list(args) if args is not None else sys.argv[1:]
        json_token = _JSON_OUTPUT.set(self._parse_root_json_output(raw_args, prog_name, extra))
        exit_token = _CAPTURE_EXPLICIT_EXIT.set(True)
        try:
            try:
                super().main(
                    args=raw_args,
                    prog_name=prog_name,
                    complete_var=complete_var,
                    standalone_mode=False,
                    windows_expand_args=windows_expand_args,
                    **extra,
                )
            except _ExplicitClickExit as exc:
                raise SystemExit(exc.exit_code) from exc
            except click.ClickException as exc:
                if _JSON_OUTPUT.get():
                    click.echo(
                        json.dumps(
                            {
                                "ok": False,
                                "error": exc.format_message(),
                                "exit_code": exc.exit_code,
                            },
                            ensure_ascii=False,
                        ),
                        err=True,
                    )
                else:
                    exc.show()
                raise SystemExit(exc.exit_code) from exc
            except click.Abort as exc:
                if _JSON_OUTPUT.get():
                    click.echo(
                        json.dumps({"ok": False, "error": "Aborted.", "exit_code": 1}),
                        err=True,
                    )
                else:
                    click.echo("Aborted!", err=True)
                raise SystemExit(1) from exc
            raise SystemExit(0)
        finally:
            _CAPTURE_EXPLICIT_EXIT.reset(exit_token)
            _JSON_OUTPUT.reset(json_token)


@click.group(
    cls=_MachineReadableGroup, help=lifecycle_group_help("Claude Hub command-line interface.")
)
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
    """Root CLI entry (help text lives on the Click group decorator)."""
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
    from claude_hub.cli.commands.schedule import schedule
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
    cli.add_command(schedule)
    cli.add_command(feishu)


_register()
