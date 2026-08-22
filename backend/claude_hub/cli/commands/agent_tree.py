"""``agent-tree`` command group over the existing Agent Tree REST contract."""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.commands.common import parse_kv_pairs
from claude_hub.cli.output import emit, print_rows

RUN_COLUMNS = ["id", "parent_id", "executor_kind", "status", "title", "ack_sequence"]
EVENT_COLUMNS = ["sequence", "type", "author", "recipient", "call_id"]
CURSOR_MODEL_ERROR = "Cursor executor does not support an explicit model override"
ACK_FAILED_NOTE = "events delivered but ACK failed/not acknowledged"


def _raise_hub_error(exc: HubError) -> None:
    """Map HubError to the Agent Tree Click contract and stop.

    API errors become ``HTTP {status}: {detail}``. Transport errors keep the
    original detail. Click then prints ``Error: ...`` and exits 1.
    """
    if exc.status is not None:
        raise click.ClickException(f"HTTP {exc.status}: {exc.message}") from exc
    raise click.ClickException(exc.message) from exc


def _echo_call_id(call_id: str) -> None:
    click.echo(f"call_id={call_id}", err=True)


def _resolve_call_id(explicit: Optional[str]) -> str:
    return explicit or str(uuid.uuid4())


def _emit_runs(rows: List[dict], as_json: bool) -> None:
    if as_json:
        emit(rows, True)
    else:
        print_rows(rows, RUN_COLUMNS)


def _emit_events(rows: List[Any], as_json: bool) -> None:
    if as_json:
        emit(rows, True)
    else:
        print_rows(rows, EVENT_COLUMNS)
    sys.stdout.flush()


def _spawn_config_requested(
    agent_type: Optional[str],
    model: Optional[str],
    target: Optional[str],
    cwd: Optional[str],
    env: Dict[str, str],
    solo_mode: Optional[bool],
    remote_profile_id: Optional[str],
    remote_cwd: Optional[str],
    remote_reconnect: Optional[bool],
) -> bool:
    return (
        agent_type is not None
        or model is not None
        or target is not None
        or cwd is not None
        or bool(env)
        or solo_mode is not None
        or remote_profile_id is not None
        or remote_cwd is not None
        or remote_reconnect is not None
    )


def _build_executor_config(
    agent_type: Optional[str],
    model: Optional[str],
    target: Optional[str],
    cwd: Optional[str],
    env: Dict[str, str],
    solo_mode: Optional[bool],
    remote_profile_id: Optional[str],
    remote_cwd: Optional[str],
    remote_reconnect: Optional[bool],
) -> Dict[str, Any]:
    chosen = agent_type or "claude"
    if chosen == "cursor" and model:
        raise click.ClickException(CURSOR_MODEL_ERROR)
    config: Dict[str, Any] = {"agent_type": chosen}
    if model is not None:
        config["model"] = model
    if target is not None:
        config["target"] = target
    if cwd is not None:
        config["cwd"] = cwd
    if env:
        config["env"] = env
    if solo_mode is not None:
        config["solo_mode"] = solo_mode
    if remote_profile_id is not None:
        config["remote_profile_id"] = remote_profile_id
    if remote_cwd is not None:
        config["remote_cwd"] = remote_cwd
    if remote_reconnect is not None:
        config["remote_reconnect"] = remote_reconnect
    return config


@click.group("agent-tree")
def agent_tree() -> None:
    """Thin Agent Tree client over /api/agent-tree (managed_task only)."""


@agent_tree.command("roots")
@click.argument("workspace_id")
@click.pass_context
def roots(ctx: click.Context, workspace_id: str) -> None:
    """List resident_root runs (client filter of GET /runs)."""
    try:
        with cli_main.get_client(ctx) as client:
            rows = client.list_agent_tree_runs(workspace_id)
    except HubError as exc:
        _raise_hub_error(exc)
    listed = rows if isinstance(rows, list) else []
    runs = [
        row
        for row in listed
        if isinstance(row, dict) and row.get("executor_kind") == "resident_root"
    ]
    _emit_runs(runs, cli_main.as_json(ctx))


@agent_tree.command("runs")
@click.argument("workspace_id")
@click.option("--root-id", default=None, help="Limit to this root subtree.")
@click.option("--status", default=None, help="Run status filter (running, failed, ...).")
@click.pass_context
def runs(
    ctx: click.Context,
    workspace_id: str,
    root_id: Optional[str],
    status: Optional[str],
) -> None:
    """GET /api/agent-tree/runs."""
    try:
        with cli_main.get_client(ctx) as client:
            rows = client.list_agent_tree_runs(workspace_id, root_id=root_id, status=status)
    except HubError as exc:
        _raise_hub_error(exc)
    listed = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    _emit_runs(listed, cli_main.as_json(ctx))


