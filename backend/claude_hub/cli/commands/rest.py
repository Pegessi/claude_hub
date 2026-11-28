"""CLI groups for standalone tabs, quota, remote profiles, auth, and raw API."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional, TextIO

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import (
    fetch_chat_stream_tail_lines,
    merge_payload,
    parse_json_object,
    parse_kv_pairs,
    parse_query_pairs,
)
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
    emit(
        {
            "status_code": response.status_code,
            "location": response.headers.get("location"),
        },
        True,
    )


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
    emit(
        {
            "status_code": response.status_code,
            "location": response.headers.get("location"),
        },
        True,
    )


QUOTA_LIMIT_COLUMNS = ["scope", "label", "state", "used", "remaining", "resets_at"]


def _quota_limit_rows(provider: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Flatten one provider's limits into table rows."""
    rows: list[Dict[str, Any]] = []
    limits = provider.get("limits")
    for limit in limits if isinstance(limits, list) else []:
        if not isinstance(limit, dict):
            continue
        rows.append(
            {
                "scope": limit.get("scope", ""),
                "label": limit.get("label", ""),
                "state": limit.get("state", ""),
                "used": "-" if limit.get("used_percent") is None else limit["used_percent"],
                "remaining": (
                    "-" if limit.get("remaining_percent") is None else limit["remaining_percent"]
                ),
                "resets_at": limit.get("resets_at") or "-",
            }
        )
    return rows


def _echo_provider_quota(provider: Dict[str, Any]) -> None:
    """Print one provider's heading, notes and limit table."""
    details = [
        f"{key}={str(provider[key]).lower() if isinstance(provider[key], bool) else provider[key]}"
        for key in ("plan", "credit_balance", "enforcing", "as_of")
        if provider.get(key) is not None
    ]
    if provider.get("stale"):
        details.append("stale=true")
    suffix = f" ({', '.join(details)})" if details else ""
    click.echo(f"{provider.get('provider', '?')}: {provider.get('state', 'unknown')}{suffix}")
    if provider.get("message"):
        click.echo(f"  {provider['message']}")
    rows = _quota_limit_rows(provider)
    if rows:
        print_rows(rows, QUOTA_LIMIT_COLUMNS)


@click.command("quota")
@click.option("--refresh", is_flag=True, default=False, help="Bypass the cached reading.")
@click.pass_context
def quota(ctx: click.Context, refresh: bool) -> None:
    """Show remaining provider quota (Codex, MASO, and Antigravity)."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_quota(refresh=refresh)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit(data, True)
        return
    providers = data.get("providers") if isinstance(data, dict) else None
    if not providers:
        click.echo("(none)")
        return
    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            continue
        if index:
            click.echo("")
        _echo_provider_quota(provider)


@click.group()
def tab() -> None:
    """Manage standalone interactive Chat and Terminal tabs."""


TAB_COLUMNS = ["id", "name", "session_kind", "agent_type", "target", "cwd", "is_active"]
TAB_AGENT_TYPES = [
    "claude",
    "codex",
    "traex",
    "maso",
    "cursor",
    "antigravity",
    "terminal",
]


@tab.command("list")
@click.option("--archived", is_flag=True, default=False, help="List archived tabs instead.")
@click.pass_context
def tab_list(ctx: click.Context, archived: bool) -> None:
    """List standalone Chat and Terminal tabs."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_archived_tabs() if archived else client.list_tabs()
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit(data, True)
    else:
        print_rows(data or [], TAB_COLUMNS)


@tab.command("status")
@click.pass_context
def tab_status(ctx: click.Context) -> None:
    """List best-effort tab runtime statuses."""
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
    raw_payload = parse_json_object(payload_json)
    if raw_payload.get("session_kind") == "agent":
        raise click.ClickException("session_kind='agent' is retired; use 'chat' or 'terminal'.")
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
    type=click.Choice(TAB_AGENT_TYPES),
    default="claude",
    help="Agent type.",
)
@click.option(
    "--session-kind",
    type=click.Choice(["chat", "terminal"]),
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
    shell: Optional[str],
    cwd: Optional[str],
    solo_mode: bool,
    agent_type: str,
    session_kind: str,
    target: str,
    remote_profile_id: Optional[str],
    remote_cwd: Optional[str],
    remote_reconnect: Optional[bool],
    env_values: tuple,
    payload_json: Optional[str],
) -> None:
    """Create a standalone Chat or Terminal tab."""
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


