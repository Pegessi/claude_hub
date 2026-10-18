"""Cursor provenance operations with explicit runtime dependencies."""

from __future__ import annotations

import ipaddress
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from ...models import AgentType, ExecutionTarget, SessionKind
from .bindings import RuntimeBindings
from .defaults import CURSOR_TRANSCRIPT_SCHEMA, SUPPORTED_CURSOR_TRANSCRIPT_VERSIONS


def _is_loopback_url(runtime: RuntimeBindings, value: str) -> bool:
    """Return whether an HTTP service URL names this host's loopback interface."""

    hostname = urlsplit(value).hostname
    if hostname == "localhost":
        return True
    if hostname is None:
        return False
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _promote_cursor_chat_transport(
    runtime: RuntimeBindings,
    session_kind: SessionKind,
    agent_type: AgentType,
    target: ExecutionTarget,
    cursor_transport: str,
) -> str:
    """Promote a local Cursor Chat tab to the native transport.

    The frontend renders every ``session_kind=chat`` tab as a native
    structured surface (``TerminalPane.vue`` shows "… Chat · native
    structured" and mounts ``StructuredPane``), so a Cursor Chat tab left on
    the raw ``terminal`` transport fails closed in the adapter registry
    (``get_adapter_for_session`` returns ``None`` → "no structured adapter").

    The native ``ProviderSession`` owns the Cursor process and does not need a
    cwd (it spawns with ``cwd=None``, inheriting the backend cwd), so this
    promotion intentionally does NOT require one. The ``and cwd`` guard that
    used to gate promotion belonged to the retired ``terminal_transcript``
    mode, which needed a cwd to locate the transcript file.

    Applied at every transport-assignment boundary — ``create_tab``, the
    state-restore path, and ``update_tab`` agent switches — so legacy rows
    persisted as ``terminal`` are healed on the next reload. The explicit
    ``terminal_transcript`` compatibility fallback is preserved (only the raw
    ``terminal`` transport is promoted).
    """
    if (
        session_kind == SessionKind.CHAT
        and agent_type == AgentType.CURSOR
        and target == ExecutionTarget.LOCAL
        and cursor_transport == "terminal"
    ):
        return "native"
    return cursor_transport


def get_tab_native_runtime_snapshot(runtime: RuntimeBindings, tab_id: str) -> Any:
    """Lazy bridge to avoid importing transcript adapters during ttyd startup."""

    from ..agent_stream.tailer import get_tab_native_runtime_snapshot as peek

    return peek(tab_id)


def native_provider_binary_available(runtime: RuntimeBindings, agent_type: AgentType) -> bool:
    from ..agent_stream.native import native_provider_binary_available as available

    return available(agent_type)


def cursor_cli_version_from_executable(runtime: RuntimeBindings) -> Optional[str]:
    """Return the installed Cursor CLI version without starting another agent.

    The official launcher is a symlink into ``.../versions/<version>/`` in the
    supported local install. A layout we cannot attribute fails closed.
    """
    executable = shutil.which("agent")
    if not executable:
        return None
    try:
        resolved = Path(executable).resolve(strict=True)
    except OSError:
        return None
    for parent in resolved.parents:
        if parent.parent.name == "versions" and parent.name:
            return parent.name
    return None


def cursor_data_dir_for_env(runtime: RuntimeBindings, env: Optional[Dict[str, str]]) -> str:
    """Return the canonical Cursor data directory that a child process will use."""
    if env:
        explicit = env.get("CURSOR_DATA_DIR")
        if explicit and explicit.strip():
            return str(Path(explicit).expanduser().resolve(strict=False))
        child_home = env.get("HOME")
        if child_home and child_home.strip():
            return str((Path(child_home).expanduser() / ".cursor").resolve(strict=False))
    return str((Path.home() / ".cursor").resolve(strict=False))


def cursor_terminal_transcript_path(
    runtime: RuntimeBindings,
    cwd: str,
    session_id: str,
    *,
    data_dir: Optional[str] = None,
) -> Path:
    """Return the authoritative Cursor CLI transcript for one exact cwd/SID."""
    canonical_cwd = str(Path(cwd).resolve(strict=False))
    project_key = re.sub(r"[^A-Za-z0-9]+", "-", canonical_cwd).strip("-")
    root = (
        Path(data_dir).expanduser().resolve(strict=False)
        if data_dir
        else (Path.home() / ".cursor").resolve(strict=False)
    )
    return (
        root / "projects" / project_key / "agent-transcripts" / session_id / f"{session_id}.jsonl"
    )


def cursor_terminal_transcript_provenance_valid(
    runtime: RuntimeBindings,
    *,
    cwd: Optional[str],
    session_id: Optional[str],
    cli_version: Optional[str],
    transcript_path: Optional[str],
    transcript_schema: Optional[str],
    data_dir: Optional[str],
    env: Optional[Dict[str, str]] = None,
) -> bool:
    """Fail closed unless stored same-pane transcript provenance is exact."""
    if not all((cwd, session_id, cli_version, transcript_path, data_dir)):
        return False
    try:
        parsed = uuid.UUID(str(session_id))
    except (ValueError, AttributeError):
        return False
    if str(parsed) != str(session_id):
        return False
    if transcript_schema != CURSOR_TRANSCRIPT_SCHEMA:
        return False
    if cli_version not in SUPPORTED_CURSOR_TRANSCRIPT_VERSIONS:
        return False
    if runtime.cursor_cli_version_from_executable() != cli_version:
        return False
    if str(
        Path(str(data_dir)).expanduser().resolve(strict=False)
    ) != runtime.cursor_data_dir_for_env(env):
        return False
    expected = runtime.cursor_terminal_transcript_path(
        str(cwd), str(session_id), data_dir=str(data_dir)
    )
    return Path(str(transcript_path)).resolve(strict=False) == expected.resolve(strict=False)
