"""``session`` command group — managed workspace sessions."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import (
    fetch_chat_stream_tail_lines,
    is_missing_route_404,
    lifecycle_group_help,
    merge_payload,
    parse_attachment_json,
    parse_json_object,
    parse_kv_pairs,
    redact_session_env_payload,
    resolve_cli_local_path,
    resolve_workspace_id,
)
from claude_hub.cli.output import emit, print_rows
from claude_hub.models import AgentType, SessionKind
from claude_hub.models.schemas import validate_workspace_session_surface

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

SESSION_COLUMNS = [
    "id",
    "role",
    "agent_type",
    "session_kind",
    "status",
    "archived",
    "current_task_id",
    "last_activity_at",
]
SESSION_STATUS_FIELDS = [
    "workspace_id",
    "id",
    "role",
    "agent_type",
    "session_kind",
    "status",
    "archived",
    "runtime_status",
    "current_task_id",
    "tab_id",
    "target",
    "remote_profile_id",
    "remote_cwd",
    "branch",
    "last_activity_at",
    "updated_at",
]


def _resolve_session_lifetime_flags(
    ephemeral: Optional[bool], reuse_existing: Optional[bool]
) -> tuple[bool, bool]:
    """Resolve tri-state lifetime/reuse flags into ``(ephemeral, reuse_existing)``."""
    if ephemeral is True and reuse_existing is True:
        raise click.ClickException("Cannot combine --ephemeral with --reuse-existing.")
    if ephemeral is not None:
        effective_ephemeral = ephemeral
    elif reuse_existing is True:
        effective_ephemeral = False
    else:
        effective_ephemeral = True
    return effective_ephemeral, reuse_existing is True


def _chat_capable(agent_type: str) -> bool:
    try:
        validate_workspace_session_surface(AgentType(agent_type), SessionKind.CHAT)
    except ValueError:
        return False
    return True


def _workspace_is_remote(client: Any, workspace_id: str) -> Optional[bool]:
    try:
        workspaces = client.list_workspaces()
    except HubError:
        return None
    for workspace in workspaces if isinstance(workspaces, list) else []:
        if isinstance(workspace, dict) and workspace_id in (
            workspace.get("id"),
            workspace.get("name"),
        ):
            return workspace.get("target") == "remote"
    return None


def _default_session_kind(client: Any, workspace_id: str, agent_type: str, *, remote: bool) -> str:
    if remote or not _chat_capable(agent_type):
        return "terminal"
    if _workspace_is_remote(client, workspace_id) is not False:
        return "terminal"
    return "chat"


@click.group(help=lifecycle_group_help("Manage workspace agent sessions."))
def session() -> None:
    pass


@session.command("create")
@click.argument("workspace_id", required=False, default=None)
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "traex", "maso", "cursor", "antigravity", "terminal"]),
    default="codex",
    show_default=True,
    help="Agent type to run.",
)
@click.option(
    "--session-kind",
    type=click.Choice(["terminal", "chat"]),
    default=None,
    help=(
        "Managed session surface. Default: chat for a local Chat-capable agent; "
        "remote SSH and terminal-only agent types use terminal."
    ),
)
@click.option("--title", default=None, help="Optional session title.")
@click.option(
    "--role",
    type=click.Choice(["worker", "orchestrator", "reviewer", "dispatcher"]),
    default="orchestrator",
    help="Session role.",
)
@click.option(
    "--reuse-existing/--no-reuse-existing",
    default=None,
    help="Reuse a compatible idle persistent session (off by default).",
)
@click.option("--cwd", default=None, help="Session working directory override.")
@click.option(
    "--solo-mode/--no-solo-mode",
    default=True,
    help="Run the agent in solo mode (default on).",
)
@click.option(
    "--target",
    type=click.Choice(["local", "remote"]),
    default=None,
    help="Execution target override.",
)
@click.option("--remote-profile-id", default=None, help="Remote profile for remote sessions.")
@click.option("--remote-cwd", default=None, help="Remote working directory.")
@click.option(
    "--remote-reconnect/--no-remote-reconnect",
    default=None,
    help="Reconnect remote sessions automatically.",
)
@click.option("--env", "env_values", multiple=True, help="Environment variable KEY=VALUE.")
@click.option(
    "--env-preset",
    default=None,
    help="Any built-in or saved custom env preset, selected by id or name.",
)
@click.option(
    "--ephemeral/--persistent",
    default=None,
    help=(
        "Session lifetime. Default: --ephemeral (isolated session); "
        "pass --persistent for a durable interactive session."
    ),
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def session_create(
    ctx: click.Context,
    workspace_id: Optional[str],
    agent_type: str,
    session_kind: Optional[str],
    title: Optional[str],
    role: str,
    reuse_existing: Optional[bool],
    cwd: Optional[str],
    solo_mode: bool,
    target: Optional[str],
    remote_profile_id: Optional[str],
    remote_cwd: Optional[str],
    remote_reconnect: Optional[bool],
    env_values: tuple,
    env_preset: Optional[str],
    ephemeral: Optional[bool],
    payload_json: Optional[str],
) -> None:
    """Create a managed workspace session (isolated ephemeral Chat by default)."""
    effective_ephemeral, effective_reuse = _resolve_session_lifetime_flags(
        ephemeral, reuse_existing
    )
    raw_payload = parse_json_object(payload_json)
    settings = ctx.obj
    resolved_preset = (
        env_preset
        or settings.env_preset_for_agent_type(agent_type)
        or getattr(settings, "default_env_preset", None)
    )
    resolved_cwd = resolve_cli_local_path(cwd) if cwd is not None else None

    try:
        with cli_main.get_client(ctx) as client:
            resolved_ws = resolve_workspace_id(client, workspace_id)
            effective_kind = session_kind
            if effective_kind is None and "session_kind" not in raw_payload:
                remote_fields = ("remote_profile_id", "remote_cwd", "remote_reconnect")
                effective_kind = _default_session_kind(
                    client,
                    resolved_ws,
                    agent_type,
                    remote=bool(
                        (target or raw_payload.get("target")) == "remote"
                        or remote_profile_id
                        or remote_cwd
                        or remote_reconnect is not None
                        or any(raw_payload.get(field) is not None for field in remote_fields)
                    ),
                )
            body: Dict[str, Any] = merge_payload(
                payload_json,
                agent_type=agent_type,
                session_kind=effective_kind,
                role=role,
                reuse_existing=effective_reuse,
                cwd=resolved_cwd,
                solo_mode=solo_mode,
                target=target,
                remote_profile_id=remote_profile_id,
                remote_cwd=remote_cwd,
                remote_reconnect=remote_reconnect,
                ephemeral=effective_ephemeral,
                caller_owned_ephemeral=effective_ephemeral,
                env_preset=resolved_preset,
            )
            if title is not None:
                body["title"] = title
            if env_values:
                body["env"] = parse_kv_pairs(env_values, "--env")
            data = client.ensure_agent(resolved_ws, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    data = redact_session_env_payload(data)
    emit(data, cli_main.as_json(ctx))


@session.command("list")
@click.argument("workspace_id", required=False, default=None)
@click.option("--role", default=None, help="Client-side filter on session role.")
@click.option("--include-archived", is_flag=True, help="Include archived sessions.")
@click.pass_context
def session_list(
    ctx: click.Context,
    workspace_id: Optional[str],
    role: Optional[str],
    include_archived: bool,
) -> None:
    """List managed sessions for a workspace."""
    try:
        with cli_main.get_client(ctx) as client:
            resolved_ws = resolve_workspace_id(client, workspace_id)
            board = client.get_board(resolved_ws, include_archived=include_archived)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    sessions: List[dict] = board.get("sessions", []) if isinstance(board, dict) else []
    if not include_archived:
        sessions = [s for s in sessions if not s.get("archived", False)]
    if role is not None:
        sessions = [s for s in sessions if s.get("role") == role]
    if cli_main.as_json(ctx):
        emit(sessions, True)
    else:
        print_rows(sessions, SESSION_COLUMNS)


def _fetch_session_dict(
    client: Any, session_id: str, workspace_id: Optional[str] = None
) -> Dict[str, Any]:
    """Fetch session metadata in O(1), falling back to board scan on older Hub servers."""
    try:
        fetched = client.get_session(session_id)
        if isinstance(fetched, dict):
            if workspace_id is not None and fetched.get("workspace_id") != workspace_id:
                raise click.ClickException(
                    f"Session {session_id} not found in workspace {workspace_id}."
                )
            return fetched
    except HubError as e:
        if not is_missing_route_404(e):
            if e.status == 404:
                where = f" in workspace {workspace_id}" if workspace_id else ""
                raise click.ClickException(f"Session {session_id} not found{where}.") from e
            raise click.ClickException(str(e)) from e

    boards: List[tuple[Optional[str], dict]] = []
    if workspace_id is not None:
        board = client.get_board(workspace_id, include_archived=True)
        if isinstance(board, dict):
            boards.append((workspace_id, board))
    else:
        workspaces = None
        if hasattr(client, "list_workspaces"):
            try:
                workspaces = client.list_workspaces()
            except Exception:
                workspaces = None
        if isinstance(workspaces, list) and workspaces:
            for ws_entry in workspaces:
                ws_id = ws_entry.get("id") if isinstance(ws_entry, dict) else None
                if not ws_id:
                    continue
                board = client.get_board(str(ws_id), include_archived=True)
                if isinstance(board, dict):
                    boards.append((str(ws_id), board))
        elif hasattr(client, "get_board_overview"):
            overview = client.get_board_overview()
            if isinstance(overview, dict):
                boards.append((None, overview))
            elif isinstance(overview, list):
                for entry in overview:
                    if isinstance(entry, dict):
                        ws = entry.get("workspace")
                        ws_id = str(ws.get("id")) if isinstance(ws, dict) and ws.get("id") else None
                        boards.append((ws_id, entry))
    for ws_id, board in boards:
        for item in board.get("sessions", []) if isinstance(board, dict) else []:
            if isinstance(item, dict) and item.get("id") == session_id:
                enriched = dict(item)
                enriched.setdefault("workspace_id", item.get("workspace_id") or ws_id)
                return enriched
    where = f" in workspace {workspace_id}" if workspace_id else ""
    raise click.ClickException(f"Session {session_id} not found{where}.")


@session.command("status")
@click.argument("session_id")
@click.option(
    "--workspace-id",
    default=None,
    help="Optional workspace id assertion.",
)
@click.pass_context
def session_status(
    ctx: click.Context,
    session_id: str,
    workspace_id: Optional[str],
) -> None:
    """Show backend runtime state for one managed session."""
    try:
        with cli_main.get_client(ctx) as client:
            match = _fetch_session_dict(client, session_id, workspace_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit(match, True)
        return
    summary = {field: match.get(field) for field in SESSION_STATUS_FIELDS if field in match}
    emit(summary, False)


@session.command("logs")
@click.argument("session_id")
@click.option(
    "-n",
    "--lines",
    type=int,
    default=50,
    help="Number of trailing lines to show (default 50).",
)
@click.pass_context
def session_logs(ctx: click.Context, session_id: str, lines: int) -> None:
    """Show a session's recent conversation or terminal output."""
    if lines < 1:
        raise click.ClickException("--lines must be >= 1.")
    try:
        with cli_main.get_client(ctx) as client:
            info = _fetch_session_dict(client, session_id)
            kind = str(info.get("session_kind") or "terminal")
            tab_id = info.get("tab_id")
            if kind == "chat":
                tail = "\n".join(
                    fetch_chat_stream_tail_lines(
                        lambda params: client.get_session_stream_events(session_id, params=params),
                        lines=lines,
                        persisted_only=bool(info.get("archived")),
                    )
                )
            else:
                if not tab_id:
                    raise click.ClickException(f"Session {session_id} has no terminal tab.")
                data = client.get_terminal_history(str(tab_id), lines=max(lines, 100))
                history = data.get("history", "") if isinstance(data, dict) else ""
                tail = "\n".join(history.splitlines()[-lines:])
    except HubError as e:
        if e.status == 404:
            raise click.ClickException(f"Session {session_id} not found.") from e
        raise click.ClickException(str(e)) from e

    if cli_main.as_json(ctx):
        emit(
            {
                "session_id": session_id,
                "session_kind": kind,
                "tab_id": tab_id,
                "lines": lines,
                "history": tail,
            },
            True,
        )
    else:
        click.echo(tail)


