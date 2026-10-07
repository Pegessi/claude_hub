"""``task`` command group."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import (
    lifecycle_group_help,
    merge_payload,
    parse_attachment_json,
    parse_json_object,
)
from claude_hub.cli.output import emit, print_rows
from claude_hub.models.schemas import WorkspaceTaskStatus

TASK_COLUMNS = [
    "id",
    "title",
    "status",
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


@click.group(help=lifecycle_group_help("Manage workspace tasks."))
def task() -> None:
    pass


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
    type=click.Choice(["direct", "reviewed", "autonomous", "subagent"]),
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
@click.option(
    "--depends-on", multiple=True, help="Prerequisite task id (repeatable; requires done)."
)
@click.option("--agent-tag", default=None, help="Optional agent label tag.")
@click.option("--session-id", default=None, help="Target existing session id.")
@click.option(
    "--clear-context/--no-clear-context",
    default=None,
    help="Clear agent context before starting (omitted unless set).",
)
@click.option(
    "--timeout-seconds",
    type=int,
    default=None,
    help="Task timeout in seconds (omitted unless set).",
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
    depends_on: tuple,
    agent_tag: Optional[str],
    session_id: Optional[str],
    clear_context: Optional[bool],
    timeout_seconds: Optional[int],
    attachment_json: tuple,
    payload_json: Optional[str],
) -> None:
    """Create a workspace-controlled Task record without starting it."""
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
        agent_tag=agent_tag,
        session_id=session_id,
        clear_context=clear_context,
        timeout_seconds=timeout_seconds,
    )
    if depends_on:
        body["depends_on_task_ids"] = list(depends_on)
    if attachment_json:
        body["attachments"] = parse_attachment_json(attachment_json)
    _reject_secret_task_fields(body)
    if body.get("execution_control", "workspace") != "workspace":
        raise click.ClickException(
            "task create records workspace-controlled work; use task register for initiator work."
        )
    body["execution_control"] = "workspace"
    try:
        with cli_main.get_client(ctx) as client:
            data = client.create_task(workspace_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("run")
@click.argument("workspace_id")
@click.option("--title", required=True, help="Task title.")
@click.option("--prompt", required=True, help="Task prompt.")
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "cursor", "terminal"]),
    default="claude",
    help="Agent type (default: claude).",
)
@click.option(
    "--task-mode",
    type=click.Choice(["subagent", "reviewed", "autonomous"]),
    default="subagent",
    help="Task mode (default: subagent). Use subagent for simple tasks you judge "
    "yourself; reviewed for complex tasks needing AI review; autonomous for "
    "multi-iteration self-driving work.",
)
@click.option(
    "--execution-complexity",
    type=click.Choice(["auto", "simple", "complex"]),
    default="auto",
    help="Execution complexity hint.",
)
@click.option("--cwd", default=None, help="Agent working directory (defaults to workspace path).")
@click.option(
    "--env-preset",
    default=None,
    help="Env preset by id or name. Defaults to per-agent-type preset from config, "
    "then global default_env_preset.",
)
@click.option("--env", "env_values", multiple=True, help="Environment variable KEY=VALUE.")
@click.option(
    "--clear-context/--no-clear-context",
    default=None,
    help="Clear agent context before starting.",
)
@click.option("--related-task-id", default=None, help="Related task id.")
@click.option("--parent-task-id", default=None, help="Parent task id.")
@click.option(
    "--timeout-seconds",
    type=int,
    default=1800,
    show_default=True,
    help="Task timeout in seconds.",
)
@click.option(
    "--attachment-json",
    "attachment_json",
    multiple=True,
    help="Attachment JSON object (repeatable).",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_run(
    ctx: click.Context,
    workspace_id: str,
    title: str,
    prompt: str,
    agent_type: str,
    task_mode: str,
    execution_complexity: str,
    cwd: Optional[str],
    env_preset: Optional[str],
    env_values: tuple,
    clear_context: Optional[bool],
    related_task_id: Optional[str],
    parent_task_id: Optional[str],
    timeout_seconds: int,
    attachment_json: tuple,
    payload_json: Optional[str],
) -> None:
    """Create and start a task in one step.

    Ensures a compatible agent (reusing an idle one if available), creates the
    task, and dispatches it. Defaults to subagent mode for agent-to-agent
    delegation.
    """
    from claude_hub.cli.commands.common import (
        parse_kv_pairs,
        resolve_cli_local_path,
    )

    settings = ctx.obj
    resolved_preset = (
        env_preset
        or settings.env_preset_for_agent_type(agent_type)
        or getattr(settings, "default_env_preset", None)
    )
    resolved_cwd = resolve_cli_local_path(cwd) if cwd is not None else None

    raw_payload = parse_json_object(payload_json)
    _reject_secret_task_fields(raw_payload)
    if raw_payload.get("execution_control", "workspace") != "workspace":
        raise click.ClickException(
            "task run only dispatches workspace-controlled work; use task register for initiator work."
        )
    try:
        with cli_main.get_client(ctx) as client:
            # 1. Ensure an agent (reuse compatible idle orchestrator if available).
            agent_body: Dict[str, Any] = {
                "agent_type": agent_type,
                "reuse_existing": True,
                "role": "orchestrator",
            }
            if resolved_cwd is not None:
                agent_body["cwd"] = resolved_cwd
            if resolved_preset is not None:
                agent_body["env_preset"] = resolved_preset
            if env_values:
                agent_body["env"] = parse_kv_pairs(env_values, "--env")
            agent = client.ensure_agent(workspace_id, agent_body)
            session_id = agent["id"]

            # 2. Create the task assigned to that session.
            task_body = merge_payload(
                payload_json,
                title=title,
                prompt=prompt,
                agent_type=agent_type,
                task_mode=task_mode,
                execution_complexity=execution_complexity,
                related_task_id=related_task_id,
                parent_task_id=parent_task_id,
                session_id=session_id,
                clear_context=clear_context,
                timeout_seconds=timeout_seconds,
            )
            if attachment_json:
                task_body["attachments"] = parse_attachment_json(attachment_json)
            task_body["execution_control"] = "workspace"
            task = client.create_task(workspace_id, task_body)
            task_id = task["id"]

            # 3. Start the task.
            start_body: Dict[str, Any] = {"target_session_id": session_id}
            if clear_context is not None:
                start_body["clear_context"] = clear_context
            if related_task_id:
                start_body["related_task_id"] = related_task_id
            started = client.start_task(task_id, start_body)
    except HubError as e:
        raise click.ClickException(str(e)) from e

    result = {
        "task_id": task_id,
        "session_id": session_id,
        "status": started.get("status"),
    }
    emit(result, cli_main.as_json(ctx))


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
    """Compatibility alias for explicit execution; prefer task dispatch."""
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
    type=click.Choice(["todo", "queued", "working", "review", "done", "failed"]),
    default=None,
    help="Task status.",
)
@click.option(
    "--task-mode",
    type=click.Choice(["direct", "reviewed", "autonomous", "subagent"]),
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
@click.option(
    "--depends-on", multiple=True, help="Replace prerequisite ids (repeatable; todo only)."
)
@click.option("--clear-dependencies", is_flag=True, help="Clear all prerequisites (todo only).")
@click.option("--agent-tag", default=None, help="Agent label tag (empty string clears).")
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
    depends_on: tuple,
    clear_dependencies: bool,
    agent_tag: Optional[str],
    session_id: Optional[str],
    clear_context: Optional[bool],
    attachment_json: tuple,
    removed_attachment_ids: tuple,
    payload_json: Optional[str],
) -> None:
    """Update task metadata or status."""
    if depends_on and clear_dependencies:
        raise click.UsageError("--depends-on and --clear-dependencies are mutually exclusive")
    body = merge_payload(
        payload_json,
        title=title,
        prompt=prompt,
        status=status,
        task_mode=task_mode,
        execution_complexity=execution_complexity,
        related_task_id=related_task_id,
        agent_tag=agent_tag,
        session_id=session_id,
        clear_context=clear_context,
    )
    if depends_on or clear_dependencies:
        body["depends_on_task_ids"] = list(depends_on)
    if review_profiles:
        body["review_profiles"] = list(review_profiles)
    if attachment_json:
        body["add_attachments"] = parse_attachment_json(attachment_json)
    if removed_attachment_ids:
        body["removed_attachment_ids"] = list(removed_attachment_ids)
    _reject_task_execution_fields(body)
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
@click.option(
    "--cleanup-session",
    is_flag=True,
    default=False,
    help="After marking done, run safe task cleanup for caller-owned ephemeral sessions.",
)
@click.pass_context
def task_accept(
    ctx: click.Context,
    task_id: str,
    workspace_id: Optional[str],
    message: Optional[str],
    cleanup_session: bool,
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
            if status not in (WorkspaceTaskStatus.REVIEW.value, WorkspaceTaskStatus.FAILED.value):
                raise click.ClickException(
                    f"Task {task_id} is '{status}', not 'review' or 'failed'; "
                    "only tasks in review or failed can be accepted."
                )
            if status == WorkspaceTaskStatus.REVIEW.value and not match.get(
                "human_acceptance_requested_at"
            ):
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
            if cleanup_session:
                cleanup = client.cleanup_task(task_id)
                if cli_main.as_json(ctx):
                    emit({"task": data, "cleanup": cleanup}, True)
                    return
                click.echo(
                    f"cleanup: {cleanup.get('action')} {cleanup.get('reason') or ''}".strip()
                )
                emit(data, False)
                return
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("cleanup")
@click.argument("task_id")
@click.pass_context
def task_cleanup(ctx: click.Context, task_id: str) -> None:
    """Safely delete a caller-owned ephemeral session after a terminal task."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.cleanup_task(task_id)
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
    """POST Task followup with a durable call_id on the Task Graph surface."""
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