@tab.command("logs")
@click.argument("tab_id")
@click.option(
    "-n",
    "--lines",
    type=int,
    default=100,
    show_default=True,
    help="Trailing lines to show.",
)
@click.pass_context
def tab_logs(ctx: click.Context, tab_id: str, lines: int) -> None:
    """Show captured terminal output or Chat conversation history for a tab."""
    if lines < 1:
        raise click.ClickException("--lines must be >= 1.")
    try:
        with cli_main.get_client(ctx) as client:
            tab_info: Optional[Dict[str, Any]] = None
            try:
                fetched = client.get_tab(tab_id)
                if isinstance(fetched, dict):
                    tab_info = fetched
            except HubError as e:
                if e.status != 404:
                    raise
                archived = client.list_archived_tabs()
                if isinstance(archived, list):
                    tab_info = next(
                        (
                            item
                            for item in archived
                            if isinstance(item, dict) and item.get("id") == tab_id
                        ),
                        None,
                    )
            kind = str((tab_info or {}).get("session_kind") or "terminal")
            if kind == "chat":
                managed_sid = (tab_info or {}).get("managed_session_id")

                def _fetch_stream(params: Dict[str, Any]) -> Any:
                    if isinstance(managed_sid, str) and managed_sid:
                        return client.get_session_stream_events(managed_sid, params=params)
                    return client.get_tab_stream_events(tab_id, params=params)

                tail = "\n".join(
                    fetch_chat_stream_tail_lines(
                        _fetch_stream,
                        lines=lines,
                        persisted_only=bool((tab_info or {}).get("archived")),
                    )
                )
            else:
                data = client.get_terminal_history(tab_id, lines=max(lines, 100))
                history = data.get("history", "") if isinstance(data, dict) else ""
                tail = "\n".join(history.splitlines()[-lines:])
    except HubError as e:
        raise click.ClickException(str(e)) from e

    if cli_main.as_json(ctx):
        emit(
            {"tab_id": tab_id, "session_kind": kind, "lines": lines, "history": tail},
            True,
        )
    else:
        click.echo(tail)


@tab.command("message")
@click.argument("tab_id")
@click.option("--message", default=None, help="Text to enqueue as an ordinary user turn.")
@click.option(
    "--message-file",
    type=click.File("r", encoding="utf-8"),
    default=None,
    help="Read UTF-8 text from FILE (use - for stdin); alternative to --message.",
)
@click.option("--call-id", default=None, help="Stable retry id; generated when omitted.")
@click.option("--source-tab-id", default=None, help="Unverified caller hint; grants no authority.")
@click.pass_context
def tab_message(
    ctx: click.Context,
    tab_id: str,
    message: Optional[str],
    message_file: Optional[TextIO],
    call_id: Optional[str],
    source_tab_id: Optional[str],
) -> None:
    """Durably enqueue text. A receipt is not model completion."""
    if (message is None) == (message_file is None):
        raise click.UsageError("Provide exactly one of --message or --message-file.")
    if message_file is not None:
        try:
            message = message_file.read()
        except (OSError, UnicodeError) as e:
            raise click.ClickException(f"Cannot read message file: {e}") from e
    if message is None or not message.strip():
        raise click.UsageError("Message must not be blank.")
    if len(message.encode("utf-8")) > 65536:
        raise click.UsageError("Message must not exceed 65536 UTF-8 bytes.")
    if call_id is not None and (not call_id.strip() or len(call_id) > 256):
        raise click.UsageError("--call-id must be nonblank and at most 256 characters.")
    if source_tab_id is not None and not source_tab_id.strip():
        raise click.UsageError("--source-tab-id must not be blank.")
    effective_call_id = call_id if call_id is not None else f"tab-message:{uuid.uuid4()}"
    body: Dict[str, Any] = {"message": message, "call_id": effective_call_id}
    if source_tab_id is not None:
        body["source_tab_id"] = source_tab_id
    click.echo(f"call_id={effective_call_id}", err=True)
    try:
        with cli_main.get_client(ctx) as client:
            data = client.send_tab_message(tab_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@tab.command("message-status")
@click.argument("tab_id")
@click.argument("message_id")
@click.pass_context
def tab_message_status(ctx: click.Context, tab_id: str, message_id: str) -> None:
    """Read an inbox receipt; queued/accepted/completed are distinct states."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_tab_message(tab_id, message_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@tab.command("message-cancel")
@click.argument("tab_id")
@click.argument("message_id")
@click.pass_context
def tab_message_cancel(ctx: click.Context, tab_id: str, message_id: str) -> None:
    """Cancel a queued/waiting message; submitted messages cannot be cancelled here."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.cancel_tab_message(tab_id, message_id)
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
    type=click.Choice(TAB_AGENT_TYPES),
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
    name: Optional[str],
    shell: Optional[str],
    cwd: Optional[str],
    solo_mode: Optional[bool],
    agent_type: Optional[str],
    target: Optional[str],
    remote_profile_id: Optional[str],
    remote_cwd: Optional[str],
    remote_reconnect: Optional[bool],
    env_values: tuple,
    payload_json: Optional[str],
) -> None:
    """Update a standalone tab."""
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


