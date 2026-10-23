"""``agent run`` and companions: one-call cross-provider subagents.

Unlike Workspace agents/Tasks there is no board, review or acceptance: the
command starts a native Chat with the chosen provider, waits for its turn and
prints the final answer.
"""

from __future__ import annotations

import os
import re
import sys
import time
import uuid
from typing import Any, Callable, Dict, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubClient, HubError
from claude_hub.cli.commands.common import resolve_cli_local_path
from claude_hub.cli.commands.workspaces import agent
from claude_hub.cli.output import emit, print_rows
from claude_hub.models.subagent import SUBAGENT_ID_ENV

PROVIDERS = ["claude", "codex", "traex", "cursor", "maso"]
EXIT_FAILED = 1
EXIT_TIMEOUT = 124
EXIT_CANCELLED = 130
_POLL_SECONDS = 20.0
_TRANSPORT_RETRY_SECONDS = 90.0
_PROGRESS_EVERY_SECONDS = 60.0
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
SUBAGENT_COLUMNS = [
    "id",
    "agent_type",
    "name",
    "keep",
    "closed",
    "last_status",
    "turn_count",
    "tab_id",
]

RUN_HELP = """Ask another provider for help and print its final answer.

\b
For help from another provider:
  claude-hub agent run --provider codex --prompt-file - <<'EOF'
  ...task...
  EOF
returns the subagent's final answer on stdout (progress goes to stderr).
Use Workspace Tasks only for tracked, reviewed work.

\b
The subagent is a native Hub Chat (visible in the UI) linked to the calling
Chat ($CLAUDE_HUB_TAB_ID); archiving or deleting that Chat closes it. By
default it runs in the current directory, blocks until done and is archived
a few seconds afterwards. --keep leaves it open for `agent send`.
--background prints ids immediately; collect with `agent result ID --wait`.

\b
Approvals: the subagent inherits the calling Chat's solo mode; without a
parent it runs solo (no approvals). --solo/--no-solo overrides. Nobody can
approve from here: a turn that stops for an approval or question fails with
instructions (re-run with --solo, or --keep and answer in Web Chat).

\b
Ctrl-C in `run`/`send` cancels the turn that command started (also while it
is still being created); in `agent result --wait` it only stops watching.
Subagents nest at most 2 levels deep, counted from the calling Chat; a
subagent's own subagents are always linked to it (--no-parent is refused).
Exit codes: 0 completed, 1 failed, 124 timeout, 130 cancelled/interrupted.
Retries reuse --call-id so one call never starts twice.
"""


def _parse_duration(value: str) -> float:
    match = _DURATION_RE.match(value)
    if not match:
        raise click.BadParameter("use e.g. 90s, 30m, 2h or plain seconds", param_hint="--timeout")
    seconds = float(match.group(1)) * _UNITS[match.group(2).lower()]
    if seconds <= 0:
        raise click.BadParameter("must be positive", param_hint="--timeout")
    return seconds


