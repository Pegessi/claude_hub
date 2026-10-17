"""Feishu cards for status and infrastructure inventories."""

from typing import Any, Dict, List

from claude_hub.cli.feishu_cards_impl.primitives import (
    _header,
    _markdown,
    _note,
    _wrap,
)


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
            _row(
                f"`{marker}` {_get(item, 'name', '?')}{suffix}",
                str(_get(item, "path", "")),
            )
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
    listing: Any,
    *,
    title: str = "Filesystem",
    kind: str = "filesystem",
    limit: int = 12,
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
            "Remote Profiles",
            "_No remote profiles configured._",
            kind="remote_profiles",
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
        (
            "`terminal`",
            "`claude-hub feishu build-card --kind terminal --tab-id <tab_id>`",
        ),
        ("`send`", "`claude-hub session send <session_id> --message ...`"),
        (
            "`approve` / `confirm`",
            "`claude-hub task accept <task_id>` when task_id is present",
        ),
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
