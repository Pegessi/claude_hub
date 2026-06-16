"""Feishu interactive-card builders for the Claude Hub CLI bridge.

These are PURE functions that return Feishu interactive-card JSON (the classic
``config`` / ``header`` / ``elements`` schema accepted by the IM
``CreateMessage`` API with ``msg_type="interactive"``). They take no lark-oapi
dependency and never perform IO, so they are fully unit-testable.

Scenario A (an external agent drives the CLI, which pushes a card to a human and
blocks for the human's decision) needs every *interactive* control to carry a
correlation token so the ``card.action.trigger`` callback can route the human's
decision back to the originating CLI invocation. The contract is:

* Every actionable control's ``value`` is a dict with two reserved keys:
  ``hub_token`` (the opaque correlation token) and ``hub_action`` (a short
  decision key such as ``approve`` / ``reject`` / ``submit``). Builders may add
  extra keys, but those two are always present on interactive cards.
* Input is collected with a ``form`` container: its ``input`` elements are named
  so the callback's ``form_value`` maps field-name -> entered text, and the
  submit button carries ``hub_action="submit"``.

Display-only cards (status board, task detail) carry no token because they need
no response.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Reserved keys embedded in every interactive control's ``value`` payload. The
# card.action.trigger handler reads these to correlate a human decision with the
# blocked CLI invocation.
TOKEN_KEY = "hub_token"
ACTION_KEY = "hub_action"

# The kinds of card ``feishu send-card`` can build.
INTERACTIVE_KINDS = ("approval", "needs_input", "plan_confirm")
DISPLAY_KINDS = ("status", "task")
CARD_KINDS = INTERACTIVE_KINDS + DISPLAY_KINDS

# Header colours per kind (Feishu template names).
_TEMPLATES = {
    "approval": "orange",
    "needs_input": "blue",
    "plan_confirm": "purple",
    "status": "turquoise",
    "task": "grey",
}


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

    This is the inverse of the card-building contract above and the single
    integration point for an EXTERNAL bot: when your own Feishu bot receives a
    ``card.action.trigger`` event, pass the raw callback body here to pull out
    the correlation token and the human's choice, then POST the result to
    ``/api/feishu/cards/result`` (e.g. via
    :meth:`claude_hub.cli.client.HubClient.submit_card_result`).

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

        return {
            "token": str(token),
            "action": value.get(ACTION_KEY),
            "form": dict(form),
            "operator_id": operator_id,
            "chat_id": chat_id,
        }
    except Exception:  # noqa: BLE001 - never crash on a malformed callback
        logger.exception("feishu: failed to parse card action payload")
        return None


def _plain_text(content: str) -> Dict[str, Any]:
    """Build a ``plain_text`` text object."""
    return {"tag": "plain_text", "content": content}


def _markdown(content: str) -> Dict[str, Any]:
    """Build a ``lark_md`` (markdown) ``div`` element."""
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _header(title: str, kind: str) -> Dict[str, Any]:
    """Build a card header with a kind-appropriate colour template."""
    return {"title": _plain_text(title), "template": _TEMPLATES.get(kind, "blue")}


def _action_value(token: str, action: str, **extra: Any) -> Dict[str, Any]:
    """Build the reserved ``value`` payload carried by an interactive control."""
    value: Dict[str, Any] = {TOKEN_KEY: token, ACTION_KEY: action}
    value.update(extra)
    return value


def _button(text: str, token: str, action: str, button_type: str = "default") -> Dict[str, Any]:
    """Build an interactive button that posts ``action`` for ``token``."""
    return {
        "tag": "button",
        "text": _plain_text(text),
        "type": button_type,
        "value": _action_value(token, action),
    }


def _note(content: str) -> Dict[str, Any]:
    """Build a small footnote element (used to surface the token)."""
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}


def _wrap(header: Dict[str, Any], elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Wrap header + elements in the standard card envelope."""
    return {
        "config": {"wide_screen_mode": True},
        "header": header,
        "elements": elements,
    }


