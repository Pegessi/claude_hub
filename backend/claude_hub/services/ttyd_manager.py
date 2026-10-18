"""Compatibility entry point and sole assembly site for terminal runtime owners.

Runtime implementations receive finite typed bindings. Those bindings resolve
this module's named dependencies at call time, preserving historical patch paths
without a reverse import or an extra registry of mutable runtime state.
"""

# Retain historical dependency exports: callers patch these shared module
# objects (for example asyncio.create_subprocess_exec and subprocess.run).
import asyncio
import base64
import errno
import glob
import hashlib
import ipaddress
import itertools
import json
import logging
import os
import re
import shlex
import shutil
import socket
import sqlite3  # noqa: F401  (re-exported indirectly for tests)
import subprocess
import sys
import tempfile
import time
import uuid
from collections import namedtuple
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import cached_property, partial
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Tuple, TypedDict
from urllib.parse import urlsplit

import claude_hub.services.pty_exec as pty_exec

from ..config import settings
from ..models import (
    AgentRuntimeStatus,
    AgentStreamEvent,
    AgentType,
    ChatMode,
    ExecutionTarget,
    RemoteProfile,
    SessionKind,
    TerminalAgentStatus,
    TerminalTab,
    WorkspaceSessionRole,
)
from ..models.schemas import CHAT_REMOTE_UNSUPPORTED
from ._cursor_verify import _cursor_id_exists
from .agent_status_markers import codex_output_is_working
from .provider_proxy import has_provider_proxy_settings, provider_subprocess_env
from .proxy_rules import PROXY_ENV_KEYS, with_proxy_env_aliases
from .remote_profiles import (
    profile_capabilities,
    reject_unsupported_interactive,
    remote_profile_manager,
)
from .runtime_isolation import resolve_runtime_home, tmux_command, tmux_socket_args
from .terminal_runtime import catalog as _terminal_session_catalog
from .terminal_runtime import cursor_provenance, local_io, session_discovery
from .terminal_runtime import snapshot as _terminal_snapshot
from .terminal_runtime import tmux_control
from .terminal_runtime.bindings import RuntimeBindings
from .terminal_runtime.catalog import _CODEX_SESSION_LINE_RE as _CODEX_SESSION_LINE_RE
from .terminal_runtime.catalog import _CODEX_SKIP_TITLE_PREFIXES as _CODEX_SKIP_TITLE_PREFIXES
from .terminal_runtime.catalog import _CODEX_TASK_INLINE_RE as _CODEX_TASK_INLINE_RE
from .terminal_runtime.catalog import _CODEX_TASK_TITLE_RE as _CODEX_TASK_TITLE_RE
from .terminal_runtime.catalog import _CODEX_TITLE_MAX_LEN as _CODEX_TITLE_MAX_LEN
from .terminal_runtime.catalog import _CODEX_TITLE_SCAN_MAX_LINES as _CODEX_TITLE_SCAN_MAX_LINES
from .terminal_runtime.catalog import (
    _CODEX_WORKSPACE_BOOTSTRAP_PREFIXES as _CODEX_WORKSPACE_BOOTSTRAP_PREFIXES,
)
from .terminal_runtime.catalog import _CODEX_WORKSPACE_LINE_RE as _CODEX_WORKSPACE_LINE_RE
from .terminal_runtime.catalog import (
    _CODEX_WORKSPACE_RECOVERY_PREFIXES as _CODEX_WORKSPACE_RECOVERY_PREFIXES,
)
from .terminal_runtime.catalog import (
    _CODEX_WORKSPACE_TASK_PREFIXES as _CODEX_WORKSPACE_TASK_PREFIXES,
)
from .terminal_runtime.defaults import _ANSI_ESCAPE_RE as _ANSI_ESCAPE_RE
from .terminal_runtime.defaults import _ATTENTION_TAIL_PATTERNS as _ATTENTION_TAIL_PATTERNS
from .terminal_runtime.defaults import _BACKFILL_ISOLATION_S as _BACKFILL_ISOLATION_S
from .terminal_runtime.defaults import _BACKFILL_MATCH_WINDOW_S as _BACKFILL_MATCH_WINDOW_S
from .terminal_runtime.defaults import _BACKFILL_MTIME_SLACK_S as _BACKFILL_MTIME_SLACK_S
from .terminal_runtime.defaults import _BARE_SHELL_PROMPT_RE as _BARE_SHELL_PROMPT_RE
from .terminal_runtime.defaults import _CLAUDE_WORKING_STATUS_RE as _CLAUDE_WORKING_STATUS_RE
from .terminal_runtime.defaults import _CODEX_FENCE_SILENCE_S as _CODEX_FENCE_SILENCE_S
from .terminal_runtime.defaults import _CODEX_NEW_PIN_TS_EARLY_S as _CODEX_NEW_PIN_TS_EARLY_S
from .terminal_runtime.defaults import _CODEX_NEW_PIN_TS_LATE_S as _CODEX_NEW_PIN_TS_LATE_S
from .terminal_runtime.defaults import _CODEX_POLL_INTERVAL_S as _CODEX_POLL_INTERVAL_S
from .terminal_runtime.defaults import _CODEX_POLL_MAX_ATTEMPTS as _CODEX_POLL_MAX_ATTEMPTS
from .terminal_runtime.defaults import _CODEX_SESSIONS_CACHE_TTL as _CODEX_SESSIONS_CACHE_TTL
from .terminal_runtime.defaults import _CURSOR_WORKING_STATUS_RE as _CURSOR_WORKING_STATUS_RE
from .terminal_runtime.defaults import _IDLE_TAIL_HINTS as _IDLE_TAIL_HINTS
from .terminal_runtime.defaults import _MANAGED_TMUX_SESSION_RE as _MANAGED_TMUX_SESSION_RE
from .terminal_runtime.defaults import _MAX_TCP_PORT as _MAX_TCP_PORT
from .terminal_runtime.defaults import _MAX_TTYD_BIND_ATTEMPTS as _MAX_TTYD_BIND_ATTEMPTS
from .terminal_runtime.defaults import _MODEL_ENV_KEYS as _MODEL_ENV_KEYS
from .terminal_runtime.defaults import (
    _ORPHAN_TMUX_PRUNE_GRACE_SECONDS as _ORPHAN_TMUX_PRUNE_GRACE_SECONDS,
)
from .terminal_runtime.defaults import _PORT_CHECK_TIMEOUT_SECONDS as _PORT_CHECK_TIMEOUT_SECONDS
from .terminal_runtime.defaults import (
    _REMOTE_CAPTURE_TIMEOUT_SECONDS as _REMOTE_CAPTURE_TIMEOUT_SECONDS,
)
from .terminal_runtime.defaults import (
    _SINGLE_TAB_DISCOVERY_DELAY_S as _SINGLE_TAB_DISCOVERY_DELAY_S,
)
from .terminal_runtime.defaults import _STATUS_CACHE_TTL_SECONDS as _STATUS_CACHE_TTL_SECONDS
from .terminal_runtime.defaults import _TAB_CLEANUP_AUTHORIZATION as _TAB_CLEANUP_AUTHORIZATION
from .terminal_runtime.defaults import (
    _TAB_CREATE_STARTUP_TIMEOUT_SECONDS as _TAB_CREATE_STARTUP_TIMEOUT_SECONDS,
)
from .terminal_runtime.defaults import _TAB_ID_RE as _TAB_ID_RE
from .terminal_runtime.defaults import _TABS_STATE_VERSION as _TABS_STATE_VERSION
from .terminal_runtime.defaults import (
    _TMUX_CONTROL_TIMEOUT_SECONDS as _TMUX_CONTROL_TIMEOUT_SECONDS,
)
from .terminal_runtime.defaults import _TMUX_LAUNCH_ENV_KEYS as _TMUX_LAUNCH_ENV_KEYS
from .terminal_runtime.defaults import _TMUX_STALE_ENV_KEYS as _TMUX_STALE_ENV_KEYS
from .terminal_runtime.defaults import (
    _VOLCENGINE_CODING_PLAN_MODEL_ALIASES as _VOLCENGINE_CODING_PLAN_MODEL_ALIASES,
)
from .terminal_runtime.defaults import _WORKING_FRAME_STALE_SECONDS as _WORKING_FRAME_STALE_SECONDS
from .terminal_runtime.defaults import _WORKING_TAIL_PATTERNS as _WORKING_TAIL_PATTERNS
from .terminal_runtime.defaults import CURSOR_TRANSCRIPT_SCHEMA as CURSOR_TRANSCRIPT_SCHEMA
from .terminal_runtime.defaults import DEFAULT_CLAUDE_LAUNCH_ENV as DEFAULT_CLAUDE_LAUNCH_ENV
from .terminal_runtime.defaults import (
    SUPPORTED_CURSOR_TRANSCRIPT_VERSIONS as SUPPORTED_CURSOR_TRANSCRIPT_VERSIONS,
)
from .terminal_runtime.defaults import TMUX_SESSION_PREFIX as TMUX_SESSION_PREFIX
from .terminal_runtime.manager.attribution import ManagerAttributionMixin
from .terminal_runtime.manager.create import ManagerCreateMixin
from .terminal_runtime.manager.retirement import ManagerRetirementMixin
from .terminal_runtime.manager.startup import ManagerStartupMixin
from .terminal_runtime.manager.state import ManagerStateMixin
from .terminal_runtime.manager.status import ManagerStatusMixin
from .terminal_runtime.manager.tabs import ManagerTabsMixin
from .terminal_runtime.process.capture import ProcessCaptureMixin
from .terminal_runtime.process.environment import ProcessEnvironmentMixin
from .terminal_runtime.process.launch import ProcessLaunchMixin
from .terminal_runtime.process.remote import ProcessRemoteMixin
from .terminal_runtime.process.state import ProcessStateMixin
from .terminal_runtime.process.switch import ProcessSwitchMixin
from .terminal_runtime.records import CursorPosition as CursorPosition
from .terminal_runtime.records import ScanEntry as ScanEntry
from .terminal_runtime.records import TabCleanupPendingError as TabCleanupPendingError
from .terminal_runtime.records import TabLimitExceededError as TabLimitExceededError
from .terminal_runtime.records import TabStartupTimeoutError as TabStartupTimeoutError
from .terminal_runtime.records import TabStateError as TabStateError
from .terminal_runtime.records import _AgentStatusSnapshot as _AgentStatusSnapshot
from .terminal_runtime.records import _PendingTabCleanup as _PendingTabCleanup
from .terminal_runtime.records import _TabStateCommitError as _TabStateCommitError

