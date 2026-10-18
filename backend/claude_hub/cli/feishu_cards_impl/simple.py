"""Compact terminal, lesson, and task Feishu cards."""

from typing import Any, Dict, List

from claude_hub.cli.feishu_cards_impl.inventory import _get, build_result_card
from claude_hub.cli.feishu_cards_impl.primitives import _header, _markdown, _wrap


def build_terminal_card(tab_id: str, text: str, *, max_lines: int = 40) -> Dict[str, Any]:
    """Build a display-only terminal snapshot card."""
    lines = (text or "").splitlines()
    if max_lines > 0:
        lines = lines[-max_lines:]
    snapshot = "\n".join(lines)
    body = f"```\n{snapshot}\n```" if snapshot else "_(no output)_"
    return _wrap(_header(f"Terminal · {tab_id}", "task"), [_markdown(body)])


def build_lessons_card(workspace_id: str, lessons: Any) -> Dict[str, Any]:
    """Build a display-only list of lessons for a workspace."""
    items = [le for le in (lessons or []) if isinstance(le, dict)]
    if not items:
        return build_result_card(f"Lessons · {workspace_id}", "_No lessons found._", kind="status")
    rows = "\n".join(f"- `{_get(le, 'id', '?')}` {_get(le, 'title', '')}".rstrip() for le in items)
    return _wrap(_header(f"Lessons · {workspace_id}", "status"), [_markdown(rows)])


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
