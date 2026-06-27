"""``session`` command group."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import merge_payload, parse_attachment_json
from claude_hub.cli.output import emit, print_rows

REPORT_STATES = [
    "started",
    "working",
    "blocked",
    "needs_input",
    "ready_for_review",
    "completed",
    "review_started",
    "review_passed",
    "review_failed",
    "review_needs_input",
]


@click.group()
def session() -> None:
    """Interact with managed agent sessions."""


SESSION_COLUMNS = [
    "id",
    "role",
    "agent_type",
    "status",
    "current_task_id",
    "last_activity_at",
]


@session.command("list")
@click.argument("workspace_id")
@click.option("--role", default=None, help="Client-side filter on session role.")
@click.pass_context
def session_list(ctx: click.Context, workspace_id: str, role: Optional[str]) -> None:
    """List managed sessions for a workspace."""
    try:
        with cli_main.get_client(ctx) as client:
            board = client.get_board(workspace_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    sessions: List[dict] = board.get("sessions", []) if isinstance(board, dict) else []
    if role is not None:
        sessions = [s for s in sessions if s.get("role") == role]
    if cli_main.as_json(ctx):
        emit(sessions, True)
    else:
        print_rows(sessions, SESSION_COLUMNS)


@session.command("logs")
@click.argument("session_id")
@click.option(
    "--lines", type=int, default=50, help="Number of trailing lines to show (default 50)."
)
@click.pass_context
def session_logs(ctx: click.Context, session_id: str, lines: int) -> None:
    """Show a session's recent terminal output."""
    if lines < 1:
        raise click.ClickException("--lines must be >= 1.")
    try:
        with cli_main.get_client(ctx) as client:
            tab_id = _find_session_tab(client, session_id)
            if tab_id is None:
                raise click.ClickException(
                    f"Session {session_id} not found, or has no terminal tab."
                )
            data = client.get_terminal_history(tab_id, lines=max(lines, 100))
    except HubError as e:
        raise click.ClickException(str(e)) from e
    history = data.get("history", "") if isinstance(data, dict) else ""
    tail = "\n".join(history.splitlines()[-lines:])
    if cli_main.as_json(ctx):
        emit({"session_id": session_id, "tab_id": tab_id, "lines": lines, "history": tail}, True)
    else:
        click.echo(tail)


def _find_session_tab(client: Any, session_id: str) -> Optional[str]:
    """Return the terminal ``tab_id`` for ``session_id`` by scanning boards."""
    workspaces = client.list_workspaces()
    items = workspaces if isinstance(workspaces, list) else []
    for ws in items:
        ws_id = ws.get("id") if isinstance(ws, dict) else None
        if not ws_id:
            continue
        board = client.get_board(ws_id)
        sessions: List[dict] = board.get("sessions", []) if isinstance(board, dict) else []
        match = next((s for s in sessions if s.get("id") == session_id), None)
        if match is not None:
            tab = match.get("tab_id")
            return str(tab) if tab else None
    return None


@session.command("send")
@click.argument("session_id")
@click.option("--message", default=None, help="Message to send to the session.")
@click.option(
    "--attachment-json",
    "attachment_json",
    multiple=True,
    help="Attachment JSON object (repeatable).",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def session_send(
    ctx: click.Context,
    session_id: str,
    message: Optional[str],
    attachment_json: tuple,
    payload_json: Optional[str],
) -> None:
    """Send a message to a managed session."""
    body = merge_payload(payload_json, message=message)
    if attachment_json:
        body["attachments"] = parse_attachment_json(attachment_json)
    elif "attachments" not in body:
        body["attachments"] = []
    if not body.get("message"):
        raise click.ClickException("--message is required unless supplied by --payload-json.")
    try:
        with cli_main.get_client(ctx) as client:
            client.send_session(session_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit({"ok": True}, True)
    else:
        click.echo("sent")


@session.command("delete")
@click.argument("session_id")
@click.pass_context
def session_delete(ctx: click.Context, session_id: str) -> None:
    """Delete an idle managed session and its terminal tab."""
    try:
        with cli_main.get_client(ctx) as client:
            client.delete_session(session_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit({"ok": True}, True)
    else:
        click.echo(f"deleted {session_id}")


@session.command("report")
@click.argument("session_id")
@click.option(
    "--state",
    required=True,
    type=click.Choice(REPORT_STATES),
    help="Report state.",
)
@click.option("--message", required=True, help="Report message.")
@click.option("--message-en", default=None, help="English report message.")
@click.option("--message-zh", default=None, help="Chinese report message.")
@click.option("--task-id", default=None, help="Task id this report applies to.")
@click.option("--validation", default=None, help="Validation summary.")
@click.option("--risks", default=None, help="Known risks.")
@click.option("--review-decision", default=None, help="Review decision: auto/request/skip.")
@click.option("--review-reason", default=None, help="Reason for the review decision.")
@click.option("--risk-level", default=None, help="Risk level label.")
@click.option(
    "--changed-file",
    "changed_files",
    multiple=True,
    help="Changed file path (repeatable).",
)
@click.option(
    "--payload-json",
    default=None,
    help="Raw JSON object merged into the report body for structured fields.",
)
@click.pass_context
def session_report(
    ctx: click.Context,
    session_id: str,
    state: str,
    message: str,
    message_en: Optional[str],
    message_zh: Optional[str],
    task_id: Optional[str],
    validation: Optional[str],
    risks: Optional[str],
    review_decision: Optional[str],
    review_reason: Optional[str],
    risk_level: Optional[str],
    changed_files: tuple,
    payload_json: Optional[str],
) -> None:
    """Append a progress report to a managed session.

    ``--payload-json`` accepts the remaining AgentReportCreate fields, such as
    ``goal_packet``, ``acceptance_check``, ``artifact_refs`` and
    ``requires_human_judgment``. Explicit flags override matching JSON fields.
    """
    body: Dict[str, Any] = {
        "state": state,
        "message": message,
    }
    if payload_json is not None:
        try:
            extra = json.loads(payload_json)
        except json.JSONDecodeError as e:
            raise click.ClickException(f"invalid --payload-json: {e}") from e
        if not isinstance(extra, dict):
            raise click.ClickException("--payload-json must decode to a JSON object.")
        body.update(extra)
        body["state"] = state
        body["message"] = message
    if changed_files or "changed_files" not in body:
        body["changed_files"] = list(changed_files)
    if message_en is not None:
        body["message_en"] = message_en
    if message_zh is not None:
        body["message_zh"] = message_zh
    if task_id is not None:
        body["task_id"] = task_id
    if validation is not None:
        body["validation"] = validation
    if risks is not None:
        body["risks"] = risks
    if review_decision is not None:
        body["review_decision"] = review_decision
    if review_reason is not None:
        body["review_reason"] = review_reason
    if risk_level is not None:
        body["risk_level"] = risk_level
    try:
        with cli_main.get_client(ctx) as client:
            data = client.create_report(session_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