logger = logging.getLogger(__name__)
_RUNTIME_HOME = resolve_runtime_home()
STATE_FILE = _RUNTIME_HOME / "tabs.json"
ORDER_FILE = _RUNTIME_HOME / "tab_order.json"
LAUNCH_ENV_DIR = _RUNTIME_HOME / "launch_env"
GLOBAL_CODEX_LAUNCH_LOCK = asyncio.Lock()
_CODEX_SESSIONS_CACHE: Optional[Tuple[Tuple[int, int], List[Dict[str, Any]]]] = None


def _write_private_launch_file(path: Path, content: str) -> None:
    return local_io._write_private_launch_file(_RUNTIME, path, content)


def _is_loopback_url(value: str) -> bool:
    return cursor_provenance._is_loopback_url(_RUNTIME, value)


def _promote_cursor_chat_transport(
    session_kind: SessionKind, agent_type: AgentType, target: ExecutionTarget, cursor_transport: str
) -> str:
    return cursor_provenance._promote_cursor_chat_transport(
        _RUNTIME, session_kind, agent_type, target, cursor_transport
    )


def get_tab_native_runtime_snapshot(tab_id: str) -> Any:
    return cursor_provenance.get_tab_native_runtime_snapshot(_RUNTIME, tab_id)


def native_provider_binary_available(agent_type: AgentType) -> bool:
    return cursor_provenance.native_provider_binary_available(_RUNTIME, agent_type)