def build_approval_card(
    token: str,
    title: str,
    body: str,
    *,
    options: Optional[Sequence[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Build an approval card with one button per option.

    ``options`` is a sequence of ``{"text": ..., "action": ..., "type": ...}``
    mappings; ``type`` is an optional Feishu button style (``primary`` /
    ``danger`` / ``default``). When omitted, a default Approve/Reject pair is
    used.
    """
    if not options:
        options = [
            {"text": "Approve", "action": "approve", "type": "primary"},
            {"text": "Reject", "action": "reject", "type": "danger"},
        ]
    buttons = [
        _button(
            opt.get("text", opt.get("action", "?")),
            token,
            opt.get("action", "approve"),
            opt.get("type", "default"),
        )
        for opt in options
    ]
    elements: List[Dict[str, Any]] = [
        _markdown(body),
        {"tag": "action", "actions": buttons},
        _note(f"token: {token}"),
    ]
    return _wrap(_header(title, "approval"), elements)


def build_needs_input_card(
    token: str,
    title: str,
    prompt: str,
    *,
    field_name: str = "reply",
    placeholder: str = "Type your response…",
    submit_text: str = "Submit",
) -> Dict[str, Any]:
    """Build a single-field input card.

    The ``form`` container groups a named ``input`` with a submit button so the
    callback's ``form_value`` carries ``{field_name: entered_text}`` and the
    button value carries ``hub_action="submit"``.
    """
    form = {
        "tag": "form",
        "name": "hub_input_form",
        "elements": [
            {
                "tag": "input",
                "name": field_name,
                "placeholder": _plain_text(placeholder),
            },
            {
                "tag": "button",
                "text": _plain_text(submit_text),
                "type": "primary",
                "action_type": "form_submit",
                "name": "hub_submit",
                "value": _action_value(token, "submit", field=field_name),
            },
        ],
    }
    elements: List[Dict[str, Any]] = [
        _markdown(prompt),
        form,
        _note(f"token: {token}"),
    ]
    return _wrap(_header(title, "needs_input"), elements)


def build_plan_confirm_card(
    token: str,
    title: str,
    plan: str,
    *,
    confirm_text: str = "Confirm",
    reject_text: str = "Reject",
) -> Dict[str, Any]:
    """Build a plan-confirmation card with Confirm / Reject buttons."""
    elements: List[Dict[str, Any]] = [
        _markdown(plan),
        {
            "tag": "action",
            "actions": [
                _button(confirm_text, token, "confirm", "primary"),
                _button(reject_text, token, "reject", "danger"),
            ],
        },
        _note(f"token: {token}"),
    ]
    return _wrap(_header(title, "plan_confirm"), elements)


def _get(obj: Any, key: str, default: Any = "") -> Any:
    """Read ``key`` from a dict-like API response, falling back to ``default``."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


def build_status_card(workspace_id: str, board: Any, *, limit: int = 8) -> Dict[str, Any]:
    """Build a display-only status board card for a workspace.

    No token: this card requires no human response.
    """
    tasks = list(_get(board, "tasks", []) or []) if isinstance(board, dict) else []
    counts: Dict[str, int] = {}
    for task in tasks:
        status = str(_get(task, "status", "?"))
        counts[status] = counts.get(status, 0) + 1

    summary = ", ".join(f"**{status}**: {n}" for status, n in sorted(counts.items()))
    elements: List[Dict[str, Any]] = [
        _markdown(f"Workspace `{workspace_id}` — {len(tasks)} task(s)"),
    ]
    if summary:
        elements.append(_markdown(summary))
    if tasks:
        elements.append({"tag": "hr"})
        rows = "\n".join(
            f"- `{_get(t, 'id', '?')}` {_get(t, 'title', '')} " f"**[{_get(t, 'status', '?')}]**"
            for t in tasks[:limit]
        )
        elements.append(_markdown(rows))
        if len(tasks) > limit:
            elements.append(_note(f"… and {len(tasks) - limit} more"))
    return _wrap(_header(f"Status · {workspace_id}", "status"), elements)


def build_task_card(task: Any) -> Dict[str, Any]:
    """Build a display-only detail card for a single task."""
    task_id = _get(task, "id", "?")
    fields = [
        ("Status", _get(task, "status", "?")),
        ("Mode", _get(task, "mode", "?")),
        ("Agent", _get(task, "target_agent", _get(task, "agent", "?"))),
    ]
    body = "\n".join(f"**{label}**: {value}" for label, value in fields if value not in ("", None))
    elements: List[Dict[str, Any]] = [
        _markdown(f"**{_get(task, 'title', '(untitled)')}**\n`{task_id}`"),
    ]
    if body:
        elements.append(_markdown(body))
    prompt = _get(task, "prompt", "")
    if prompt:
        elements.append({"tag": "hr"})
        elements.append(_markdown(str(prompt)))
    return _wrap(_header(f"Task · {task_id}", "task"), elements)
