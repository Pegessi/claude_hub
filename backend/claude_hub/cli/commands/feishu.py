"""``feishu`` command group — push interactive cards to humans and collect results.

Scenario A: an external agent invokes the CLI, which pushes an interactive card
to a human over Feishu and (optionally) blocks until the human responds, then
prints the decision so the agent can act on it.

Subcommands:

* ``feishu bind <name> --chat-id <id>`` / ``bindings`` / ``unbind <name>`` --
  manage friendly aliases for Feishu chat ids (see :mod:`feishu_store`).
* ``feishu send-card`` -- build a card of a given ``--kind``, push it to a chat,
  and with ``--wait`` long-poll the backend result store for the human's
  decision. ``--dry-run`` prints the card JSON without sending.
* ``feishu result <token>`` -- read the current decision for a token.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Dict, List, Optional

import click

from claude_hub.cli import main as cli_main
from claude_hub.cli.client import HubError
from claude_hub.cli.feishu_cards import (
    CARD_KINDS,
    INTERACTIVE_KINDS,
    build_approval_card,
    build_needs_input_card,
    build_plan_confirm_card,
    build_status_card,
    build_task_card,
)
from claude_hub.cli.feishu_sender import FeishuSendError, send_card
from claude_hub.cli.feishu_store import (
    load_bindings,
    remove_binding,
    resolve_target,
    set_binding,
)
from claude_hub.cli.output import emit

# Default cadence/ceiling for the --wait long poll.
DEFAULT_TIMEOUT = 300.0
POLL_INTERVAL = 2.0


@click.group()
def feishu() -> None:
    """Push interactive cards to humans over Feishu and collect their replies."""


# -- Bindings ---------------------------------------------------------------


@feishu.command("bind")
@click.argument("name")
@click.option("--chat-id", required=True, help="Feishu chat id (oc_...) to bind this name to.")
@click.pass_context
def feishu_bind(ctx: click.Context, name: str, chat_id: str) -> None:
    """Bind a friendly NAME to a Feishu chat id."""
    set_binding(name, chat_id)
    if cli_main.as_json(ctx):
        emit({"name": name, "chat_id": chat_id}, True)
    else:
        click.echo(f"bound {name} -> {chat_id}")


@feishu.command("bindings")
@click.pass_context
def feishu_bindings(ctx: click.Context) -> None:
    """List all chat-id bindings."""
    bindings = load_bindings()
    if cli_main.as_json(ctx):
        emit(bindings, True)
        return
    if not bindings:
        click.echo("(none)")
        return
    for name in sorted(bindings):
        click.echo(f"{name}: {bindings[name]}")


@feishu.command("unbind")
@click.argument("name")
@click.pass_context
def feishu_unbind(ctx: click.Context, name: str) -> None:
    """Remove a chat-id binding by NAME."""
    removed = remove_binding(name)
    if not removed:
        raise click.ClickException(f"no binding named {name!r}")
    if cli_main.as_json(ctx):
        emit({"name": name, "removed": True}, True)
    else:
        click.echo(f"unbound {name}")


# -- Card building ----------------------------------------------------------


def _build_card(
    ctx: click.Context,
    kind: str,
    token: str,
    *,
    title: Optional[str],
    body: Optional[str],
    workspace_id: Optional[str],
    task_id: Optional[str],
    field_name: str,
) -> Dict[str, Any]:
    """Build the interactive-card JSON for ``kind``.

    Interactive kinds embed ``token``; display kinds (status/task) fetch live
    data from the backend and carry no token. Raises ``click.ClickException``
    for missing required inputs.
    """
    if kind == "approval":
        return build_approval_card(token, title or "Approval required", body or "")
    if kind == "needs_input":
        return build_needs_input_card(
            token, title or "Input required", body or "", field_name=field_name
        )
    if kind == "plan_confirm":
        return build_plan_confirm_card(token, title or "Confirm plan", body or "")

    # Display kinds need live backend data.
    if kind == "status":
        if not workspace_id:
            raise click.ClickException("--workspace-id is required for kind=status")
        try:
            with cli_main.get_client(ctx) as client:
                board = client.get_board(workspace_id)
        except HubError as e:
            raise click.ClickException(str(e)) from e
        return build_status_card(workspace_id, board)

    if kind == "task":
        if not (workspace_id and task_id):
            raise click.ClickException("--workspace-id and --task-id are required for kind=task")
        try:
            with cli_main.get_client(ctx) as client:
                board = client.get_board(workspace_id)
        except HubError as e:
            raise click.ClickException(str(e)) from e
        tasks: List[dict] = board.get("tasks", []) if isinstance(board, dict) else []
        match = next((t for t in tasks if t.get("id") == task_id), None)
        if match is None:
            raise click.ClickException(f"task {task_id} not found in workspace {workspace_id}")
        return build_task_card(match)

    raise click.ClickException(f"unknown kind: {kind}")


def _resolve_credentials(app_id: Optional[str], app_secret: Optional[str]) -> tuple[str, str]:
    """Resolve Feishu app credentials (flag > env > backend settings)."""
    import os

    from claude_hub.config import settings as backend_settings

    resolved_id = app_id or os.environ.get("FEISHU_APP_ID") or backend_settings.feishu_app_id
    resolved_secret = (
        app_secret or os.environ.get("FEISHU_APP_SECRET") or backend_settings.feishu_app_secret
    )
    if not resolved_id or not resolved_secret:
        raise click.ClickException(
            "Feishu app credentials missing: set --app-id/--app-secret, "
            "$FEISHU_APP_ID/$FEISHU_APP_SECRET, or feishu_app_id/feishu_app_secret in config."
        )
    return resolved_id, resolved_secret


def _poll_result(ctx: click.Context, token: str, timeout: float) -> Dict[str, Any]:
    """Long-poll the backend result store until resolved or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    last: Dict[str, Any] = {"token": token, "status": "pending"}
    while True:
        try:
            with cli_main.get_client(ctx) as client:
                last = client.get_card_result(token)
        except HubError as e:
            raise click.ClickException(str(e)) from e
        if isinstance(last, dict) and last.get("status") == "resolved":
            return last
        if time.monotonic() >= deadline:
            return {"token": token, "status": "timeout", "last": last}
        time.sleep(POLL_INTERVAL)


