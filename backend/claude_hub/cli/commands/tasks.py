"""``task`` command group."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.output import emit, print_rows
from claude_hub.models.schemas import WorkspaceTaskStatus

TASK_COLUMNS = ["id", "title", "status", "agent_type", "task_mode"]


@click.group()
def task() -> None:
    """Manage workspace tasks."""


@task.command("list")
@click.argument("workspace_id")
@click.option("--status", default=None, help="Client-side filter on task status.")
@click.pass_context
def task_list(ctx: click.Context, workspace_id: str, status: Optional[str]) -> None:
    """List tasks for a workspace."""
    try:
        with cli_main.get_client(ctx) as client:
            board = client.get_board(workspace_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    tasks: List[dict] = board.get("tasks", []) if isinstance(board, dict) else []
    if status is not None:
        tasks = [t for t in tasks if t.get("status") == status]
    if cli_main.as_json(ctx):
        emit(tasks, True)
    else:
        print_rows(tasks, TASK_COLUMNS)


TASK_DETAIL_FIELDS = [
    "id",
    "title",
    "status",
    "agent_type",
    "task_mode",
    "execution_complexity",
    "session_id",
    "review_cycle",
    "reviewed_cycle",
    "review_attempts",
    "created_at",
    "updated_at",
    "prompt",
]


def _find_task_board(client: Any, task_id: str) -> tuple:
    """Locate ``task_id`` by scanning workspace boards."""
    workspaces = client.list_workspaces()
    items = workspaces if isinstance(workspaces, list) else []
    for ws in items:
        ws_id = ws.get("id") if isinstance(ws, dict) else None
        if not ws_id:
            continue
        board = client.get_board(ws_id)
        tasks: List[dict] = board.get("tasks", []) if isinstance(board, dict) else []
        match = next((t for t in tasks if t.get("id") == task_id), None)
        if match is not None:
            return ws_id, match
    return None, None


@task.command("get")
@click.argument("task_id")
@click.option(
    "--workspace-id",
    default=None,
    help="Workspace to look in (skips the cross-workspace scan).",
)
@click.option("--reports/--no-reports", default=True, help="Include report history (default on).")
@click.pass_context
def task_get(
    ctx: click.Context,
    task_id: str,
    workspace_id: Optional[str],
    reports: bool,
) -> None:
    """Show details for a single task."""
    try:
        with cli_main.get_client(ctx) as client:
            if workspace_id is not None:
                board = client.get_board(workspace_id)
                tasks: List[dict] = board.get("tasks", []) if isinstance(board, dict) else []
                match = next((t for t in tasks if t.get("id") == task_id), None)
                ws_id = workspace_id
            else:
                ws_id, match = _find_task_board(client, task_id)
            if match is None:
                where = f" in workspace {workspace_id}" if workspace_id else ""
                raise click.ClickException(f"Task {task_id} not found{where}.")
            report_history: List[dict] = []
            if reports and ws_id:
                fetched = client.get_task_reports(ws_id, task_id)
                report_history = fetched if isinstance(fetched, list) else []
    except HubError as e:
        raise click.ClickException(str(e)) from e

    detail: Dict[str, Any] = {"workspace_id": ws_id}
    detail.update(match)
    detail["reports"] = report_history

    if cli_main.as_json(ctx):
        emit(detail, True)
        return

    summary: Dict[str, Any] = {"workspace_id": ws_id}
    summary.update({k: match.get(k) for k in TASK_DETAIL_FIELDS if k in match})
    emit(summary, False)
    click.echo("")
    click.echo(f"reports ({len(report_history)}):")
    if report_history:
        print_rows(report_history, ["created_at", "state", "review_decision", "message"])
    else:
        click.echo("(none)")


def _resolve_ws_id(client: Any, task_id: str, workspace_id: Optional[str]) -> str:
    """Return the workspace id for ``task_id``, scanning boards when not given."""
    if workspace_id is not None:
        board = client.get_board(workspace_id)
        tasks: List[dict] = board.get("tasks", []) if isinstance(board, dict) else []
        if not any(t.get("id") == task_id for t in tasks):
            raise click.ClickException(f"Task {task_id} not found in workspace {workspace_id}.")
        return workspace_id
    ws_id, match = _find_task_board(client, task_id)
    if match is None or ws_id is None:
        raise click.ClickException(f"Task {task_id} not found.")
    return str(ws_id)


REVIEW_STATES = {
    "review_started",
    "review_passed",
    "review_failed",
    "review_needs_input",
}


@task.command("report")
@click.argument("task_id")
@click.option(
    "--workspace-id",
    default=None,
    help="Workspace to look in (skips the cross-workspace scan).",
)
@click.option("--limit", type=int, default=None, help="Show only the N most recent reports.")
@click.pass_context
def task_report(
    ctx: click.Context,
    task_id: str,
    workspace_id: Optional[str],
    limit: Optional[int],
) -> None:
    """Show a task's progress reports, newest first."""
    if limit is not None and limit < 1:
        raise click.ClickException("--limit must be >= 1.")
    try:
        with cli_main.get_client(ctx) as client:
            ws_id = _resolve_ws_id(client, task_id, workspace_id)
            fetched = client.get_task_reports(ws_id, task_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    reports: List[dict] = fetched if isinstance(fetched, list) else []
    reports = list(reversed(reports))
    if limit is not None:
        reports = reports[:limit]
    if cli_main.as_json(ctx):
        emit(reports, True)
    else:
        print_rows(
            reports,
            ["created_at", "state", "review_decision", "session_id", "message"],
        )


@task.command("review")
@click.argument("task_id")
@click.option(
    "--workspace-id",
    default=None,
    help="Workspace to look in (skips the cross-workspace scan).",
)
@click.pass_context
def task_review(ctx: click.Context, task_id: str, workspace_id: Optional[str]) -> None:
    """Show a task's review timeline."""
    try:
        with cli_main.get_client(ctx) as client:
            ws_id = _resolve_ws_id(client, task_id, workspace_id)
            fetched = client.get_task_reports(ws_id, task_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    reports: List[dict] = fetched if isinstance(fetched, list) else []
    rounds = [
        {
            "review_cycle": r.get("review_cycle"),
            "verdict": r.get("state"),
            "reviewer": r.get("session_id"),
            "created_at": r.get("created_at"),
            "notes": r.get("review_reason") or r.get("message"),
        }
        for r in reports
        if r.get("state") in REVIEW_STATES
    ]
    if cli_main.as_json(ctx):
        emit(rounds, True)
    else:
        print_rows(rounds, ["review_cycle", "verdict", "reviewer", "created_at", "notes"])


@task.command("create")
@click.argument("workspace_id")
@click.option("--title", required=True, help="Task title.")
@click.option("--prompt", required=True, help="Task prompt.")
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "cursor", "terminal"]),
    default="codex",
    help="Agent type.",
)
@click.option(
    "--task-mode",
    type=click.Choice(["direct", "reviewed", "autonomous"]),
    default="reviewed",
    help="Task automation mode.",
)
@click.option(
    "--execution-complexity",
    type=click.Choice(["auto", "simple", "complex"]),
    default="auto",
    help="Execution complexity hint.",
)
@click.option(
    "--review-profile",
    "review_profiles",
    multiple=True,
    type=click.Choice(["general", "code", "ui", "artifact", "delivery", "boundary"]),
    help="Review profile (repeatable).",
)
@click.pass_context
def task_create(
    ctx: click.Context,
    workspace_id: str,
    title: str,
    prompt: str,
    agent_type: str,
    task_mode: str,
    execution_complexity: str,
    review_profiles: tuple,
) -> None:
    """Create a task in a workspace."""
    body: Dict[str, Any] = {
        "title": title,
        "prompt": prompt,
        "agent_type": agent_type,
        "task_mode": task_mode,
        "execution_complexity": execution_complexity,
        "review_profiles": list(review_profiles),
    }
    try:
        with cli_main.get_client(ctx) as client:
            data = client.create_task(workspace_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("start")
@click.argument("task_id")
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "cursor", "terminal"]),
    default=None,
    help="Override the agent type.",
)
@click.option("--target-session-id", default=None, help="Target a specific session.")
@click.option(
    "--clear-context/--no-clear-context",
    default=None,
    help="Clear agent context before starting (omitted unless set).",
)
@click.option("--related-task-id", default=None, help="Related task id.")
@click.pass_context
def task_start(
    ctx: click.Context,
    task_id: str,
    agent_type: Optional[str],
    target_session_id: Optional[str],
    clear_context: Optional[bool],
    related_task_id: Optional[str],
) -> None:
    """Queue / start a task."""
    body: Dict[str, Any] = {}
    if agent_type is not None:
        body["agent_type"] = agent_type
    if target_session_id is not None:
        body["target_session_id"] = target_session_id
    if clear_context is not None:
        body["clear_context"] = clear_context
    if related_task_id is not None:
        body["related_task_id"] = related_task_id
    try:
        with cli_main.get_client(ctx) as client:
            data = client.start_task(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("continue")
@click.argument("task_id")
@click.option("--message", default=None, help="Message to send when continuing.")
@click.pass_context
def task_continue(ctx: click.Context, task_id: str, message: Optional[str]) -> None:
    """Continue a task from review with its original agent."""
    body: Dict[str, Any] = {"attachments": []}
    if message is not None:
        body["message"] = message
    try:
        with cli_main.get_client(ctx) as client:
            data = client.continue_task(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("accept")
@click.argument("task_id")
@click.option(
    "--workspace-id",
    default=None,
    help="Workspace to look in (skips the cross-workspace scan).",
)
@click.option("--message", default=None, help="Optional acceptance note (recorded as a report).")
@click.pass_context
def task_accept(
    ctx: click.Context,
    task_id: str,
    workspace_id: Optional[str],
    message: Optional[str],
) -> None:
    """Human-accept a task in review and mark it done."""
    try:
        with cli_main.get_client(ctx) as client:
            if workspace_id is not None:
                board = client.get_board(workspace_id)
                tasks: List[dict] = board.get("tasks", []) if isinstance(board, dict) else []
                match = next((t for t in tasks if t.get("id") == task_id), None)
            else:
                _, match = _find_task_board(client, task_id)
            if match is None:
                where = f" in workspace {workspace_id}" if workspace_id else ""
                raise click.ClickException(f"Task {task_id} not found{where}.")
            status = match.get("status")
            if status != WorkspaceTaskStatus.REVIEW.value:
                raise click.ClickException(
                    f"Task {task_id} is '{status}', not 'review'; "
                    "only tasks in review can be accepted."
                )
            if not match.get("human_acceptance_requested_at"):
                raise click.ClickException(
                    f"Task {task_id} is in review but is not awaiting human acceptance; "
                    "wait for review_passed or review_skipped before accepting."
                )
            if message is not None:
                session_id = match.get("session_id")
                if session_id:
                    client.create_report(
                        session_id,
                        {
                            "state": "completed",
                            "message": message,
                            "task_id": task_id,
                            "changed_files": [],
                        },
                    )
            data = client.update_task(task_id, {"status": WorkspaceTaskStatus.DONE.value})
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("send")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option("--message", required=True, help="Message to send to the task's agent.")
@click.pass_context
def task_send(ctx: click.Context, workspace_id: str, task_id: str, message: str) -> None:
    """Send a message to the agent session currently running a task.

    Convenience wrapper: resolves the task's session from the workspace board,
    then delegates to the session send endpoint. Errors clearly if the task has
    no active session (not started yet, or already finished) — in that case use
    ``session send`` directly, or ``task continue`` to resume from review.
    """
    try:
        with cli_main.get_client(ctx) as client:
            board = client.get_board(workspace_id)
            tasks: List[dict] = board.get("tasks", []) if isinstance(board, dict) else []
            match = next((t for t in tasks if t.get("id") == task_id), None)
            if match is None:
                raise click.ClickException(f"Task {task_id} not found in workspace {workspace_id}.")
            session_id = match.get("session_id")
            if not session_id:
                raise click.ClickException(
                    f"Task {task_id} has no active session "
                    f"(status: {match.get('status', '?')}). "
                    "Use `session send` directly, or `task continue` to resume from review."
                )
            client.send_session(session_id, {"message": message, "attachments": []})
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit({"ok": True, "task_id": task_id, "session_id": session_id}, True)
    else:
        click.echo(f"sent to session {session_id}")


@task.command("abort")
@click.argument("task_id")
@click.option("--reason", required=True, help="Reason for aborting.")
@click.pass_context
def task_abort(ctx: click.Context, task_id: str, reason: str) -> None:
    """Abort a task."""
    body: Dict[str, Any] = {"reason": reason}
    try:
        with cli_main.get_client(ctx) as client:
            data = client.abort_task(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("request-review")
@click.argument("task_id")
@click.option("--message", default=None, help="Optional note for the reviewer.")
@click.pass_context
def task_request_review(ctx: click.Context, task_id: str, message: Optional[str]) -> None:
    """Manually request reviewer checks for a task."""
    body: Dict[str, Any] = {}
    if message is not None:
        body["message"] = message
    try:
        with cli_main.get_client(ctx) as client:
            data = client.request_task_review(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