def cursor_cli_version_from_executable() -> Optional[str]:
    return cursor_provenance.cursor_cli_version_from_executable(_RUNTIME)


def cursor_data_dir_for_env(env: Optional[Dict[str, str]]) -> str:
    return cursor_provenance.cursor_data_dir_for_env(_RUNTIME, env)


def cursor_terminal_transcript_path(
    cwd: str, session_id: str, *, data_dir: Optional[str] = None
) -> Path:
    return cursor_provenance.cursor_terminal_transcript_path(
        _RUNTIME, cwd, session_id, data_dir=data_dir
    )


def cursor_terminal_transcript_provenance_valid(
    *,
    cwd: Optional[str],
    session_id: Optional[str],
    cli_version: Optional[str],
    transcript_path: Optional[str],
    transcript_schema: Optional[str],
    data_dir: Optional[str],
    env: Optional[Dict[str, str]] = None,
) -> bool:
    return cursor_provenance.cursor_terminal_transcript_provenance_valid(
        _RUNTIME,
        cwd=cwd,
        session_id=session_id,
        cli_version=cli_version,
        transcript_path=transcript_path,
        transcript_schema=transcript_schema,
        data_dir=data_dir,
        env=env,
    )


def _is_local_port_listening(port: int) -> bool:
    return local_io._is_local_port_listening(_RUNTIME, port)


