"""``feishu`` command group — two stateless helpers for an agent that *is* a bot.

The agent driving this CLI is itself the Feishu bot: it sends the card to a human
and receives the ``card.action.trigger`` callback in the same process, so Hub
needs no result store, no token relay, and no outbound sender. It only needs two
pure, IO-free helpers exposed as thin commands:

* ``feishu build-card`` -- build the interactive/display card JSON for a given
  ``--kind`` and print it (the agent then sends it to Feishu itself).
* ``feishu parse-action`` -- parse a raw ``card.action.trigger`` callback into a
  normalized ``{token, action, form, operator_id, chat_id}`` decision.

Both speak JSON on stdout so the agent can ``subprocess`` them and parse the
result without writing any Python.
"""

from __future__ import annotations

import json
import secrets
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
    parse_card_action,
)
from claude_hub.cli.output import emit


@click.group()
def feishu() -> None:
    """Build Feishu cards and parse their action callbacks (stateless helpers)."""


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
    """Build the card JSON for ``kind``.

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


@feishu.command("build-card")
@click.option(
    "--kind",
    type=click.Choice(list(CARD_KINDS)),
    required=True,
    help="Card kind to build.",
)
@click.option("--title", default=None, help="Card title.")
@click.option("--body", default=None, help="Card body/prompt/plan markdown text.")
@click.option("--workspace-id", default=None, help="Workspace id (for kind=status/task).")
@click.option("--task-id", default=None, help="Task id (for kind=task).")
@click.option("--field-name", default="reply", help="Form field name (for kind=needs_input).")
@click.option(
    "--token",
    default=None,
    help="Correlation token to embed (interactive kinds). Auto-generated if omitted.",
)
@click.pass_context
def feishu_build_card(
    ctx: click.Context,
    kind: str,
    title: Optional[str],
    body: Optional[str],
    workspace_id: Optional[str],
    task_id: Optional[str],
    field_name: str,
    token: Optional[str],
) -> None:
    """Build a card of KIND and print ``{kind, token, card}`` as JSON.

    The agent sends ``card`` to Feishu itself. For interactive kinds the printed
    ``token`` is embedded in every control's ``value`` so the later
    ``card.action.trigger`` callback can be correlated via ``parse-action``.
    """
    is_interactive = kind in INTERACTIVE_KINDS
    if is_interactive:
        card_token = token or secrets.token_urlsafe(16)
    else:
        card_token = ""

    card = _build_card(
        ctx,
        kind,
        card_token,
        title=title,
        body=body,
        workspace_id=workspace_id,
        task_id=task_id,
        field_name=field_name,
    )
    emit({"kind": kind, "token": card_token or None, "card": card}, as_json=True)


# -- Action parsing ---------------------------------------------------------


@feishu.command("parse-action")
@click.argument("payload", required=False)
@click.pass_context
def feishu_parse_action(ctx: click.Context, payload: Optional[str]) -> None:
    """Parse a ``card.action.trigger`` callback into a decision.

    PAYLOAD is the raw callback JSON; pass it as an argument or pipe it on stdin.
    Prints ``{token, action, form, operator_id, chat_id}`` as JSON. Exits 1 and
    prints ``null`` for a foreign card / non-interactive control (no token), so a
    caller can branch on the exit code.
    """
    raw = payload if payload is not None else click.get_text_stream("stdin").read()
    if not raw or not raw.strip():
        raise click.ClickException("no callback payload given (pass an argument or pipe stdin)")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"invalid JSON payload: {e}") from e

    decision = parse_card_action(body)
    if decision is None:
        emit(None, as_json=True)
        ctx.exit(1)
    emit(decision, as_json=True)
