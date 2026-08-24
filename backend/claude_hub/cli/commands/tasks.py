"""``task`` command group."""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import (
    merge_payload,
    parse_attachment_json,
    parse_json_object,
)
from claude_hub.cli.output import emit, print_rows
from claude_hub.models.schemas import WorkspaceTaskStatus

TASK_COLUMNS = ["id", "title", "status", "agent_type", "task_mode"]
TASK_TREE_COLUMNS = ["id", "title", "status", "parent_task_id", "agent_type"]
TASK_EVENT_COLUMNS = ["sequence", "type", "call_id", "task_id", "consumer_key"]
ACK_FAILED_NOTE = "events delivered but ACK failed/not acknowledged"
TASK_STATUS_FIELDS = [
    "workspace_id",
    "id",
    "title",
    "status",
    "agent_type",
    "task_mode",
    "execution_complexity",
    "session_id",
    "review_session_id",
    "review_cycle",
    "reviewed_cycle",
    "review_attempts",
    "review_requested_at",
    "review_completed_at",
    "review_skipped_at",
    "human_acceptance_requested_at",
    "human_accepted_at",
    "updated_at",
]


def _echo_call_id(call_id: str) -> None:
    click.echo(f"call_id={call_id}", err=True)


def _resolve_call_id(explicit: Optional[str]) -> str:
    return explicit or str(uuid.uuid4())


def _emit_task_events(rows: List[Any], as_json: bool) -> None:
    if as_json:
        emit(rows, True)
    else:
        print_rows(rows, TASK_EVENT_COLUMNS)
    sys.stdout.flush()


def _emit_task_tree(rows: List[dict], as_json: bool) -> None:
    if as_json:
        emit(rows, True)
    else:
        print_rows(rows, TASK_TREE_COLUMNS)


GOAL_PACKET_FIELDS = ["status", "objective", "updated_at", "source"]
ACCEPTANCE_COLUMNS = ["criterion", "status", "evidence"]
REVIEW_COLUMNS = [
    "created_at",
    "state",
    "review_cycle",
    "session_id",
    "review_decision",
    "review_reason",
]


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


def _report_message(report: Optional[dict]) -> str:
    if not report:
        return ""
    return str(report.get("message_zh") or report.get("message_en") or report.get("message") or "")


def _latest_report(reports: List[dict]) -> Optional[dict]:
    if not reports:
        return None
    return max(reports, key=lambda report: str(report.get("created_at", "")))


def _acceptance_check_items(report: Optional[dict]) -> List[dict]:
    if not report:
        return []
    acceptance = report.get("acceptance_check")
    if not isinstance(acceptance, list):
        return []
    return [item for item in acceptance if isinstance(item, dict)]


def _latest_acceptance_report(reports: List[dict]) -> Optional[dict]:
    acceptance_reports = [report for report in reports if _acceptance_check_items(report)]
    if not acceptance_reports:
        return None
    return max(acceptance_reports, key=lambda report: str(report.get("created_at", "")))


def _task_status_payload(
    workspace_id: Optional[str], task: dict, reports: List[dict]
) -> Dict[str, Any]:
    latest = _latest_report(reports)
    acceptance_report = _latest_acceptance_report(reports)
    review_reports = [report for report in reports if report.get("state") in REVIEW_STATES]
    latest_acceptance = _acceptance_check_items(acceptance_report)
    return {
        "workspace_id": workspace_id,
        "task": task,
        "goal_packet": task.get("goal_packet"),
        "latest_report": latest,
        "latest_report_message": _report_message(latest),
        "latest_acceptance_report": acceptance_report,
        "latest_acceptance_check": latest_acceptance,
        "review_reports": review_reports,
        "human_acceptance_requested_at": task.get("human_acceptance_requested_at"),
        "human_accepted_at": task.get("human_accepted_at"),
    }


