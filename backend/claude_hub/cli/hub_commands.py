"""Shared ``/hub`` chat command logic for Feishu bot integrations.

The single public entry point, :func:`run_hub_chat_command`, strips Feishu
mention tokens, parses a whitelisted ``/hub`` command, executes it by calling
the local backend through a :class:`~claude_hub.cli.client.HubClient`, and
returns the reply string. It never raises: backend failures are mapped to a
short ``error: ...`` string and unknown / malformed commands fall back to the
help text. Messages not addressed to the bot (no ``/hub`` prefix) return the
empty string so the bot stays quiet in group chats.

This is a reusable, lark-independent helper: a user's own Feishu bot can call
it directly to turn an incoming chat message into a backend action and a reply.
Keeping it free of any lark-oapi dependency also makes it unit-testable with an
``httpx.MockTransport``-backed client (see ``tests/test_hub_commands.py``).
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List

from claude_hub.cli.client import HubClient, HubError

# Mention tokens injected by Feishu look like ``@_user_1``.
_MENTION_RE = re.compile(r"@_user_\d+")

HELP_TEXT = (
    "Claude Hub bot commands:\n"
    "/hub help — show this help\n"
    "/hub workspaces — list workspaces\n"
    "/hub status <workspace_id> — task summary for a workspace\n"
    '/hub task create <workspace_id> "<title>" "<prompt>" — create a task\n'
    "/hub task start <task_id> — queue/dispatch a task\n"
    '/hub task abort <task_id> "<reason>" — abort an active task\n'
    "/hub lessons <workspace_id> [query] — top feedback lessons"
)


def strip_mentions(text: str) -> str:
    """Remove Feishu mention tokens and surrounding whitespace."""
    return _MENTION_RE.sub("", text).strip()


def run_hub_chat_command(client: HubClient, text: str) -> str:
    """Parse and execute a whitelisted ``/hub`` command, returning a reply.

    Never raises: any backend / IO error is converted to a short ``error: ...``
    string, and unknown / malformed commands fall back to the help text.
    Returns an empty string for messages that are not addressed to the bot.
    """
    cleaned = strip_mentions(text)
    if not cleaned.startswith("/hub"):
        return ""

    try:
        tokens = shlex.split(cleaned)
    except ValueError:
        return HELP_TEXT

    # tokens[0] == "/hub"
    args = tokens[1:]
    if not args:
        return HELP_TEXT

    verb = args[0]

    try:
        if verb == "help":
            return HELP_TEXT

        if verb == "workspaces":
            return _format_workspaces(client.list_workspaces())

        if verb == "status":
            if len(args) < 2:
                return "usage: /hub status <workspace_id>"
            workspace_id = args[1]
            return _format_board(workspace_id, client.get_board(workspace_id))

        if verb == "task":
            return _handle_task(client, args)

        if verb == "lessons":
            if len(args) < 2:
                return "usage: /hub lessons <workspace_id> [query]"
            workspace_id = args[1]
            query = " ".join(args[2:]) if len(args) > 2 else ""
            lessons = client.list_lessons(workspace_id, {"query": query, "limit": 5})
            return _format_lessons(workspace_id, lessons)

    except HubError as e:
        return f"error: {e.message}"

    return HELP_TEXT


def _handle_task(client: HubClient, args: List[str]) -> str:
    """Handle ``/hub task ...`` subcommands."""
    if len(args) < 2:
        return "usage: /hub task <create|start|abort> ..."
    action = args[1]

    if action == "create":
        # /hub task create <workspace_id> "<title>" "<prompt>"
        if len(args) < 5:
            return 'usage: /hub task create <workspace_id> "<title>" "<prompt>"'
        workspace_id, title, prompt = args[2], args[3], args[4]
        body: Dict[str, Any] = {"title": title, "prompt": prompt}
        task = client.create_task(workspace_id, body)
        return f"Created task {_get(task, 'id', '?')}: {_get(task, 'title', '')}"

    if action == "start":
        if len(args) < 3:
            return "usage: /hub task start <task_id>"
        task_id = args[2]
        task = client.start_task(task_id, {})
        return f"Started task {_get(task, 'id', task_id)} (status: {_get(task, 'status', '?')})"

    if action == "abort":
        # /hub task abort <task_id> "<reason>"
        if len(args) < 4:
            return 'usage: /hub task abort <task_id> "<reason>"'
        task_id, reason = args[2], args[3]
        task = client.abort_task(task_id, {"reason": reason})
        return f"Aborted task {_get(task, 'id', task_id)} (status: {_get(task, 'status', '?')})"

    return "usage: /hub task <create|start|abort> ..."


def _get(obj: Any, key: str, default: Any = "") -> Any:
    """Read ``key`` from a dict-like API response, falling back to ``default``."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


def _format_workspaces(workspaces: Any) -> str:
    """Summarize a list of workspaces into a short reply."""
    items = list(workspaces or []) if isinstance(workspaces, list) else []
    if not items:
        return "No workspaces configured."
    lines = ["Workspaces:"]
    for ws in items:
        lines.append(f"- {_get(ws, 'id', '?')}: {_get(ws, 'name', '')}")
    return "\n".join(lines)


def _format_board(workspace_id: str, board: Any) -> str:
    """Summarize a workspace board into a short human-readable string."""
    tasks = list(_get(board, "tasks", []) or []) if isinstance(board, dict) else []
    counts: Dict[str, int] = {}
    for task in tasks:
        status = str(_get(task, "status", "?"))
        counts[status] = counts.get(status, 0) + 1

    lines = [f"Workspace {workspace_id}: {len(tasks)} task(s)"]
    if counts:
        summary = ", ".join(f"{status}={n}" for status, n in sorted(counts.items()))
        lines.append(summary)
    for task in tasks[:5]:
        lines.append(
            f"- {_get(task, 'id', '?')}: {_get(task, 'title', '')} "
            f"[{_get(task, 'status', '?')}]"
        )
    return "\n".join(lines)


def _format_lessons(workspace_id: str, lessons: Any) -> str:
    """Summarize feedback lessons into a short reply."""
    items = list(lessons or []) if isinstance(lessons, list) else []
    if not items:
        return f"No lessons found for {workspace_id}."
    lines = [f"Lessons for {workspace_id}:"]
    for lesson in items:
        title = _get(lesson, "title", "") or _get(lesson, "summary", "")
        lines.append(f"- {_get(lesson, 'id', '?')}: {title}")
    return "\n".join(lines)