_SECRET_TASK_FIELDS = {"reporter_key", "new_reporter_key"}


def _reject_secret_task_fields(body: Dict[str, Any]) -> None:
    if _SECRET_TASK_FIELDS & set(body):
        raise click.ClickException(
            "Reporter keys must be supplied through the dedicated key-file option."
        )


def _create_reporter_key(path: Path) -> str:
    key = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise click.ClickException(
            f"Reporter key file already exists: {path}. Inspect the existing Task before retrying; "
            "use the explicit reuse flag only for the same logical request."
        ) from exc
    except OSError as exc:
        raise click.ClickException(f"Cannot create reporter key file: {path}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise click.ClickException(
            f"Cannot persist reporter key file directory: {path.parent}"
        ) from exc
    return key


def _prepare_reporter_key(path: Path, reuse: bool) -> str:
    return _read_reporter_key(path) if reuse else _create_reporter_key(path)


def _registration_request_key(reporter_key: str) -> str:
    return hashlib.sha256(
        b"claude-hub-task-register-v1\0" + reporter_key.encode("utf-8")
    ).hexdigest()


def _source_reference(
    kind: Optional[str], tab_id: Optional[str], agent_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    if kind is None:
        if tab_id is not None or agent_id is not None:
            raise click.UsageError("--source-tab-id/--source-agent-id require --source-kind")
        return None
    source: Dict[str, Any] = {"kind": kind}
    if tab_id is not None:
        source["tab_id"] = tab_id
    if agent_id is not None:
        source["agent_id"] = agent_id
    return source


def _execution_reference(
    provider: Optional[str],
    session_id: Optional[str],
    thread_id: Optional[str],
    turn_id: Optional[str],
    run_epoch: Optional[int],
) -> Optional[Dict[str, Any]]:
    values = {
        "provider": provider,
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "run_epoch": run_epoch,
    }
    result = {key: value for key, value in values.items() if value is not None}
    return result or None


def _action_summary(data: Any, *, reporter_key_file: Path | None = None) -> Dict[str, Any]:
    envelope = data if isinstance(data, dict) else {}
    task_obj = envelope.get("task", envelope)
    task = task_obj if isinstance(task_obj, dict) else {}
    event_obj = envelope.get("event")
    event = event_obj if isinstance(event_obj, dict) else {}
    event_payload_obj = event.get("payload")
    event_payload = event_payload_obj if isinstance(event_payload_obj, dict) else {}
    result: Dict[str, Any] = {
        "workspace_id": task.get("workspace_id"),
        "task_id": task.get("id"),
        "status": task.get("status"),
        "execution_control": task.get("execution_control"),
        "execution_epoch": task.get("execution_epoch"),
        "progress_revision": task.get("progress_revision"),
    }
    if event_payload.get("state") is not None:
        result["progress_state"] = event_payload["state"]
    if "replayed" in envelope:
        result["replayed"] = bool(envelope["replayed"])
    if reporter_key_file is not None:
        result["reporter_key_file"] = str(reporter_key_file)
    return result


_REPORTER_KEY_MIN_BYTES = 32
_REPORTER_KEY_MAX_BYTES = 256


def _read_reporter_key(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise click.ClickException(
                    "Reporter key file must be a regular file with mode 0600."
                )
            raw = os.read(descriptor, _REPORTER_KEY_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
    except click.ClickException:
        raise
    except OSError as exc:
        raise click.ClickException(f"Cannot read reporter key file: {path}") from exc
    if not (_REPORTER_KEY_MIN_BYTES <= len(raw) <= _REPORTER_KEY_MAX_BYTES):
        raise click.ClickException("Reporter key file is invalid.")
    if any(byte < 33 or byte > 126 for byte in raw):
        raise click.ClickException("Reporter key file is invalid.")
    return raw.decode("ascii")


_SAFE_TASK_ERROR_CODES = {"task_create_request_key_conflict"}


def _safe_task_operation_error(operation: str, exc: HubError) -> click.ClickException:
    if exc.message in _SAFE_TASK_ERROR_CODES:
        return click.ClickException(exc.message)
    by_status = {
        400: f"invalid_task_{operation}",
        403: f"task_{operation}_forbidden",
        404: "task_not_found",
        409: f"task_{operation}_conflict",
        422: f"invalid_task_{operation}",
    }
    code = by_status.get(exc.status) if exc.status is not None else None
    return click.ClickException(code or f"task_{operation}_failed")


_TASK_EXECUTION_FIELDS = {
    "execution_control",
    "execution_epoch",
    "progress_revision",
    "execution_released",
    "latest_progress",
    "reporter_key",
    "new_reporter_key",
}


def _reject_task_execution_fields(body: Dict[str, Any]) -> None:
    if _TASK_EXECUTION_FIELDS & set(body):
        raise click.ClickException(
            "Execution control and progress use task handoff/progress, not task update."
        )


def _task_in_board(board: Any, task_id: str) -> Optional[dict]:
    tasks = board.get("tasks", []) if isinstance(board, dict) else []
    return next(
        (item for item in tasks if isinstance(item, dict) and item.get("id") == task_id),
        None,
    )


def _context_summary(
    workspace_id: str, task_value: Optional[dict], capabilities: Any
) -> Dict[str, Any]:
    task = task_value or {}
    caps = capabilities if isinstance(capabilities, dict) else {}
    return {
        "workspace_id": workspace_id,
        "task_id": task.get("id"),
        "status": task.get("status"),
        "task_mode": task.get("task_mode"),
        "execution_control": task.get("execution_control"),
        "execution_epoch": task.get("execution_epoch"),
        "progress_revision": task.get("progress_revision"),
        "execution_released": task.get("execution_released"),
        "supported_execution_controls": caps.get("supported_execution_controls", []),
        "progress_states": caps.get("progress_states", []),
        "record_only_requires_reporter_key": caps.get("record_only_requires_reporter_key"),
        "handoff_requires_release": caps.get("handoff_requires_release"),
        "legacy_chat_work_create": caps.get("legacy_chat_work_create"),
    }


def _execution_ref_options(function: Callable[..., Any]) -> Callable[..., Any]:
    options = (
        click.option("--execution-run-epoch", type=int, default=None),
        click.option("--execution-turn-id", default=None),
        click.option("--execution-thread-id", default=None),
        click.option("--execution-session-id", default=None),
        click.option("--execution-provider", default=None),
    )
    for option in reversed(options):
        function = option(function)
    return function


@task.command("register")
@click.argument("workspace_id")
@click.option("--title", required=True, help="Independent Task goal title.")
@click.option("--prompt", required=True, help="Task goal and acceptance context.")
@click.option(
    "--reporter-key-file",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="New private file used for this Task's progress credential.",
)
@click.option(
    "--reuse-reporter-key-file",
    is_flag=True,
    help="Reuse the existing private file only when retrying the same registration.",
)
@click.option("--source-kind", type=click.Choice(["human", "chat", "agent"]), default=None)
@click.option("--source-tab-id", default=None)
@click.option("--source-agent-id", default=None)
@click.option("--parent-task-id", default=None)
@click.option("--depends-on", multiple=True)
@click.option(
    "--execution-complexity",
    type=click.Choice(["auto", "simple", "complex"]),
    default="auto",
)
@_execution_ref_options
@click.pass_context
def task_register(
    ctx: click.Context,
    workspace_id: str,
    title: str,
    prompt: str,
    reporter_key_file: Path,
    reuse_reporter_key_file: bool,
    source_kind: Optional[str],
    source_tab_id: Optional[str],
    source_agent_id: Optional[str],
    parent_task_id: Optional[str],
    depends_on: tuple,
    execution_complexity: str,
    execution_provider: Optional[str],
    execution_session_id: Optional[str],
    execution_thread_id: Optional[str],
    execution_turn_id: Optional[str],
    execution_run_epoch: Optional[int],
) -> None:
    """Register initiator-managed work without dispatching a Workspace agent.

    Run task context first and pass any execution reference explicitly;
    retries must repeat the same values.
    """
    source = _source_reference(source_kind, source_tab_id, source_agent_id)
    execution_ref = _execution_reference(
        execution_provider,
        execution_session_id,
        execution_thread_id,
        execution_turn_id,
        execution_run_epoch,
    )
    body: Dict[str, Any] = {
        "title": title,
        "prompt": prompt,
        "execution_control": "initiator",
        "execution_complexity": execution_complexity,
    }
    if source is not None:
        body["source"] = source
    if execution_ref is not None:
        body["execution_ref"] = execution_ref
    if parent_task_id is not None:
        body["parent_task_id"] = parent_task_id
    if depends_on:
        body["depends_on_task_ids"] = list(depends_on)
    try:
        with cli_main.get_client(ctx) as client:
            try:
                capabilities = client.get_task_capabilities(workspace_id)
            except HubError as exc:
                raise click.ClickException("initiator_task_registration_unavailable") from exc
            controls = (
                capabilities.get("supported_execution_controls", [])
                if isinstance(capabilities, dict)
                else []
            )
            if "initiator" not in controls:
                raise click.ClickException("initiator_task_registration_unavailable")
            reporter_key = _prepare_reporter_key(reporter_key_file, reuse_reporter_key_file)
            body["reporter_key"] = reporter_key
            body["request_key"] = _registration_request_key(reporter_key)
            data = client.register_task(workspace_id, body)
    except HubError as exc:
        raise _safe_task_operation_error("register", exc) from exc
    emit(
        _action_summary(data, reporter_key_file=reporter_key_file),
        cli_main.as_json(ctx),
    )


@task.command("progress")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option(
    "--reporter-key-file",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
)
@click.option(
    "--state",
    required=True,
    type=click.Choice(
        ["started", "working", "blocked", "needs_input", "completed", "failed", "released"]
    ),
)
@click.option("--summary", required=True)
@click.option("--expected-execution-epoch", required=True, type=click.IntRange(min=1))
@click.option("--expected-progress-revision", required=True, type=click.IntRange(min=0))
@click.option("--call-id", default=None, help="Stable retry id; a UUID is generated if omitted.")
@click.option("--validation", default=None)
@click.option("--risks", default=None)
@click.option("--artifact-ref", "artifact_refs", multiple=True)
@_execution_ref_options
@click.pass_context
def task_progress(
    ctx: click.Context,
    workspace_id: str,
    task_id: str,
    reporter_key_file: Path,
    state: str,
    summary: str,
    expected_execution_epoch: int,
    expected_progress_revision: int,
    call_id: Optional[str],
    validation: Optional[str],
    risks: Optional[str],
    artifact_refs: tuple,
    execution_provider: Optional[str],
    execution_session_id: Optional[str],
    execution_thread_id: Optional[str],
    execution_turn_id: Optional[str],
    execution_run_epoch: Optional[int],
) -> None:
    """Record initiator progress; never dispatch or use managed-session reports.

    Run task context first and pass any execution reference explicitly;
    retries must repeat the same values.
    """
    reporter_key = _read_reporter_key(reporter_key_file)
    resolved_call_id = _resolve_call_id(call_id)
    _echo_call_id(resolved_call_id)
    body: Dict[str, Any] = {
        "call_id": resolved_call_id,
        "expected_execution_epoch": expected_execution_epoch,
        "expected_progress_revision": expected_progress_revision,
        "state": state,
        "summary": summary,
        "artifact_refs": list(artifact_refs),
    }
    if validation is not None:
        body["validation"] = validation
    if risks is not None:
        body["risks"] = risks
    execution_ref = _execution_reference(
        execution_provider,
        execution_session_id,
        execution_thread_id,
        execution_turn_id,
        execution_run_epoch,
    )
    if execution_ref is not None:
        body["execution_ref"] = execution_ref
    try:
        with cli_main.get_client(ctx) as client:
            data = client.record_task_progress(workspace_id, task_id, body, reporter_key)
    except HubError as exc:
        raise _safe_task_operation_error("progress", exc) from exc
    emit(_action_summary(data), cli_main.as_json(ctx))


@task.command("handoff")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option(
    "--execution-control",
    required=True,
    type=click.Choice(["workspace", "initiator"]),
)
@click.option("--expected-execution-epoch", required=True, type=click.IntRange(min=1))
@click.option("--expected-progress-revision", required=True, type=click.IntRange(min=0))
@click.option("--call-id", default=None, help="Stable retry id; a UUID is generated if omitted.")
@click.option(
    "--new-reporter-key-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="New private credential file required when handing to an initiator.",
)
@click.option(
    "--reuse-new-reporter-key-file",
    is_flag=True,
    help="Reuse that private file only when retrying the same handoff call_id.",
)
@_execution_ref_options
@click.pass_context
def task_handoff(
    ctx: click.Context,
    workspace_id: str,
    task_id: str,
    execution_control: str,
    expected_execution_epoch: int,
    expected_progress_revision: int,
    call_id: Optional[str],
    new_reporter_key_file: Optional[Path],
    reuse_new_reporter_key_file: bool,
    execution_provider: Optional[str],
    execution_session_id: Optional[str],
    execution_thread_id: Optional[str],
    execution_turn_id: Optional[str],
    execution_run_epoch: Optional[int],
) -> None:
    """Transfer the same Task without starting its next executor."""
    if execution_control == "initiator" and new_reporter_key_file is None:
        raise click.UsageError("--new-reporter-key-file is required for initiator handoff")
    if execution_control == "workspace" and (
        new_reporter_key_file is not None or reuse_new_reporter_key_file
    ):
        raise click.UsageError("Reporter key options are only valid for initiator handoff")
    if reuse_new_reporter_key_file and new_reporter_key_file is None:
        raise click.UsageError("--reuse-new-reporter-key-file requires --new-reporter-key-file")
    if reuse_new_reporter_key_file and call_id is None:
        raise click.UsageError("--reuse-new-reporter-key-file requires an explicit --call-id")
    resolved_call_id = _resolve_call_id(call_id)
    _echo_call_id(resolved_call_id)
    body: Dict[str, Any] = {
        "call_id": resolved_call_id,
        "expected_execution_epoch": expected_execution_epoch,
        "expected_progress_revision": expected_progress_revision,
        "execution_control": execution_control,
    }
    if new_reporter_key_file is not None:
        body["new_reporter_key"] = _prepare_reporter_key(
            new_reporter_key_file, reuse_new_reporter_key_file
        )
    execution_ref = _execution_reference(
        execution_provider,
        execution_session_id,
        execution_thread_id,
        execution_turn_id,
        execution_run_epoch,
    )
    if execution_ref is not None:
        body["execution_ref"] = execution_ref
    try:
        with cli_main.get_client(ctx) as client:
            data = client.handoff_task_execution(workspace_id, task_id, body)
    except HubError as exc:
        raise _safe_task_operation_error("handoff", exc) from exc
    emit(
        _action_summary(data, reporter_key_file=new_reporter_key_file),
        cli_main.as_json(ctx),
    )


@task.command("dispatch")
@click.argument("task_id")
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "cursor", "terminal"]),
    default=None,
)
@click.option("--target-session-id", default=None)
@click.option("--clear-context/--no-clear-context", default=None)
@click.option("--related-task-id", default=None)
@click.pass_context
def task_dispatch(
    ctx: click.Context,
    task_id: str,
    agent_type: Optional[str],
    target_session_id: Optional[str],
    clear_context: Optional[bool],
    related_task_id: Optional[str],
) -> None:
    """Explicitly start a workspace-controlled Task after any required handoff."""
    body = merge_payload(
        None,
        agent_type=agent_type,
        target_session_id=target_session_id,
        clear_context=clear_context,
        related_task_id=related_task_id,
    )
    try:
        with cli_main.get_client(ctx) as client:
            data = client.start_task(task_id, body)
    except HubError as exc:
        raise click.ClickException(str(exc)) from exc
    emit(_action_summary({"task": data}), cli_main.as_json(ctx))