def _print_task_status(payload: Dict[str, Any]) -> None:
    task_obj = payload.get("task")
    task: Dict[str, Any] = task_obj if isinstance(task_obj, dict) else {}
    detail: Dict[str, Any] = {"workspace_id": payload.get("workspace_id")}
    detail.update({field: task.get(field) for field in TASK_STATUS_FIELDS if field in task})
    click.echo(f"Task: {task.get('title', '(untitled)')} ({task.get('id', '?')})")
    emit(detail, False)

    goal_packet = payload.get("goal_packet")
    click.echo("\nGoal Packet:")
    if isinstance(goal_packet, dict):
        emit({field: goal_packet.get(field) for field in GOAL_PACKET_FIELDS}, False)
        criteria = goal_packet.get("acceptance_criteria") or []
        if criteria:
            click.echo("acceptance_criteria:")
            for criterion in criteria:
                click.echo(f"- {criterion}")
    else:
        click.echo("(none)")

    latest = payload.get("latest_report")
    click.echo("\nLatest report:")
    if isinstance(latest, dict):
        emit(
            {
                "created_at": latest.get("created_at"),
                "state": latest.get("state"),
                "session_id": latest.get("session_id"),
                "review_decision": latest.get("review_decision"),
                "review_reason": latest.get("review_reason"),
                "message": _report_message(latest),
            },
            False,
        )
    else:
        click.echo("(none)")

    click.echo("\nAcceptance check:")
    acceptance_report = payload.get("latest_acceptance_report")
    if isinstance(acceptance_report, dict):
        click.echo(
            "source: "
            f"{acceptance_report.get('state') or '?'} "
            f"{acceptance_report.get('created_at') or ''}".rstrip()
        )
    print_rows(payload.get("latest_acceptance_check", []), ACCEPTANCE_COLUMNS)

    click.echo("\nReview reports:")
    print_rows(payload.get("review_reports", []), REVIEW_COLUMNS)


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


