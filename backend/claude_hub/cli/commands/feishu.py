"""``feishu`` command group — two stateless helpers for an agent that *is* a bot.

The agent driving this CLI is itself the Feishu bot: it sends the card to a human
and receives the ``card.action.trigger`` callback in the same process, so Hub
needs no result store, no token relay, and no outbound sender. It only needs two
pure, IO-free helpers exposed as thin commands:

* ``feishu build-card`` -- build the interactive/display card JSON for a given
  ``--kind`` and print it (the agent then sends it to Feishu itself).
* ``feishu parse-action`` -- parse a raw ``card.action.trigger`` callback into a
  normalized ``{token, action, form, operator_id, chat_id}`` decision.

Both speak JSON on stdout so the agent can ``subprocess`` them and parse the
result without writing any Python.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient, HubError
from claude_hub.cli.commands.tasks import _find_task_board
from claude_hub.cli.feishu_cards import (
    CARD_KINDS,
    INTERACTIVE_KINDS,
    build_action_catalog_card,
    build_agents_card,
    build_approval_card,
    build_filesystem_card,
    build_lessons_card,
    build_needs_input_card,
    build_network_card,
    build_overview_card,
    build_plan_confirm_card,
    build_remote_profiles_card,
    build_reports_card,
    build_result_card,
    build_status_card,
    build_tab_status_card,
    build_tabs_card,
    build_task_card,
    build_task_detail_card,
    build_terminal_card,
    build_workspaces_card,
    parse_card_action,
)
from claude_hub.cli.output import emit


@click.group()
def feishu() -> None:
    """Build Feishu cards and parse their action callbacks (stateless helpers)."""


# -- Card building ----------------------------------------------------------


def _resolve_task_board(
    client: HubClient, task_id: str, workspace_id: Optional[str], kind: str
) -> tuple[str, dict]:
    """Locate a task across boards, returning ``(workspace_id, task_dict)``."""
    if workspace_id is not None:
        board = client.get_board(workspace_id)
        tasks: List[dict] = board.get("tasks", []) if isinstance(board, dict) else []
        match = next((t for t in tasks if t.get("id") == task_id), None)
        ws_id: Optional[str] = workspace_id
    else:
        ws_id, match = _find_task_board(client, task_id)
    if match is None or ws_id is None:
        where = f" in workspace {workspace_id}" if workspace_id else ""
        raise click.ClickException(f"task {task_id} not found{where} (kind={kind})")
    return ws_id, match


def _compact_params(**params: Optional[str]) -> Dict[str, str]:
    """Drop empty query params so optional API defaults still apply."""
    return {key: value for key, value in params.items() if value}


def _build_card(
    ctx: click.Context,
    kind: str,
    token: str,
    *,
    title: Optional[str],
    body: Optional[str],
    workspace_id: Optional[str],
    task_id: Optional[str],
    field_name: str,
    tab_id: Optional[str],
    path: Optional[str],
    remote_profile_id: Optional[str],
    lines: int,
) -> Dict[str, Any]:
    """Build the card JSON for ``kind``.

    Interactive kinds embed ``token``; display kinds (status/task) fetch live
    data from the backend and carry no token. Raises ``click.ClickException``
    for missing required inputs.
    """
    if kind == "approval":
        return build_approval_card(token, title or "Approval required", body or "")
    if kind == "needs_input":
        return build_needs_input_card(
            token, title or "Input required", body or "", field_name=field_name
        )
    if kind == "plan_confirm":
        return build_plan_confirm_card(token, title or "Confirm plan", body or "")

    # Display kinds need live backend data.
    if kind == "status":
        if not workspace_id:
            raise click.ClickException("--workspace-id is required for kind=status")
        try:
            with cli_main.get_client(ctx) as client:
                board = client.get_board(workspace_id)
        except HubError as e:
            raise click.ClickException(str(e)) from e
        return build_status_card(workspace_id, board)

    if kind == "task":
        if not (workspace_id and task_id):
            raise click.ClickException("--workspace-id and --task-id are required for kind=task")
        try:
            with cli_main.get_client(ctx) as client:
                board = client.get_board(workspace_id)
        except HubError as e:
            raise click.ClickException(str(e)) from e
        tasks: List[dict] = board.get("tasks", []) if isinstance(board, dict) else []
        match = next((t for t in tasks if t.get("id") == task_id), None)
        if match is None:
            raise click.ClickException(f"task {task_id} not found in workspace {workspace_id}")
        return build_task_card(match)

    if kind == "workspaces":
        try:
            with cli_main.get_client(ctx) as client:
                workspaces = client.list_workspaces()
        except HubError as e:
            raise click.ClickException(str(e)) from e
        return build_workspaces_card(workspaces)

    if kind == "overview":
        if not workspace_id:
            raise click.ClickException("--workspace-id is required for kind=overview")
        try:
            with cli_main.get_client(ctx) as client:
                board = client.get_board(workspace_id)
        except HubError as e:
            raise click.ClickException(str(e)) from e
        tasks = board.get("tasks", []) if isinstance(board, dict) else []
        sessions = board.get("sessions", []) if isinstance(board, dict) else []
        markdown_documents = board.get("markdown_documents", []) if isinstance(board, dict) else []
        snapshot_path = board.get("snapshot_path") if isinstance(board, dict) else None
        ws = board.get("workspace") if isinstance(board, dict) else None
        name = ws.get("name") if isinstance(ws, dict) else None
        return build_overview_card(
            workspace_id,
            tasks,
            sessions,
            name=name,
            markdown_documents=markdown_documents,
            snapshot_path=snapshot_path,
        )

    if kind == "agents":
        if not workspace_id:
            raise click.ClickException("--workspace-id is required for kind=agents")
        try:
            with cli_main.get_client(ctx) as client:
                board = client.get_board(workspace_id)
        except HubError as e:
            raise click.ClickException(str(e)) from e
        sessions = board.get("sessions", []) if isinstance(board, dict) else []
        ws = board.get("workspace") if isinstance(board, dict) else None
        name = ws.get("name") if isinstance(ws, dict) else None
        return build_agents_card(workspace_id, sessions, name=name)

    if kind == "task_detail":
        if not task_id:
            raise click.ClickException("--task-id is required for kind=task_detail")
        try:
            with cli_main.get_client(ctx) as client:
                ws_id, match = _resolve_task_board(client, task_id, workspace_id, kind)
                board = client.get_board(ws_id)
                sessions = board.get("sessions", []) if isinstance(board, dict) else []
                session = next(
                    (s for s in sessions if s.get("id") == match.get("session_id")), None
                )
                fetched = client.get_task_reports(ws_id, task_id)
        except HubError as e:
            raise click.ClickException(str(e)) from e
        reports: List[dict] = fetched if isinstance(fetched, list) else []
        latest_report = (
            max(reports, key=lambda r: str(r.get("created_at", ""))) if reports else None
        )
        if not match.get("workspace_id"):
            match["workspace_id"] = ws_id
        return build_task_detail_card(match, session, latest_report, token=token)

    if kind == "reports":
        if not task_id:
            raise click.ClickException("--task-id is required for kind=reports")
        try:
            with cli_main.get_client(ctx) as client:
                ws_id, match = _resolve_task_board(client, task_id, workspace_id, kind)
                fetched = client.get_task_reports(ws_id, task_id)
        except HubError as e:
            raise click.ClickException(str(e)) from e
        report_list: List[dict] = fetched if isinstance(fetched, list) else []
        return build_reports_card(match, report_list)

    if kind == "terminal":
        if not tab_id:
            raise click.ClickException("--tab-id is required for kind=terminal")
        if lines < 100:
            raise click.ClickException("--lines must be >= 100 for kind=terminal")
        try:
            with cli_main.get_client(ctx) as client:
                history = client.get_terminal_history(tab_id, lines)
        except HubError as e:
            raise click.ClickException(str(e)) from e
        text = str(history.get("history", "")) if isinstance(history, dict) else str(history or "")
        return build_terminal_card(tab_id, text)

    if kind == "lessons":
        if not workspace_id:
            raise click.ClickException("--workspace-id is required for kind=lessons")
        try:
            with cli_main.get_client(ctx) as client:
                lessons = client.list_lessons(workspace_id, {})
        except HubError as e:
            raise click.ClickException(str(e)) from e
        return build_lessons_card(workspace_id, lessons)

    if kind == "tabs":
        try:
            with cli_main.get_client(ctx) as client:
                tabs = client._request("GET", "/api/tabs")
                try:
                    statuses = client._request("GET", "/api/tabs/status")
                except HubError:
                    statuses = []
        except HubError as e:
            raise click.ClickException(str(e)) from e
        return build_tabs_card(tabs, statuses)

    if kind == "tab_status":
        try:
            with cli_main.get_client(ctx) as client:
                statuses = client._request("GET", "/api/tabs/status")
        except HubError as e:
            raise click.ClickException(str(e)) from e
        return build_tab_status_card(statuses)

    if kind == "network":
        try:
            with cli_main.get_client(ctx) as client:
                network = client._request("GET", "/api/system/network-access")
        except HubError as e:
            raise click.ClickException(str(e)) from e
        return build_network_card(network)

    if kind == "filesystem":
        try:
            with cli_main.get_client(ctx) as client:
                listing = client._request(
                    "GET", "/api/filesystem/list", params=_compact_params(path=path)
                )
        except HubError as e:
            raise click.ClickException(str(e)) from e
        return build_filesystem_card(listing, title="Filesystem")

    if kind == "remote_profiles":
        try:
            with cli_main.get_client(ctx) as client:
                profiles = client._request("GET", "/api/remote/profiles")
        except HubError as e:
            raise click.ClickException(str(e)) from e
        return build_remote_profiles_card(profiles)

    if kind == "remote_filesystem":
        if not remote_profile_id:
            raise click.ClickException("--remote-profile-id is required for kind=remote_filesystem")
        try:
            with cli_main.get_client(ctx) as client:
                listing = client._request(
                    "GET",
                    "/api/remote/filesystem/list",
                    params=_compact_params(profile_id=remote_profile_id, path=path),
                )
        except HubError as e:
            raise click.ClickException(str(e)) from e
        return build_filesystem_card(
            listing, title=f"Remote FS · {remote_profile_id}", kind="remote_filesystem"
        )

    if kind == "result":
        return build_result_card(
            title or "Command result", body or "_No result body supplied._", kind="result"
        )

    if kind == "action_catalog":
        return build_action_catalog_card()

    raise click.ClickException(f"unknown kind: {kind}")


@feishu.command("build-card")
@click.option(
    "--kind",
    type=click.Choice(list(CARD_KINDS)),
    required=True,
    help="Card kind to build.",
)
@click.option("--title", default=None, help="Card title.")
@click.option("--body", default=None, help="Card body/prompt/plan markdown text.")
@click.option(
    "--workspace-id",
    default=None,
    help="Workspace id (for status/task/overview/agents/task_detail/reports/lessons).",
)
@click.option("--task-id", default=None, help="Task id (for task/task_detail/reports).")
@click.option("--field-name", default="reply", help="Form field name (for kind=needs_input).")
@click.option("--tab-id", default=None, help="Terminal tab id (for kind=terminal).")
@click.option(
    "--path", default=None, help="Directory path (for kind=filesystem/remote_filesystem)."
)
@click.option(
    "--remote-profile-id",
    default=None,
    help="Remote profile id (for kind=remote_filesystem).",
)
@click.option(
    "--lines",
    type=int,
    default=100,
    help="Terminal scrollback lines to fetch (for kind=terminal; must be >= 100).",
)
@click.option(
    "--token",
    default=None,
    help="Correlation token to embed (interactive kinds). Auto-generated if omitted.",
)
@click.pass_context
def feishu_build_card(
    ctx: click.Context,
    kind: str,
    title: Optional[str],
    body: Optional[str],
    workspace_id: Optional[str],
    task_id: Optional[str],
    field_name: str,
    tab_id: Optional[str],
    path: Optional[str],
    remote_profile_id: Optional[str],
    lines: int,
    token: Optional[str],
) -> None:
    """Build a card of KIND and print ``{kind, token, card}`` as JSON.

    The agent sends ``card`` to Feishu itself. For interactive kinds the printed
    ``token`` is embedded in every control's ``value`` so the later
    ``card.action.trigger`` callback can be correlated via ``parse-action``.
    """
    is_interactive = kind in INTERACTIVE_KINDS
    if is_interactive:
        card_token = token or secrets.token_urlsafe(16)
    else:
        card_token = ""

    card = _build_card(
        ctx,
        kind,
        card_token,
        title=title,
        body=body,
        workspace_id=workspace_id,
        task_id=task_id,
        field_name=field_name,
        tab_id=tab_id,
        path=path,
        remote_profile_id=remote_profile_id,
        lines=lines,
    )
    emit({"kind": kind, "token": card_token or None, "card": card}, as_json=True)


# -- Action parsing ---------------------------------------------------------


@feishu.command("parse-action")
@click.argument("payload", required=False)
@click.pass_context
def feishu_parse_action(ctx: click.Context, payload: Optional[str]) -> None:
    """Parse a ``card.action.trigger`` callback into a decision.

    PAYLOAD is the raw callback JSON; pass it as an argument or pipe it on stdin.
    Prints ``{token, action, form, operator_id, chat_id}`` as JSON. Exits 1 and
    prints ``null`` for a foreign card / non-interactive control (no token), so a
    caller can branch on the exit code.
    """
    raw = payload if payload is not None else click.get_text_stream("stdin").read()
    if not raw or not raw.strip():
        raise click.ClickException("no callback payload given (pass an argument or pipe stdin)")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"invalid JSON payload: {e}") from e

    decision = parse_card_action(body)
    if decision is None:
        emit(None, as_json=True)
        ctx.exit(1)
    emit(decision, as_json=True)