def _tab_context_task_ids(observed: Any) -> list[str]:
    if not isinstance(observed, dict):
        return []
    tasks = observed.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [str(item["id"]) for item in tasks if isinstance(item, dict) and item.get("id")]


@task.command("context")
@click.option("--workspace-id", default=None)
@click.option("--task-id", default=None)
@click.option("--tab-id", envvar="CLAUDE_HUB_TAB_ID", default=None)
@click.pass_context
def task_context(
    ctx: click.Context, workspace_id: Optional[str], task_id: Optional[str], tab_id: Optional[str]
) -> None:
    """Read verified Task mode and capabilities; never create or dispatch.

    Tab observation describes the existing native main turn, not this CLI
    process or a child thread. Child IDs come from the caller/native tool,
    never from TAB_ID.
    """
    if workspace_id is None and task_id is None and tab_id is None:
        raise click.UsageError(
            "Pass --workspace-id/--task-id; no Chat tab is available as a lookup clue."
        )
    try:
        with cli_main.get_client(ctx) as client:
            observed = client.get_tab_task_context(tab_id) if tab_id else None
            observed_ids = _tab_context_task_ids(observed)
            if task_id is None and workspace_id is None:
                if len(observed_ids) != 1:
                    raise click.ClickException(
                        "Current Task is not unique; pass --workspace-id and --task-id."
                    )
                task_id = observed_ids[0]
            if task_id is not None:
                if workspace_id is None:
                    workspace_id, selected_task = _find_task_board(client, task_id)
                else:
                    selected_task = _task_in_board(client.get_board(workspace_id), task_id)
                if workspace_id is None or selected_task is None:
                    raise click.ClickException(
                        f"Task {task_id} not found in the selected Workspace."
                    )
            else:
                if workspace_id is None:
                    raise click.ClickException("Workspace is not unique; pass --workspace-id.")
                client.get_board(workspace_id)
                selected_task = None
            if selected_task is not None and selected_task.get("workspace_id") not in {
                None,
                workspace_id,
            }:
                raise click.ClickException(
                    "Task workspace identity did not match the server board."
                )
            capabilities = client.get_task_capabilities(workspace_id)
    except HubError as exc:
        raise click.ClickException(str(exc)) from exc

    observed_value = observed if isinstance(observed, dict) else {}
    execution_ref = (
        observed_value.get("execution_ref")
        if observed_value.get("reason") == "active"
        and isinstance(observed_value.get("execution_ref"), dict)
        else None
    )
    summary = _context_summary(workspace_id, selected_task, capabilities)
    summary.update(
        {
            "tab_id": observed_value.get("tab_id") if observed_value else None,
            "observation_reason": observed_value.get("reason") if observed_value else None,
            "observed_task_ids": observed_ids,
            "observed_workspace_ids": observed_value.get("workspace_ids", []),
            "execution_ref": execution_ref,
        }
    )
    emit(summary, cli_main.as_json(ctx))
