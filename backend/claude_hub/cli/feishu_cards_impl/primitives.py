"""Shared Feishu card JSON primitives."""

from typing import Any, Dict, List

from claude_hub.cli.feishu_cards_impl.contract import ACTION_KEY, TOKEN_KEY

_TEMPLATES = {
    "approval": "orange",
    "needs_input": "blue",
    "plan_confirm": "purple",
    "status": "turquoise",
    "task": "grey",
    "workspaces": "turquoise",
    "overview": "turquoise",
    "agents": "turquoise",
    "task_detail": "blue",
    "reports": "turquoise",
    "terminal": "blue",
    "lessons": "turquoise",
    "tabs": "turquoise",
    "tab_status": "turquoise",
    "network": "green",
    "filesystem": "blue",
    "remote_profiles": "purple",
    "remote_filesystem": "purple",
    "result": "grey",
    "action_catalog": "blue",
}


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
