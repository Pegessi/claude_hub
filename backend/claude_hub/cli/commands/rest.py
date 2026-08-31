"""CLI groups for non-workspace REST APIs and the generic API escape hatch."""

from __future__ import annotations

from typing import Any, Dict, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import merge_payload, parse_kv_pairs, parse_query_pairs
from claude_hub.cli.output import emit, print_rows


@click.group()
def auth() -> None:
    """Use authentication endpoints."""


@auth.command("check")
@click.pass_context
def auth_check(ctx: click.Context) -> None:
    """Check authentication state."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.check_auth()
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@auth.command("me")
@click.pass_context
def auth_me(ctx: click.Context) -> None:
    """Show the current authenticated user."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_current_user()
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@auth.command("logout")
@click.pass_context
def auth_logout(ctx: click.Context) -> None:
    """Logout the current user."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.logout()
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@auth.command("login-url")
@click.pass_context
def auth_login_url(ctx: click.Context) -> None:
    """Print the OAuth redirect URL returned by the server."""
    try:
        with cli_main.get_client(ctx) as client:
            response = client.auth_login_response()
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit({"status_code": response.status_code, "location": response.headers.get("location")}, True)


@auth.command("callback")
@click.option("--code", required=True, help="OAuth callback code.")
@click.option("--state", default="", help="OAuth callback state.")
@click.pass_context
def auth_callback(ctx: click.Context, code: str, state: str) -> None:
    """Call the OAuth callback endpoint."""
    try:
        with cli_main.get_client(ctx) as client:
            response = client.auth_callback_response(code, state)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit({"status_code": response.status_code, "location": response.headers.get("location")}, True)


@click.group()
def system() -> None:
    """Use system endpoints."""


@system.command("network-access")
@click.pass_context
def system_network_access(ctx: click.Context) -> None:
    """Show local network addresses suitable for browser access."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_network_access()
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@click.group()
def tab() -> None:
    """Manage terminal tabs."""


TAB_COLUMNS = ["id", "name", "session_kind", "agent_type", "target", "cwd", "is_active"]


@tab.command("list")
@click.pass_context
def tab_list(ctx: click.Context) -> None:
    """List terminal tabs."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_tabs()
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit(data, True)
    else:
        print_rows(data or [], TAB_COLUMNS)


@tab.command("status")
@click.pass_context
def tab_status(ctx: click.Context) -> None:
    """List best-effort terminal agent statuses."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_tab_statuses()
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


def _tab_body(
    payload_json: Optional[str],
    *,
    name: Optional[str] = None,
    shell: Optional[str] = None,
    cwd: Optional[str] = None,
    solo_mode: Optional[bool] = None,
    agent_type: Optional[str] = None,
    session_kind: Optional[str] = None,
    target: Optional[str] = None,
    remote_profile_id: Optional[str] = None,
    remote_cwd: Optional[str] = None,
    remote_reconnect: Optional[bool] = None,
    env_values: tuple = (),
) -> Dict[str, Any]:
    body = merge_payload(
        payload_json,
        name=name,
        shell=shell,
        cwd=cwd,
        solo_mode=solo_mode,
        agent_type=agent_type,
        session_kind=session_kind,
        target=target,
        remote_profile_id=remote_profile_id,
        remote_cwd=remote_cwd,
        remote_reconnect=remote_reconnect,
    )
    if env_values:
        body["env"] = parse_kv_pairs(env_values, "--env")
    return body