def _is_local_port_available(port: int) -> bool:
    return local_io._is_local_port_available(_RUNTIME, port)


def _parse_optional_iso(value: Optional[str]) -> Optional[datetime]:
    return local_io._parse_optional_iso(_RUNTIME, value)


def _tmux_session_name(tab_id: str) -> str:
    return tmux_control._tmux_session_name(_RUNTIME, tab_id)


def _tmux_server_running() -> bool:
    return tmux_control._tmux_server_running(_RUNTIME)


def _refresh_tmux_server_environment() -> None:
    return tmux_control._refresh_tmux_server_environment(_RUNTIME)


def _ensure_tmux_server() -> bool:
    return tmux_control._ensure_tmux_server(_RUNTIME)


def _tmux_session_exists(session_name: str) -> bool:
    return tmux_control._tmux_session_exists(_RUNTIME, session_name)


async def _tmux_session_exists_async(session_name: str) -> bool:
    return await tmux_control._tmux_session_exists_async(_RUNTIME, session_name)


async def _tmux_list_sessions() -> set[str]:
    return await tmux_control._tmux_list_sessions(_RUNTIME)


async def _tmux_list_session_created() -> dict[str, float]:
    return await tmux_control._tmux_list_session_created(_RUNTIME)


async def _finish_tmux_launcher_rollback(
    proc: asyncio.subprocess.Process, session_name: str
) -> None:
    return await tmux_control._finish_tmux_launcher_rollback(_RUNTIME, proc, session_name)


async def _tmux_kill_session(session_name: str) -> None:
    return await tmux_control._tmux_kill_session(_RUNTIME, session_name)


def _tmux_session_created(session_name: str) -> Optional[float]:
    return tmux_control._tmux_session_created(_RUNTIME, session_name)


def _claude_project_dir_for_cwd(cwd: str) -> Path:
    return session_discovery._claude_project_dir_for_cwd(_RUNTIME, cwd)


def _jsonl_start_epoch(path: str) -> Optional[float]:
    return session_discovery._jsonl_start_epoch(_RUNTIME, path)


