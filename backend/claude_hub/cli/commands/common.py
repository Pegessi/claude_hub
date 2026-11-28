"""Shared helpers for CLI command modules."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import click

from claude_hub.cli.client import HubError
from claude_hub.models import redact_session_json_payload
from claude_hub.services.workspace_identity import (
    git_repo_identity,
    local_agent_cwd_allowed_for_workspace,
    resolve_path,
)


def is_missing_route_404(exc: HubError) -> bool:
    """Return True when a 404 came from an unregistered FastAPI route on an older server."""
    return exc.status == 404 and exc.message.strip() in {
        "Not Found",
        "HTTP 404: Not Found",
    }


LIFECYCLE_RECIPE = (
    "Lifecycle:\n"
    "- One Hub Workspace per git common-dir (`workspace ensure`); linked worktrees share it.\n"
    "- Cross-provider Subagent (no Task/review): `agent run --provider codex --prompt-file -`.\n"
    "- Tracked Task (1 isolated ephemeral Chat session per task, archived on done; `task cleanup`):\n"
    "  `task run --title ... --goal ... --done-when ...` (wake later via `--related-task-id`).\n"
    "- Feature-worktree session: `session create [WS] --agent-type claude --cwd . --env-preset NAME_OR_ID`\n"
    "  (any built-in or saved custom env preset; day1 is only an example; `--persistent` / `--reuse-existing` opt-in).\n"
    "- Never delete sessions you do not own."
)


def lifecycle_group_help(summary: str) -> str:
    """Build Click group help text with the shared lifecycle conventions appended."""
    return f"{summary}\n\n\b\n{LIFECYCLE_RECIPE}"


def resolve_cli_local_path(path: str) -> str:
    """Resolve a user-supplied local path in the CLI process working directory."""
    return str(Path(path).expanduser().resolve())


def resolve_workspace_id(
    client: Any,
    workspace_id: Optional[str] = None,
    *,
    cwd: Optional[str] = None,
) -> str:
    """Return explicit ``workspace_id`` or unambiguously match ``cwd`` read-only.

    Never creates a workspace. Raises ``ClickException`` when zero or multiple
    workspaces match ``cwd`` (e.g. both local and remote workspaces exist for
    the same repository).
    """
    if workspace_id is not None and workspace_id.strip():
        return workspace_id.strip()

    target_cwd = resolve_path(cwd or Path.cwd())
    workspaces = client.list_workspaces()
    items = workspaces if isinstance(workspaces, list) else []
    matches = [
        ws
        for ws in items
        if isinstance(ws, dict)
        and ws.get("id")
        and ws.get("path")
        and local_agent_cwd_allowed_for_workspace(str(ws["path"]), target_cwd)
    ]
    if not matches:
        raise click.ClickException(
            f"No Hub Workspace matches {target_cwd}. "
            "Pass WORKSPACE_ID explicitly or run `claude-hub workspace ensure` first."
        )
    if len(matches) > 1:
        # Only narrow by deepest directory when all matches are non-Git paths.
        if all(not git_repo_identity(str(ws["path"])).is_git for ws in matches):
            depths = [len(resolve_path(str(ws["path"])).parts) for ws in matches]
            max_depth = max(depths)
            deepest = [ws for ws, d in zip(matches, depths) if d == max_depth]
            if len(deepest) == 1:
                return str(deepest[0]["id"])
        candidates = ", ".join(
            f"{ws['id']} ({ws.get('name') or '?'}, target={ws.get('target') or 'local'})"
            for ws in matches
        )
        raise click.ClickException(
            f"Multiple Hub Workspaces match {target_cwd}: {candidates}. "
            "Pass WORKSPACE_ID explicitly."
        )
    return str(matches[0]["id"])


def read_prompt_option(
    prompt: Optional[str],
    prompt_file: Optional[str],
    *,
    fallback_goal: Optional[str] = None,
    done_when: Iterable[str] = (),
    out_of_scope: Iterable[str] = (),
    allow_tty_stdin: bool = False,
) -> str:
    """Resolve prompt text from ``--prompt``, ``--prompt-file`` ('-' for stdin), or ``--goal``."""
    if prompt is not None and prompt_file is not None:
        raise click.UsageError("Pass only one of --prompt and --prompt-file.")
    if prompt is not None:
        text = prompt
    elif prompt_file == "-" or (allow_tty_stdin and prompt_file is None and not sys.stdin.isatty()):
        text = sys.stdin.read()
    elif prompt_file is not None:
        try:
            with open(os.path.expanduser(prompt_file), encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise click.ClickException(f"cannot read --prompt-file: {exc}") from exc
    elif fallback_goal and fallback_goal.strip():
        parts = [fallback_goal.strip()]
        checks = [item.strip() for item in done_when if item and item.strip()]
        if checks:
            parts.append("Done when:\n" + "\n".join(f"- {item}" for item in checks))
        excluded = [item.strip() for item in out_of_scope if item and item.strip()]
        if excluded:
            parts.append("Out of scope:\n" + "\n".join(f"- {item}" for item in excluded))
        text = "\n\n".join(parts)
    else:
        raise click.UsageError("Provide --prompt, --prompt-file FILE ('-' for stdin), or --goal.")
    if not text.strip():
        raise click.UsageError("The prompt is empty.")
    return text


class _ChatStreamRenderer:
    """Stateful turn/delta stream formatter that preserves state across pages."""

    def __init__(self, *, max_lines: Optional[int] = None) -> None:
        self._max_lines = max_lines
        self._lines: list[str] = []
        self._current_msg_id: Optional[str] = None
        self._current_chunks: list[str] = []
        self._turn_had_assistant_delta = False

    def _append_line(self, line: str) -> None:
        self._lines.append(line)
        if self._max_lines is not None and len(self._lines) > self._max_lines * 2:
            self._lines = self._lines[-self._max_lines :]

    def _emit_prefixed(self, prefix_label: str, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        for idx, line in enumerate(cleaned.splitlines()):
            prefix = f"[{prefix_label}] " if idx == 0 else "  "
            self._append_line(f"{prefix}{line}")

    def flush(self) -> None:
        if self._current_chunks:
            self._emit_prefixed("assistant", "".join(self._current_chunks))
        self._current_msg_id = None
        self._current_chunks = []

    def feed(self, events: Iterable[Any]) -> None:
        for event in events:
            if not isinstance(event, dict):
                continue
            etype = str(event.get("type") or "")
            payload = event.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            if etype == "text_delta":
                msg_id = str(event.get("message_id") or event.get("turn_id") or "assistant")
                chunk = str(payload.get("text") or payload.get("delta") or event.get("text") or "")
                self._turn_had_assistant_delta = True
                if msg_id == self._current_msg_id:
                    self._current_chunks.append(chunk)
                else:
                    self.flush()
                    self._current_msg_id = msg_id
                    self._current_chunks = [chunk]
                continue

            self.flush()
            if etype in ("turn_started", "user_message"):
                self._turn_had_assistant_delta = False
                user_text = str(
                    payload.get("summary")
                    or payload.get("prompt")
                    or payload.get("text")
                    or payload.get("message")
                    or event.get("text")
                    or ""
                )
                self._emit_prefixed("user", user_text)
            elif etype == "assistant_message":
                assistant_text = str(
                    payload.get("text") or payload.get("message") or event.get("text") or ""
                )
                self._emit_prefixed("assistant", assistant_text)
            elif etype == "turn_completed":
                if not self._turn_had_assistant_delta:
                    fallback_text = str(
                        payload.get("final_text")
                        or payload.get("assistant_text")
                        or payload.get("text")
                        or ""
                    )
                    self._emit_prefixed("assistant", fallback_text)
                self._turn_had_assistant_delta = False
            elif etype == "tool_call_started":
                tool_name = str(payload.get("tool_name") or payload.get("name") or "tool")
                self._append_line(f"[tool] {tool_name}")
            elif etype == "error":
                err_text = str(
                    payload.get("message") or payload.get("error") or event.get("text") or "error"
                ).strip()
                self._append_line(f"[error] {err_text}")

    def lines(self) -> list[str]:
        self.flush()
        if self._max_lines is not None:
            return self._lines[-self._max_lines :]
        return list(self._lines)


def format_chat_stream_lines(page: Any) -> list[str]:
    """Render an ``AgentStreamEventPage`` dict into readable transcript lines."""
    events = page.get("events", []) if isinstance(page, dict) else []
    if not isinstance(events, list):
        return []
    renderer = _ChatStreamRenderer()
    renderer.feed(events)
    return renderer.lines()


def fetch_chat_stream_tail_lines(
    fetch_page: Any,
    *,
    lines: int,
    persisted_only: bool,
) -> list[str]:
    """Page forward through an AgentStream endpoint and return the last ``lines`` rendered lines."""
    page_limit = min(max(lines * 6, 200), 5000)
    renderer = _ChatStreamRenderer(max_lines=max(lines, 1))
    since_sequence = -1

    while True:
        page = fetch_page(
            {
                "since_sequence": since_sequence,
                "view": "conversation",
                "compact": True,
                "limit": page_limit,
                "persisted_only": persisted_only,
            }
        )
        raw_events = page.get("events", []) if isinstance(page, dict) else []
        if isinstance(raw_events, list):
            renderer.feed(raw_events)

        if not isinstance(page, dict) or not page.get("has_more"):
            break
        next_seq = page.get("next_sequence")
        if not isinstance(next_seq, int) or next_seq <= since_sequence:
            break
        since_sequence = next_seq

    return renderer.lines()[-lines:]


def redact_session_env_payload(data: Any) -> Any:
    """Redact env values from an agent session dict before CLI output."""
    return redact_session_json_payload(data)


def parse_json_object(value: Optional[str], option_name: str = "--payload-json") -> Dict[str, Any]:
    """Parse a JSON option that must decode to an object."""
    if value is None:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"invalid {option_name}: {e}") from e
    if not isinstance(parsed, dict):
        raise click.ClickException(f"{option_name} must decode to a JSON object.")
    return parsed


def parse_kv_pairs(values: Iterable[str], option_name: str) -> Dict[str, str]:
    """Parse repeated KEY=VALUE options."""
    parsed: Dict[str, str] = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise click.ClickException(f"{option_name} values must be KEY=VALUE.")
        parsed[key] = value
    return parsed


def parse_query_pairs(values: Iterable[str]) -> Dict[str, Any]:
    """Parse repeated query KEY=VALUE options."""
    return parse_kv_pairs(values, "--query")


def merge_payload(payload_json: Optional[str], **values: Any) -> Dict[str, Any]:
    """Merge JSON payload with explicit CLI values.

    Explicit values override JSON fields. Values set to ``None`` are omitted;
    empty tuples are omitted too, which lets repeatable flags remain optional.
    """
    body = parse_json_object(payload_json)
    for key, value in values.items():
        if value is None or value == ():
            continue
        body[key] = list(value) if isinstance(value, tuple) else value
    return body


def parse_attachment_json(values: Iterable[str]) -> list[Dict[str, Any]]:
    """Parse repeated attachment JSON object flags."""
    attachments = []
    for value in values:
        attachments.append(parse_json_object(value, "--attachment-json"))
    return attachments


def resolve_task_cwd_for_cli(
    client: Any,
    cwd: Optional[str],
    *,
    workspace_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve ``--cwd`` against local shell cwd only when the target workspace is local.

    Remote workspaces execute on a different host, so ``--cwd`` (such as
    ``~/proj`` or ``/opt/tiger/proj``) must never be expanded or symlink-resolved
    against the CLI caller's local filesystem.
    """
    if cwd is None:
        return None
    cleaned = cwd.strip()
    if not cleaned:
        return None
    ws_id = workspace_id
    if ws_id is None and task_id is not None:
        try:
            task_obj = client.get_task(task_id)
            if isinstance(task_obj, dict) and task_obj.get("workspace_id"):
                ws_id = str(task_obj["workspace_id"])
        except HubError:
            ws_id = None
    if ws_id is not None:
        try:
            workspaces = client.list_workspaces()
            if isinstance(workspaces, list):
                for item in workspaces:
                    if isinstance(item, dict) and item.get("id") == ws_id:
                        if item.get("target") == "remote":
                            return cleaned
                        break
        except HubError:
            pass
    return resolve_cli_local_path(cleaned)
