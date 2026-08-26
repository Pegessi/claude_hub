"""Shared helpers for CLI command modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import click

from claude_hub.models import redact_session_json_payload

LIFECYCLE_RECIPE = (
    "Lifecycle: one Claude Hub Workspace per Git repo (git common-dir);\n"
    "Git feature worktree != Hub Workspace — run workspace ensure from any checkout.\n"
    "Hub Workspace is shared by repo; agent execution cwd is separate.\n"
    "From a feature worktree pass --cwd . so the agent runs in that checkout.\n"
    "Default agent create best-effort reuses a compatible idle orchestrator.\n"
    "Check agent status first; avoid extra agents unless parallel work needs them.\n"
    "Overlapping creates may each create one; use --no-reuse-existing deliberately.\n"
    "Pass --ephemeral for task-scoped sessions you own, then task cleanup when done.\n"
    "Any built-in or saved custom env preset works by name/id; day1 is only an example.\n"
    "Local Claude (feature worktree): claude-hub agent create WORKSPACE "
    "--agent-type claude --cwd . --env-preset NAME_OR_ID\n"
    "\n"
    "Never delete reused/shared agents."
)


def lifecycle_group_help(summary: str) -> str:
    """Build Click group help text with the shared lifecycle recipe appended."""
    return f"{summary}\n\n{LIFECYCLE_RECIPE}"


def resolve_cli_local_path(path: str) -> str:
    """Resolve a user-supplied local path in the CLI process working directory."""
    return str(Path(path).expanduser().resolve())


def redact_session_env_payload(data: Any) -> Any:
    """Redact env values from an agent session dict before CLI output."""
    return redact_session_json_payload(data)


def resolve_agent_reuse(ephemeral: bool, reuse_existing: bool, no_reuse_existing: bool) -> bool:
    """Resolve tri-state agent reuse flags."""
    if reuse_existing and no_reuse_existing:
        raise click.ClickException("Cannot pass both --reuse-existing and --no-reuse-existing.")
    if ephemeral and reuse_existing:
        raise click.ClickException("Cannot combine --ephemeral with --reuse-existing.")
    if no_reuse_existing:
        return False
    if ephemeral:
        return False
    return True


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
