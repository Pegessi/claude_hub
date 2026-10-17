"""Feishu cards for workspace agents, reports, and task detail."""

from typing import Any, Dict, List, Optional

from claude_hub.cli.feishu_cards_impl.inventory import _ACTIVE_STATUSES, _get
from claude_hub.cli.feishu_cards_impl.primitives import (
    _action_value,
    _header,
    _markdown,
    _note,
    _plain_text,
    _wrap,
)


def build_overview_card(
    workspace_id: str,
    tasks: Any,
    sessions: Any,
    *,
    name: Optional[str] = None,
    markdown_documents: Any = None,
    snapshot_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a workspace overview: active/recent tasks plus an agent summary."""
    label = name or workspace_id
    task_items = [t for t in (tasks or []) if isinstance(t, dict)]
    session_items = [s for s in (sessions or []) if isinstance(s, dict)]

    active = [t for t in task_items if str(_get(t, "status", "")) in _ACTIVE_STATUSES]
    recent = [t for t in task_items if str(_get(t, "status", "")) not in _ACTIVE_STATUSES]

    elements: List[Dict[str, Any]] = []
    if active:
        rows = "\n".join(
            f"- `{_get(t, 'id', '?')}` {_get(t, 'title', '')} "
            f"**[{_get(t, 'status', '?')}]**".rstrip()
            for t in active
        )
        elements.append(_markdown(f"**Active tasks**\n{rows}"))
    if recent:
        rows = "\n".join(
            f"- `{_get(t, 'id', '?')}` {_get(t, 'title', '')} "
            f"**[{_get(t, 'status', '?')}]**".rstrip()
            for t in recent[:5]
        )
        elements.append(_markdown(f"**Recent**\n{rows}"))
    if not active and not recent:
        elements.append(_markdown("_No tasks yet._"))

    if session_items:
        rows = "\n".join(
            f"- `{_get(s, 'id', '?')}` {_get(s, 'role', '')} · {_get(s, 'agent_type', '?')} · "
            f"{_get(s, 'runtime_status', _get(s, 'status', '?'))}"
            + (
                f" · task `{_get(s, 'current_task_id', '')}`"
                if _get(s, "current_task_id", "")
                else ""
            )
            for s in session_items[:5]
        )
        elements.append(_markdown(f"**Agents**\n{rows}"))

    documents = [d for d in (markdown_documents or []) if isinstance(d, dict)]
    doc_note = f"{len(documents)} markdown doc(s)"
    if snapshot_path:
        doc_note += f" · snapshot: {snapshot_path}"
    elements.append(
        _note(f"{len(session_items)} agent(s) · {doc_note} · `/ag` to list · `/t` for tasks")
    )
    return _wrap(_header(f"Workspace · {label}", "status"), elements)


_ROLE_GROUPS = (
    ("Orchestrator", ("orchestrator",)),
    ("Reviewer", ("reviewer",)),
    ("Terminal", ("worker", "terminal", "dispatcher")),
)


def build_agents_card(
    workspace_id: str, sessions: Any, *, name: Optional[str] = None
) -> Dict[str, Any]:
    """Build an agent roster grouped by role."""
    items = [s for s in (sessions or []) if isinstance(s, dict)]

    def _role(sess: Dict[str, Any]) -> str:
        return str(_get(sess, "role", "")).lower()

    elements: List[Dict[str, Any]] = []
    seen: set[int] = set()
    for label, roles in _ROLE_GROUPS:
        group = [s for s in items if _role(s) in roles]
        seen.update(id(s) for s in group)
        if not group:
            continue
        lines = [f"**{label}**"]
        for s in group:
            sid = _get(s, "id", "?")
            agent_type = _get(s, "agent_type", "?")
            runtime = _get(s, "runtime_status", "?")
            line = f"- `{sid}` · {agent_type} · {runtime}"
            current = _get(s, "current_task_id", "")
            if current:
                line += f" · task `{current}`"
            lines.append(line)
        elements.append(_markdown("\n".join(lines)))

    other = [s for s in items if id(s) not in seen]
    if other:
        elements.append(
            _markdown(
                "**Other**\n"
                + "\n".join(
                    f"- `{_get(s, 'id', '?')}` · {_get(s, 'agent_type', '?')} · "
                    f"{_get(s, 'runtime_status', '?')}"
                    for s in other
                )
            )
        )
    if not elements:
        elements.append(_markdown("_No agents in this workspace yet._"))
    return _wrap(_header(f"Agents · {name or workspace_id}", "status"), elements)


def _report_line(report: Dict[str, Any]) -> str:
    """Render one progress report as a markdown bullet."""
    state = _get(report, "state", "?")
    message = _get(report, "message_zh", "") or _get(report, "message", "")
    when = _get(report, "created_at", "")
    head = f"**[{state}]** {message}".rstrip()
    return f"{head}\n_{when}_" if when else head


def build_reports_card(task: Any, reports: Any) -> Dict[str, Any]:
    """Build a newest-first progress-report history card for a task."""
    task_id = _get(task, "id", "?")
    items = [r for r in (reports or []) if isinstance(r, dict)]
    items.sort(key=lambda r: str(_get(r, "created_at", "")), reverse=True)

    elements: List[Dict[str, Any]] = []
    if items:
        elements.append(_markdown("\n\n".join(_report_line(r) for r in items)))
    else:
        elements.append(_markdown("_No progress reports yet._"))
    return _wrap(_header(f"Reports · {task_id}", "status"), elements)


def build_task_detail_card(
    task: Any,
    session: Any,
    latest_report: Any,
    *,
    acceptance_report: Any = None,
    token: str,
) -> Dict[str, Any]:
    """Build a task detail card with status, latest progress, and action buttons."""
    task_id = _get(task, "id", "?")
    workspace_id = _get(task, "workspace_id", "")
    session_id = _get(session, "id", "") if isinstance(session, dict) else ""
    if not session_id:
        session_id = _get(task, "session_id", "")
    tab_id = _get(session, "tab_id", "") if isinstance(session, dict) else ""
    fields = [
        ("Status", _get(task, "status", "?")),
        ("Mode", _get(task, "task_mode", _get(task, "mode", ""))),
        ("Agent", _get(task, "agent_type", "")),
        (
            "Review",
            f"{_get(task, 'reviewed_cycle', 0)}/{_get(task, 'review_cycle', 0)}",
        ),
        (
            "Acceptance",
            "requested" if _get(task, "human_acceptance_requested_at", "") else "",
        ),
    ]
    goal_packet = _get(task, "goal_packet", None)
    if isinstance(goal_packet, dict):
        fields.append(("Goal Packet", _get(goal_packet, "status", "")))
    body = "\n".join(f"**{label}**: {value}" for label, value in fields if value not in ("", None))
    elements: List[Dict[str, Any]] = [
        _markdown(f"**{_get(task, 'title', '(untitled)')}**\n`{task_id}`"),
    ]
    if body:
        elements.append(_markdown(body))

    if latest_report:
        elements.append({"tag": "hr"})
        elements.append(_markdown(f"**Latest progress**\n{_report_line(latest_report)}"))

    acceptance_source = acceptance_report or latest_report
    if acceptance_source:
        acceptance = _get(acceptance_source, "acceptance_check", [])
        items = (
            [item for item in acceptance if isinstance(item, dict)]
            if isinstance(acceptance, list)
            else []
        )
        if items:
            summary = ", ".join(f"{_get(item, 'status', '?')}" for item in items)
            rows = []
            for item in items[:5]:
                criterion = _get(item, "criterion", "criterion")
                status = _get(item, "status", "?")
                evidence = _get(item, "evidence", "")
                suffix = f": {evidence}" if evidence else ""
                rows.append(f"- `{status}` {criterion}{suffix}")
            if len(items) > 5:
                rows.append(f"- ... {len(items) - 5} more")
            elements.append(_markdown("**Acceptance check**\n" + "\n".join(rows)))
            source = " ".join(
                part
                for part in (
                    str(_get(acceptance_source, "state", "") or ""),
                    str(_get(acceptance_source, "created_at", "") or ""),
                )
                if part
            )
            suffix = f" · source {source}" if source else ""
            elements.append(_note(f"acceptance_check: {len(items)} item(s) · {summary}{suffix}"))

    buttons = [
        {
            "tag": "button",
            "text": _plain_text("Focus"),
            "type": "primary",
            "value": _action_value(token, "focus", workspace_id=workspace_id, task_id=task_id),
        },
        {
            "tag": "button",
            "text": _plain_text("Terminal"),
            "type": "default",
            "value": _action_value(token, "terminal", tab_id=tab_id),
        },
        {
            "tag": "button",
            "text": _plain_text("Send"),
            "type": "default",
            "value": _action_value(token, "send", session_id=session_id, task_id=task_id),
        },
    ]
    elements.append({"tag": "action", "actions": buttons})
    return _wrap(_header(f"Task · {task_id}", "task"), elements)