@agent_tree.command("events")
@click.argument("run_id")
@click.option("--since-sequence", default=0, type=int, show_default=True)
@click.option("--subtree/--no-subtree", default=True, show_default=True)
@click.pass_context
def events(
    ctx: click.Context,
    run_id: str,
    since_sequence: int,
    subtree: bool,
) -> None:
    """GET /api/agent-tree/runs/{run_id}/events."""
    try:
        with cli_main.get_client(ctx) as client:
            rows = client.get_agent_tree_events(
                run_id, since_sequence=since_sequence, subtree=subtree
            )
    except HubError as exc:
        _raise_hub_error(exc)
    listed = rows if isinstance(rows, list) else []
    _emit_events(listed, cli_main.as_json(ctx))


@agent_tree.command("spawn")
@click.argument("workspace_id")
@click.argument("parent_run_id")
@click.option("--message", required=True, help="Initial mailbox message.")
@click.option("--title", default=None)
@click.option("--session-id", default=None, help="Pin to an existing managed session.")
@click.option(
    "--agent-type",
    type=click.Choice(["claude", "codex", "cursor"], case_sensitive=True),
    default=None,
    help="managed_task CLI. Defaults to claude when config-driven.",
)
@click.option("--model", default=None, help="Claude/Codex model. Rejected for Cursor.")
@click.option("--target", type=click.Choice(["local", "remote"], case_sensitive=True), default=None)
@click.option("--cwd", default=None)
@click.option("--env", "env_pairs", multiple=True, help="KEY=VALUE launch env (repeatable).")
@click.option("--solo-mode/--no-solo-mode", "solo_mode", default=None)
@click.option("--remote-profile-id", default=None)
@click.option("--remote-cwd", default=None)
@click.option("--remote-reconnect/--no-remote-reconnect", "remote_reconnect", default=None)
@click.option("--call-id", default=None, help="Stable retry id. New UUID if omitted.")
@click.pass_context
def spawn(
    ctx: click.Context,
    workspace_id: str,
    parent_run_id: str,
    message: str,
    title: Optional[str],
    session_id: Optional[str],
    agent_type: Optional[str],
    model: Optional[str],
    target: Optional[str],
    cwd: Optional[str],
    env_pairs: Tuple[str, ...],
    solo_mode: Optional[bool],
    remote_profile_id: Optional[str],
    remote_cwd: Optional[str],
    remote_reconnect: Optional[bool],
    call_id: Optional[str],
) -> None:
    """POST /api/agent-tree/spawn as managed_task only."""
    env = parse_kv_pairs(env_pairs, "--env")
    config_requested = _spawn_config_requested(
        agent_type,
        model,
        target,
        cwd,
        env,
        solo_mode,
        remote_profile_id,
        remote_cwd,
        remote_reconnect,
    )
    body: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "parent_id": parent_run_id,
        "executor_kind": "managed_task",
        "initial_message": message,
    }
    if title is not None:
        body["title"] = title
    if session_id is not None:
        body["session_id"] = session_id
    if session_id is None or config_requested:
        body["executor_config"] = _build_executor_config(
            agent_type,
            model,
            target,
            cwd,
            env,
            solo_mode,
            remote_profile_id,
            remote_cwd,
            remote_reconnect,
        )
    resolved = _resolve_call_id(call_id)
    body["call_id"] = resolved
    _echo_call_id(resolved)
    try:
        with cli_main.get_client(ctx) as client:
            result = client.spawn_agent_run(body)
    except HubError as exc:
        _raise_hub_error(exc)
    emit(result, cli_main.as_json(ctx))


@agent_tree.command("send")
@click.argument("workspace_id")
@click.argument("author_run_id")
@click.argument("recipient_run_id")
@click.option("--message", required=True)
@click.option("--call-id", default=None, help="Stable retry id. New UUID if omitted.")
@click.pass_context
def send(
    ctx: click.Context,
    workspace_id: str,
    author_run_id: str,
    recipient_run_id: str,
    message: str,
    call_id: Optional[str],
) -> None:
    """POST /api/agent-tree/send."""
    resolved = _resolve_call_id(call_id)
    body = {
        "workspace_id": workspace_id,
        "author_id": author_run_id,
        "recipient_id": recipient_run_id,
        "message": message,
        "call_id": resolved,
    }
    _echo_call_id(resolved)
    try:
        with cli_main.get_client(ctx) as client:
            result = client.send_agent_message(body)
    except HubError as exc:
        _raise_hub_error(exc)
    emit(result, cli_main.as_json(ctx))