@task.command("status")
@click.argument("task_id")
@click.option(
    "--workspace-id",
    default=None,
    help="Workspace to look in (skips the cross-workspace scan).",
)
@click.pass_context
def task_status(
    ctx: click.Context,
    task_id: str,
    workspace_id: Optional[str],
) -> None:
    """Show Goal Packet, review, and acceptance state for a task."""
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
            fetched = client.get_task_reports(ws_id, task_id) if ws_id else []
    except HubError as e:
        raise click.ClickException(str(e)) from e
    reports: List[dict] = fetched if isinstance(fetched, list) else []
    payload = _task_status_payload(ws_id, match, reports)
    if cli_main.as_json(ctx):
        emit(payload, True)
    else:
        _print_task_status(payload)


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
@click.option("--related-task-id", default=None, help="Related task id.")
@click.option(
    "--parent-task-id",
    default=None,
    help="Parent Task id for an explicit Task Graph edge.",
)
@click.option("--session-id", default=None, help="Target existing session id.")
@click.option(
    "--clear-context/--no-clear-context",
    default=None,
    help="Clear agent context before starting (omitted unless set).",
)
@click.option(
    "--attachment-json",
    "attachment_json",
    multiple=True,
    help="Attachment JSON object (repeatable).",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
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
    related_task_id: Optional[str],
    parent_task_id: Optional[str],
    session_id: Optional[str],
    clear_context: Optional[bool],
    attachment_json: tuple,
    payload_json: Optional[str],
) -> None:
    """Create a task in a workspace."""
    body = merge_payload(
        payload_json,
        title=title,
        prompt=prompt,
        agent_type=agent_type,
        task_mode=task_mode,
        execution_complexity=execution_complexity,
        review_profiles=list(review_profiles),
        related_task_id=related_task_id,
        parent_task_id=parent_task_id,
        session_id=session_id,
        clear_context=clear_context,
    )
    if attachment_json:
        body["attachments"] = parse_attachment_json(attachment_json)
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
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_start(
    ctx: click.Context,
    task_id: str,
    agent_type: Optional[str],
    target_session_id: Optional[str],
    clear_context: Optional[bool],
    related_task_id: Optional[str],
    payload_json: Optional[str],
) -> None:
    """Queue / start a task."""
    body = merge_payload(
        payload_json,
        agent_type=agent_type,
        target_session_id=target_session_id,
        clear_context=clear_context,
        related_task_id=related_task_id,
    )
    try:
        with cli_main.get_client(ctx) as client:
            data = client.start_task(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("continue")
@click.argument("task_id")
@click.option("--message", default=None, help="Message to send when continuing.")
@click.option(
    "--attachment-json",
    "attachment_json",
    multiple=True,
    help="Attachment JSON object (repeatable).",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_continue(
    ctx: click.Context,
    task_id: str,
    message: Optional[str],
    attachment_json: tuple,
    payload_json: Optional[str],
) -> None:
    """Continue a task from review with its original agent."""
    body = merge_payload(payload_json, message=message)
    if attachment_json:
        body["attachments"] = parse_attachment_json(attachment_json)
    elif "attachments" not in body:
        body["attachments"] = []
    try:
        with cli_main.get_client(ctx) as client:
            data = client.continue_task(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("update")
@click.argument("task_id")
@click.option("--title", default=None, help="Task title.")
@click.option("--prompt", default=None, help="Task prompt.")
@click.option(
    "--status",
    type=click.Choice(["todo", "queued", "working", "review", "done"]),
    default=None,
    help="Task status.",
)
@click.option(
    "--task-mode",
    type=click.Choice(["direct", "reviewed", "autonomous"]),
    default=None,
    help="Task automation mode.",
)
@click.option(
    "--execution-complexity",
    type=click.Choice(["auto", "simple", "complex"]),
    default=None,
    help="Execution complexity hint.",
)
@click.option(
    "--review-profile",
    "review_profiles",
    multiple=True,
    type=click.Choice(["general", "code", "ui", "artifact", "delivery", "boundary"]),
    help="Review profile list (repeatable).",
)
@click.option("--related-task-id", default=None, help="Related task id.")
@click.option("--session-id", default=None, help="Session id.")
@click.option(
    "--clear-context/--no-clear-context",
    default=None,
    help="Clear agent context before starting (omitted unless set).",
)
@click.option(
    "--attachment-json",
    "attachment_json",
    multiple=True,
    help="Attachment JSON object to add (repeatable).",
)
@click.option(
    "--remove-attachment-id",
    "removed_attachment_ids",
    multiple=True,
    help="Attachment id to remove (repeatable).",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_update(
    ctx: click.Context,
    task_id: str,
    title: Optional[str],
    prompt: Optional[str],
    status: Optional[str],
    task_mode: Optional[str],
    execution_complexity: Optional[str],
    review_profiles: tuple,
    related_task_id: Optional[str],
    session_id: Optional[str],
    clear_context: Optional[bool],
    attachment_json: tuple,
    removed_attachment_ids: tuple,
    payload_json: Optional[str],
) -> None:
    """Update task metadata or status."""
    body = merge_payload(
        payload_json,
        title=title,
        prompt=prompt,
        status=status,
        task_mode=task_mode,
        execution_complexity=execution_complexity,
        related_task_id=related_task_id,
        session_id=session_id,
        clear_context=clear_context,
    )
    if review_profiles:
        body["review_profiles"] = list(review_profiles)
    if attachment_json:
        body["add_attachments"] = parse_attachment_json(attachment_json)
    if removed_attachment_ids:
        body["removed_attachment_ids"] = list(removed_attachment_ids)
    try:
        with cli_main.get_client(ctx) as client:
            data = client.update_task(task_id, body)
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


@task.command("tree")
@click.argument("workspace_id")
@click.argument("task_id", required=False)
@click.pass_context
def task_tree(ctx: click.Context, workspace_id: str, task_id: Optional[str]) -> None:
    """List top-level Tasks, or the subtree rooted at TASK_ID."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.list_task_tree(workspace_id, task_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    rows = data if isinstance(data, list) else []
    _emit_task_tree(rows, cli_main.as_json(ctx))


@task.command("events")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option("--since-sequence", default=0, type=int, show_default=True)
@click.option("--subtree/--no-subtree", default=False, show_default=True)
@click.pass_context
def task_events(
    ctx: click.Context,
    workspace_id: str,
    task_id: str,
    since_sequence: int,
    subtree: bool,
) -> None:
    """List Task mailbox events for ``task:<task_id>``."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_task_events(
                workspace_id,
                task_id,
                since_sequence=since_sequence,
                subtree=subtree,
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    rows = data if isinstance(data, list) else []
    _emit_task_events(rows, cli_main.as_json(ctx))


@task.command("wait")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option("--since-sequence", default=0, type=int, show_default=True)
@click.option("--subtree/--no-subtree", default=False, show_default=True)
@click.option("--timeout-seconds", default=30.0, type=float, show_default=True)
@click.option(
    "--ack",
    is_flag=True,
    default=False,
    help="After a non-empty flushed event list, POST /ack at max(sequence).",
)
@click.pass_context
def task_wait(
    ctx: click.Context,
    workspace_id: str,
    task_id: str,
    since_sequence: int,
    subtree: bool,
    timeout_seconds: float,
    ack: bool,
) -> None:
    """Wait on the Task mailbox for ``task:<task_id>``."""
    as_json = cli_main.as_json(ctx)
    try:
        with cli_main.get_client(ctx) as client:
            rows = client.wait_task_events(
                workspace_id,
                task_id,
                since_sequence=since_sequence,
                subtree=subtree,
                timeout_seconds=timeout_seconds,
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    events = rows if isinstance(rows, list) else []
    _emit_task_events(events, as_json)
    if not ack or not events:
        return
    sequences = [
        int(item["sequence"])
        for item in events
        if isinstance(item, dict) and item.get("sequence") is not None
    ]
    if not sequences:
        raise click.ClickException("wait events missing sequence; not acknowledging")
    max_sequence = max(sequences)
    try:
        with cli_main.get_client(ctx) as client:
            client.ack_task_events(workspace_id, task_id, max_sequence)
    except HubError as e:
        click.echo(ACK_FAILED_NOTE, err=True)
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps({"acked_sequence": max_sequence}), err=True)
    else:
        click.echo(f"acked sequence {max_sequence}")


@task.command("ack")
@click.argument("workspace_id")
@click.argument("task_id")
@click.argument("sequence", type=int)
@click.pass_context
def task_ack(
    ctx: click.Context,
    workspace_id: str,
    task_id: str,
    sequence: int,
) -> None:
    """ACK ``task:<task_id>`` at SEQUENCE."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.ack_task_events(
                workspace_id,
                task_id,
                sequence,
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("followup")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option("--message", required=True, help="Follow-up message for the Task inbox.")
@click.option("--call-id", default=None, help="Stable retry id. New UUID if omitted.")
@click.pass_context
def task_followup(
    ctx: click.Context,
    workspace_id: str,
    task_id: str,
    message: str,
    call_id: Optional[str],
) -> None:
    """POST Task followup with a durable call_id. Does not write AgentRun."""
    resolved = _resolve_call_id(call_id)
    _echo_call_id(resolved)
    try:
        with cli_main.get_client(ctx) as client:
            data = client.followup_task(
                workspace_id,
                task_id,
                {"message": message, "call_id": resolved},
            )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("send")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option("--message", required=True, help="Follow-up message for the Task inbox.")
@click.option("--call-id", default=None, help="Stable retry id. New UUID if omitted.")
@click.pass_context
def task_send(
    ctx: click.Context,
    workspace_id: str,
    task_id: str,
    message: str,
    call_id: Optional[str],
) -> None:
    """Compatibility alias for ``task followup``. Uses Task call_id, not session send."""
    ctx.forward(task_followup)


@task.command("abort")
@click.argument("task_id")
@click.option("--reason", required=True, help="Reason for aborting.")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_abort(
    ctx: click.Context,
    task_id: str,
    reason: str,
    payload_json: Optional[str],
) -> None:
    """Abort a task."""
    body = merge_payload(payload_json, reason=reason)
    try:
        with cli_main.get_client(ctx) as client:
            data = client.abort_task(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("request-review")
@click.argument("task_id")
@click.option("--message", default=None, help="Optional note for the reviewer.")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_request_review(
    ctx: click.Context,
    task_id: str,
    message: Optional[str],
    payload_json: Optional[str],
) -> None:
    """Manually request reviewer checks for a task."""
    body = merge_payload(payload_json, message=message)
    try:
        with cli_main.get_client(ctx) as client:
            data = client.request_task_review(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("delete")
@click.argument("task_id")
@click.pass_context
def task_delete(ctx: click.Context, task_id: str) -> None:
    """Delete a task and its reports."""
    try:
        with cli_main.get_client(ctx) as client:
            client.delete_task(task_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if cli_main.as_json(ctx):
        emit({"ok": True}, True)
    else:
        click.echo(f"deleted {task_id}")


@task.command("spawn")
@click.argument("task_id")
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "cursor", "terminal"]),
    default=None,
    help="Worker agent type.",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_spawn(
    ctx: click.Context,
    task_id: str,
    agent_type: Optional[str],
    payload_json: Optional[str],
) -> None:
    """Spawn a worker session for a task."""
    body = merge_payload(payload_json, agent_type=agent_type)
    try:
        with cli_main.get_client(ctx) as client:
            data = client.spawn_worker(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("dispatch-decision")
@click.argument("task_id")
@click.option("--target-session-id", required=True, help="Target orchestrator session.")
@click.option(
    "--clear-context/--no-clear-context",
    default=False,
    help="Clear target agent context before dispatching.",
)
@click.option("--reason", default=None, help="Decision reason.")
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_dispatch_decision(
    ctx: click.Context,
    task_id: str,
    target_session_id: str,
    clear_context: bool,
    reason: Optional[str],
    payload_json: Optional[str],
) -> None:
    """Apply a structured dispatcher decision."""
    body = merge_payload(
        payload_json,
        target_session_id=target_session_id,
        clear_context=clear_context,
        reason=reason,
    )
    try:
        with cli_main.get_client(ctx) as client:
            data = client.apply_dispatch_decision(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.group("feedback")
def task_feedback() -> None:
    """Manage task feedback evidence."""


@task_feedback.command("reap")
@click.argument("task_id")
@click.option("--source", default=None, help="Feedback source.")
@click.option("--summary", default=None, help="Feedback summary.")
@click.option("--tag", "tags", multiple=True, help="Feedback tag (repeatable).")
@click.option(
    "--lesson-draft-json",
    "lesson_drafts",
    multiple=True,
    help="Lesson draft JSON object (repeatable).",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_feedback_reap(
    ctx: click.Context,
    task_id: str,
    source: Optional[str],
    summary: Optional[str],
    tags: tuple,
    lesson_drafts: tuple,
    payload_json: Optional[str],
) -> None:
    """Manually collect feedback evidence and optional lesson drafts."""
    body = merge_payload(payload_json, source=source, summary=summary)
    if tags:
        body["tags"] = list(tags)
    if lesson_drafts:
        body["lesson_drafts"] = [
            parse_json_object(value, "--lesson-draft-json") for value in lesson_drafts
        ]
    try:
        with cli_main.get_client(ctx) as client:
            data = client.reap_task_feedback(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