def _pick_backfill_session(
    session_created: float, candidates: List[Tuple[float, str, str]]
) -> Optional[str]:
    return session_discovery._pick_backfill_session(_RUNTIME, session_created, candidates)


def _codex_home_dir() -> Path:
    return session_discovery._codex_home_dir(_RUNTIME)


def _parse_session_meta(
    first_line: str,
) -> Tuple[Optional[str], Optional[float], Optional[str], str]:
    return _terminal_session_catalog.parse_session_meta(first_line, datetime_type=datetime)


def _codex_roots() -> List[Tuple[Path, bool]]:
    return session_discovery._codex_roots(_RUNTIME)


def _codex_scan_sessions(
    with_title: bool = False, skip_title_sids: Optional[set] = None
) -> Dict[str, ScanEntry]:
    return session_discovery._codex_scan_sessions(_RUNTIME, with_title, skip_title_sids)


def _diff_scans(pre: Dict[str, ScanEntry], post: Dict[str, ScanEntry]) -> Dict[str, str]:
    return _terminal_session_catalog.diff_scans(pre, post)


def _codex_id_exists(sid: str, cwd: Optional[str] = None) -> bool:
    return session_discovery._codex_id_exists(_RUNTIME, sid, cwd)


def _codex_candidates_for_cwd(
    cwd: str, scan: Optional[Dict[str, ScanEntry]] = None
) -> List[Tuple[float, str, str]]:
    return session_discovery._codex_candidates_for_cwd(_RUNTIME, cwd, scan)


def _extract_task_title_from_text(text: str) -> str | None:
    return _terminal_session_catalog.extract_task_title_from_text(
        text, task_title_re=_CODEX_TASK_TITLE_RE, task_inline_re=_CODEX_TASK_INLINE_RE
    )


def _bootstrap_role_label(text: str) -> str | None:
    return _terminal_session_catalog.bootstrap_role_label(
        text, workspace_line_re=_CODEX_WORKSPACE_LINE_RE
    )


def _codex_classify_and_extract(text: str) -> tuple[str, str | None]:
    return _terminal_session_catalog.classify_and_extract(
        text,
        bootstrap_prefixes=_CODEX_WORKSPACE_BOOTSTRAP_PREFIXES,
        task_prefixes=_CODEX_WORKSPACE_TASK_PREFIXES,
        recovery_prefixes=_CODEX_WORKSPACE_RECOVERY_PREFIXES,
        role_label=_bootstrap_role_label,
        task_title=_extract_task_title_from_text,
    )


def _codex_title_from_lines(lines: Iterable[str]) -> str:
    return _terminal_session_catalog.title_from_lines(
        lines,
        skip_prefixes=_CODEX_SKIP_TITLE_PREFIXES,
        max_length=_CODEX_TITLE_MAX_LEN,
        classify=_codex_classify_and_extract,
    )


def _codex_session_title(path: str) -> str:
    return session_discovery._codex_session_title(_RUNTIME, path)


def _codex_load_session_index() -> Dict[str, str]:
    return session_discovery._codex_load_session_index(_RUNTIME)


def _codex_sessions_cache_key() -> Tuple[int, int]:
    return session_discovery._codex_sessions_cache_key(_RUNTIME)


