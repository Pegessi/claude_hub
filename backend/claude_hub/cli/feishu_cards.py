"""Feishu interactive-card builders for the Claude Hub CLI bridge.

These are PURE functions that return Feishu interactive-card JSON (the classic
``config`` / ``header`` / ``elements`` schema accepted by the IM
``CreateMessage`` API with ``msg_type="interactive"``). They take no lark-oapi
dependency and never perform IO, so they are fully unit-testable.

Scenario A (an external agent drives the CLI; the agent is itself the Feishu bot,
so it sends the card and receives the ``card.action.trigger`` callback in one
process) needs every *interactive* control to carry a correlation token so the
callback can be matched back to the card the agent sent. The contract is:

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
# agent's card.action.trigger handler reads these to correlate a human decision
# with the card it sent.
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
)
CARD_KINDS = INTERACTIVE_KINDS + DISPLAY_KINDS

# Header colours per kind (Feishu template names).
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

        return {
            "token": str(token),
            "action": value.get(ACTION_KEY),
            "value": dict(value),
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


_ACTIVE_STATUSES = frozenset({"working", "review", "todo", "queued", "needs_input", "attention"})
_STATUS_SORT = {"review": 0, "todo": 1, "queued": 2, "done": 5}
_STATUS_SORT_DEFAULT = 3


def _workspace_name(workspace_id: str, board: Any) -> str:
    """Resolve a human workspace name from a board, falling back to the id."""
    if isinstance(board, dict):
        ws = board.get("workspace")
        if isinstance(ws, dict):
            name = ws.get("name")
            if name:
                return str(name)
    return workspace_id


def _column(content: str) -> Dict[str, Any]:
    """Build a single weighted ``column`` wrapping one markdown cell."""
    return {
        "tag": "column",
        "width": "weighted",
        "weight": 1,
        "elements": [_markdown(content)],
    }


def _row(left: str, right: str) -> Dict[str, Any]:
    """Build a two-column ``column_set`` grid row."""
    return {"tag": "column_set", "columns": [_column(left), _column(right)]}


def build_status_card(workspace_id: str, board: Any, *, limit: int = 8) -> Dict[str, Any]:
    """Build a display-only status board card for a workspace.

    No token: this card requires no human response.
    """
    name = _workspace_name(workspace_id, board)
    tasks = list(_get(board, "tasks", []) or []) if isinstance(board, dict) else []
    ordered = sorted(
        tasks,
        key=lambda t: _STATUS_SORT.get(str(_get(t, "status", "")), _STATUS_SORT_DEFAULT),
    )

    elements: List[Dict[str, Any]] = [
        _markdown(f"**{name}** — {len(tasks)} task(s)"),
    ]
    if ordered:
        elements.append(_row("**Title**", "**Status**"))
        for t in ordered[:limit]:
            elements.append(_row(str(_get(t, "title", "")), str(_get(t, "status", "?"))))
        if len(ordered) > limit:
            elements.append(_note(f"… and {len(ordered) - limit} more"))
    return _wrap(_header(f"Status · {name}", "status"), elements)


def build_result_card(title: str, body: str, *, kind: str = "status") -> Dict[str, Any]:
    """Build a generic display card with a title and a markdown body."""
    return _wrap(_header(title, kind), [_markdown(body)])


def build_workspaces_card(workspaces: Any) -> Dict[str, Any]:
    """Build a display-only list of workspaces with `/cd` hints."""
    items = [ws for ws in (workspaces or []) if isinstance(ws, dict)]
    if not items:
        return build_result_card("Workspaces", "_No workspaces yet._", kind="status")
    rows = "\n".join(
        f"- `{_get(ws, 'id', '?')}` **{_get(ws, 'name', '')}** "
        f"— `/cd {_get(ws, 'name', _get(ws, 'id', ''))}`".rstrip()
        for ws in items
    )
    return _wrap(_header("Workspaces", "status"), [_markdown(rows)])


def build_overview_card(
    workspace_id: str, tasks: Any, sessions: Any, *, name: Optional[str] = None
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

    elements.append(_note(f"{len(session_items)} agent(s) · `/ag` to list · `/t` for tasks"))
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
    ]
    body = "\n".join(f"**{label}**: {value}" for label, value in fields if value not in ("", None))
    elements: List[Dict[str, Any]] = [
        _markdown(f"**{_get(task, 'title', '(untitled)')}**\n`{task_id}`"),
    ]
    if body:
        elements.append(_markdown(body))

    if latest_report:
        elements.append({"tag": "hr"})
        elements.append(_markdown(f"**Latest progress**\n{_report_line(latest_report)}"))

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
