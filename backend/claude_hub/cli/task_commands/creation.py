"""Task creation and 1-step isolated task run commands."""

import uuid
from typing import Any, Callable, Dict, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import (
    merge_payload,
    parse_attachment_json,
    parse_json_object,
    read_prompt_option,
    resolve_task_cwd_for_cli,
    resolve_workspace_id,
)
from claude_hub.cli.output import emit
from claude_hub.cli.task_commands.execution_support import _reject_secret_task_fields
from claude_hub.cli.task_commands.group import task

TASK_AGENT_TYPES = [
    "claude",
    "codex",
    "traex",
    "maso",
    "cursor",
    "antigravity",
    "terminal",
]
TASK_MODES = ["direct", "reviewed", "autonomous", "subagent"]
TASK_RECIPES = [
    "standard",
    "triage_diagnose",
    "spec_implement_review",
    "eval_and_report",
]
REVIEW_PROFILES = ["general", "code", "ui", "artifact", "delivery", "boundary"]


def _task_spec_options(func: Callable[..., Any]) -> Callable[..., Any]:
    """Shared contract and execution options for ``task create`` and ``task run``."""
    options = (
        click.argument("workspace_id", required=False, default=None),
        click.option("--title", required=True, help="Task title."),
        click.option("--prompt", default=None, help="Task prompt (optional when --goal is set)."),
        click.option(
            "--prompt-file",
            default=None,
            help="Read task prompt from FILE ('-' for stdin).",
        ),
        click.option("--goal", default=None, help="Desired outcome (creates GoalPacket)."),
        click.option("--done-when", multiple=True, help="Acceptance criterion (repeatable)."),
        click.option("--out-of-scope", multiple=True, help="Excluded work (repeatable)."),
        click.option(
            "--agent-type",
            type=click.Choice(TASK_AGENT_TYPES),
            default="codex",
            show_default=True,
            help="Agent provider.",
        ),
        click.option("--model", default=None, help="Explicit provider model ID."),
        click.option(
            "--env-preset",
            default=None,
            help="Env preset by id or name.",
        ),
        click.option(
            "--cwd",
            default=None,
            help="Explicit agent working directory (mutually exclusive with --auto-worktree).",
        ),
        click.option(
            "--readonly",
            is_flag=True,
            default=False,
            help="Read-only investigation: sets triage_diagnose recipe, subagent mode, no worktree.",
        ),
        click.option(
            "--session-kind",
            type=click.Choice(["terminal", "chat"]),
            default=None,
            help="Managed session surface (default: chat locally, terminal for remote SSH).",
        ),
        click.option(
            "--task-mode",
            type=click.Choice(TASK_MODES),
            default="reviewed",
            show_default=True,
            help="Task automation mode.",
        ),
        click.option(
            "--recipe",
            type=click.Choice(TASK_RECIPES),
            default=None,
            help="Standardized pipeline execution recipe.",
        ),
        click.option("--demand-id", "demand_id", default=None, help="Linked InfoWall demand ID."),
        click.option(
            "--auto-worktree/--no-auto-worktree",
            default=None,
            help="Automatically provision an isolated Git feature worktree on start.",
        ),
        click.option(
            "--auto-push-remote/--no-auto-push-remote",
            default=None,
            help="Automatically push the feature branch to origin after review_passed.",
        ),
        click.option(
            "--execution-complexity",
            type=click.Choice(["auto", "simple", "complex"]),
            default="auto",
            help="Execution complexity hint.",
        ),
        click.option(
            "--review-profile",
            "review_profiles",
            multiple=True,
            type=click.Choice(REVIEW_PROFILES),
            help="Review profile (repeatable).",
        ),
        click.option(
            "--related-task-id",
            default=None,
            help="Related prior task id (auto-wakes its archived Chat session on start).",
        ),
        click.option(
            "--parent-task-id",
            default=None,
            help="Parent Task id for an explicit Task Graph edge.",
        ),
        click.option(
            "--depends-on",
            multiple=True,
            help="Prerequisite task id (repeatable; requires done).",
        ),
        click.option("--agent-tag", default=None, help="Optional agent label tag."),
        click.option("--session-id", default=None, help="Bind an explicit existing session id."),
        click.option(
            "--clear-context/--no-clear-context",
            default=None,
            help="Clear agent context before starting.",
        ),
        click.option(
            "--timeout-seconds",
            type=int,
            default=None,
            help="Task timeout in seconds.",
        ),
        click.option(
            "--request-key",
            default=None,
            help="Idempotency key for safe create retries.",
        ),
        click.option(
            "--attachment-json",
            "attachment_json",
            multiple=True,
            help="Attachment JSON object (repeatable).",
        ),
        click.option("--payload-json", default=None, help="Raw JSON object merged into the body."),
    )
    for opt in reversed(options):
        func = opt(func)
    return func


