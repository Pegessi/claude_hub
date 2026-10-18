"""Task registration and executor protocol commands."""

import shlex
from pathlib import Path
from typing import Any, Dict, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import merge_payload
from claude_hub.cli.output import emit
from claude_hub.cli.task_commands.execution_support import (
    _action_summary,
    _context_summary,
    _default_reporter_key_file,
    _drop_superseded_default_key,
    _execution_ref_options,
    _execution_reference,
    _install_reporter_key,
    _prepare_reporter_key,
    _read_reporter_key,
    _registration_request_key,
    _safe_task_operation_error,
    _settle_staged_key,
    _source_reference,
    _staged_key_or_none,
    _staged_reporter_key_file,
    _task_in_board,
)
from claude_hub.cli.task_commands.group import task
from claude_hub.cli.task_commands.read import (
    _echo_call_id,
    _find_task_board,
    _resolve_call_id,
)


@task.command("register")
@click.argument("workspace_id")
@click.option("--title", required=True, help="Independent Task goal title.")
@click.option("--prompt", required=True, help="Task goal and acceptance context.")
@click.option(
    "--reporter-key-file",
    default=None,
    type=click.Path(path_type=Path, dir_okay=False),
    help=(
        "New private file for this Task's progress credential. Defaults to "
        "~/.claude_hub/reporter-keys/<task-id>.key, which task progress finds by Task ID."
    ),
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
    reporter_key_file: Optional[Path],
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
    if reuse_reporter_key_file and reporter_key_file is None:
        raise click.UsageError("--reuse-reporter-key-file requires --reporter-key-file")
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
    staged: Path | None = None
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
            if reporter_key_file is None:
                staged = key_path = _staged_reporter_key_file()
            else:
                key_path = reporter_key_file
                staged = _staged_key_or_none(reporter_key_file)
            reporter_key = _prepare_reporter_key(key_path, reuse_reporter_key_file)
            body["reporter_key"] = reporter_key
            body["request_key"] = _registration_request_key(reporter_key)
            data = client.register_task(workspace_id, body)
    except HubError as exc:
        raise _settle_staged_key(
            _safe_task_operation_error("register", exc),
            exc,
            staged,
            fresh=not reuse_reporter_key_file,
            retry_flags=lambda path: (
                f"--reporter-key-file {shlex.quote(str(path))} --reuse-reporter-key-file"
            ),
        ) from exc
    if staged is not None:
        envelope = data if isinstance(data, dict) else {}
        task_obj = envelope.get("task", envelope)
        task_id = task_obj.get("id") if isinstance(task_obj, dict) else None
        key_path = _install_reporter_key(staged, task_id)
    emit(
        _action_summary(data, reporter_key_file=key_path),
        cli_main.as_json(ctx),
    )


@task.command("progress")
@click.argument("workspace_id")
@click.argument("task_id")
@click.option(
    "--reporter-key-file",
    default=None,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Private credential file. Defaults to ~/.claude_hub/reporter-keys/<task-id>.key.",
)
@click.option(
    "--state",
    required=True,
    type=click.Choice(
        [
            "started",
            "working",
            "blocked",
            "needs_input",
            "completed",
            "failed",
            "released",
        ]
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
    reporter_key_file: Optional[Path],
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
    if reporter_key_file is None:
        reporter_key_file = _default_reporter_key_file(task_id)
        if not reporter_key_file.exists():
            raise click.ClickException(
                f"No reporter key at {reporter_key_file}. Pass --reporter-key-file with this "
                "Task's key; never try another Task's key."
            )
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
    help=(
        "New private credential file for an initiator handoff. Defaults to replacing "
        "~/.claude_hub/reporter-keys/<task-id>.key after the handoff succeeds; another "
        "file supersedes that default file, which is then removed."
    ),
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
    staged: Path | None = None
    if execution_control == "initiator" and new_reporter_key_file is None:
        # The previous epoch's key stays usable until the handoff succeeds.
        _default_reporter_key_file(task_id)
        staged = new_reporter_key_file = _staged_reporter_key_file()
    elif new_reporter_key_file is not None:
        staged = _staged_key_or_none(new_reporter_key_file)
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
        raise _settle_staged_key(
            _safe_task_operation_error("handoff", exc),
            exc,
            staged,
            fresh=not reuse_new_reporter_key_file,
            retry_flags=lambda path: (
                f"--call-id {shlex.quote(resolved_call_id)} "
                f"--new-reporter-key-file {shlex.quote(str(path))} "
                "--reuse-new-reporter-key-file "
                f"--expected-execution-epoch {expected_execution_epoch} "
                f"--expected-progress-revision {expected_progress_revision}"
            ),
        ) from exc
    if staged is not None:
        new_reporter_key_file = _install_reporter_key(staged, task_id)
    elif execution_control == "initiator" and new_reporter_key_file is not None:
        _drop_superseded_default_key(task_id, new_reporter_key_file)
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
    ctx: click.Context,
    workspace_id: Optional[str],
    task_id: Optional[str],
    tab_id: Optional[str],
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