def list_codex_sessions() -> List[Dict[str, Any]]:
    """List available Codex sessions from on-disk rollout files.

    Returns a list of session dicts grouped by working directory, each with:
    - ``session_id``: stable codex session UUID (for ``codex resume <id>``)
    - ``cwd``: working directory the session was started in
    - ``start_time``: ISO-8601 timestamp of the session start
    - ``title``: human-readable title. Prefers the Codex app's conversation
      title (``thread_name`` from ``session_index.jsonl``); falls back to the
      first real user message extracted from the rollout file.

    Sessions are sorted most-recent-first within each cwd group, and cwd
    groups are ordered by their most recent session. Walks both active
    (``sessions/``) and archived (``archived_sessions/``) locations since
    ``codex resume`` works against both.

    Only user-initiated sessions (``thread_source == "user"`` or legacy
    rollouts without the field) are returned; subagent and automation
    sessions are filtered out. Results are cached in-memory keyed on the
    session index and rollout directory mtimes.
    """
    global _CODEX_SESSIONS_CACHE

    cache_key = _codex_sessions_cache_key()
    if _CODEX_SESSIONS_CACHE is not None:
        cached_key, cached_result = _CODEX_SESSIONS_CACHE
        if cached_key == cache_key:
            return cached_result

    # Load the Codex app's conversation titles once. This covers the majority
    # of sessions and avoids reading the (large) rollout files for titles.
    thread_names = _codex_load_session_index()

    # Single-pass scan: read session_meta for every session, but only read the
    # rollout body (for title extraction) for sessions NOT in the index.
    # Sessions in the index use thread_name directly.
    scan = _codex_scan_sessions(with_title=True, skip_title_sids=set(thread_names.keys()))
    result = _terminal_session_catalog.group_sessions(scan, thread_names, datetime_type=datetime)

    _CODEX_SESSIONS_CACHE = (cache_key, result)
    return result


def _agent_spawn_env() -> dict:
    return local_io._agent_spawn_env(_RUNTIME)


def get_default_command() -> str:
    return local_io.get_default_command(_RUNTIME)


def get_agent_command(agent_type: AgentType) -> str:
    return local_io.get_agent_command(_RUNTIME, agent_type)


def _event_turn_ordinals(events: List[AgentStreamEvent]) -> List[int]:
    return local_io._event_turn_ordinals(_RUNTIME, events)


def _parse_tabs_snapshot(value: Any) -> Tuple[List[Dict[str, Any]], Dict[str, _PendingTabCleanup]]:
    return _terminal_snapshot.parse_tabs_snapshot(
        value,
        state_version=_TABS_STATE_VERSION,
        cleanup_authorization=_TAB_CLEANUP_AUTHORIZATION,
        tab_id_re=_TAB_ID_RE,
        state_error=TabStateError,
        pending_cleanup_type=_PendingTabCleanup,
        datetime_type=datetime,
    )


def _write_tabs_snapshot(payload: Dict[str, Any]) -> None:
    return local_io._write_tabs_snapshot(_RUNTIME, payload)


_codex_id_in_index = _codex_id_exists


class TTYDProcess(
    ProcessStateMixin,
    ProcessEnvironmentMixin,
    ProcessLaunchMixin,
    ProcessSwitchMixin,
    ProcessRemoteMixin,
    ProcessCaptureMixin,
):
    """Concrete terminal owner; mixins operate on this one instance."""

    _tunnel_registry: dict[str, tuple[int, subprocess.Popen]] = {}

    @staticmethod
    def _clean_env(env: Dict[str, str]) -> Dict[str, str]:
        cleaned: Dict[str, str] = {}
        for key, value in env.items():
            name = str(key).strip()
            if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"Invalid environment variable name: {name or '<empty>'}")
            cleaned[name] = str(value)
        return with_proxy_env_aliases(cleaned)

    @staticmethod
    def _default_env_for_agent(agent_type: AgentType) -> Dict[str, str]:
        if agent_type != AgentType.CLAUDE:
            return {}
        return DEFAULT_CLAUDE_LAUNCH_ENV.copy()

    @property
    def _runtime(self) -> RuntimeBindings:
        return _RUNTIME


class TTYDManager(
    ManagerStateMixin,
    ManagerCreateMixin,
    ManagerRetirementMixin,
    ManagerTabsMixin,
    ManagerStatusMixin,
    ManagerAttributionMixin,
    ManagerStartupMixin,
):
    """Concrete terminal owner; mixins operate on this one instance."""

    @property
    def _runtime(self) -> RuntimeBindings:
        return _RUNTIME


