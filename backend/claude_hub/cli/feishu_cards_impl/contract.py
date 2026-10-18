"""Feishu card callback contract and action normalization."""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("claude_hub.cli.feishu_cards")

TOKEN_KEY = "hub_token"
ACTION_KEY = "hub_action"

# The kinds of card ``feishu build-card`` can build.
INTERACTIVE_KINDS = ("approval", "needs_input", "plan_confirm", "task_detail")
DISPLAY_KINDS = (
    "status",
    "task",
    "workspaces",
    "overview",
    "agents",
    "reports",
    "terminal",
    "lessons",
    "tabs",
    "tab_status",
    "network",
    "filesystem",
    "remote_profiles",
    "remote_filesystem",
    "result",
    "action_catalog",
)
CARD_KINDS = INTERACTIVE_KINDS + DISPLAY_KINDS


def _attr_or_key(obj: Any, name: str) -> Any:
    """Read ``name`` from ``obj`` whether it is a mapping or an attribute object.

    Feishu callbacks arrive as raw JSON (plain dicts) when your own bot forwards
    the webhook body, but the lark-oapi SDK delivers attribute-style event
    objects. Supporting both lets one parser serve every relay.
    """
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def parse_card_action(payload: Any) -> Optional[Dict[str, Any]]:
    """Extract a normalized decision from a ``card.action.trigger`` callback.

    This is the inverse of the card-building contract above. When the agent's bot
    receives a ``card.action.trigger`` event, pass the raw callback body here to
    pull out the correlation token and the human's choice, then match the
    ``token`` against the card it sent (exposed by the CLI as ``feishu
    parse-action``).

    ``payload`` may be the raw JSON dict Feishu delivers, the inner ``event``
    object, or a lark-oapi event object — attribute and key access are both
    supported. Returns a dict with:

    * ``token`` — the reserved ``hub_token`` (required; ``None`` is returned for
      foreign cards or non-interactive controls that carry no token),
    * ``action`` — the reserved ``hub_action`` decision key,
    * ``form`` — ``form_value`` field-name -> entered text (``{}`` when absent),
    * ``operator_id`` — the clicker's open/union id when present,
    * ``chat_id`` — the source chat id when present.

    Never raises: a malformed payload yields ``None``.
    """
    try:
        # Accept the full callback body, or the inner event directly.
        event = _attr_or_key(payload, "event")
        if event is None:
            event = payload

        action = _attr_or_key(event, "action")
        if action is None:
            return None

        value = _attr_or_key(action, "value")
        if not isinstance(value, dict):
            return None
        token = value.get(TOKEN_KEY)
        if not token:
            return None

        form = _attr_or_key(action, "form_value")
        if not isinstance(form, dict):
            form = {}

        operator = _attr_or_key(event, "operator")
        operator_id = (
            _attr_or_key(operator, "open_id")
            or _attr_or_key(operator, "operator_id")
            or _attr_or_key(operator, "union_id")
        )

        context = _attr_or_key(event, "context")
        chat_id = _attr_or_key(context, "open_chat_id") or _attr_or_key(event, "open_chat_id")

        decision = {
            "token": str(token),
            "action": value.get(ACTION_KEY),
            "value": dict(value),
            "form": dict(form),
            "operator_id": operator_id,
            "chat_id": chat_id,
        }
        command = action_to_cli_command(decision)
        if command:
            decision["cli_command"] = command
        return decision
    except Exception:  # noqa: BLE001 - never crash on a malformed callback
        logger.exception("feishu: failed to parse card action payload")
        return None


def action_to_cli_command(decision: Dict[str, Any]) -> Optional[str]:
    """Return a suggested ``claude-hub`` command for a parsed card action."""
    value = decision.get("value")
    if not isinstance(value, dict):
        value = {}
    form = decision.get("form")
    if not isinstance(form, dict):
        form = {}

    action = str(decision.get("action") or value.get(ACTION_KEY) or "")
    workspace_id = value.get("workspace_id")
    task_id = value.get("task_id")
    tab_id = value.get("tab_id")
    session_id = value.get("session_id")

    if action == "focus" and task_id:
        suffix = f" --workspace-id {workspace_id}" if workspace_id else ""
        return f"claude-hub task get {task_id}{suffix}"
    if action == "terminal" and tab_id:
        return f"claude-hub feishu build-card --kind terminal --tab-id {tab_id}"
    if action == "send" and session_id:
        message = form.get("message") or "<message>"
        return f"claude-hub session send {session_id} --message {message!r}"
    if action in {"approve", "confirm"} and task_id:
        suffix = f" --workspace-id {workspace_id}" if workspace_id else ""
        return f"claude-hub task accept {task_id}{suffix}"
    if action == "reject" and task_id:
        return f"claude-hub task send {workspace_id or '<workspace_id>'} {task_id} --message '<feedback>'"
    return None