@tab.command("archive")
@click.argument("tab_id")
@click.pass_context
def tab_archive(ctx: click.Context, tab_id: str) -> None:
    """Archive a standalone tab: stop its runtime while preserving history."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.archive_tab(tab_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit(data, True)
    else:
        click.echo(f"archived {tab_id}")


@tab.command("unarchive")
@click.argument("tab_id")
@click.pass_context
def tab_unarchive(ctx: click.Context, tab_id: str) -> None:
    """Restore an archived standalone tab and best-effort restart its runtime."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.unarchive_tab(tab_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit(data, True)
    else:
        click.echo(f"unarchived {tab_id}")


@tab.command("delete")
@click.argument("tab_id")
@click.pass_context
def tab_delete(ctx: click.Context, tab_id: str) -> None:
    """Permanently delete a standalone tab."""
    try:
        with cli_main.get_client(ctx) as client:
            client.delete_tab(tab_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit({"ok": True, "tab_id": tab_id}, True)
    else:
        click.echo(f"deleted {tab_id}")


@tab.group("question")
def tab_question() -> None:
    """Inspect and answer exact pending Chat questions."""


@tab_question.command("list")
@click.argument("tab_id")
@click.pass_context
def tab_question_list(ctx: click.Context, tab_id: str) -> None:
    with cli_main.get_client(ctx) as client:
        emit(client.request("GET", f"/api/workspaces/tabs/{tab_id}/questions"), True)


@tab_question.command("answer")
@click.argument("tab_id")
@click.option("--question-ref", required=True)
@click.option("--answers-file", required=True, type=click.File("r"))
@click.option("--call-id", required=True)
@click.pass_context
def tab_question_answer(
    ctx: click.Context,
    tab_id: str,
    question_ref: str,
    answers_file: TextIO,
    call_id: str,
) -> None:
    selected = json.load(answers_file)
    with cli_main.get_client(ctx) as client:
        emit(
            client.request(
                "POST",
                f"/api/workspaces/tabs/{tab_id}/questions/{question_ref}/answers",
                json={"call_id": call_id, "selected": selected},
            ),
            True,
        )


@click.group()
def remote() -> None:
    """Inspect remote SSH profiles and remote directories."""


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
def remote_list(ctx: click.Context, profile_id: str, path: Optional[str]) -> None:
    """List a remote directory."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_remote_directory(profile_id, path)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@click.group()
def api() -> None:
    """Generic REST API escape hatch."""


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
    payload_json: Optional[str],
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
