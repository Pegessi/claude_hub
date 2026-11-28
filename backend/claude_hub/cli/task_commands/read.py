"""Task listing, status, and O(1) lookup helpers."""

import sys
import uuid
from typing import Any, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import is_missing_route_404, resolve_workspace_id
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
    "model",
    "env_preset",
    "cwd",
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
REVIEW_STATES = {
    "review_started",
    "review_passed",
    "review_failed",
    "review_needs_input",
}


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


def _board_tasks(board: Any) -> List[dict]:
    """Board tasks with the server-derived lifecycle attached to each task."""
    if not isinstance(board, dict):
        return []
    lifecycles = board.get("task_lifecycles")
    lifecycles = lifecycles if isinstance(lifecycles, dict) else {}
    tasks: List[dict] = []
    for item in board.get("tasks", []):
        if not isinstance(item, dict):
            continue
        lifecycle = lifecycles.get(item.get("id"))
        if isinstance(lifecycle, dict):
            item = {
                **item,
                "lifecycle": lifecycle,
                "phase": lifecycle.get("phase"),
                "phase_reason": lifecycle.get("reason"),
                "blocked_by": lifecycle.get("blocked_by_task_id"),
            }
        tasks.append(item)
    return tasks


def _legacy_fetch_task_from_board(
    client: Any, task_id: str, workspace_id: Optional[str] = None
) -> tuple[str, dict]:
    """Fallback board scan when talking to an older Hub server without ``GET /tasks/{id}``."""
    boards: List[tuple[Optional[str], dict]] = []
    if workspace_id is not None:
        board = client.get_board(workspace_id)
        if isinstance(board, dict):
            ws = board.get("workspace")
            board_ws: Optional[str] = (
                str(ws.get("id")) if isinstance(ws, dict) and ws.get("id") else workspace_id
            )
            boards.append((board_ws, board))
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
                board = client.get_board(str(ws_id))
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
                        entry_ws: Optional[str] = (
                            str(ws.get("id")) if isinstance(ws, dict) and ws.get("id") else None
                        )
                        boards.append((entry_ws, entry))
    for ws_id, board in boards:
        for item in _board_tasks(board):
            if item.get("id") == task_id:
                effective_ws = str(item.get("workspace_id") or ws_id or "")
                return effective_ws, item
    where = f" in workspace {workspace_id}" if workspace_id else ""
    raise click.ClickException(f"Task {task_id} not found{where}.")


def _fetch_task_dict(
    client: Any, task_id: str, workspace_id: Optional[str] = None
) -> tuple[str, dict]:
    """Fetch ``(workspace_id, task_dict)`` in O(1) via ``GET /api/workspaces/tasks/{task_id}``."""
    try:
        fetched = client.get_task(task_id)
    except HubError as e:
        if is_missing_route_404(e):
            return _legacy_fetch_task_from_board(client, task_id, workspace_id)
        if e.status == 404:
            where = f" in workspace {workspace_id}" if workspace_id else ""
            raise click.ClickException(f"Task {task_id} not found{where}.") from e
        raise
    if not isinstance(fetched, dict) or not fetched.get("workspace_id"):
        raise click.ClickException(f"Task {task_id} not found.")
    ws_id = str(fetched["workspace_id"])
    if workspace_id is not None and ws_id != workspace_id:
        raise click.ClickException(f"Task {task_id} not found in workspace {workspace_id}.")
    return ws_id, fetched


def _find_task_board(client: Any, task_id: str) -> tuple[Optional[str], Optional[dict]]:
    """Locate ``task_id`` in O(1), returning ``(workspace_id, task_dict)`` or ``(None, None)``."""
    try:
        return _fetch_task_dict(client, task_id)
    except click.ClickException:
        return None, None


def _resolve_ws_id(client: Any, task_id: str, workspace_id: Optional[str] = None) -> str:
    """Return verified ``workspace_id`` for ``task_id`` in O(1)."""
    ws_id, _ = _fetch_task_dict(client, task_id, workspace_id)
    return ws_id


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
    workspace_id: Optional[str],
    task_obj: dict,
    reports: List[dict],
    *,
    reports_limit: int = 0,
    brief_markdown: Optional[str] = None,
) -> Dict[str, Any]:
    latest = _latest_report(reports)
    acceptance_report = _latest_acceptance_report(reports)
    review_reports = [report for report in reports if report.get("state") in REVIEW_STATES]
    latest_review = review_reports[-1] if review_reports else None
    latest_acceptance = _acceptance_check_items(acceptance_report)
    newest_first = list(reversed(reports))
    selected = newest_first[:reports_limit] if reports_limit > 0 else []
    return {
        "workspace_id": workspace_id,
        "task": task_obj,
        "lifecycle": task_obj.get("lifecycle"),
        "goal_packet": task_obj.get("goal_packet"),
        "latest_report": latest,
        "latest_report_message": _report_message(latest),
        "latest_acceptance_report": acceptance_report,
        "latest_acceptance_check": latest_acceptance,
        "latest_review": latest_review,
        "review_reports": review_reports,
        "reports": selected,
        "reports_meta": {
            "total": len(reports),
            "returned": len(selected),
            "truncated": len(reports) > len(selected),
        },
        "brief_markdown": brief_markdown,
        "human_acceptance_requested_at": task_obj.get("human_acceptance_requested_at"),
        "human_accepted_at": task_obj.get("human_accepted_at"),
    }