def _read_prompt(prompt: Optional[str], prompt_file: Optional[str]) -> str:
    if prompt is not None and prompt_file is not None:
        raise click.UsageError("Pass only one of --prompt and --prompt-file.")
    if prompt is not None:
        text = prompt
    elif prompt_file == "-" or (prompt_file is None and not sys.stdin.isatty()):
        text = sys.stdin.read()
    elif prompt_file is not None:
        try:
            with open(os.path.expanduser(prompt_file), encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise click.ClickException(f"cannot read --prompt-file: {exc}") from exc
    else:
        raise click.UsageError("Provide the task with --prompt, --prompt-file FILE, or stdin.")
    if not text.strip():
        raise click.UsageError("The prompt is empty.")
    return text


def _note(message: str) -> None:
    click.echo(f"[agent] {message}", err=True)


def _post_with_retry(call: Any) -> Any:
    """Retry transport failures only; the call_id makes the POST idempotent."""
    deadline = time.monotonic() + _TRANSPORT_RETRY_SECONDS
    delay = 1.0
    while True:
        try:
            return call()
        except HubError as exc:
            if exc.status is not None or time.monotonic() >= deadline:
                raise
            _note(f"Hub unreachable ({exc}); retrying")
            time.sleep(delay)
            delay = min(delay * 2, 10.0)


def _wait_for_turn(
    client: HubClient, subagent_id: str, turn_id: str, *, timeout_seconds: Optional[float]
) -> Dict[str, Any]:
    """Repeated long-polls, each well within the client HTTP timeout."""
    started = time.monotonic()
    # The server times the turn out itself; this local bound only guards a
    # Hub that never answers.
    hard_deadline = None if timeout_seconds is None else started + timeout_seconds + 120.0
    last_progress = started
    unreachable_since: Optional[float] = None
    result: Dict[str, Any] = {}
    while True:
        try:
            result = client.get_subagent_turn(subagent_id, turn_id, wait=_POLL_SECONDS)
            unreachable_since = None
        except HubError as exc:
            if exc.status is not None:
                raise
            now = time.monotonic()
            unreachable_since = unreachable_since or now
            if now - unreachable_since > _TRANSPORT_RETRY_SECONDS:
                raise
            _note(f"Hub unreachable ({exc}); retrying")
            time.sleep(2.0)
            continue
        if result.get("status") != "running":
            return result
        now = time.monotonic()
        if hard_deadline is not None and now > hard_deadline:
            result = dict(result)
            result["status"] = "timeout"
            result["error"] = "Gave up waiting for the Hub to report a result"
            return result
        if now - last_progress >= _PROGRESS_EVERY_SECONDS:
            last_progress = now
            _note(f"{subagent_id} still running ({int(now - started)}s)")


def _cancel_quietly(client: HubClient, subagent_id: str, turn_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = client.cancel_subagent(subagent_id, turn_id)
    except HubError as exc:
        _note(f"cancel failed: {exc}")
        return None
    turn = data.get("turn") if isinstance(data, dict) else None
    return turn if isinstance(turn, dict) else None


def _print_started(started: Dict[str, Any], json_output: bool) -> None:
    if json_output:
        emit(started, True)
    else:
        click.echo(f"{started['subagent_id']} {started['turn_id']}")


def _finish(result: Dict[str, Any], json_output: bool) -> None:
    status = result.get("status")
    if json_output:
        emit(result, True)
    elif status == "completed":
        click.echo(result.get("final_text") or "")
    else:
        _note(f"{result.get('subagent_id')} turn {status}: {result.get('error') or 'no answer'}")
    if status == "completed":
        return
    code = {"timeout": EXIT_TIMEOUT, "cancelled": EXIT_CANCELLED, "running": EXIT_TIMEOUT}.get(
        str(status), EXIT_FAILED
    )
    click.get_current_context().exit(code)


def _cancel_started(client: HubClient, started: Dict[str, Any], json_output: bool) -> None:
    subagent_id = str(started["subagent_id"])
    turn_id = str(started["turn_id"])
    _note(f"interrupted; cancelling {subagent_id} turn {turn_id}")
    cancelled = _cancel_quietly(client, subagent_id, turn_id)
    result = cancelled or dict(started, status="cancelled", error="Interrupted")
    if result.get("status") == "running":
        result = dict(result, status="cancelled", error="Interrupted")
    _finish(result, json_output)


def _start(
    client: HubClient, post: Callable[[], Any], *, call_id: str, json_output: bool
) -> Dict[str, Any]:
    """POST a create/send; Ctrl-C meanwhile cancels whatever that call started.

    The interrupted POST may already have started a turn. Re-posting the same
    body and call_id returns that turn without starting a second one (or
    starts it now), so it can be cancelled by id.
    """
    try:
        started: Dict[str, Any] = _post_with_retry(post)
        return started
    except KeyboardInterrupt:
        _note(f"interrupted while starting; cancelling call {call_id}")
    try:
        started = post()
    except (KeyboardInterrupt, HubError) as exc:
        reason = str(exc) if isinstance(exc, HubError) else "interrupted again"
        _note(
            f"could not confirm the cancellation ({reason}). If call {call_id} started a "
            "turn, find it with `claude-hub agent subagents` and stop it with "
            "`claude-hub agent cancel SUBAGENT_ID`."
        )
        click.get_current_context().exit(EXIT_CANCELLED)
    _cancel_started(client, started, json_output)
    # The turn completed before the cancel landed; its answer is printed.
    click.get_current_context().exit(0)


def _block(
    ctx: click.Context,
    client: HubClient,
    started: Dict[str, Any],
    *,
    timeout_seconds: Optional[float],
    json_output: bool,
    cancel_on_interrupt: bool,
) -> None:
    """Wait for the turn. Ctrl-C cancels it only for the command that started it."""
    subagent_id = str(started["subagent_id"])
    turn_id = str(started["turn_id"])
    if started.get("status") != "running":
        _finish(started, json_output)
        return
    try:
        result = _wait_for_turn(client, subagent_id, turn_id, timeout_seconds=timeout_seconds)
    except KeyboardInterrupt:
        if cancel_on_interrupt:
            _cancel_started(client, started, json_output)
            return
        _note(
            f"stopped watching; {subagent_id} turn {turn_id} keeps running. Resume with "
            f"`claude-hub agent result {subagent_id} {turn_id} --wait` or stop it with "
            f"`claude-hub agent cancel {subagent_id}`."
        )
        ctx.exit(EXIT_CANCELLED)
    _finish(result, json_output)


def _common_turn_options(func: Any) -> Any:
    func = click.option(
        "--json", "json_flag", is_flag=True, default=False, help="Print the structured result."
    )(func)
    func = click.option(
        "--background",
        is_flag=True,
        default=False,
        help="Return ids immediately; collect with `agent result ID --wait`.",
    )(func)
    func = click.option(
        "--call-id",
        default=None,
        help="Idempotency key (default: a fresh id per invocation; reuse it to retry).",
    )(func)
    func = click.option(
        "--timeout",
        "timeout",
        default="30m",
        show_default=True,
        help="Turn time limit (e.g. 90s, 30m, 2h); the turn is cancelled when it expires.",
    )(func)
    func = click.option(
        "--prompt-file", default=None, help="Read the prompt from FILE ('-' for stdin)."
    )(func)
    func = click.option("--prompt", default=None, help="Prompt text.")(func)
    return func


@agent.command("run", help=RUN_HELP)
@click.option(
    "--provider",
    "--agent-type",
    "provider",
    type=click.Choice(PROVIDERS),
    required=True,
    help="Chat provider for the subagent.",
)
@click.option("--model", default=None, help="Provider model id.")
@click.option("--effort", default=None, help="Reasoning effort (Codex/TraeX only).")
@click.option("--cwd", default=None, help="Working directory (default: current directory).")
@click.option(
    "--env-preset",
    default=None,
    help="Built-in or saved env preset by name/id (default: CLI config per provider).",
)
@click.option("--keep", is_flag=True, default=False, help="Keep the Chat for `agent send`.")
@click.option("--name", default=None, help="Chat tab name.")
@click.option(
    "--parent-tab-id",
    envvar="CLAUDE_HUB_TAB_ID",
    default=None,
    help="Calling Chat tab (default $CLAUDE_HUB_TAB_ID); its archive/delete closes the subagent.",
)
@click.option(
    "--no-parent",
    is_flag=True,
    default=False,
    help="Do not link to a parent tab (refused inside a subagent).",
)
@click.option(
    "--solo/--no-solo",
    "solo",
    default=None,
    help="Run without/with approvals (default: the parent Chat's setting; solo without parent).",
)
@_common_turn_options
@click.pass_context
def agent_run(
    ctx: click.Context,
    provider: str,
    model: Optional[str],
    effort: Optional[str],
    cwd: Optional[str],
    env_preset: Optional[str],
    keep: bool,
    name: Optional[str],
    parent_tab_id: Optional[str],
    no_parent: bool,
    solo: Optional[bool],
    prompt: Optional[str],
    prompt_file: Optional[str],
    timeout: str,
    call_id: Optional[str],
    background: bool,
    json_flag: bool,
) -> None:
    timeout_seconds = _parse_duration(timeout)
    text = _read_prompt(prompt, prompt_file)
    settings = ctx.obj
    preset = (
        env_preset
        or settings.env_preset_for_agent_type(provider)
        or getattr(settings, "default_env_preset", None)
    )
    body: Dict[str, Any] = {
        "agent_type": provider,
        "prompt": text,
        "cwd": resolve_cli_local_path(cwd or "."),
        "keep": keep,
        "call_id": call_id or f"agent-run-{uuid.uuid4().hex}",
        "timeout_seconds": timeout_seconds,
    }
    if solo is not None:
        body["solo_mode"] = solo
    # Caller identity always goes along, independent of --parent-tab-id and
    # --no-parent: the server counts nesting depth from the caller and links a
    # subagent caller's subagents to its own Chat.
    for key, value in (
        ("model", model),
        ("effort", effort),
        ("env_preset", preset),
        ("name", name),
        ("parent_tab_id", None if no_parent else (parent_tab_id or None)),
        ("caller_tab_id", os.environ.get("CLAUDE_HUB_TAB_ID", "").strip()),
        ("caller_subagent_id", os.environ.get(SUBAGENT_ID_ENV, "").strip()),
    ):
        if value:
            body[key] = value
    json_output = json_flag or cli_main.as_json(ctx)
    try:
        with cli_main.get_client(ctx) as client:
            started = _start(
                client,
                lambda: client.create_subagent(body),
                call_id=body["call_id"],
                json_output=json_output,
            )
            _note(
                f"subagent {started['subagent_id']} ({provider}) started in tab "
                f"{started.get('tab_id')}; turn {started['turn_id']}"
            )
            if background:
                _print_started(started, json_output)
                return
            _block(
                ctx,
                client,
                started,
                timeout_seconds=timeout_seconds,
                json_output=json_output,
                cancel_on_interrupt=True,
            )
    except HubError as exc:
        raise click.ClickException(str(exc)) from exc


@agent.command("send")
@click.argument("subagent_id")
@_common_turn_options
@click.pass_context
def agent_send(
    ctx: click.Context,
    subagent_id: str,
    prompt: Optional[str],
    prompt_file: Optional[str],
    timeout: str,
    call_id: Optional[str],
    background: bool,
    json_flag: bool,
) -> None:
    """Send a follow-up turn to a subagent created with `agent run --keep`."""
    timeout_seconds = _parse_duration(timeout)
    body = {
        "prompt": _read_prompt(prompt, prompt_file),
        "call_id": call_id or f"agent-send-{uuid.uuid4().hex}",
        "timeout_seconds": timeout_seconds,
    }
    json_output = json_flag or cli_main.as_json(ctx)
    try:
        with cli_main.get_client(ctx) as client:
            started = _start(
                client,
                lambda: client.send_subagent_turn(subagent_id, body),
                call_id=str(body["call_id"]),
                json_output=json_output,
            )
            if background:
                _print_started(started, json_output)
                return
            _block(
                ctx,
                client,
                started,
                timeout_seconds=timeout_seconds,
                json_output=json_output,
                cancel_on_interrupt=True,
            )
    except HubError as exc:
        raise click.ClickException(str(exc)) from exc


@agent.command("result")
@click.argument("subagent_id")
@click.argument("turn_id", required=False)
@click.option("--wait", is_flag=True, default=False, help="Block until the turn finishes.")
@click.option(
    "--json", "json_flag", is_flag=True, default=False, help="Print the structured result."
)
@click.pass_context
def agent_result(
    ctx: click.Context, subagent_id: str, turn_id: Optional[str], wait: bool, json_flag: bool
) -> None:
    """Show a subagent turn result (default: its latest turn).

    Prints the final answer when completed. Exit codes as for `agent run`;
    a still-running turn without --wait exits 124 after printing its status.
    Ctrl-C during --wait only stops watching (exit 130); the turn keeps running.
    """
    json_output = json_flag or cli_main.as_json(ctx)
    try:
        with cli_main.get_client(ctx) as client:
            result = client.get_subagent_turn(subagent_id, turn_id)
            if wait and result.get("status") == "running":
                _block(
                    ctx,
                    client,
                    result,
                    timeout_seconds=None,
                    json_output=json_output,
                    cancel_on_interrupt=False,
                )
                return
    except HubError as exc:
        raise click.ClickException(str(exc)) from exc
    _finish(result, json_output)


@agent.command("subagents")
@click.option(
    "--parent-tab-id",
    envvar="CLAUDE_HUB_TAB_ID",
    default=None,
    help="Only subagents of this tab (default $CLAUDE_HUB_TAB_ID).",
)
@click.option("--all", "all_parents", is_flag=True, default=False, help="Any parent.")
@click.option("--include-closed", is_flag=True, default=False, help="Include closed subagents.")
@click.pass_context
def agent_subagents(
    ctx: click.Context, parent_tab_id: Optional[str], all_parents: bool, include_closed: bool
) -> None:
    """List subagents started by `agent run`."""
    params: Dict[str, Any] = {"include_closed": include_closed}
    if parent_tab_id and not all_parents:
        params["parent_tab_id"] = parent_tab_id
    try:
        with cli_main.get_client(ctx) as client:
            rows = client.list_subagents(params)
    except HubError as exc:
        raise click.ClickException(str(exc)) from exc
    if cli_main.as_json(ctx):
        emit(rows, True)
    else:
        print_rows(rows, SUBAGENT_COLUMNS)


@agent.command("cancel")
@click.argument("subagent_id")
@click.pass_context
def agent_cancel(ctx: click.Context, subagent_id: str) -> None:
    """Cancel a subagent's running turn."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.cancel_subagent(subagent_id)
    except HubError as exc:
        raise click.ClickException(str(exc)) from exc
    emit(data, cli_main.as_json(ctx))


@agent.command("close")
@click.argument("subagent_id")
@click.pass_context
def agent_close(ctx: click.Context, subagent_id: str) -> None:
    """Cancel any running turn and archive the subagent's Chat (history kept)."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.close_subagent(subagent_id)
    except HubError as exc:
        raise click.ClickException(str(exc)) from exc
    emit(data, cli_main.as_json(ctx))