_RUNTIME = RuntimeBindings(
    read_GLOBAL_CODEX_LAUNCH_LOCK=lambda: GLOBAL_CODEX_LAUNCH_LOCK,
    read_LAUNCH_ENV_DIR=lambda: LAUNCH_ENV_DIR,
    read_ORDER_FILE=lambda: ORDER_FILE,
    read_STATE_FILE=lambda: STATE_FILE,
    read_TTYDProcess=lambda: TTYDProcess,
    read__CODEX_TITLE_SCAN_MAX_LINES=lambda: _CODEX_TITLE_SCAN_MAX_LINES,
    read__TAB_CREATE_STARTUP_TIMEOUT_SECONDS=lambda: _TAB_CREATE_STARTUP_TIMEOUT_SECONDS,
    read__agent_spawn_env=lambda: _agent_spawn_env,
    read__claude_project_dir_for_cwd=lambda: _claude_project_dir_for_cwd,
    read__codex_candidates_for_cwd=lambda: _codex_candidates_for_cwd,
    read__codex_home_dir=lambda: _codex_home_dir,
    read__codex_id_exists=lambda: _codex_id_exists,
    read__codex_roots=lambda: _codex_roots,
    read__codex_scan_sessions=lambda: _codex_scan_sessions,
    read__codex_title_from_lines=lambda: _codex_title_from_lines,
    read__cursor_id_exists=lambda: _cursor_id_exists,
    read__diff_scans=lambda: _diff_scans,
    read__ensure_tmux_server=lambda: _ensure_tmux_server,
    read__event_turn_ordinals=lambda: _event_turn_ordinals,
    read__finish_tmux_launcher_rollback=lambda: _finish_tmux_launcher_rollback,
    read__is_local_port_available=lambda: _is_local_port_available,
    read__is_local_port_listening=lambda: _is_local_port_listening,
    read__is_loopback_url=lambda: _is_loopback_url,
    read__jsonl_start_epoch=lambda: _jsonl_start_epoch,
    read__parse_optional_iso=lambda: _parse_optional_iso,
    read__parse_session_meta=lambda: _parse_session_meta,
    read__parse_tabs_snapshot=lambda: _parse_tabs_snapshot,
    read__pick_backfill_session=lambda: _pick_backfill_session,
    read__promote_cursor_chat_transport=lambda: _promote_cursor_chat_transport,
    read__refresh_tmux_server_environment=lambda: _refresh_tmux_server_environment,
    read__tmux_kill_session=lambda: _tmux_kill_session,
    read__tmux_list_session_created=lambda: _tmux_list_session_created,
    read__tmux_list_sessions=lambda: _tmux_list_sessions,
    read__tmux_server_running=lambda: _tmux_server_running,
    read__tmux_session_created=lambda: _tmux_session_created,
    read__tmux_session_exists_async=lambda: _tmux_session_exists_async,
    read__tmux_session_name=lambda: _tmux_session_name,
    read__write_private_launch_file=lambda: _write_private_launch_file,
    read__write_tabs_snapshot=lambda: _write_tabs_snapshot,
    read_cursor_cli_version_from_executable=lambda: cursor_cli_version_from_executable,
    read_cursor_data_dir_for_env=lambda: cursor_data_dir_for_env,
    read_cursor_terminal_transcript_path=lambda: cursor_terminal_transcript_path,
    read_cursor_terminal_transcript_provenance_valid=lambda: cursor_terminal_transcript_provenance_valid,
    read_datetime=lambda: datetime,
    read_get_agent_command=lambda: get_agent_command,
    read_get_default_command=lambda: get_default_command,
    read_get_tab_native_runtime_snapshot=lambda: get_tab_native_runtime_snapshot,
    read_native_provider_binary_available=lambda: native_provider_binary_available,
    read_tmux_command=lambda: tmux_command,
)


ttyd_manager = TTYDManager()
