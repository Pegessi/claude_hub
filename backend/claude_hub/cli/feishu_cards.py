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
    "tabs": "turquoise",
    "tab_status": "turquoise",
    "network": "green",
    "filesystem": "blue",
    "remote_profiles": "purple",
    "remote_filesystem": "purple",
    "result": "grey",
    "action_catalog": "blue",
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


def _count_by(items: list[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        label = str(_get(item, key, "unknown") or "unknown")
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def _counts_text(counts: Dict[str, int]) -> str:
    return " · ".join(f"{key}={value}" for key, value in counts.items()) or "none"


def _reports_by_task(board: Any) -> Dict[str, Dict[str, Any]]:
    reports: Dict[str, Dict[str, Any]] = {}
    for report in (_get(board, "reports", []) or []) if isinstance(board, dict) else []:
        if isinstance(report, dict) and _get(report, "task_id", ""):
            reports[str(_get(report, "task_id"))] = report
    return reports


def build_status_card(workspace_id: str, board: Any, *, limit: int = 8) -> Dict[str, Any]:
    """Build a display-only status board card for a workspace.

    No token: this card requires no human response.
    """
    name = _workspace_name(workspace_id, board)
    tasks = list(_get(board, "tasks", []) or []) if isinstance(board, dict) else []
    sessions = [s for s in (_get(board, "sessions", []) or []) if isinstance(s, dict)]
    documents = [d for d in (_get(board, "markdown_documents", []) or []) if isinstance(d, dict)]
    reports = _reports_by_task(board)
    ordered = sorted(
        tasks,
        key=lambda t: _STATUS_SORT.get(str(_get(t, "status", "")), _STATUS_SORT_DEFAULT),
    )

    elements: List[Dict[str, Any]] = [
        _markdown(
            f"**{name}** — {len(tasks)} task(s), {len(sessions)} agent(s)\n"
            f"tasks: {_counts_text(_count_by([t for t in tasks if isinstance(t, dict)], 'status'))}\n"
            f"agents: {_counts_text(_count_by(sessions, 'runtime_status'))}"
        ),
    ]
    if ordered:
        elements.append(_row("**Task**", "**Status / latest**"))
        for t in ordered[:limit]:
            report = reports.get(str(_get(t, "id", "")), {})
            latest = _get(report, "state", "")
            msg = _get(report, "message_zh", "") or _get(report, "message", "")
            right = f"**{_get(t, 'status', '?')}**"
            if latest:
                right += f"\n{latest}: {msg}".rstrip()
            elements.append(_row(str(_get(t, "title", "")), right))
        if len(ordered) > limit:
            elements.append(_note(f"… and {len(ordered) - limit} more"))
    if documents or _get(board, "snapshot_path", ""):
        elements.append(
            _note(
                f"{len(documents)} markdown document(s)"
                + (
                    f" · snapshot: {_get(board, 'snapshot_path', '')}"
                    if _get(board, "snapshot_path", "")
                    else ""
                )
            )
        )
    return _wrap(_header(f"Status · {name}", "status"), elements)


def build_result_card(title: str, body: str, *, kind: str = "status") -> Dict[str, Any]:
    """Build a generic display card with a title and a markdown body."""
    return _wrap(_header(title, kind), [_markdown(body)])


def _item_rows(items: list[Dict[str, Any]], *, limit: int) -> list[Dict[str, Any]]:
    """Render filesystem-style items as compact markdown rows."""
    rows: list[Dict[str, Any]] = []
    for item in items[:limit]:
        marker = "[dir]" if _get(item, "is_dir", False) else "[file]"
        suffix = " ->" if _get(item, "is_symlink", False) else ""
        rows.append(
            _row(f"`{marker}` {_get(item, 'name', '?')}{suffix}", str(_get(item, "path", "")))
        )
    if len(items) > limit:
        rows.append(_note(f"... and {len(items) - limit} more"))
    return rows


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


def build_tabs_card(tabs: Any, statuses: Any = None, *, limit: int = 12) -> Dict[str, Any]:
    """Build a display-only terminal tab inventory card."""
    items = [tab for tab in (tabs or []) if isinstance(tab, dict)]
    status_by_tab = {
        str(_get(status, "tab_id", "")): status
        for status in (statuses or [])
        if isinstance(status, dict)
    }
    if not items:
        return build_result_card("Tabs", "_No terminal tabs._", kind="tabs")

    elements: List[Dict[str, Any]] = [_row("**Tab**", "**Runtime**")]
    for tab in items[:limit]:
        tab_id = str(_get(tab, "id", "?"))
        status = status_by_tab.get(tab_id, {})
        name = _get(tab, "name", "")
        agent_type = _get(tab, "agent_type", "?")
        target = _get(tab, "target", "local")
        workspace = _get(tab, "workspace_name", "") or _get(tab, "workspace_id", "")
        left = f"`{tab_id}` {name}\n{agent_type} · {target}".rstrip()
        if workspace:
            left += f" · {workspace}"
        runtime = _get(status, "status", "unknown") if status else "unknown"
        detail = _get(status, "status_text", "") if status else ""
        if not detail:
            detail = _get(tab, "cwd", "") or _get(tab, "remote_cwd", "")
        elements.append(_row(left, f"**{runtime}**\n{detail}".rstrip()))
    if len(items) > limit:
        elements.append(_note(f"... and {len(items) - limit} more"))
    return _wrap(_header("Tabs", "tabs"), elements)


def build_tab_status_card(statuses: Any, *, limit: int = 12) -> Dict[str, Any]:
    """Build a display-only runtime-status card for terminal agents."""
    items = [status for status in (statuses or []) if isinstance(status, dict)]
    if not items:
        return build_result_card("Tab Status", "_No runtime status samples._", kind="tab_status")
    elements: List[Dict[str, Any]] = [_row("**Agent**", "**Status**")]
    for status in items[:limit]:
        left = f"`{_get(status, 'tab_id', '?')}` {_get(status, 'tab_name', '')}\n{_get(status, 'agent_type', '?')}"
        right = f"**{_get(status, 'status', '?')}**\n{_get(status, 'status_text', '')}".rstrip()
        detail = _get(status, "detail", "")
        if detail:
            right += f"\n{detail}"
        elements.append(_row(left, right))
    if len(items) > limit:
        elements.append(_note(f"... and {len(items) - limit} more"))
    return _wrap(_header("Tab Status", "tab_status"), elements)


def build_network_card(network: Any) -> Dict[str, Any]:
    """Build a display-only network access card."""
    hostname = _get(network, "hostname", "localhost")
    addresses = [addr for addr in (_get(network, "addresses", []) or []) if isinstance(addr, dict)]
    if addresses:
        rows = "\n".join(
            f"- `{_get(addr, 'address', '?')}` — {_get(addr, 'label', 'LAN IP')}"
            for addr in addresses
        )
    else:
        rows = "_No non-loopback IPv4 addresses found._"
    return _wrap(_header(f"Network · {hostname}", "network"), [_markdown(rows)])


def build_filesystem_card(
    listing: Any, *, title: str = "Filesystem", kind: str = "filesystem", limit: int = 12
) -> Dict[str, Any]:
    """Build a display-only local/remote directory listing card."""
    current = str(_get(listing, "current_path", ""))
    items = [item for item in (_get(listing, "items", []) or []) if isinstance(item, dict)]
    elements: List[Dict[str, Any]] = [
        _markdown(f"`{current}`" if current else "_No path returned._")
    ]
    if items:
        elements.append(_row("**Name**", "**Path**"))
        elements.extend(_item_rows(items, limit=limit))
    else:
        elements.append(_markdown("_Directory is empty._"))
    parent = _get(listing, "parent_path", "")
    if parent:
        elements.append(_note(f"parent: {parent}"))
    return _wrap(_header(title, kind), elements)


def build_remote_profiles_card(profiles: Any, *, limit: int = 12) -> Dict[str, Any]:
    """Build a display-only remote profile inventory card."""
    items = [profile for profile in (profiles or []) if isinstance(profile, dict)]
    if not items:
        return build_result_card(
            "Remote Profiles", "_No remote profiles configured._", kind="remote_profiles"
        )
    elements: List[Dict[str, Any]] = [_row("**Profile**", "**Target**")]
    for profile in items[:limit]:
        left = f"`{_get(profile, 'id', '?')}` {_get(profile, 'name', '')}".rstrip()
        host = _get(profile, "ssh_host", "?")
        user = _get(profile, "user", "")
        port = _get(profile, "port", 22)
        target = f"{user + '@' if user else ''}{host}:{port}"
        cwd = _get(profile, "default_cwd", "")
        elements.append(_row(left, f"{target}\n{cwd}".rstrip()))
    if len(items) > limit:
        elements.append(_note(f"... and {len(items) - limit} more"))
    return _wrap(_header("Remote Profiles", "remote_profiles"), elements)


def build_action_catalog_card() -> Dict[str, Any]:
    """Build a display-only catalog of card actions and suggested CLI commands."""
    rows = [
        ("`focus`", "`claude-hub task get <task_id> [--workspace-id <ws_id>]`"),
        ("`terminal`", "`claude-hub feishu build-card --kind terminal --tab-id <tab_id>`"),
        ("`send`", "`claude-hub session send <session_id> --message ...`"),
        ("`approve` / `confirm`", "`claude-hub task accept <task_id>` when task_id is present"),
        (
            "`reject`",
            "`claude-hub task send <ws_id> <task_id> --message '<feedback>'` when task_id is present",
        ),
    ]
    elements = [_row(action, command) for action, command in rows]
    elements.append(
        _note("parse-action returns cli_command when the callback value has enough ids")
    )
    return _wrap(_header("Feishu Actions", "action_catalog"), elements)


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
        ("Review", f"{_get(task, 'reviewed_cycle', 0)}/{_get(task, 'review_cycle', 0)}"),
        ("Acceptance", "requested" if _get(task, "human_acceptance_requested_at", "") else ""),
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