def _build_task_create_body(
    ctx: click.Context,
    *,
    title: str,
    prompt: Optional[str],
    prompt_file: Optional[str],
    goal: Optional[str],
    done_when: tuple[str, ...],
    out_of_scope: tuple[str, ...],
    agent_type: str,
    model: Optional[str],
    env_preset: Optional[str],
    cwd: Optional[str],
    readonly: bool,
    session_kind: Optional[str],
    task_mode: str,
    recipe: Optional[str],
    demand_id: Optional[str],
    auto_worktree: Optional[bool],
    auto_push_remote: Optional[bool],
    execution_complexity: str,
    review_profiles: tuple,
    related_task_id: Optional[str],
    parent_task_id: Optional[str],
    depends_on: tuple,
    agent_tag: Optional[str],
    session_id: Optional[str],
    clear_context: Optional[bool],
    timeout_seconds: Optional[int],
    request_key: Optional[str],
    attachment_json: tuple,
    payload_json: Optional[str],
    apply_default_preset: bool,
) -> Dict[str, Any]:
    raw_payload = parse_json_object(payload_json)
    _reject_secret_task_fields(raw_payload)
    if raw_payload.get("execution_control", "workspace") != "workspace":
        raise click.ClickException(
            "Workspace tasks use execution_control='workspace'; use `task register` for initiator work."
        )

    effective_prompt = raw_payload.get("prompt")
    if prompt is not None or prompt_file is not None or effective_prompt is None:
        effective_prompt = read_prompt_option(
            prompt,
            prompt_file,
            fallback_goal=goal or raw_payload.get("goal"),
            done_when=done_when,
            out_of_scope=out_of_scope,
        )

    effective_recipe = recipe
    effective_mode: Optional[str] = task_mode
    effective_auto_wt = auto_worktree
    if readonly:
        if effective_recipe is None:
            effective_recipe = "triage_diagnose"
        if ctx.get_parameter_source("task_mode") == click.core.ParameterSource.DEFAULT:
            effective_mode = "subagent"
        if effective_auto_wt is None:
            effective_auto_wt = False
    elif (
        effective_recipe == "triage_diagnose"
        and ctx.get_parameter_source("task_mode") == click.core.ParameterSource.DEFAULT
    ):
        effective_mode = None

    cleaned_cwd = cwd.strip() if cwd is not None else None
    if cleaned_cwd and effective_auto_wt is True:
        raise click.ClickException("Cannot combine --cwd with --auto-worktree.")

    resolved_preset = env_preset
    if resolved_preset is None and apply_default_preset:
        settings = ctx.obj
        resolved_preset = settings.env_preset_for_agent_type(agent_type) or getattr(
            settings, "default_env_preset", None
        )

    body = merge_payload(
        payload_json,
        title=title,
        prompt=effective_prompt,
        goal=goal,
        agent_type=agent_type,
        model=model,
        env_preset=resolved_preset,
        cwd=cleaned_cwd or None,
        session_kind=session_kind,
        task_mode=effective_mode,
        recipe=effective_recipe,
        infowall_demand_id=demand_id,
        auto_worktree=effective_auto_wt,
        auto_push_remote=auto_push_remote,
        execution_complexity=execution_complexity,
        review_profiles=list(review_profiles),
        related_task_id=related_task_id,
        parent_task_id=parent_task_id,
        agent_tag=agent_tag,
        session_id=session_id,
        clear_context=clear_context,
        timeout_seconds=timeout_seconds,
        request_key=request_key,
    )
    if done_when:
        body["done_when"] = list(done_when)
    if out_of_scope:
        body["out_of_scope"] = list(out_of_scope)
    if depends_on:
        body["depends_on_task_ids"] = list(depends_on)
    if attachment_json:
        body["attachments"] = parse_attachment_json(attachment_json)
    body["execution_control"] = "workspace"
    return body


@task.command("create")
@_task_spec_options
@click.pass_context
def task_create(
    ctx: click.Context,
    workspace_id: Optional[str],
    **kwargs: Any,
) -> None:
    """Create a workspace-controlled Task record without starting it."""
    body = _build_task_create_body(ctx, apply_default_preset=False, **kwargs)
    try:
        with cli_main.get_client(ctx) as client:
            resolved_ws = resolve_workspace_id(client, workspace_id)
            if kwargs.get("cwd") is not None:
                body["cwd"] = resolve_task_cwd_for_cli(
                    client, kwargs["cwd"], workspace_id=resolved_ws
                )
            data = client.create_task(resolved_ws, body)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))


@task.command("run")
@_task_spec_options
@click.pass_context
def task_run(
    ctx: click.Context,
    workspace_id: Optional[str],
    **kwargs: Any,
) -> None:
    """Create and start a Task in one step on a fresh isolated session.

    Provisions a dedicated per-task ephemeral Chat session (archived on done
    with full history retained; woken automatically by future tasks passing
    ``--related-task-id``). Pass ``--session-id`` only to target an explicit
    existing session.
    """
    body = _build_task_create_body(ctx, apply_default_preset=True, **kwargs)
    if not body.get("request_key"):
        body["request_key"] = f"task-run-{uuid.uuid4().hex}"
    start_body: Dict[str, Any] = {}
    if kwargs.get("session_id"):
        start_body["target_session_id"] = kwargs["session_id"]
    if kwargs.get("clear_context") is not None:
        start_body["clear_context"] = kwargs["clear_context"]
    if kwargs.get("related_task_id"):
        start_body["related_task_id"] = kwargs["related_task_id"]

    created_task_id: Optional[str] = None
    try:
        with cli_main.get_client(ctx) as client:
            resolved_ws = resolve_workspace_id(client, workspace_id)
            if kwargs.get("cwd") is not None:
                body["cwd"] = resolve_task_cwd_for_cli(
                    client, kwargs["cwd"], workspace_id=resolved_ws
                )
            created = client.create_task(resolved_ws, body)
            task_id = str(created["id"])
            created_task_id = task_id
            started = client.start_task(task_id, start_body)
    except HubError as e:
        if created_task_id is not None:
            raise click.ClickException(
                f"Task {created_task_id} created (request_key={body['request_key']}) "
                f"but start failed: {e}"
            ) from e
        raise click.ClickException(str(e)) from e

    result = {
        "workspace_id": resolved_ws,
        "task_id": task_id,
        "session_id": started.get("session_id"),
        "worktree_path": started.get("worktree_path"),
        "status": started.get("status"),
    }
    emit(result, cli_main.as_json(ctx))