@tab.command("create")
@click.option("--name", required=True, help="Tab name.")
@click.option("--shell", default=None, help="Shell command.")
@click.option("--cwd", default=None, help="Working directory.")
@click.option("--solo-mode/--no-solo-mode", default=False, help="Run in agent solo mode.")
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "cursor", "terminal"]),
    default="claude",
    help="Agent type.",
)
@click.option(
    "--session-kind",
    type=click.Choice(["agent", "terminal"]),
    default="terminal",
    help="Fixed UI surface for the session.",
)
@click.option(
    "--target",
    type=click.Choice(["local", "remote"]),
    default="local",
    help="Execution target.",
)
@click.option("--remote-profile-id", default=None, help="Remote profile id.")
@click.option("--remote-cwd", default=None, help="Remote working directory.")
@click.option("--remote-reconnect/--no-remote-reconnect", default=None, help="Reconnect SSH.")
@click.option("--env", "env_values", multiple=True, help="Environment variable KEY=VALUE.")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def tab_create(
    ctx: click.Context,
    name: str,
    shell: str,
    cwd: str,
    solo_mode: bool,
    agent_type: str,
    session_kind: str,
    target: str,
    remote_profile_id: str,
    remote_cwd: str,
    remote_reconnect: bool,
    env_values: tuple,
    payload_json: str,
) -> None:
    """Create a terminal tab."""
    body = _tab_body(
        payload_json,
        name=name,
        shell=shell,
        cwd=cwd,
        solo_mode=solo_mode,
        agent_type=agent_type,
        session_kind=session_kind,
        target=target,
        remote_profile_id=remote_profile_id,
        remote_cwd=remote_cwd,
        remote_reconnect=remote_reconnect,
        env_values=env_values,
    )
    try:
        with cli_main.get_client(ctx) as client:
            data = client.create_tab(body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@tab.command("get")
@click.argument("tab_id")
@click.pass_context
def tab_get(ctx: click.Context, tab_id: str) -> None:
    """Fetch a tab."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_tab(tab_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@tab.command("update")
@click.argument("tab_id")
@click.option("--name", default=None, help="Tab name.")
@click.option("--shell", default=None, help="Shell command.")
@click.option("--cwd", default=None, help="Working directory.")
@click.option("--solo-mode/--no-solo-mode", default=None, help="Run in agent solo mode.")
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "cursor", "terminal"]),
    default=None,
    help="Agent type.",
)
@click.option(
    "--target",
    type=click.Choice(["local", "remote"]),
    default=None,
    help="Execution target.",
)
@click.option("--remote-profile-id", default=None, help="Remote profile id.")
@click.option("--remote-cwd", default=None, help="Remote working directory.")
@click.option("--remote-reconnect/--no-remote-reconnect", default=None, help="Reconnect SSH.")
@click.option("--env", "env_values", multiple=True, help="Environment variable KEY=VALUE.")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def tab_update(
    ctx: click.Context,
    tab_id: str,
    name: str,
    shell: str,
    cwd: str,
    solo_mode: Optional[bool],
    agent_type: str,
    target: str,
    remote_profile_id: str,
    remote_cwd: str,
    remote_reconnect: Optional[bool],
    env_values: tuple,
    payload_json: str,
) -> None:
    """Update a terminal tab."""
    body = _tab_body(
        payload_json,
        name=name,
        shell=shell,
        cwd=cwd,
        solo_mode=solo_mode,
        agent_type=agent_type,
        target=target,
        remote_profile_id=remote_profile_id,
        remote_cwd=remote_cwd,
        remote_reconnect=remote_reconnect,
        env_values=env_values,
    )
    try:
        with cli_main.get_client(ctx) as client:
            data = client.update_tab(tab_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@tab.command("delete")
@click.argument("tab_id")
@click.pass_context
def tab_delete(ctx: click.Context, tab_id: str) -> None:
    """Delete a terminal tab."""
    try:
        with cli_main.get_client(ctx) as client:
            client.delete_tab(tab_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"deleted {tab_id}")


@tab.command("duplicate")
@click.argument("tab_id")
@click.pass_context
def tab_duplicate(ctx: click.Context, tab_id: str) -> None:
    """Duplicate a terminal tab."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.duplicate_tab(tab_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@tab.command("order")
@click.argument("tab_ids", nargs=-1, required=True)
@click.pass_context
def tab_order(ctx: click.Context, tab_ids: tuple) -> None:
    """Set terminal tab order."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.update_tab_order(list(tab_ids))
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@click.group(name="filesystem")
def filesystem() -> None:
    """Browse local filesystem endpoints."""


@filesystem.command("list")
@click.option("--path", default=None, help="Directory path.")
@click.pass_context
def filesystem_list(ctx: click.Context, path: str) -> None:
    """List a local directory."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_directory(path)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@filesystem.command("home")
@click.pass_context
def filesystem_home(ctx: click.Context) -> None:
    """Print the server user's home directory."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_home_directory()
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@click.group()
def remote() -> None:
    """Use remote profile and remote filesystem endpoints."""


@remote.command("profiles")
@click.pass_context
def remote_profiles(ctx: click.Context) -> None:
    """List configured/discovered remote profiles."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_remote_profiles()
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@remote.command("list")
@click.option("--profile-id", required=True, help="Remote profile id.")
@click.option("--path", default=None, help="Remote directory path.")
@click.pass_context
def remote_list(ctx: click.Context, profile_id: str, path: str) -> None:
    """List a remote directory."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_remote_directory(profile_id, path)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@click.group()
def clipboard() -> None:
    """Use clipboard endpoints."""


@clipboard.command("image")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--content-type", default=None, help="Image content type override.")
@click.pass_context
def clipboard_image(ctx: click.Context, path: str, content_type: str) -> None:
    """Upload an image and set the macOS clipboard."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.upload_clipboard_image(path, content_type=content_type)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@click.group()
def terminal() -> None:
    """Use terminal endpoints."""


@terminal.command("history")
@click.argument("tab_id")
@click.option("--lines", type=int, default=100, help="History lines.")
@click.pass_context
def terminal_history(ctx: click.Context, tab_id: str, lines: int) -> None:
    """Fetch captured terminal history."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_terminal_history(tab_id, lines=lines)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@terminal.command("proxy-url")
@click.argument("tab_id")
@click.option("--iframe", is_flag=True, default=False, help="Print the iframe redirect URL.")
def terminal_proxy_url(tab_id: str, iframe: bool) -> None:
    """Print the terminal proxy URL path."""
    if iframe:
        click.echo(f"/api/terminal/proxy-iframe/{tab_id}")
    else:
        click.echo(f"/api/terminal/proxy/{tab_id}/")


@click.group()
def api() -> None:
    """Use generic API escape hatch commands."""


@api.command("raw")
@click.argument(
    "method",
    type=click.Choice(["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], case_sensitive=False),
)
@click.argument("path")
@click.option("--query", "query_values", multiple=True, help="Query parameter KEY=VALUE.")
@click.option("--payload-json", default=None, help="JSON request body.")
@click.pass_context
def api_raw(
    ctx: click.Context,
    method: str,
    path: str,
    query_values: tuple,
    payload_json: str,
) -> None:
    """Call an arbitrary REST endpoint and print the decoded response."""
    if not path.startswith("/"):
        path = f"/{path}"
    params = parse_query_pairs(query_values) if query_values else None
    body = merge_payload(payload_json) if payload_json is not None else None
    try:
        with cli_main.get_client(ctx) as client:
            data = client.request(method.upper(), path, json=body, params=params)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, True if cli_main.as_json(ctx) else isinstance(data, (dict, list)))


# Short alias for filesystem.
fs = filesystem
