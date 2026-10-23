"""Task listing, detail, status, and review commands."""

import sys
import uuid
from typing import Any, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.output import emit, print_rows
from claude_hub.cli.task_commands.group import task

TASK_COLUMNS = [
    "id",
    "title",
    "status",
    "phase",
    "phase_reason",
    "blocked_by",
    "agent_type",
    "task_mode",
    "execution_control",
    "execution_epoch",
    "progress_revision",
    "execution_released",
]
TASK_TREE_COLUMNS = ["id", "title", "status", "parent_task_id", "agent_type"]
TASK_EVENT_COLUMNS = ["sequence", "type", "call_id", "task_id", "consumer_key"]
ACK_FAILED_NOTE = "events delivered but ACK failed/not acknowledged"
TASK_STATUS_FIELDS = [
    "workspace_id",
    "id",
    "title",
    "status",
    "phase",
    "phase_reason",
    "blocked_by",
    "agent_type",
    "task_mode",
    "execution_complexity",
    "depends_on_task_ids",
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
    "execution_control",
    "execution_epoch",
    "progress_revision",
    "execution_released",
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


def _board_tasks(board: Any) -> List[dict]:
    """Board tasks with the server-derived lifecycle attached to each task."""
    if not isinstance(board, dict):
        return []
    lifecycles = board.get("task_lifecycles")
    lifecycles = lifecycles if isinstance(lifecycles, dict) else {}
    tasks: List[dict] = []
    for task in board.get("tasks", []):
        if not isinstance(task, dict):
            continue
        lifecycle = lifecycles.get(task.get("id"))
        if isinstance(lifecycle, dict):
            task = {
                **task,
                "lifecycle": lifecycle,
                "phase": lifecycle.get("phase"),
                "phase_reason": lifecycle.get("reason"),
                "blocked_by": lifecycle.get("blocked_by_task_id"),
            }
        tasks.append(task)
    return tasks


@task.command("list")
@click.argument("workspace_id")
@click.option("--status", default=None, help="Client-side filter on task status.")
@click.option("--phase", default=None, help="Client-side filter on lifecycle phase.")
@click.option(
    "--attention",
    is_flag=True,
    help="Only tasks that need a human: needs_user, awaiting_acceptance or orphaned.",
)
@click.pass_context
def task_list(
    ctx: click.Context,
    workspace_id: str,
    status: Optional[str],
    phase: Optional[str],
    attention: bool,
) -> None:
    """List tasks for a workspace with their lifecycle phase and owner."""
    try:
        with cli_main.get_client(ctx) as client:
            board = client.get_board(workspace_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    tasks = _board_tasks(board)
    if (phase is not None or attention) and not (
        isinstance(board, dict) and "task_lifecycles" in board
    ):
        click.echo(
            "warning: this Hub does not report task lifecycles; "
            "--phase/--attention cannot filter.",
            err=True,
        )
    if status is not None:
        tasks = [t for t in tasks if t.get("status") == status]
    if phase is not None:
        tasks = [t for t in tasks if t.get("phase") == phase]
    if attention:
        tasks = [t for t in tasks if (t.get("lifecycle") or {}).get("attention")]
    if cli_main.as_json(ctx):
        emit(tasks, True)
    else:
        print_rows(tasks, TASK_COLUMNS)


TASK_DETAIL_FIELDS = [
    "id",
    "title",
    "status",
    "phase",
    "phase_reason",
    "blocked_by",
    "agent_type",
    "task_mode",
    "execution_complexity",
    "depends_on_task_ids",
    "session_id",
    "review_cycle",
    "reviewed_cycle",
    "review_attempts",
    "created_at",
    "updated_at",
    "prompt",
    "execution_control",
    "execution_epoch",
    "progress_revision",
    "execution_released",
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
        tasks = _board_tasks(board)
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
        "lifecycle": task.get("lifecycle"),
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
                tasks = _board_tasks(board)
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
                tasks = _board_tasks(board)
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