@agent_tree.command("followup")
@click.argument("workspace_id")
@click.argument("author_run_id")
@click.argument("recipient_run_id")
@click.option("--message", required=True)
@click.option("--call-id", default=None, help="Stable retry id. New UUID if omitted.")
@click.pass_context
def followup(
    ctx: click.Context,
    workspace_id: str,
    author_run_id: str,
    recipient_run_id: str,
    message: str,
    call_id: Optional[str],
) -> None:
    """POST /api/agent-tree/followup."""
    resolved = _resolve_call_id(call_id)
    body = {
        "workspace_id": workspace_id,
        "author_id": author_run_id,
        "recipient_id": recipient_run_id,
        "message": message,
        "call_id": resolved,
    }
    _echo_call_id(resolved)
    try:
        with cli_main.get_client(ctx) as client:
            result = client.followup_agent_run(body)
    except HubError as exc:
        _raise_hub_error(exc)
    emit(result, cli_main.as_json(ctx))


@agent_tree.command("wait")
@click.argument("workspace_id")
@click.argument("recipient_run_id")
@click.option("--since-sequence", default=0, type=int, show_default=True)
@click.option("--timeout-seconds", default=30.0, type=float, show_default=True)
@click.option("--subtree/--no-subtree", default=True, show_default=True)
@click.option(
    "--ack",
    is_flag=True,
    default=False,
    help="After a non-empty flushed event list, POST /ack at max(sequence).",
)
@click.pass_context
def wait(
    ctx: click.Context,
    workspace_id: str,
    recipient_run_id: str,
    since_sequence: int,
    timeout_seconds: float,
    subtree: bool,
    ack: bool,
) -> None:
    """POST /api/agent-tree/wait. Does not ACK unless --ack."""
    as_json = cli_main.as_json(ctx)
    body = {
        "workspace_id": workspace_id,
        "recipient_id": recipient_run_id,
        "since_sequence": since_sequence,
        "subtree": subtree,
        "timeout_seconds": timeout_seconds,
    }
    try:
        with cli_main.get_client(ctx) as client:
            rows = client.wait_agent_events(body)
    except HubError as exc:
        _raise_hub_error(exc)
    events = rows if isinstance(rows, list) else []
    _emit_events(events, as_json)
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
            client.ack_agent_run(workspace_id, recipient_run_id, max_sequence)
    except HubError as exc:
        click.echo(ACK_FAILED_NOTE, err=True)
        _raise_hub_error(exc)
    # Events are already flushed. JSON confirmation stays off stdout so the
    # event array remains one document; human mode prints a text line.
    if as_json:
        click.echo(json.dumps({"acked_sequence": max_sequence}), err=True)
    else:
        click.echo(f"acked sequence {max_sequence}")


@agent_tree.command("ack")
@click.argument("workspace_id")
@click.argument("run_id")
@click.argument("sequence", type=int)
@click.pass_context
def ack(ctx: click.Context, workspace_id: str, run_id: str, sequence: int) -> None:
    """POST /api/agent-tree/ack (query workspace_id, run_id, sequence only)."""
    try:
        with cli_main.get_client(ctx) as client:
            result = client.ack_agent_run(workspace_id, run_id, sequence)
    except HubError as exc:
        _raise_hub_error(exc)
    emit(result, cli_main.as_json(ctx))


@agent_tree.command("interrupt")
@click.argument("workspace_id")
@click.argument("run_id")
@click.option("--reason", default=None)
@click.option("--call-id", default=None, help="Stable retry id. New UUID if omitted.")
@click.pass_context
def interrupt(
    ctx: click.Context,
    workspace_id: str,
    run_id: str,
    reason: Optional[str],
    call_id: Optional[str],
) -> None:
    """POST /api/agent-tree/interrupt."""
    resolved = _resolve_call_id(call_id)
    body: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "run_id": run_id,
        "call_id": resolved,
    }
    if reason is not None:
        body["reason"] = reason
    _echo_call_id(resolved)
    try:
        with cli_main.get_client(ctx) as client:
            result = client.interrupt_agent_run(body)
    except HubError as exc:
        _raise_hub_error(exc)
    emit(result, cli_main.as_json(ctx))
