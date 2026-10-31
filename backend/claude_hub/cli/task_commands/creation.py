"""Task creation and combined create-and-run commands."""

from typing import Any, Dict, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import (
    merge_payload,
    parse_attachment_json,
    parse_json_object,
)
from claude_hub.cli.output import emit
from claude_hub.cli.task_commands.execution_support import (
    _reject_secret_task_fields,
)
from claude_hub.cli.task_commands.group import task


@task.command("create")
@click.argument("workspace_id")
@click.option("--title", required=True, help="Task title.")
@click.option("--prompt", required=True, help="Task prompt.")
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "cursor", "maso", "antigravity", "terminal"]),
    default="codex",
    help="Agent type.",
)
@click.option(
    "--session-kind",
    type=click.Choice(["terminal", "chat"]),
    default=None,
    help="Managed session surface (omitted to use backend default or bound-session surface).",
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
    "--depends-on",
    multiple=True,
    help="Prerequisite task id (repeatable; requires done).",
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
    session_kind: Optional[str],
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
        session_kind=session_kind,
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
    type=click.Choice(["claude", "codex", "cursor", "maso", "antigravity", "terminal"]),
    default="claude",
    help="Agent type (default: claude).",
)
@click.option(
    "--session-kind",
    type=click.Choice(["terminal", "chat"]),
    default="terminal",
    show_default=True,
    help="Managed session surface.",
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
    session_kind: str,
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
                "session_kind": session_kind,
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
                session_kind=session_kind,
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