def _print_task_status(payload: Dict[str, Any], *, show_reports: bool = False) -> None:
    task_obj = payload.get("task")
    task_dict: Dict[str, Any] = task_obj if isinstance(task_obj, dict) else {}
    detail: Dict[str, Any] = {"workspace_id": payload.get("workspace_id")}
    detail.update(
        {
            field: task_dict.get(field)
            for field in TASK_STATUS_FIELDS
            if field in task_dict and task_dict.get(field) is not None
        }
    )
    click.echo(f"Task: {task_dict.get('title', '(untitled)')} ({task_dict.get('id', '?')})")
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

    if show_reports:
        meta = payload.get("reports_meta") or {}
        click.echo(f"\nRecent reports ({meta.get('returned', 0)}/{meta.get('total', 0)}):")
        print_rows(
            payload.get("reports", []),
            ["created_at", "state", "review_decision", "session_id", "message"],
        )


@task.command("list")
@click.argument("workspace_id", required=False, default=None)
@click.option(
    "--all-workspaces",
    is_flag=True,
    help="List tasks across every Workspace (all open tasks + recent done).",
)
@click.option(
    "--tree",
    "tree_view",
    is_flag=True,
    default=False,
    help="List top-level Tasks in tree view (or subtree under --root).",
)
@click.option(
    "--root",
    "root_task_id",
    default=None,
    help="Root Task ID for --tree subtree.",
)
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
    workspace_id: Optional[str],
    all_workspaces: bool,
    tree_view: bool,
    root_task_id: Optional[str],
    status: Optional[str],
    phase: Optional[str],
    attention: bool,
) -> None:
    """List tasks for a workspace (defaults to cwd match), --all-workspaces, or --tree."""
    if all_workspaces and (workspace_id is not None or tree_view or root_task_id is not None):
        raise click.ClickException(
            "Cannot combine --all-workspaces with WORKSPACE_ID, --tree, or --root."
        )
    try:
        with cli_main.get_client(ctx) as client:
            if tree_view or root_task_id is not None:
                if root_task_id is not None and workspace_id is None:
                    resolved_ws = _resolve_ws_id(client, root_task_id, None)
                else:
                    resolved_ws = resolve_workspace_id(client, workspace_id)
                data = client.list_task_tree(resolved_ws, root_task_id)
                rows = data if isinstance(data, list) else []
                if status is not None:
                    rows = [t for t in rows if t.get("status") == status]
                _emit_task_tree(rows, cli_main.as_json(ctx))
                return

            if all_workspaces:
                board = client.get_board_overview()
            else:
                resolved_ws = resolve_workspace_id(client, workspace_id)
                board = client.get_board(resolved_ws)
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


@task.command("status")
@click.argument("task_id")
@click.option(
    "--reports",
    "reports_limit",
    type=click.IntRange(0, 200),
    default=0,
    show_default=True,
    help="Include up to N recent historical reports (default 0 keeps output bounded).",
)
@click.option(
    "--brief",
    is_flag=True,
    default=False,
    help="Include bounded (<2 KB) AI handoff brief (prints markdown in text mode).",
)
@click.option(
    "--workspace-id",
    default=None,
    help="Optional workspace id assertion.",
)
@click.pass_context
def task_status(
    ctx: click.Context,
    task_id: str,
    reports_limit: int,
    brief: bool,
    workspace_id: Optional[str],
) -> None:
    """Show unified Goal Packet, lifecycle, review, acceptance, and optional reports/brief."""
    payload: Optional[Dict[str, Any]] = None
    try:
        with cli_main.get_client(ctx) as client:
            try:
                fetched = client.get_task_status(
                    task_id,
                    reports_limit=reports_limit,
                    include_brief=brief,
                )
                if isinstance(fetched, dict):
                    payload = fetched
            except HubError as e:
                if not is_missing_route_404(e):
                    raise
                ws_id, task_obj = _fetch_task_dict(client, task_id, workspace_id)
                raw_reports = client.get_task_reports(ws_id, task_id)
                reports = (
                    [r for r in raw_reports if isinstance(r, dict)]
                    if isinstance(raw_reports, list)
                    else []
                )
                brief_md: Optional[str] = None
                if brief:
                    brief_resp = client.get_task_brief(ws_id, task_id)
                    if isinstance(brief_resp, dict):
                        raw_md = (
                            brief_resp.get("markdown")
                            if "markdown" in brief_resp
                            else brief_resp.get("brief")
                        )
                        brief_md = str(raw_md or "")
                payload = _task_status_payload(
                    ws_id,
                    task_obj,
                    reports,
                    reports_limit=reports_limit,
                    brief_markdown=brief_md,
                )
    except HubError as e:
        if e.status == 404:
            where = f" in workspace {workspace_id}" if workspace_id else ""
            raise click.ClickException(f"Task {task_id} not found{where}.") from e
        raise click.ClickException(str(e)) from e

    if not isinstance(payload, dict):
        raise click.ClickException(f"Task {task_id} not found.")
    if workspace_id is not None and payload.get("workspace_id") != workspace_id:
        raise click.ClickException(f"Task {task_id} not found in workspace {workspace_id}.")

    if cli_main.as_json(ctx):
        emit(payload, True)
        return
    if brief:
        click.echo(str(payload.get("brief_markdown") or ""))
        return
    _print_task_status(payload, show_reports=reports_limit > 0)
