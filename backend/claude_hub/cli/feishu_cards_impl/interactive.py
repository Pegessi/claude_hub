"""Interactive Feishu approval and input cards."""

from typing import Any, Dict, List, Optional, Sequence

from claude_hub.cli.feishu_cards_impl.primitives import (
    _action_value,
    _button,
    _header,
    _markdown,
    _note,
    _plain_text,
    _wrap,
)


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