@session.command("send")
@click.argument("session_id")
@click.option("--message", default=None, help="Message to send to the session.")
@click.option(
    "--call-id",
    default=None,
    help="Stable retry id; generated and printed when omitted.",
)
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
    call_id: Optional[str],
    attachment_json: tuple,
    payload_json: Optional[str],
) -> None:
    """Send a message to a managed session."""
    if call_id is not None and not call_id.strip():
        raise click.ClickException("--call-id must not be blank.")
    body = merge_payload(payload_json, message=message, call_id=call_id)
    if attachment_json:
        body["attachments"] = parse_attachment_json(attachment_json)
    elif "attachments" not in body:
        body["attachments"] = []
    if not body.get("message"):
        raise click.ClickException("--message is required unless supplied by --payload-json.")
    effective_call_id = body.get("call_id")
    if effective_call_id is None:
        effective_call_id = f"session-send:{uuid.uuid4()}"
        body["call_id"] = effective_call_id
    elif not isinstance(effective_call_id, str) or not effective_call_id.strip():
        raise click.ClickException("call_id must be a non-blank string.")
    click.echo(f"call_id={effective_call_id}", err=True)
    try:
        with cli_main.get_client(ctx) as client:
            client.send_session(session_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit({"ok": True, "call_id": effective_call_id}, True)
    else:
        click.echo("sent")


def _change_session_archive(
    ctx: click.Context, session_id: str, workspace_id: Optional[str], *, archived: bool
) -> None:
    try:
        with cli_main.get_client(ctx) as client:
            if workspace_id is None:
                info = _fetch_session_dict(client, session_id)
                workspace_id = info.get("workspace_id") if isinstance(info, dict) else None
                if not workspace_id:
                    raise click.ClickException(f"Session {session_id} not found.")
            if archived:
                data = client.archive_session(workspace_id, session_id)
            else:
                data = client.unarchive_session(workspace_id, session_id)
    except HubError as e:
        if e.status == 404:
            raise click.ClickException(f"Session {session_id} not found.") from e
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit(data, True)
    else:
        action = "archived" if archived else "unarchived"
        click.echo(f"{action} {session_id}")


@session.command("archive")
@click.argument("session_id")
@click.option("--workspace-id", default=None, help="Workspace containing the session.")
@click.pass_context
def session_archive(ctx: click.Context, session_id: str, workspace_id: Optional[str]) -> None:
    """Release an idle managed Chat session's runtime, retaining resumable history."""
    _change_session_archive(ctx, session_id, workspace_id, archived=True)


@session.command("unarchive")
@click.argument("session_id")
@click.option("--workspace-id", default=None, help="Workspace containing the session.")
@click.pass_context
def session_unarchive(ctx: click.Context, session_id: str, workspace_id: Optional[str]) -> None:
    """Restore an archived managed Chat session and best-effort restart its runtime."""
    _change_session_archive(ctx, session_id, workspace_id, archived=False)


@session.command("delete")
@click.argument("session_id")
@click.pass_context
def session_delete(ctx: click.Context, session_id: str) -> None:
    """Permanently delete an active or archived idle session and its history."""
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
    "--call-id",
    default=None,
    help="Idempotency key for safe report retries.",
)
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
    call_id: Optional[str],
    changed_files: tuple,
    payload_json: Optional[str],
) -> None:
    """Append a progress report to a managed session."""
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
    if call_id is not None:
        body["call_id"] = call_id
    try:
        with cli_main.get_client(ctx) as client:
            data = client.create_report(session_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