@feishu.command("send-card")
@click.option(
    "--kind",
    type=click.Choice(list(CARD_KINDS)),
    required=True,
    help="Card kind to build and send.",
)
@click.option("--to", "target", default=None, help="Chat binding name or raw chat id (oc_...).")
@click.option("--title", default=None, help="Card title.")
@click.option("--body", default=None, help="Card body/prompt/plan markdown text.")
@click.option("--workspace-id", default=None, help="Workspace id (for kind=status/task).")
@click.option("--task-id", default=None, help="Task id (for kind=task).")
@click.option("--field-name", default="reply", help="Form field name (for kind=needs_input).")
@click.option("--app-id", default=None, help="Feishu app id (falls back to env/config).")
@click.option("--app-secret", default=None, help="Feishu app secret (falls back to env/config).")
@click.option("--wait", is_flag=True, default=False, help="Block until the human responds.")
@click.option(
    "--timeout",
    type=float,
    default=DEFAULT_TIMEOUT,
    help=f"Seconds to wait when --wait is set (default {DEFAULT_TIMEOUT:.0f}).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Print the card JSON; do not send.")
@click.pass_context
def feishu_send_card(
    ctx: click.Context,
    kind: str,
    target: Optional[str],
    title: Optional[str],
    body: Optional[str],
    workspace_id: Optional[str],
    task_id: Optional[str],
    field_name: str,
    app_id: Optional[str],
    app_secret: Optional[str],
    wait: bool,
    timeout: float,
    dry_run: bool,
) -> None:
    """Build a card, push it to a Feishu chat, and optionally await the reply."""
    is_interactive = kind in INTERACTIVE_KINDS
    # Display-only cards never wait — there is nothing to collect.
    if wait and not is_interactive:
        raise click.ClickException(
            f"--wait is only valid for interactive kinds {INTERACTIVE_KINDS}"
        )

    token = secrets.token_urlsafe(16) if is_interactive else ""
    card = _build_card(
        ctx,
        kind,
        token,
        title=title,
        body=body,
        workspace_id=workspace_id,
        task_id=task_id,
        field_name=field_name,
    )

    if dry_run:
        emit({"kind": kind, "token": token or None, "card": card}, as_json=True)
        return

    chat_id = resolve_target(target) if target else None
    if not chat_id:
        raise click.ClickException("--to is required (a binding name or raw oc_... chat id)")

    # Register the token as pending BEFORE sending, so a fast human click cannot
    # race ahead of an unregistered token (submit would 409).
    if is_interactive:
        try:
            with cli_main.get_client(ctx) as client:
                client.register_card({"token": token, "chat_id": chat_id, "kind": kind})
        except HubError as e:
            raise click.ClickException(str(e)) from e

    resolved_id, resolved_secret = _resolve_credentials(app_id, app_secret)
    try:
        message_id = send_card(resolved_id, resolved_secret, chat_id, card)
    except FeishuSendError as e:
        raise click.ClickException(str(e)) from e

    if not (wait and is_interactive):
        emit(
            {"kind": kind, "token": token or None, "chat_id": chat_id, "message_id": message_id},
            cli_main.as_json(ctx),
        )
        return

    result = _poll_result(ctx, token, timeout)
    emit(result, cli_main.as_json(ctx))


@feishu.command("result")
@click.argument("token")
@click.pass_context
def feishu_result(ctx: click.Context, token: str) -> None:
    """Read the current decision for a card TOKEN."""
    try:
        with cli_main.get_client(ctx) as client:
            data = client.get_card_result(token)
    except HubError as e:
        raise click.ClickException(str(e)) from e
    emit(data, cli_main.as_json(ctx))
