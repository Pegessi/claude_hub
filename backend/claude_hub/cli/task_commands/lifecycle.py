"""Task lifecycle mutation commands."""

from typing import Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import (
    merge_payload,
    parse_attachment_json,
    resolve_task_cwd_for_cli,
)
from claude_hub.cli.output import emit
from claude_hub.cli.task_commands.creation import (
    REVIEW_PROFILES,
    TASK_AGENT_TYPES,
    TASK_MODES,
)
from claude_hub.cli.task_commands.execution_support import (
    _reject_task_execution_fields,
)
from claude_hub.cli.task_commands.group import task
from claude_hub.cli.task_commands.read import _fetch_task_dict
from claude_hub.models.schemas import WorkspaceTaskStatus


@task.command("start")
@click.argument("task_id")
@click.option(
    "--agent-type",
    type=click.Choice(TASK_AGENT_TYPES),
    default=None,
    help="Override the agent provider.",
)
@click.option("--model", default=None, help="Override provider model ID.")
@click.option("--env-preset", default=None, help="Override env preset by id or name.")
@click.option("--cwd", default=None, help="Override agent working directory.")
@click.option("--target-session-id", default=None, help="Target a specific existing session.")
@click.option(
    "--clear-context/--no-clear-context",
    default=None,
    help="Clear agent context before starting (omitted unless set).",
)
@click.option(
    "--related-task-id",
    default=None,
    help="Related prior task id (wakes its archived Chat session when compatible).",
)
@click.option("--payload-json", default=None, help="Raw JSON object merged into the body.")
@click.pass_context
def task_start(
    ctx: click.Context,
    task_id: str,
    agent_type: Optional[str],
    model: Optional[str],
    env_preset: Optional[str],
    cwd: Optional[str],
    target_session_id: Optional[str],
    clear_context: Optional[bool],
    related_task_id: Optional[str],
    payload_json: Optional[str],
) -> None:
    """Start (dispatch) a workspace-controlled Task."""
    body = merge_payload(
        payload_json,
        agent_type=agent_type,
        model=model,
        env_preset=env_preset,
        target_session_id=target_session_id,
        clear_context=clear_context,
        related_task_id=related_task_id,
    )
    try:
        with cli_main.get_client(ctx) as client:
            if cwd is not None:
                body["cwd"] = resolve_task_cwd_for_cli(client, cwd, task_id=task_id)
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
    """Continue a task from review or failed with its agent."""
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
@click.option("--goal", default=None, help="Desired outcome (updates GoalPacket).")
@click.option("--done-when", multiple=True, help="Replace acceptance criteria (repeatable).")
@click.option("--out-of-scope", multiple=True, help="Replace out-of-scope items (repeatable).")
@click.option(
    "--status",
    type=click.Choice(["todo", "queued", "working", "review", "done", "failed"]),
    default=None,
    help="Task status.",
)
@click.option(
    "--agent-type",
    type=click.Choice(TASK_AGENT_TYPES),
    default=None,
    help="Agent provider (todo tasks only).",
)
@click.option("--model", default=None, help="Provider model ID (todo tasks only).")
@click.option("--env-preset", default=None, help="Env preset (todo tasks only).")
@click.option("--cwd", default=None, help="Agent working directory (todo tasks only).")
@click.option(
    "--auto-worktree/--no-auto-worktree",
    default=None,
    help="Auto-worktree preference (todo tasks only).",
)
@click.option(
    "--task-mode",
    type=click.Choice(TASK_MODES),
    default=None,
    help="Task automation mode.",
)
@click.option(
    "--session-kind",
    type=click.Choice(["terminal", "chat"]),
    default=None,
    help="Managed session surface (todo tasks only).",
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
    type=click.Choice(REVIEW_PROFILES),
    help="Review profile list (repeatable).",
)
@click.option("--related-task-id", default=None, help="Related task id.")
@click.option(
    "--depends-on",
    multiple=True,
    help="Replace prerequisite ids (repeatable; todo only).",
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
    goal: Optional[str],
    done_when: tuple[str, ...],
    out_of_scope: tuple[str, ...],
    status: Optional[str],
    agent_type: Optional[str],
    model: Optional[str],
    env_preset: Optional[str],
    cwd: Optional[str],
    auto_worktree: Optional[bool],
    task_mode: Optional[str],
    session_kind: Optional[str],
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
    """Update task contract, execution config, metadata, or status."""
    if depends_on and clear_dependencies:
        raise click.UsageError("--depends-on and --clear-dependencies are mutually exclusive")
    body = merge_payload(
        payload_json,
        title=title,
        prompt=prompt,
        goal=goal,
        status=status,
        agent_type=agent_type,
        model=model,
        env_preset=env_preset,
        auto_worktree=auto_worktree,
        task_mode=task_mode,
        session_kind=session_kind,
        execution_complexity=execution_complexity,
        related_task_id=related_task_id,
        agent_tag=agent_tag,
        session_id=session_id,
        clear_context=clear_context,
    )
    if done_when:
        body["done_when"] = list(done_when)
    if out_of_scope:
        body["out_of_scope"] = list(out_of_scope)
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
            if cwd is not None:
                body["cwd"] = resolve_task_cwd_for_cli(client, cwd, task_id=task_id)
            data = client.update_task(task_id, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("accept")
@click.argument("task_id")
@click.option(
    "--workspace-id",
    default=None,
    help="Optional workspace id assertion.",
)
@click.option("--message", default=None, help="Optional acceptance note (recorded as a report).")
@click.option(
    "--cleanup-session",
    is_flag=True,
    default=False,
    help="After marking done, permanently delete a caller-owned ephemeral session.",
)
@click.pass_context
def task_accept(
    ctx: click.Context,
    task_id: str,
    workspace_id: Optional[str],
    message: Optional[str],
    cleanup_session: bool,
) -> None:
    """Human-accept a task in review or failed and mark it done."""
    try:
        with cli_main.get_client(ctx) as client:
            _, match = _fetch_task_dict(client, task_id, workspace_id)
            status = match.get("status")
            if status not in (
                WorkspaceTaskStatus.REVIEW.value,
                WorkspaceTaskStatus.FAILED.value,
            ):
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
    """Permanently delete a caller-owned ephemeral session after a terminal task."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.cleanup_task(task_id)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
