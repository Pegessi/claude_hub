import asyncio
import errno
import glob
import hashlib
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
import time
import uuid
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple, TypedDict

from ..config import settings
from ..models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    TerminalAgentStatus,
    TerminalTab,
    WorkspaceSessionRole,
)
from ._cursor_verify import _cursor_id_exists
from .agent_status_markers import codex_output_is_working
from .remote_profiles import remote_profile_manager
from .runtime_isolation import resolve_runtime_home, tmux_command, tmux_socket_args

logger = logging.getLogger(__name__)

_RUNTIME_HOME = resolve_runtime_home()
STATE_FILE = _RUNTIME_HOME / "tabs.json"
ORDER_FILE = _RUNTIME_HOME / "tab_order.json"
LAUNCH_ENV_DIR = _RUNTIME_HOME / "launch_env"
TMUX_SESSION_PREFIX = "claude-hub-"
_ORPHAN_TMUX_PRUNE_GRACE_SECONDS = 60.0
_MANAGED_TMUX_SESSION_RE = re.compile(rf"^{re.escape(TMUX_SESSION_PREFIX)}[0-9a-f]{{8}}$")

#: Schema identifier for the same-pane Cursor CLI transcript format.
CURSOR_TRANSCRIPT_SCHEMA = "cli-transcript-v1"

#: Cursor CLI versions whose same-pane transcript row shape is known to match
#: :data:`CURSOR_TRANSCRIPT_SCHEMA`. Pinned so an unknown CLI version fails
#: closed rather than mis-normalizing rows.
SUPPORTED_CURSOR_TRANSCRIPT_VERSIONS: frozenset[str] = frozenset({"2026.08.25-3e8eec8"})


def cursor_cli_version_from_executable() -> Optional[str]:
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


def cursor_data_dir_for_env(env: Optional[Dict[str, str]]) -> str:
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
    if cursor_cli_version_from_executable() != cli_version:
        return False
    if str(Path(str(data_dir)).expanduser().resolve(strict=False)) != cursor_data_dir_for_env(env):
        return False
    expected = cursor_terminal_transcript_path(str(cwd), str(session_id), data_dir=str(data_dir))
    return Path(str(transcript_path)).resolve(strict=False) == expected.resolve(strict=False)


# ANSI escape sequences (CSI, OSC, charset selection) — stripped before
# pattern matching so cursor blinks and color codes don't churn the hash
# or break substring checks.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]")

# Bare shell prompt (zsh/bash/fish/powerlevel) at end of last line → idle.
_BARE_SHELL_PROMPT_RE = re.compile(r"[❯>$#%»→λ]\s*$")

# Strict tail anchors used for classification. Each tuple is matched against
# the lowercased last 5 non-empty lines of the captured pane. Order of checks
# in `_classify_agent_status` is: ATTENTION → bare shell prompt (IDLE) → IDLE
# hints → WORKING. A bare shell prompt or idle hint on the last lines takes
# priority over working markers higher in the scrollback, so an agent that has
# finished and returned to a prompt is promptly classified idle rather than
# lingering in "working" on a stale "esc to interrupt" marker.
_ATTENTION_TAIL_PATTERNS = (
    "do you want to proceed",
    "do you want to continue",
    "enter to select",
    "tab/arrow keys to navigate",
    "arrow keys to navigate",
    "type something.",
    "esc to cancel",
    "(y/n)",
    "(y/n/a)",
    "[y/n]",
    "[y/n/a]",
    "press enter to continue",
    "press any key to continue",
)

_WORKING_TAIL_PATTERNS = (
    "esc to interrupt",
    "ctrl+c to interrupt",
    "ctrl-c to interrupt",
    # Cursor CLI shows "ctrl+c to stop" while the agent is actively running.
    "ctrl+c to stop",
    "ctrl-c to stop",
    "running…",
    "running...",
)

# Claude Code also reports active work with spinner-style status lines such
# as "✢ Gusting… (52s · ↑ 1.5k tokens · thought for 2s)".
_CLAUDE_WORKING_STATUS_RE = re.compile(r"^[✻✢✶✳✷✸✹✺✽✦✧]\s+\S+…\s+\(", re.MULTILINE)

# Cursor CLI reports active work with a status line like "Running 662 tokens"
# (no ellipsis, capitalized). Match it as a strict regex to avoid false hits
# from arbitrary "running" mentions in command output.
_CURSOR_WORKING_STATUS_RE = re.compile(r"\brunning\s+\d[\d,\.]*\s+tokens?\b", re.IGNORECASE)

# Bottom-of-UI hints emitted by Claude Code / Codex when idle and waiting for
# user input. Presence of any of these means the agent UI is showing but no
# work is in flight.
_IDLE_TAIL_HINTS = (
    "? for shortcuts",
    "/ for commands",
)

_STATUS_CACHE_TTL_SECONDS = 0.75
# A genuinely-working agent repaints its spinner/elapsed-time roughly every
# second, so the captured frame content changes on every status sample. If a
# pane still shows working markers (spinner / "esc to interrupt") but its
# content has not changed for this long, the agent has stopped while leaving a
# frozen "working" frame on screen — treat it as stuck rather than working.
# Kept well above the monitor interval and spinner tick, with enough headroom
# so a slow tool call that briefly stops repainting is not flagged prematurely,
# but short enough that a genuinely stopped agent (e.g. one that finished
# without emitting a clean shell prompt) is surfaced promptly instead of
# lingering in "working" for up to 3 minutes.
_WORKING_FRAME_STALE_SECONDS = 45.0
_PORT_CHECK_TIMEOUT_SECONDS = 0.2
_MAX_TCP_PORT = 65535
_MAX_TTYD_BIND_ATTEMPTS = 3
_REMOTE_CAPTURE_TIMEOUT_SECONDS = 10.0
_VOLCENGINE_CODING_PLAN_MODEL_ALIASES = {
    "ark/seed-code-0602": "doubao-seed-2.0-code",
    "ark/seed-code-0602[1m]": "doubao-seed-2.0-code",
    "ark/seed-code-6062": "doubao-seed-2.0-code",
    "ark/seed-code-6062[1m]": "doubao-seed-2.0-code",
}
_MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
DEFAULT_CLAUDE_LAUNCH_ENV: Dict[str, str] = {
    "ANTHROPIC_BASE_URL": "https://ark.cn-beijing.volces.com/api/coding",
    "ANTHROPIC_MODEL": "doubao-seed-2.0-code",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "doubao-seed-2.0-code",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "doubao-seed-2.0-code",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "doubao-seed-2.0-code",
    "CLAUDE_CODE_SUBAGENT_MODEL": "doubao-seed-2.0-code",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


class CursorPosition(TypedDict):
    cursor_x: int
    cursor_y: int


class _AgentStatusSnapshot(TypedDict):
    hash: str
    last_changed_at: Optional[datetime]
    frame_first_seen_at: Optional[datetime]


def _is_local_port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(_PORT_CHECK_TIMEOUT_SECONDS)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _is_local_port_available(port: int) -> bool:
    """Return whether a loopback TCP port can currently be bound.

    Unlike a connect probe this detects bound-but-not-yet-listening sockets and
    does not send traffic to unrelated local services. The check cannot reserve
    the port for ttyd, so create_tab also retries a raced bind failure.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return False
            raise
    return True


def _tmux_session_name(tab_id: str) -> str:
    return f"{TMUX_SESSION_PREFIX}{tab_id[:8]}"


def _tmux_server_running() -> bool:
    """Check if tmux server is running."""
    try:
        ret = subprocess.run(
            tmux_command("ls"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
    except FileNotFoundError:
        return False
    return ret == 0 or ret == 1  # 0 = has sessions, 1 = no sessions but server running


def _ensure_tmux_server() -> bool:
    """Ensure tmux server is running, start it if not."""
    if _tmux_server_running():
        logger.debug("tmux server is already running")
        return True
    try:
        # Start a dummy session to initialize tmux server, then detach
        # This ensures the tmux server stays running even with no sessions
        logger.info("tmux server not running, starting it...")
        ret = subprocess.run(
            tmux_command("new-session", "-d", "-s", "__tmux_server_keepalive__"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        if ret == 0:
            logger.info("tmux server started successfully with keepalive session")
        return True
    except Exception as e:
        logger.warning(f"Failed to start tmux server: {e}")
        return False


def _tmux_session_exists(session_name: str) -> bool:
    """Check if a tmux session exists (sync, used during startup).

    Uses ``subprocess.run`` rather than ``os.system`` so it does not spawn a
    shell and does not block the asyncio event loop via the shell's own
    waitpid. Hot async paths should prefer :func:`_tmux_session_exists_async`
    or batch existence via :func:`_tmux_list_sessions`.
    """
    try:
        ret = subprocess.run(
            tmux_command("has-session", "-t", session_name),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
    except FileNotFoundError:
        return False
    return ret == 0


async def _tmux_session_exists_async(session_name: str) -> bool:
    """Async, non-blocking check for whether a tmux session exists."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *tmux_command("has-session", "-t", session_name),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False
    return await proc.wait() == 0


async def _tmux_list_sessions() -> set[str]:
    """Return the set of live tmux session names in a single subprocess call.

    Batching existence into one call lets board refreshes check many tabs
    against an in-memory set instead of spawning one ``has-session`` process
    per tab (previously a blocking ``os.system`` per tab that serialized the
    event loop).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *tmux_command("list-sessions", "-F", "#{session_name}"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return set()
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return set()
    return {line for line in stdout.decode("utf-8", errors="ignore").splitlines() if line}


async def _tmux_list_session_created() -> dict[str, float]:
    """Return live tmux session names with their creation epoch."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *tmux_command("list-sessions", "-F", "#{session_name}\t#{session_created}"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return {}
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return {}

    sessions: dict[str, float] = {}
    for line in stdout.decode("utf-8", errors="ignore").splitlines():
        name, separator, created = line.partition("\t")
        if not separator:
            continue
        try:
            sessions[name] = float(created)
        except ValueError:
            logger.warning("Ignoring tmux session with invalid creation time: %r", line)
    return sessions


async def _tmux_kill_session(session_name: str) -> None:
    """Kill a tmux session and prove it is no longer present.

    ``tmux kill-session`` can return non-zero for an already-absent session,
    which is an acceptable terminal state.  A command that returns success
    while the session remains present is not acceptable for ownership
    quarantine, so always verify absence after the command completes.
    """
    proc = await asyncio.create_subprocess_exec(
        *tmux_command("kill-session", "-t", session_name),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if await _tmux_session_exists_async(session_name):
        detail = stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(
            f"tmux session {session_name} still exists after kill-session "
            f"(rc={proc.returncode}, stderr={detail!r})"
        )


# Conservative session-id backfill tuning (see _backfill_agent_session_ids).
# best candidate must start within this many seconds of the tmux session
_BACKFILL_MATCH_WINDOW_S = 90.0
# runner-up must be at least this far away for the best to be "isolated"
_BACKFILL_ISOLATION_S = 600.0
# the matched jsonl must have been written at/after this slack before session start
_BACKFILL_MTIME_SLACK_S = 5.0

# --------------------------------------------------------------------------
# Cold-restart codex session attribution (v16 design).
#
# After a full cold restart (tmux gone) we relaunch N codex tabs in parallel
# and must positively attribute each newly-spawned codex agent to the rollout
# file it just wrote, so that each tab resumes ITS OWN conversation next
# time instead of cross-wiring. Five bugs in the prior code made this unsafe
# (BUG-1 single global launch_epoch; BUG-2 600 s threshold wrong gap; BUG-3
# cwd-scoped --last/--continue fallback; BUG-4 per-tab discovery cannot
# resolve N-way ambiguity; BUG-5 ttyd is lazy so clocks must stamp in
# ensure_tmux_session not start()).
#
# Fix: a GLOBAL_CODEX_LAUNCH_LOCK serializes all cold codex launches (trivial
# attribution — only one codex starting at a time), and we take a full
# dict[sidad, ScanEntry] snapshot before each tab launches and diff it
# against post-launch scans to detect new-file vs append-resume-ok activity.
# An empirical FENCE poll loop (200 ms × 6 s wall) watches for activity then
# waits 0.7 s of silence before calling it. Attribution on new-pin SIDs uses
# a [-2 s, +8 s] timestamp window around _launch_wall; append-resume uses
# file identity (same sid, size grew, mtime grew) which is clock-independent.
# Phase-R reconciliation is the fail-closed net: any unexplained SIDs or
# mismatches quarantine the affected cwd's tabs rather than cross-wiring.
# --------------------------------------------------------------------------

# Empirically calibrated (V0): codex writes session_meta within ~100-300 ms of
# spawning; 0.7 s of silence after the last change is p99.9 sufficient to
# guarantee the session_meta line is flushed. The fence is NOT a mathematical
# proof; Phase-R reconciliation is the real safety net.
_CODEX_FENCE_SILENCE_S = 0.7
# Total polling budget per tab: 30 polls × 200 ms = 6 s wall.
_CODEX_POLL_INTERVAL_S = 0.2
_CODEX_POLL_MAX_ATTEMPTS = 30
# New-pin SID timestamp window: session_meta.ts must be within this of
# _launch_wall. [-2, +8] accounts for clock skew (UTC rollout ts vs local
# wall) and startup work before session_meta is emitted.
_CODEX_NEW_PIN_TS_EARLY_S = 2.0
_CODEX_NEW_PIN_TS_LATE_S = 8.0

# Single-tab (create_tab / ensure_tab_tmux_session) discovery delay: codex
# outside the bulk startup path has no global lock and no Phase R; we simply
# wait this long then timestamp-window match.
_SINGLE_TAB_DISCOVERY_DELAY_S = 5.0

# Global lock serializing cold codex launches (cold = session_exists=False).
# Makes attribution trivial: only one codex writes to ~/.codex at a time.
GLOBAL_CODEX_LAUNCH_LOCK = asyncio.Lock()

# Per-rollout snapshot entry. mtime_ns/size are st_mtime_ns/st_size; cwd is
# parsed from session_meta (realpath of workspace); ts is session_meta epoch.
# title is the extracted session title (only populated when the scan is run
# with with_title=True; otherwise "").
# thread_source is the session's thread_source from session_meta ("user",
# "subagent", "automation", or "" for legacy rollouts that lack the field).
ScanEntry = namedtuple(
    "ScanEntry",
    ["path", "mtime_ns", "size", "cwd", "ts", "is_archived", "title", "thread_source"],
    defaults=["", ""],
)


def _tmux_session_created(session_name: str) -> Optional[float]:
    """Return the tmux ``session_created`` epoch for a live session.

    tmux survives a backend restart, so the creation time is the deploy-moment
    anchor used to correlate a tab with the Claude conversation that started
    alongside it. Returns ``None`` when the session is not alive (e.g. after a
    reboot) or tmux is unavailable.
    """
    try:
        proc = subprocess.run(
            tmux_command(
                "display-message",
                "-p",
                "-t",
                session_name,
                "#{session_created}",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _claude_project_dir_for_cwd(cwd: str) -> Path:
    """Map a cwd to the Claude project log dir under ``~/.claude/projects``.

    Claude derives the dir name by dropping the leading slash and replacing
    every ``/``, ``_`` and ``.`` with ``-``, then prefixing ``-``. Example:
    ``/Users/bytedance/claude_hub`` -> ``-Users-bytedance-claude-hub``.
    """
    key = re.sub(r"[/_.]", "-", cwd.lstrip("/"))
    return Path.home() / ".claude" / "projects" / f"-{key}"


def _jsonl_start_epoch(path: str) -> Optional[float]:
    """Epoch of the first timestamped line in a Claude conversation jsonl.

    Reads only up to the first line carrying a ``timestamp`` field so we never
    slurp whole (potentially large) conversation logs.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = record.get("timestamp")
                if not isinstance(ts, str):
                    continue
                try:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
    except OSError:
        return None
    return None


def _pick_backfill_session(
    session_created: float,
    candidates: List[Tuple[float, str, str]],
) -> Optional[str]:
    """Pure decision: pick a conversation id only when the match is unambiguous.

    ``candidates`` is a list of ``(delta, session_id, file)`` where ``delta`` is
    ``abs(conversation_start - session_created)``. Returns the chosen session id
    or ``None`` when no confident match exists. A match requires ALL of:

    - best delta <= window, AND
    - exactly one candidate OR second-best delta >= isolation threshold, AND
    - the best file was modified at/after ``session_created`` (minus slack), so
      the conversation actually lived during this tab's session.
    """
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda c: c[0])
    best_delta, best_sid, best_file = ranked[0]
    if best_delta > _BACKFILL_MATCH_WINDOW_S:
        return None
    if len(ranked) > 1 and ranked[1][0] < _BACKFILL_ISOLATION_S:
        return None
    try:
        if os.path.getmtime(best_file) < session_created - _BACKFILL_MTIME_SLACK_S:
            return None
    except OSError:
        return None
    return best_sid


def _codex_home_dir() -> Path:
    """Codex CLI home directory (respects $CODEX_HOME if set)."""
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def _parse_session_meta(
    first_line: str,
) -> Tuple[Optional[str], Optional[float], Optional[str], str]:
    """Parse a codex rollout first line into (sid, ts_epoch, cwd, thread_source).

    Returns ``(None, None, None, "")`` for unparseable lines. Canonical sid is
    ``payload.id`` (V0 verified: matches the filename uuid and is what codex
    resume keys off when the exact uuid is passed); ``payload.session_id`` is
    the stable parent thread id and differs on forks, so we treat it as a
    secondary alias only for back-compat with older legacy-flat rollouts.
    Defensive against legacy format (no ``payload`` wrapper) which stores
    ``session_id``/``id``/``cwd``/``timestamp`` at the top level.

    ``thread_source`` is one of ``"user"``, ``"subagent"``, ``"automation"``,
    or ``""`` for legacy rollouts that don't carry the field.
    """
    try:
        record = json.loads(first_line)
    except (json.JSONDecodeError, TypeError):
        return None, None, None, ""
    if not isinstance(record, dict):
        return None, None, None, ""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        payload = record  # legacy flat format
    sid = payload.get("id") or payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None, None, None, ""
    ts_raw = payload.get("timestamp") or record.get("timestamp")
    ts_epoch: Optional[float] = None
    if isinstance(ts_raw, str):
        try:
            ts_epoch = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts_epoch = None
    cwd = payload.get("cwd")
    if not isinstance(cwd, str):
        cwd = None
    thread_source = payload.get("thread_source")
    if not isinstance(thread_source, str):
        thread_source = ""
    return sid, ts_epoch, cwd, thread_source


def _codex_roots() -> List[Tuple[Path, bool]]:
    """Return [(root, is_archived)] for active + archived codex rollout dirs."""
    home = _codex_home_dir()
    roots: List[Tuple[Path, bool]] = []
    active = home / "sessions"
    if active.is_dir():
        roots.append((active, False))
    archived = home / "archived_sessions"
    if archived.is_dir():
        roots.append((archived, True))
    return roots


def _codex_scan_sessions(
    with_title: bool = False, skip_title_sids: Optional[set] = None
) -> Dict[str, ScanEntry]:
    """Scan all codex rollouts (active + archived).

    Returns a dict mapping canonical sid -> ScanEntry. When the same sid is
    seen in multiple locations (active vs archived, or duplicate files), we
    prefer: active over archived (active is live), then highest mtime_ns.
    Corrupt / unreadable files are silently skipped (with a WARNING log the
    first time per path).

    When ``with_title`` is True, the session title is extracted into
    ``ScanEntry.title``. To avoid reading the (potentially large) rollout body
    for sessions that already have a title from ``session_index.jsonl``, pass
    ``skip_title_sids`` — the set of sids that do NOT need a rollout-extracted
    title (because they have a ``thread_name`` in the index). Only sids NOT in
    this set have their first ``_CODEX_TITLE_SCAN_MAX_LINES`` lines read; all
    others read just the ``session_meta`` line.
    """
    result: Dict[str, ScanEntry] = {}
    for root, is_archived in _codex_roots():
        pattern = "**/rollout-*.jsonl"
        for path_str in glob.iglob(str(root / pattern), recursive=True):
            try:
                st = os.stat(path_str)
            except OSError:
                continue
            try:
                with open(path_str, "r", encoding="utf-8", errors="ignore") as f:
                    first = f.readline()
                    sid, ts_epoch, cwd, thread_source = _parse_session_meta(first)
                    if sid is None:
                        logger.warning("codex scan: cannot parse session_meta from %s", path_str)
                        continue
                    if with_title and (skip_title_sids is None or sid not in skip_title_sids):
                        # first line already consumed; read up to N-1 more for
                        # title extraction, then prepend the session_meta line.
                        rest = list(itertools.islice(f, _CODEX_TITLE_SCAN_MAX_LINES - 1))
                        title = _codex_title_from_lines([first] + rest)
                    else:
                        title = ""
            except OSError:
                logger.warning("codex scan: cannot read %s", path_str)
                continue
            entry = ScanEntry(
                path=path_str,
                mtime_ns=st.st_mtime_ns,
                size=st.st_size,
                cwd=cwd or "",
                ts=ts_epoch,
                is_archived=is_archived,
                title=title,
                thread_source=thread_source,
            )
            existing = result.get(sid)
            if existing is None:
                result[sid] = entry
                continue
            # Duplicate: prefer active > archived, then latest mtime.
            if existing.is_archived and not is_archived:
                result[sid] = entry
            elif is_archived == existing.is_archived and st.st_mtime_ns > existing.mtime_ns:
                result[sid] = entry
    return result


def _diff_scans(pre: Dict[str, ScanEntry], post: Dict[str, ScanEntry]) -> Dict[str, str]:
    """Compute per-sid change between a pre and post snapshot.

    Returns a dict sid -> change_type, where change_type is one of:
    * ``'new'``       — sid not in pre; a fresh rollout file was created.
    * ``'appended'``  — file existed pre and both size AND mtime grew.
    * ``'attr_changed'`` — existed pre but mtime changed without size growth
                        (e.g. fs touch, codex metadata rewrite); NOT treated
                        as session activity for attribution purposes.
    Sids present in pre but absent in post (archival / deletion) are omitted.
    """
    out: Dict[str, str] = {}
    for sid, post_entry in post.items():
        pre_entry = pre.get(sid)
        if pre_entry is None:
            out[sid] = "new"
            continue
        if post_entry.path != pre_entry.path:
            # sid relocated (e.g. moved to archived_sessions) — treat as attr
            out[sid] = "attr_changed"
            continue
        if post_entry.size > pre_entry.size and post_entry.mtime_ns > pre_entry.mtime_ns:
            out[sid] = "appended"
            continue
        if post_entry.mtime_ns != pre_entry.mtime_ns:
            out[sid] = "attr_changed"
    return out


def _codex_id_exists(sid: str, cwd: Optional[str] = None) -> bool:
    """Return True if ``sid`` is a known codex session (rollout-backed).

    Uses the same live+archived scan as the cold-start attribution logic.
    Optionally filters by cwd realpath so the gate only confirms the sid
    belongs to the workspace we care about.
    """
    if not sid:
        return False
    try:
        target_cwd = os.path.realpath(cwd) if cwd else None
    except OSError:
        target_cwd = cwd
    for scan_sid, entry in _codex_scan_sessions().items():
        if scan_sid != sid:
            continue
        if target_cwd is None:
            return True
        if entry.cwd:
            try:
                if os.path.realpath(entry.cwd) == target_cwd:
                    return True
            except OSError:
                if entry.cwd == cwd:
                    return True
    return False


# Backward-compat alias used by earlier revisions / tests.
_codex_id_in_index = _codex_id_exists


def _codex_candidates_for_cwd(cwd: str) -> List[Tuple[float, str, str]]:
    """Find codex rollout sessions started in ``cwd`` and return candidates.

    Returns list of ``(start_epoch, session_id, path)`` tuples. Used by the
    startup backfill to correlate live tmux session creation times with
    persisted rollouts.
    """
    out: List[Tuple[float, str, str]] = []
    try:
        target = os.path.realpath(cwd)
    except OSError:
        target = cwd
    for sid, entry in _codex_scan_sessions().items():
        if not entry.cwd:
            continue
        try:
            entry_cwd = os.path.realpath(entry.cwd)
        except OSError:
            entry_cwd = entry.cwd
        if entry_cwd == target and entry.ts is not None:
            out.append((entry.ts, sid, entry.path))
    return out


# Prefixes that mark a user message as boilerplate context (env / permissions /
# AGENTS.md / recommended plugins) rather than the user's actual first prompt.
# These are injected by codex (or the AGENTS.md preamble) at the start of every
# session and should not be used as the session title. A message counts as
# boilerplate if ANY of its content items starts with one of these prefixes.
_CODEX_SKIP_TITLE_PREFIXES = (
    "<environment_context>",
    "<permissions instructions>",
    "<system-reminder>",
    "<recommended_plugins>",
    "# AGENTS.md instructions",
    "<codex_internal_context",
)

# Prefixes for workspace-manager-injected bootstrap/role prompts. These are
# idle-session preamble (waiting instructions, reviewer guidelines, dispatcher
# setup, resident-agent directives) and do not contain a task title. They are
# skipped in the same way as codex's own boilerplate so that later real
# messages (task assignment, review, or human input) can surface as the title.
_CODEX_WORKSPACE_BOOTSTRAP_PREFIXES = (
    "You are a resident",
    "You are the dispatcher agent for this workspace.",
    "You are an independent reviewer agent for this workspace.",
    "You are this workspace's RESIDENT",
)

# Prefixes for workspace-manager task-delivery prompts. These ARE meaningful
# first messages but start with a system line rather than a human-authored
# title. For these we extract the "Task title:" field (or inline variant) and
# use it as the session title.
_CODEX_WORKSPACE_TASK_PREFIXES = (
    "New workspace task assigned.",
    "Review workspace task.",
    "Continue workspace task from review.",
    "Dispatch decision needed.",
)

# Prefixes for workspace-manager hard-recovery prompts. These start with a
# warning emoji paragraph but contain a "Task title:" line further down.
_CODEX_WORKSPACE_RECOVERY_PREFIXES = (
    "⚠️  Your previous context was automatically cleared",
    "⚠️  Context refreshed after error.",
)

# Regex to extract "Task title: <title>" from a task-delivery prompt body.
_CODEX_TASK_TITLE_RE = re.compile(r"^\s*Task title:\s*(.+?)\s*$", re.MULTILINE)

# Regex to extract the inline "Task: <id> (<title>)" variant used in the
# revision-resume prompt.
_CODEX_TASK_INLINE_RE = re.compile(
    r"^\s*Task:\s*[0-9a-fA-F-]+\s+\((.+?)\)\s*(?:mode=|complexity=|iteration=|$)",
    re.MULTILINE,
)

# Regex to extract "Workspace: <name>" and "Session: <id>" from bootstrap
# prompts so we can fall back to a role label like "Reviewer (my-ws)".
_CODEX_WORKSPACE_LINE_RE = re.compile(r"^\s*Workspace:\s*(.+?)\s*$", re.MULTILINE)
_CODEX_SESSION_LINE_RE = re.compile(r"^\s*Session:\s*(.+?)\s*$", re.MULTILINE)

# Maximum length of the extracted session title (truncated with ellipsis).
_CODEX_TITLE_MAX_LEN = 80
# The first meaningful user message (and any task-delivery prompt) always
# appears near the top of the rollout — within the first few dozen lines.
# Scanning the whole file (which can be 10+ MB) for sessions that never
# contain a task-title prompt is wasteful, so cap the scan.
_CODEX_TITLE_SCAN_MAX_LINES = 100


def _extract_task_title_from_text(text: str) -> str | None:
    """Try to pull a task title from a workspace task/recovery prompt body.

    Checks for both ``Task title: <x>`` and the inline ``Task: <id> (<x>)``
    variants. Returns the first match stripped, or None if neither is found.
    """
    m = _CODEX_TASK_TITLE_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _CODEX_TASK_INLINE_RE.search(text)
    if m:
        return m.group(1).strip()
    return None


def _bootstrap_role_label(text: str) -> str | None:
    """Derive a short role label from a workspace bootstrap/idle prompt.

    Uses the role-identifying first line plus workspace name when available,
    e.g. "Reviewer (my-workspace)". Returns None if the text does not look like
    a bootstrap prompt.
    """
    first_line = text.strip().split("\n", 1)[0]
    role_map = {
        "You are a resident": "Agent",
        "You are the dispatcher agent for this workspace.": "Dispatcher",
        "You are an independent reviewer agent for this workspace.": "Reviewer",
        "You are this workspace's RESIDENT": "Resident",
    }
    role = None
    for prefix, label in role_map.items():
        if first_line.startswith(prefix):
            role = label
            break
    if role is None:
        return None
    ws_match = _CODEX_WORKSPACE_LINE_RE.search(text)
    if ws_match:
        return f"{role} ({ws_match.group(1).strip()})"
    return role


def _codex_classify_and_extract(text: str) -> tuple[str, str | None]:
    """Classify a user message and return (action, extracted_title).

    Returns one of:
      - ("skip", None)   — boilerplate / bootstrap preamble, keep scanning
      - ("title", t)     — use ``t`` as the session title
      - ("fallback", t)  — use ``t`` (truncated first-line / collapsed text)
                           when no task title or role label applied
    """
    stripped = text.strip()
    if not stripped:
        return ("skip", None)

    # Workspace bootstrap/idle prompts: these come FIRST in a managed session
    # (before any task is assigned), so we do NOT return the role label
    # immediately — we remember it as a fallback and keep scanning for a later
    # task message. If the session is truly idle the caller will pick up the
    # remembered fallback at end-of-rollout.
    if any(stripped.startswith(p) for p in _CODEX_WORKSPACE_BOOTSTRAP_PREFIXES):
        label = _bootstrap_role_label(stripped)
        if label:
            return ("fallback", label)
        return ("skip", None)

    # Task-delivery prompts (assignment / review / continue / dispatch):
    # extract the embedded Task title.
    if any(stripped.startswith(p) for p in _CODEX_WORKSPACE_TASK_PREFIXES):
        t = _extract_task_title_from_text(stripped)
        if t:
            return ("title", t)
        return ("fallback", stripped)

    # Hard-recovery / revision-resume prompts: same extraction, just prefixed
    # with the warning emoji.
    if any(stripped.startswith(p) for p in _CODEX_WORKSPACE_RECOVERY_PREFIXES):
        t = _extract_task_title_from_text(stripped)
        if t:
            return ("title", t)
        return ("fallback", stripped)

    return ("fallback", stripped)


def _codex_title_from_lines(lines: Iterable[str]) -> str:
    """Extract a session title from an iterable of rollout JSONL lines.

    See :func:`_codex_session_title` for the extraction rules. This variant
    operates on already-read lines so callers can combine session_meta parsing
    and title extraction in a single file open.
    """
    fallback_title = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload") or {}
        if payload.get("role") != "user":
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        texts: List[str] = []
        is_preamble = False
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "input_text":
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            if any(text.startswith(p) for p in _CODEX_SKIP_TITLE_PREFIXES):
                is_preamble = True
                break
            texts.append(text)
        if is_preamble:
            continue
        if not texts:
            continue

        joined = "\n\n".join(texts)
        action, extracted = _codex_classify_and_extract(joined)
        if action == "skip":
            continue
        if action == "title" and extracted:
            title = " ".join(extracted.split())
            if len(title) > _CODEX_TITLE_MAX_LEN:
                title = title[: _CODEX_TITLE_MAX_LEN - 1] + "…"
            return title
        if action == "fallback" and extracted:
            collapsed = " ".join(extracted.split())
            if len(collapsed) > _CODEX_TITLE_MAX_LEN:
                collapsed = collapsed[: _CODEX_TITLE_MAX_LEN - 1] + "…"
            fallback_title = collapsed
    return fallback_title


def _codex_session_title(path: str) -> str:
    """Extract a human-readable title from a codex rollout file.

    The title is resolved from the first meaningful user message, skipping
    codex-internal boilerplate (environment/permissions/AGENTS.md/plugins) and
    workspace-manager bootstrap preamble. For workspace task-delivery prompts
    (assignment, review, continue, hard-recovery) the embedded ``Task title:``
    is used; for idle role-bootstrap sessions a label like
    ``"Reviewer (my-workspace)"`` is produced; for plain sessions the first
    real user message text is returned. Returns an empty string if no usable
    message is found. Defensive against malformed files: silently returns "".
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return _codex_title_from_lines(itertools.islice(f, _CODEX_TITLE_SCAN_MAX_LINES))
    except OSError:
        return ""


def _codex_load_session_index() -> Dict[str, str]:
    """Load ``~/.codex/session_index.jsonl`` as ``{session_id: thread_name}``.

    ``thread_name`` is the conversation title shown in the Codex app / IDE
    extension — exactly what the user expects to see in the session picker.
    The index is small (a few hundred KB) so loading it is cheap and lets us
    avoid scanning the (potentially multi-GB) rollout files for titles.
    Returns an empty dict if the file is missing or unreadable.
    """
    index_path = _codex_home_dir() / "session_index.jsonl"
    out: Dict[str, str] = {}
    try:
        with open(index_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = record.get("id")
                name = record.get("thread_name")
                if isinstance(sid, str) and sid and isinstance(name, str) and name:
                    out[sid] = name
    except OSError:
        return {}
    return out


# Cache for list_codex_sessions(): (cache_key, result). The cache key is a
# tuple of (session_index mtime_ns, max rollout mtime_ns) so we invalidate
# whenever the index or any rollout changes. TTL-bounded as a safety net.
_CODEX_SESSIONS_CACHE: Optional[Tuple[Tuple[int, int], List[Dict[str, Any]]]] = None
_CODEX_SESSIONS_CACHE_TTL = 30.0  # seconds


def _codex_sessions_cache_key() -> Tuple[int, int]:
    """Return a cache key for the codex sessions listing.

    The key is ``(session_index_mtime_ns, max_rollout_mtime_ns)``. When either
    changes (user renamed a thread, or a new rollout was written), the cache is
    invalidated. We stat the index and every date-level directory under the
    codex session roots; since rollout files are append-only and never modified
    in place, directory mtime changes (file additions/removals) are sufficient
    to detect new sessions.
    """
    index_path = _codex_home_dir() / "session_index.jsonl"
    try:
        index_mtime = index_path.stat().st_mtime_ns
    except OSError:
        index_mtime = 0

    max_rollout_mtime = 0
    for root, _ in _codex_roots():
        if not root.is_dir():
            continue
        try:
            dir_mtime = root.stat().st_mtime_ns
            if dir_mtime > max_rollout_mtime:
                max_rollout_mtime = dir_mtime
        except OSError:
            pass
        # Walk date subdirectories (codex organises rollouts as
        # sessions/YYYY/MM/DD/...). Stat each level's mtime; a new rollout
        # bumps the DD dir mtime.
        try:
            for year_dir in root.iterdir():
                if not year_dir.is_dir():
                    continue
                try:
                    ym = year_dir.stat().st_mtime_ns
                    if ym > max_rollout_mtime:
                        max_rollout_mtime = ym
                except OSError:
                    pass
                for month_dir in year_dir.iterdir():
                    if not month_dir.is_dir():
                        continue
                    try:
                        mm = month_dir.stat().st_mtime_ns
                        if mm > max_rollout_mtime:
                            max_rollout_mtime = mm
                    except OSError:
                        pass
                    for day_dir in month_dir.iterdir():
                        if not day_dir.is_dir():
                            continue
                        try:
                            dm = day_dir.stat().st_mtime_ns
                            if dm > max_rollout_mtime:
                                max_rollout_mtime = dm
                        except OSError:
                            pass
        except OSError:
            pass

    return (index_mtime, max_rollout_mtime)


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
    raw: Dict[str, Dict[str, Any]] = {}
    for sid, entry in _codex_scan_sessions(
        with_title=True, skip_title_sids=set(thread_names.keys())
    ).items():
        # Filter out subagent and automation sessions. Only user-initiated
        # sessions (thread_source == "user") and legacy rollouts (empty
        # thread_source) are shown in the picker.
        if entry.thread_source in ("subagent", "automation"):
            continue
        start_epoch = entry.ts or 0.0
        existing = raw.get(sid)
        if existing and existing["start_epoch"] >= start_epoch:
            continue
        title = thread_names.get(sid) or entry.title
        raw[sid] = {
            "session_id": sid,
            "cwd": entry.cwd or "",
            "start_epoch": start_epoch,
            "start_time": datetime.fromtimestamp(start_epoch).isoformat() if start_epoch else "",
            "title": title,
        }

    # Group by cwd.
    by_cwd: Dict[str, List[Dict[str, Any]]] = {}
    for sess in raw.values():
        by_cwd.setdefault(sess["cwd"], []).append(sess)

    # Sort within each group (most recent first) and order groups by latest.
    result: List[Dict[str, Any]] = []
    for cwd, sessions in sorted(
        by_cwd.items(),
        key=lambda kv: max(s["start_epoch"] for s in kv[1]),
        reverse=True,
    ):
        sessions.sort(key=lambda s: s["start_epoch"], reverse=True)
        for s in sessions:
            s.pop("start_epoch", None)  # internal-only, not for the API
        result.append(
            {
                "cwd": cwd,
                "sessions": sessions,
            }
        )

    _CODEX_SESSIONS_CACHE = (cache_key, result)
    return result


def _agent_spawn_env() -> dict:
    """Environment for ttyd/tmux-spawned agent panes.

    Agent TUIs (Cursor/Claude/Codex) gate their styled rendering on color
    support. Two things flatten them into a colorless, low-contrast UI here:

    1. Inside tmux the inner TERM is ``tmux-256color`` with ``COLORTERM`` unset.
    2. When the backend is launched from a parent process that disables color
       (e.g. another agent CLI exports ``NO_COLOR=1`` / ``FORCE_COLOR=0``),
       those flags are inherited all the way down to the agent panes and
       suppress every escape sequence — so the input prompt, dimmed
       placeholders and accent colors all collapse to one flat shade.

    Normalize the environment so panes always render with their full palette,
    regardless of how the backend itself was started.
    """
    env = dict(os.environ)
    env.pop("NO_COLOR", None)
    env["COLORTERM"] = "truecolor"
    env["FORCE_COLOR"] = "3"
    return env


def get_default_command() -> str:
    return settings.default_command


def get_agent_command(agent_type: AgentType) -> str:
    if agent_type == AgentType.CODEX:
        return "codex"
    if agent_type == AgentType.CURSOR:
        return "agent"
    return get_default_command()


class TTYDProcess:
    """Manages a single ttyd process backed by a tmux session for persistence."""

    def __init__(
        self,
        tab_id: str,
        port: int,
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        created_at: Optional[datetime] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: Optional[WorkspaceSessionRole] = None,
        env: Optional[Dict[str, str]] = None,
        agent_session_id: Optional[str] = None,
        from_persisted_state: bool = False,
        resume_quarantined: bool = False,
        shell_explicitly_provided: Optional[bool] = None,
        cursor_transport: str = "terminal",
        cursor_data_dir: Optional[str] = None,
        cursor_cli_version: Optional[str] = None,
        cursor_transcript_path: Optional[str] = None,
        cursor_transcript_schema: Optional[str] = None,
    ):
        self.tab_id = tab_id
        self.port = port
        self.name = name
        self.cwd = cwd
        self.solo_mode = solo_mode
        self.agent_type = agent_type
        self.target = target
        self.remote_profile_id = remote_profile_id
        self.remote_cwd = remote_cwd
        self.remote_reconnect = remote_reconnect
        self.remote_forward_port = remote_forward_port
        self.workspace_id = workspace_id
        self.workspace_name = workspace_name
        self.workspace_role = workspace_role
        self.cursor_transport = (
            cursor_transport if cursor_transport in {"acp", "terminal_transcript"} else "terminal"
        )
        self.cursor_data_dir = cursor_data_dir
        self.cursor_cli_version = cursor_cli_version
        self.cursor_transcript_path = cursor_transcript_path
        self.cursor_transcript_schema = cursor_transcript_schema
        launch_env = env if env else self._default_env_for_agent(agent_type)
        self.env = self._clean_env(launch_env)
        if self.cursor_transport == "terminal_transcript" and self.cursor_data_dir:
            # Pin the exact source directory into the child environment. This
            # keeps the process that owns the raw pane and the provenance that
            # gates Structured on the same Cursor data root, including custom
            # HOME/CURSOR_DATA_DIR launch environments.
            self.env["CURSOR_DATA_DIR"] = self.cursor_data_dir
        self._prepare_agent_env()
        if agent_type != AgentType.CLAUDE:
            self._setup_tunnel_env()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.created_at = created_at or datetime.now()
        self.is_active = False
        self.tmux_session = _tmux_session_name(tab_id)
        # Stable per-tab agent conversation id. Pinned at first launch via the
        # agent CLI's --session-id flag (for agents that support it) or
        # discovered from on-disk rollout shortly after launch (codex), so that
        # after a machine reboot (tmux sessions gone) we can resume EXACTLY this
        # tab's conversation instead of a fresh one. Many tabs share one cwd, so
        # a cwd-scoped --continue / --last would collide across tabs; an
        # explicit id keeps each tab distinct.
        #
        # - claude: supports --session-id at launch → pinned immediately.
        # - codex: no --session-id flag at launch; we generate an id at
        #   construction time and discover the REAL codex-assigned uuid from the
        #   rollout file shortly after the tab starts (see
        #   _discover_codex_session_id / _backfill_codex_session_ids), then
        #   overwrite self.agent_session_id with it. The generated value is
        #   only used temporarily and will be replaced before the next persist.
        #   For tabs that never successfully start (e.g. codex auth missing),
        #   the generated id stays — on recovery, `codex resume <fake-uuid>`
        #   fails and starts fresh, re-pinning via the same discovery path.
        # - cursor: constructive pin — `agent --resume <uuid>` creates a fresh
        #   chat store if none exists, so we ALWAYS pin a uuid4 at construction
        #   (defensive: start_all_tabs phase 0 backfills any legacy-None values).
        self.agent_session_id: Optional[str]
        if agent_session_id:
            self.agent_session_id = agent_session_id
        elif agent_type in (AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR):
            self.agent_session_id = str(uuid.uuid4())
        else:
            self.agent_session_id = None
        # True only when an explicit session id was supplied at construction
        # time (e.g. the user selected a specific Codex session to resume in
        # the create-tab UI). A generated uuid4 placeholder (above) is used
        # solely for conversation pinning and must NOT be treated as a
        # "resume this session" signal — see _should_recover.
        self._has_explicit_session_id: bool = bool(agent_session_id)
        if self.cursor_transport == "terminal_transcript" and not (
            self.agent_type == AgentType.CURSOR
            and self.target == ExecutionTarget.LOCAL
            and cursor_terminal_transcript_provenance_valid(
                cwd=self.cwd,
                session_id=self.agent_session_id,
                cli_version=self.cursor_cli_version,
                transcript_path=self.cursor_transcript_path,
                transcript_schema=self.cursor_transcript_schema,
                data_dir=self.cursor_data_dir,
                env=self.env,
            )
        ):
            logger.warning(
                "Cursor terminal transcript provenance mismatch for tab %s; "
                "failing closed to terminal transport",
                self.tab_id,
            )
            self.cursor_transport = "terminal"
            self.cursor_data_dir = None
            self.cursor_cli_version = None
            self.cursor_transcript_path = None
            self.cursor_transcript_schema = None
        # True when this process was reconstructed from persisted tabs.json on
        # startup (vs. freshly created by a user/API call). Combined with an
        # absent tmux session, this is the signal that we are recovering after a
        # reboot and should resume the prior conversation.
        self.from_persisted_state = from_persisted_state
        # Codex-only: True when a prior cold-start attribution failed and we
        # quarantined this tab to avoid cross-wiring. On the next launch we
        # MUST start fresh (no `codex resume`) to prevent replaying a
        # potentially mis-attributed session. Cleared by a successful FRESH_PIN
        # or the pre-sibling salvage check in Phase 1S; preserved across backend
        # restarts via tabs.json. Defaults to False for back-compat
        # with persisted states created before this field existed.
        self.resume_quarantined: bool = bool(resume_quarantined)
        # --- Runtime-only fields (NOT persisted in tabs.json) ---
        # Wall-clock and monotonic timestamps stamped immediately BEFORE
        # tmux new-session -d spawns the agent shell in ensure_tmux_session.
        # These anchor the new-pin timestamp window used for Phase 1C codex
        # attribution. Both must be stamped there (not in start()), because
        # ttyd is lazy: the agent actually runs inside tmux and the
        # 1-second await asyncio.sleep(1) in start() happens AFTER the agent
        # has already been producing output for ~1.1 s.
        self._launch_wall: Optional[float] = None
        self._launch_mono: Optional[float] = None
        # True iff this cold launch produced a new rollout file (vs appending
        # to an existing one on a successful resume). Set by Phase 1C/salvage.
        self._is_new_pin: Optional[bool] = None
        # Full dict[sidad, ScanEntry] snapshot taken immediately before
        # ensure_tmux_session. Retained so Phase-R reconciliation can verify
        # append-resume entries grew (size + mtime) relative to this
        # pre-launch baseline — a frozenset of sids cannot detect growth.
        self._pre_scan: Optional[Dict[str, ScanEntry]] = None
        # Set by Phase 1C while signal/exception failure is being salvaged before
        # a sibling launch. None means "launched cleanly".
        self._pending_quarantine_reason: Optional[str] = None
        # Track whether the caller explicitly supplied a shell. For agent tabs
        # (claude/codex/cursor) the default launch command is the agent CLI;
        # an explicit shell overrides that so the tab runs a plain shell
        # instead (used by tests that need deterministic scrollback without a
        # real agent login).
        #
        # NOTE: self.shell is always non-None (it falls back to the agent
        # command or the user's shell), so we cannot derive this flag from
        # self.shell after construction. It must be passed in explicitly when
        # reconstructing from persisted state.
        if shell_explicitly_provided is not None:
            self._shell_explicitly_provided = shell_explicitly_provided
        else:
            # Legacy state (pre-shell_explicitly_provided): the flag was not
            # persisted. self.shell is always non-None (it falls back to the
            # agent command or the user's shell), so bool(shell) would
            # incorrectly mark every agent tab as explicit-shell and bypass
            # the agent CLI/resume path. Instead, derive the flag by comparing
            # the persisted shell to the default for this agent type:
            #   - agent tabs: shell == get_agent_command(agent_type) means the
            #     default agent CLI was used (not an explicit override).
            #   - terminal tabs: the shell is always run directly, so the flag
            #     has no behavioral effect; default to False.
            if agent_type == AgentType.TERMINAL:
                self._shell_explicitly_provided = False
            else:
                default_cmd = get_agent_command(agent_type)
                self._shell_explicitly_provided = bool(shell) and shell != default_cmd
        if shell:
            self.shell = shell
        elif agent_type == AgentType.TERMINAL:
            self.shell = os.environ.get("SHELL", "/bin/bash")
        else:
            self.shell = get_agent_command(agent_type)

    @staticmethod
    def _clean_env(env: Dict[str, str]) -> Dict[str, str]:
        cleaned: Dict[str, str] = {}
        for key, value in env.items():
            name = str(key).strip()
            if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"Invalid environment variable name: {name or '<empty>'}")
            cleaned[name] = str(value)
        # Normalize proxy env vars: add the opposite-case variant so both
        # Node.js (uppercase HTTP_PROXY) and Unix tools (lowercase http_proxy)
        # pick up the same setting regardless of what the user typed.
        _PROXY_KEYS = frozenset({"http_proxy", "https_proxy", "all_proxy", "no_proxy"})
        additions: dict[str, str] = {}
        for key in list(cleaned):
            lower = key.lower()
            if lower in _PROXY_KEYS:
                opposite = key.upper() if key.islower() else key.lower()
                if opposite not in cleaned:
                    additions[opposite] = cleaned[key]
        cleaned.update(additions)
        return cleaned

    @staticmethod
    def _default_env_for_agent(agent_type: AgentType) -> Dict[str, str]:
        if agent_type != AgentType.CLAUDE:
            return {}
        return DEFAULT_CLAUDE_LAUNCH_ENV.copy()

    def _prepare_agent_env(self) -> None:
        if self.agent_type != AgentType.CLAUDE:
            return
        self._normalize_volcengine_coding_plan_model()

    def _normalize_volcengine_coding_plan_model(self) -> None:
        base_url = self.env.get("ANTHROPIC_BASE_URL", "")
        if "ark.cn-beijing.volces.com/api/coding" not in base_url:
            return
        model = self.env.get("ANTHROPIC_MODEL")
        if not model:
            return
        normalized = _VOLCENGINE_CODING_PLAN_MODEL_ALIASES.get(model.strip().lower())
        if not normalized:
            return
        for key in _MODEL_ENV_KEYS:
            current = self.env.get(key)
            if current is None or current.strip().lower() == model.strip().lower():
                self.env[key] = normalized

    # Tunnel registry: tunnel_key -> (local_port, process)
    _tunnel_registry: dict[str, tuple[int, subprocess.Popen]] = {}
    _TUNNEL_SCRIPT_TEMPLATE = """\
import asyncio, sys

async def _proxy(reader, writer):
    try:
        pr, pw = await asyncio.open_connection({proxy_host!r}, {proxy_port})
        pw.write(f"CONNECT {target_host}:{target_port} HTTP/1.1\\r\\nHost: {target_host}:{target_port}\\r\\n\\r\\n".encode())
        await pw.drain()
        resp = await pr.readuntil(b"\\r\\n\\r\\n")
        if b"200" not in resp.split(b"\\r\\n")[0]:
            writer.close(); pw.close(); pr.close()
            return
        async def pipe(r, w):
            try:
                while True:
                    data = await r.read(65536)
                    if not data: break
                    w.write(data); await w.drain()
            except: pass
        await asyncio.gather(pipe(reader, pw), pipe(pr, writer))
    except: pass
    finally:
        writer.close(); pw.close(); pr.close()

async def _main():
    server = await asyncio.start_server(_proxy, "127.0.0.1", {local_port})
    await server.serve_forever()

asyncio.run(_main())
"""

    def _setup_tunnel_env(self) -> None:
        """If proxy env vars + ANTHROPIC_BASE_URL are set, create a TCP
        tunnel through the CONNECT proxy and rewrite the API URL to point
        at the tunnel.

        This works around Node.js undici/http not reading HTTP_PROXY.
        """
        if not self.env:
            return

        # Find proxy host:port from env (any case variant)
        proxy_url: str | None = None
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            val = self.env.get(key)
            if val:
                proxy_url = val
                break
        if not proxy_url:
            return

        # Parse proxy URL
        m = re.match(r"^(https?://)?([^:/]+)(?::(\d+))?", proxy_url)
        if not m:
            return
        proxy_host = m.group(2)
        proxy_port = int(m.group(3) or (443 if m.group(1) == "https://" else 80))

        # Find target API URL
        api_key = next(
            (k for k in ("ANTHROPIC_BASE_URL", "anthropic_base_url") if k in self.env), None
        )
        if not api_key:
            return
        api_url = self.env[api_key]

        # Parse target host:port from API URL
        m = re.match(r"^https?://([^:/]+)(?::(\d+))?(/.*)?$", api_url)
        if not m:
            return
        target_host = m.group(1)
        target_port = int(m.group(2) or 443)
        target_path = m.group(3) or ""

        # Find a free port for the tunnel
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            local_port = s.getsockname()[1]

        tunnel_key = f"{target_host}:{target_port}->{proxy_host}:{proxy_port}"

        # Reuse existing tunnel
        existing = self._tunnel_registry.get(tunnel_key)
        if existing:
            local_port, proc = existing
            if proc.poll() is None:
                # Still alive — just rewrite env
                self.env[api_key] = f"https://127.0.0.1:{local_port}{target_path}"
                self.env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
                return
            # Dead — remove and recreate
            del self._tunnel_registry[tunnel_key]

        # Start new tunnel
        script = self._TUNNEL_SCRIPT_TEMPLATE.format(
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            target_host=target_host,
            target_port=target_port,
            local_port=local_port,
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self._tunnel_registry[tunnel_key] = (local_port, proc)

        # Wait briefly so the tunnel is ready
        time.sleep(0.15)

        # Rewrite env vars
        self.env[api_key] = f"https://127.0.0.1:{local_port}{target_path}"
        self.env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"

    def _env_shell_prefix(self) -> str:
        if not self.env:
            return ""
        assignments = [f"{key}={shlex.quote(value)}" for key, value in self.env.items()]
        return "env " + " ".join(assignments) + " "

    def _env_export_commands(self) -> list[str]:
        return [f"export {key}={shlex.quote(value)}" for key, value in self.env.items()]

    def _with_env(self, command: str) -> str:
        if self.env and self.target == ExecutionTarget.LOCAL:
            return self._with_local_env_wrapper(command)
        return f"{self._env_shell_prefix()}{command}"

    def _with_local_env_wrapper(self, command: str) -> str:
        LAUNCH_ENV_DIR.mkdir(parents=True, exist_ok=True)
        script_path = LAUNCH_ENV_DIR / f"{self.tab_id}.sh"
        exports = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in self.env.items())
        script = "#!/bin/sh\n" "set -eu\n" f"{exports}\n" 'exec "${SHELL:-/bin/bash}" -lc "$1"\n'
        script_path.write_text(script, encoding="utf-8")
        os.chmod(script_path, 0o600)
        return f"/bin/sh {shlex.quote(str(script_path))} {shlex.quote(command)}"

    def _claude_settings_arg(self) -> str:
        if self.agent_type != AgentType.CLAUDE or not self.env:
            return ""
        if self.target != ExecutionTarget.LOCAL:
            return ""
        LAUNCH_ENV_DIR.mkdir(parents=True, exist_ok=True)
        settings_path = LAUNCH_ENV_DIR / f"{self.tab_id}.settings.json"
        settings_path.write_text(
            json.dumps({"env": self.env}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(settings_path, 0o600)
        return f" --settings {shlex.quote(str(settings_path))}"

    def _claude_model_arg(self) -> str:
        if self.agent_type != AgentType.CLAUDE:
            return ""
        model = self.env.get("ANTHROPIC_MODEL")
        if not model:
            return ""
        return f" --model {shlex.quote(model)}"

    def _codex_model_arg(self) -> str:
        """Return the explicit model selected by a managed-session executor launch contract.

        ``CODEX_MODEL`` is an internal launch-contract variable, not a Codex
        authentication/config variable.  Keeping the value in the persisted
        tab environment lets a cold restart rebuild the same CLI command.
        """

        if self.agent_type != AgentType.CODEX:
            return ""
        model = self.env.get("CODEX_MODEL")
        if not model:
            return ""
        return f" --model {shlex.quote(model)}"

    def _solo_command(self) -> Optional[str]:
        if self.agent_type == AgentType.CODEX:
            return (
                "codex --ask-for-approval never --sandbox danger-full-access"
                f"{self._codex_model_arg()}"
            )
        if self.agent_type == AgentType.CLAUDE:
            return (
                "IS_SANDBOX=1 claude --dangerously-skip-permissions"
                f"{self._claude_settings_arg()}{self._claude_model_arg()}"
            )
        if self.agent_type == AgentType.CURSOR:
            # Cursor agent runs in yolo by default; solo_mode toggle is a no-op.
            return "agent"
        return None

    def _tmux_shell_command(self, session_exists: bool) -> str:
        if (
            self.solo_mode
            and not session_exists
            and self.agent_type
            in {
                AgentType.CLAUDE,
                AgentType.CODEX,
            }
        ):
            user_shell = os.environ.get("SHELL", "/bin/bash")
            solo_command = self._solo_command()
            if solo_command:
                return f"{shlex.quote(user_shell)} -c {shlex.quote(f'{self._with_env(solo_command)}; exec {user_shell}')}"
        if self.agent_type == AgentType.CLAUDE and not session_exists:
            return self._with_env(self._agent_start_command())
        # Non-solo agent tabs still need env var injection.
        return self._with_env(self.shell)

    async def ensure_tmux_session(self) -> bool:
        """Create the backing tmux session before ttyd gets a browser client.

        ttyd starts the tmux command lazily when the WebSocket connects. Workspace
        orchestration needs to send prompts before the user opens the tab, so it
        must ensure the tmux session exists independently.

        Stamps ``self._launch_wall`` / ``self._launch_mono`` immediately BEFORE
        spawning tmux so cold-start codex attribution can anchor its new-pin
        timestamp window to the actual moment the agent process starts. This
        is the correct anchor (not start()): ``process.start()`` awaits
        ``asyncio.sleep(1)`` during which the agent already runs inside tmux,
        so stamping there would systematically under-estimate the codex
        session_meta timestamp by ~1 s.
        """
        if await _tmux_session_exists_async(self.tmux_session):
            return False

        _ensure_tmux_server()
        cmd = tmux_command("new-session", "-d", "-s", self.tmux_session)
        if self.cwd and self.target == ExecutionTarget.LOCAL:
            cmd.extend(["-c", self.cwd])
        cmd.append("--")
        # Session is guaranteed absent here (we returned early if it existed),
        # so recover whenever this tab was restored from persisted state.
        recover = self._should_recover(session_exists=False)
        if self.target == ExecutionTarget.REMOTE:
            cmd.append(shlex.join(self._build_remote_launcher()))
        elif self._shell_explicitly_provided:
            # Caller supplied an explicit shell (e.g. tests that need a plain
            # shell for deterministic scrollback). Run it directly instead of
            # the agent CLI.
            cmd.append(self._with_env(self.shell))
        elif self.solo_mode and self.agent_type in {AgentType.CLAUDE, AgentType.CODEX}:
            user_shell = os.environ.get("SHELL", "/bin/bash")
            cmd.append(
                shlex.join(
                    [
                        user_shell,
                        "-c",
                        f"{self._with_env(self._agent_start_command(recover=recover))}; exec {user_shell}",
                    ]
                )
            )
        elif self.agent_type == AgentType.CURSOR:
            user_shell = os.environ.get("SHELL", "/bin/bash")
            cmd.append(
                shlex.join(
                    [
                        user_shell,
                        "-c",
                        f"{self._with_env(self._agent_start_command(recover=recover))}; exec {user_shell}",
                    ]
                )
            )
        elif self.agent_type == AgentType.CLAUDE:
            cmd.append(self._with_env(self._agent_start_command(recover=recover)))
        elif self.agent_type == AgentType.CODEX:
            # Always build local Codex launches through the persisted CLI
            # contract.  In particular, ``CODEX_MODEL`` is an internal Hub
            # variable and only selects a real Codex model once it is
            # translated to ``--model`` here.  This also keeps fresh and
            # restored non-solo tabs on the same command path.
            cmd.append(self._with_env(self._codex_launch_command(recover=recover)))
        else:
            cmd.append(self._with_env(self.shell))

        # F1 clock anchor: stamped immediately before tmux new-session so the
        # codex new-pin attribution window lines up with when the agent shell
        # actually starts executing. Both clocks captured in the same awaitable
        # tick to minimize drift.
        self._launch_wall = time.time()
        self._launch_mono = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=_agent_spawn_env(),
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux new-session failed with code {proc.returncode}")
        return True

    async def start(self) -> None:
        """Start the ttyd process with tmux for session persistence."""
        if self.process and self.process.returncode is None:
            logger.warning(f"Process for tab {self.tab_id} already running")
            return

        # Ensure tmux server is running first
        _ensure_tmux_server()

        # Check if tmux session already exists
        session_exists = await _tmux_session_exists_async(self.tmux_session)
        if session_exists:
            logger.info(f"tmux session {self.tmux_session} exists, will reattach")
        else:
            logger.info(f"tmux session {self.tmux_session} does not exist, will create new")

        cmd = self._build_ttyd_command(session_exists=session_exists)
        logger.info(
            "Starting ttyd for tab %s on port %s with %s custom env vars",
            self.tab_id,
            self.port,
            len(self.env),
        )

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_agent_spawn_env(),
            )
            self.is_active = True
            logger.info(
                f"ttyd started for tab {self.tab_id} with PID {self.process.pid}, tmux session: {self.tmux_session}"
            )

            # Enable tmux mouse mode and other options
            await self._configure_tmux()

            asyncio.create_task(self._log_stderr())

            await asyncio.sleep(1)

            if self.process.returncode is not None:
                stderr = await self.process.stderr.read() if self.process.stderr else b""
                logger.error(
                    f"ttyd exited immediately with code {self.process.returncode}. Stderr: {stderr.decode()}"
                )
                raise Exception(f"ttyd failed to start: {stderr.decode()}")

        except Exception as e:
            logger.error(f"Failed to start ttyd for tab {self.tab_id}: {e}")
            self.is_active = False
            raise

    # xterm.js client options tuned for minimal input latency and maximum
    # render throughput. These are forwarded to every ttyd instance via
    # repeated ``-t key=value`` flags.
    #
    # Rationale:
    #   cursorBlink=false        — each blink repaints the cursor cell; also
    #                              makes the UI feel janky on repaint-heavy
    #                              frames. Saves ~1 repaint/500ms and removes a
    #                              common source of perceived typing jitter.
    #   rendererType=canvas      — the WebGL renderer (xterm.js v4, bundled by
    #                              ttyd 1.7.x) mis-renders the text-selection
    #                              highlight: the blue highlight rectangle is
    #                              painted offset from the glyphs it covers, so
    #                              dragging to select "串行"/drifts visually even
    #                              though the copied text is correct (xterm.js
    #                              issue #5198; there is no .xterm-selection DOM
    #                              node in WebGL mode, so the CSS workaround does
    #                              not apply). The 2D canvas renderer paints the
    #                              selection on its own layer, aligned with the
    #                              text. Output throughput is slightly lower than
    #                              WebGL but ample for agent TUIs; input latency
    #                              (SAB fast path) is renderer-independent.
    #   allowProposedApi=true    — harmless; kept from the previous baseline.
    #   drawBoldTextInBrightColors=false — avoids extra color map lookups.
    #   minimumContrastRatio=1   — skip contrast adjustment work per glyph.
    #   scrollback=100000        — kept from the previous baseline.
    #   fastScrollModifier=alt   — kept from the previous baseline.
    #   macOptionIsMeta=false    — kept from the previous baseline.
    _TTYD_CLIENT_OPTIONS: tuple[tuple[str, str], ...] = (
        ("scrollback", "100000"),
        ("fastScrollModifier", "alt"),
        ("macOptionIsMeta", "false"),
        ("cursorBlink", "false"),
        ("rendererType", "canvas"),
        ("allowProposedApi", "true"),
        ("drawBoldTextInBrightColors", "false"),
        ("minimumContrastRatio", "1"),
        # Match the native terminal's crisp monospace rendering. Without an
        # explicit fontFamily, xterm.js falls back to a chunky Courier-style
        # font that makes agent TUIs (Cursor/Claude/Codex) look ugly. ttyd
        # parses each -t value as JSON, so this string must itself be
        # double-quoted; inner double quotes around font names with spaces
        # are escaped per JSON rules.
        (
            "fontFamily",
            '"ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \\"Liberation Mono\\", monospace"',
        ),
        ("fontSize", "14"),
        ("lineHeight", "1.2"),
    )

    def _build_ttyd_command(self, session_exists: bool) -> list[str]:
        # tmux new-session -A: attach if exists, create if not.
        # This is the key to persistence across page refreshes.
        cmd: list[str] = [
            settings.ttyd_path,
            "--port",
            str(self.port),
            "--interface",
            "127.0.0.1",
            "--writable",
        ]
        for key, value in self._TTYD_CLIENT_OPTIONS:
            cmd.extend(["-t", f"{key}={value}"])
        cmd.extend(
            [
                "tmux",
                *tmux_socket_args(),
                "new-session",
                "-A",
                "-s",
                self.tmux_session,
            ]
        )
        if self.cwd and self.target == ExecutionTarget.LOCAL:
            cmd.extend(["-c", self.cwd])

        recover = self._should_recover(session_exists=session_exists)
        if self.target == ExecutionTarget.REMOTE:
            cmd.extend(self._build_remote_launcher())
        elif self._shell_explicitly_provided:
            # Caller supplied an explicit shell (e.g. tests that need a plain
            # shell for deterministic scrollback). Run it directly instead of
            # the agent CLI.
            cmd.append(self._with_env(self.shell))
        elif (
            self.solo_mode
            and not session_exists
            and self.agent_type
            in {
                AgentType.CLAUDE,
                AgentType.CODEX,
            }
        ):
            user_shell = os.environ.get("SHELL", "/bin/bash")
            cmd.extend(
                [
                    user_shell,
                    "-c",
                    f"{self._with_env(self._agent_start_command(recover=recover))}; exec {user_shell}",
                ]
            )
        elif self.agent_type == AgentType.CURSOR and not session_exists:
            user_shell = os.environ.get("SHELL", "/bin/bash")
            cmd.extend(
                [
                    user_shell,
                    "-c",
                    f"{self._with_env(self._agent_start_command(recover=recover))}; exec {user_shell}",
                ]
            )
        elif self.agent_type == AgentType.CLAUDE and not session_exists:
            cmd.append(self._with_env(self._agent_start_command(recover=recover)))
        elif self.agent_type == AgentType.CODEX and not session_exists:
            # A live tmux session is reattached below without starting a
            # second process.  Every actual local Codex launch, fresh or
            # recovered and solo or non-solo, uses the persisted command
            # builder so ``CODEX_MODEL`` becomes a real ``--model`` flag.
            cmd.append(self._with_env(self._codex_launch_command(recover=recover)))
        else:
            cmd.append(self._with_env(self.shell))

        return cmd

    def _claude_session_arg(self) -> str:
        """Fresh-launch flag pinning this tab's stable conversation id."""
        if self.agent_type != AgentType.CLAUDE:
            return ""
        if not self.agent_session_id:
            # Defensive: a persisted tab may have agent_session_id=None if it
            # was created before pinning shipped and backfill was ambiguous.
            # Generate a fresh id here so future restarts can pin/resume it.
            self.agent_session_id = str(uuid.uuid4())
            logger.info(f"generated defensive agent_session_id for claude tab {self.tab_id}")
        return f" --session-id {shlex.quote(self.agent_session_id)}"

    def _claude_command(self, session_flag: str) -> str:
        if self.solo_mode:
            base = "IS_SANDBOX=1 claude --dangerously-skip-permissions"
        else:
            base = get_default_command()
        return f"{base}{self._claude_settings_arg()}{self._claude_model_arg()}{session_flag}"

    def _should_recover(self, session_exists: bool) -> bool:
        """Recover (resume prior conversation) when:
        1. Relaunching a persisted tab whose tmux session is gone (machine
           reboot), OR
        2. A fresh tab was created with an explicit ``agent_session_id`` to
           resume (e.g. the user selected a specific Codex session in the
           create-tab UI).

        A live tmux session (backend-only restart) reattaches and must not
        resume. Scoped to local agents that expose resume flags.
        """
        if session_exists:
            return False
        if self.target != ExecutionTarget.LOCAL:
            return False
        if self.agent_type not in {AgentType.CLAUDE, AgentType.CODEX, AgentType.CURSOR}:
            return False
        if self.from_persisted_state:
            return True
        # Fresh tab created with an explicit session id to resume. The
        # launch command will use codex resume <id> / claude --resume <id>.
        # A generated uuid4 placeholder (no explicit id) is for pinning only
        # and must not trigger recovery of a (non-existent) session.
        if self._has_explicit_session_id:
            return True
        return False

    def _discover_codex_session_id(self, launch_epoch: float) -> Optional[str]:
        """Legacy single-tab codex discovery (used by create_tab /
        ensure_tab_tmux_session, which launch ONE codex at a time outside the
        bulk startup path and so don't need the global lock / Phase-R
        reconciliation).

        After a fresh codex launch (single-tab path), we wait ~5s for codex
        to flush session_meta, then scan for a new-pin rollout in this cwd
        whose session_meta.ts falls within [-2s, +8s] of ``launch_epoch``.
        Returns the discovered sid or None if ambiguous/missing.
        """
        if self.agent_type != AgentType.CODEX or not self.cwd:
            return None
        try:
            target_cwd = os.path.realpath(self.cwd)
        except OSError:
            target_cwd = self.cwd
        best: Optional[Tuple[float, str]] = None
        for sid, entry in _codex_scan_sessions().items():
            if not entry.cwd or entry.ts is None:
                continue
            try:
                if os.path.realpath(entry.cwd) != target_cwd:
                    continue
            except OSError:
                if entry.cwd != self.cwd:
                    continue
            delta = entry.ts - launch_epoch
            if delta < -_CODEX_NEW_PIN_TS_EARLY_S:
                continue
            if delta > _CODEX_NEW_PIN_TS_LATE_S:
                continue
            if best is None or abs(delta) < abs(best[0]):
                best = (delta, sid)
        if best is None:
            return None
        logger.info(
            "discovered codex session for tab %s -> %s (delta %.1fs from launch)",
            self.tab_id,
            best[1],
            best[0],
        )
        return best[1]

    def _codex_launch_command(self, recover: bool) -> str:
        """Build the codex launch command, preserving solo flags on both branches.

        When ``recover`` is True AND ``self.agent_session_id`` is a VERIFIED
        codex session (rollout exists, active or archived) AND this tab is
        NOT quarantined, recovery targets EXACTLY that session via
        ``codex resume <uuid>`` with a fresh fallback (``|| codex``) so that
        if resume fails for any reason codex starts fresh rather than
        silently attaching to a different session (BUG-3: the old
        ``resume --last`` path was cwd-scoped and cross-wired same-cwd tabs).

        When quarantine is set OR no verified id exists, start fresh — the
        Phase 1C attribution logic will pin the newly-created rollout after
        the fence, and next restart will resume exactly that conversation.

        When ``recover`` is False (fresh tab create, ttyd-attached hot
        restart), start fresh with no resume.

        The solo flags (``--ask-for-approval never --sandbox danger-full-access``)
        MUST be applied to BOTH the ``resume`` branch and the fresh fallback:
        ``codex resume`` accepts them, and omitting them on resume silently
        drops solo mode whenever the resume succeeds.
        """
        flags = " --ask-for-approval never --sandbox danger-full-access" if self.solo_mode else ""
        model_arg = self._codex_model_arg()
        fresh = f"codex{flags}{model_arg}"
        if (
            recover
            and not self.resume_quarantined
            and self.agent_session_id
            # cwd-scoped verification (BUG-3 class): a sid that exists on disk
            # but belongs to a DIFFERENT cwd must NOT be resumed here — that
            # would attach this tab to another workspace's conversation.
            and _codex_id_exists(self.agent_session_id, self.cwd)
        ):
            quoted_sid = shlex.quote(self.agent_session_id)
            return f"codex resume {quoted_sid}{flags}{model_arg} || {fresh}"
        # Quarantined / unverified: start fresh; Phase 1C will pin the new sid.
        # DO NOT fall back to `codex resume --last` — that was BUG-3 (cwd-scoped
        # cross-wiring across same-cwd tabs).
        return fresh

    def _agent_start_command(self, recover: bool = False) -> str:
        if self.agent_type == AgentType.CODEX:
            return self._codex_launch_command(recover=recover)
        if self.agent_type == AgentType.CURSOR:
            if self.cursor_transport == "terminal_transcript":
                # One authoritative process owns both the raw Terminal view
                # and the transcript tailed for Chat. Provenance is validated
                # at construction/load, so never rotate or fall back here.
                assert self.agent_session_id is not None
                flags = " --yolo" if self.solo_mode else ""
                return f"agent --resume {shlex.quote(self.agent_session_id)}{flags}"
            # Cursor CLI supports constructive pinning via `agent --resume <uuid>`:
            # V0 verified that passing an arbitrary uuid causes Cursor to create
            # a fresh store.db immediately (rather than erroring out). A persisted
            # sid is resumed only when its store verifies against this cwd. If it
            # is missing or belongs to another cwd, rotate to a fresh uuid rather
            # than asking Cursor to resolve an unsafe cross-workspace id.
            if not self.agent_session_id:
                self.agent_session_id = str(uuid.uuid4())
            flags = " --yolo" if self.solo_mode else ""
            if (
                recover
                and not self.resume_quarantined
                and _cursor_id_exists(self.agent_session_id, self.cwd or "")
            ):
                quoted_sid = shlex.quote(self.agent_session_id)
                return f"agent --resume {quoted_sid}{flags} || agent{flags}"
            if recover:
                self.agent_session_id = str(uuid.uuid4())
                self.resume_quarantined = False
            quoted_sid = shlex.quote(self.agent_session_id)
            return f"agent --resume {quoted_sid}{flags}"
        if self.agent_type == AgentType.TERMINAL:
            return "${SHELL:-/bin/bash} -l"
        # claude: pin a stable --session-id on first launch so a reboot can
        # resume EXACTLY this tab's conversation via --resume. If resume fails
        # (no such session yet, e.g. a legacy tab), fall back to a fresh start
        # that re-pins the same id for next time.
        if recover and self.agent_session_id:
            resume = self._claude_command(f" --resume {shlex.quote(self.agent_session_id)}")
            return f"{resume} || {self._claude_command(self._claude_session_arg())}"
        return self._claude_command(self._claude_session_arg())

    async def switch_env(self, new_env: Dict[str, str], solo_mode: Optional[bool] = None) -> None:
        """Hot-swap the launch env and/or solo mode of a live local Claude or Codex tab.

        Rewrites the on-disk launch wrapper (and settings.json for Claude), then
        uses ``tmux respawn-pane -k`` to relaunch the agent with resume flags so
        conversation history is preserved. Pane scrollback is preserved; the
        running agent process is killed and replaced.

        For Claude: resumes via ``--resume <agent_session_id>`` (falling back to
        ``--session-id`` so the conversation id is re-pinned).
        For Codex: resumes via ``codex resume <uuid>`` only when the id is
        verified for this cwd, otherwise starts fresh.

        Raises:
            ValueError: if the tab is not a local Claude/Codex tab.
            RuntimeError: if the tmux session is not alive or respawn fails.
        """
        if self.agent_type not in {AgentType.CLAUDE, AgentType.CODEX}:
            raise ValueError("switch_env is only supported for Claude and Codex tabs")
        if self.target != ExecutionTarget.LOCAL:
            raise ValueError("switch_env is only supported for local tabs")
        if not await _tmux_session_exists_async(self.tmux_session):
            raise RuntimeError("tmux session is not running; cannot switch env on a stopped tab")

        # Snapshot current state and on-disk launch files so we can roll back if
        # respawn-pane fails. Without rollback, a tmux-level failure (bad session,
        # tmux crashed, etc.) would leave self.env/self.solo_mode and the
        # <tabid>.sh/.settings.json out of sync with the still-running agent.
        old_env = dict(self.env)
        old_solo_mode = self.solo_mode
        LAUNCH_ENV_DIR.mkdir(parents=True, exist_ok=True)
        script_path = LAUNCH_ENV_DIR / f"{self.tab_id}.sh"
        settings_path = LAUNCH_ENV_DIR / f"{self.tab_id}.settings.json"
        old_script_bytes = script_path.read_bytes() if script_path.exists() else None
        old_settings_bytes = settings_path.read_bytes() if settings_path.exists() else None

        def _rollback_launch_files() -> None:
            self.env = self._clean_env(old_env)
            self._prepare_agent_env()
            self.solo_mode = old_solo_mode
            if old_script_bytes is not None:
                script_path.write_bytes(old_script_bytes)
            elif script_path.exists():
                script_path.unlink()
            if old_settings_bytes is not None:
                settings_path.write_bytes(old_settings_bytes)
            elif settings_path.exists():
                settings_path.unlink()

        if solo_mode is not None:
            self.solo_mode = solo_mode
        self.env = self._clean_env(new_env)
        self._prepare_agent_env()

        # Build the relaunch command first; _with_local_env_wrapper and
        # _claude_settings_arg write the .sh and .settings.json as a side effect.
        # If those writes raise (e.g. invalid env), state was already mutated
        # above but no tmux operation has happened yet, so the on-disk files are
        # consistent with self.env and next restart/switch will pick them up.
        user_shell = os.environ.get("SHELL", "/bin/bash")

        if self.agent_type == AgentType.CODEX:
            # Codex: resume only via a pinned, cwd-verified session uuid;
            # otherwise start fresh. Solo flags apply to both the exact-resume
            # and fallback branches (delegated to _codex_launch_command).
            inner_cmd = self._codex_launch_command(recover=True)
        else:
            # Claude path
            settings_arg = self._claude_settings_arg()
            model_arg = self._claude_model_arg()
            session_arg = self._claude_session_arg()

            if self.solo_mode:
                base = "IS_SANDBOX=1 claude --dangerously-skip-permissions"
            else:
                base = get_default_command()

            if self.agent_session_id:
                quoted_sid = shlex.quote(self.agent_session_id)
                resume_inner = f"{base}{settings_arg}{model_arg} --resume {quoted_sid}"
                inner_cmd = f"{resume_inner} || {base}{settings_arg}{model_arg}{session_arg}"
            else:
                # Defensive: legacy tab without a pinned session id; start fresh.
                inner_cmd = f"{base}{settings_arg}{model_arg}{session_arg}"

        wrapped = self._with_env(inner_cmd)

        # Append `; exec $SHELL` so the pane stays alive at a shell when the
        # agent exits. This matches how solo/cursor/codex tabs launch initially
        # and gives the user a visible shell prompt if resume fails and the
        # fresh fallback also errors out (otherwise the pane would die silently).
        respawn_cmd = f"{wrapped}; exec {shlex.quote(user_shell)}"

        try:
            proc = await asyncio.create_subprocess_exec(
                *tmux_command(
                    "respawn-pane",
                    "-k",
                    "-t",
                    self.tmux_session,
                    "--",
                    user_shell,
                    "-lc",
                    respawn_cmd,
                ),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=_agent_spawn_env(),
            )
            _, stderr = await proc.communicate()
        except Exception:
            _rollback_launch_files()
            raise
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            _rollback_launch_files()
            raise RuntimeError(error or f"tmux respawn-pane failed with code {proc.returncode}")

    def _remote_ssh_target(self) -> tuple[str, int]:
        if not self.remote_profile_id:
            raise ValueError("Remote tab requires remote_profile_id")
        profile = remote_profile_manager.get_profile(self.remote_profile_id)
        if not profile:
            raise ValueError(f"Remote profile not found: {self.remote_profile_id}")
        host = f"{profile.user}@{profile.ssh_host}" if profile.user else profile.ssh_host
        return host, profile.port

    @staticmethod
    def _remote_path_bootstrap() -> str:
        return (
            'if [ -d "$HOME/.nvm/versions/node" ]; then '
            'for dir in "$HOME"/.nvm/versions/node/*/bin; do '
            '[ -d "$dir" ] && PATH="$dir:$PATH"; '
            "done; "
            "export PATH; "
            "fi"
        )

    @staticmethod
    def _remote_shell_command(script: str) -> str:
        return f"exec ${{SHELL:-/bin/bash}} -lc {shlex.quote(script)}"

    def _build_remote_ssh_command(self, remote_command: str) -> list[str]:
        host, port = self._remote_ssh_target()
        cmd = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "NumberOfPasswordPrompts=0",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "LogLevel=ERROR",
        ]
        if port != 22:
            cmd.extend(["-p", str(port)])
        cmd.extend([host, remote_command])
        return cmd

    def _build_remote_attach_command(self) -> str:
        remote_session = self.tmux_session
        cwd = self.remote_cwd or self.cwd or "~"
        start_command = self._with_env(self._agent_start_command())
        quoted_session = shlex.quote(remote_session)
        quoted_start = shlex.quote(start_command)
        shell = "${SHELL:-/bin/bash}"
        bootstrap_path = self._remote_path_bootstrap()
        checks: list[str] = []

        direct_start_script = "; ".join(
            [
                "printf 'Remote tmux not found in PATH; starting without remote tmux persistence.\\n'",
                start_command,
                "status=$?",
                "printf '\\nRemote agent exited with code %s. Dropping to shell.\\n' \"$status\"",
                f"exec {shell} -l",
            ]
        )

        script = "; ".join(
            [
                bootstrap_path,
                *self._env_export_commands(),
                *checks,
                self._remote_cwd_command(cwd, shell),
                "if command -v tmux >/dev/null 2>&1; then "
                f"tmux has-session -t {quoted_session} 2>/dev/null || "
                f"tmux new-session -d -s {quoted_session} {quoted_start}; "
                f"tmux set-option -t {quoted_session} status off >/dev/null 2>&1 || true; "
                f"tmux set-option -t {quoted_session} mouse off >/dev/null 2>&1 || true; "
                f"tmux set-option -t {quoted_session} focus-events on >/dev/null 2>&1 || true; "
                f"tmux set-option -t {quoted_session} history-limit 100000 >/dev/null 2>&1 || true; "
                f"tmux set-window-option -t {quoted_session} mode-keys vi >/dev/null 2>&1 || true; "
                f"exec tmux attach-session -t {quoted_session}; "
                "fi",
                direct_start_script,
            ]
        )
        return self._remote_shell_command(script)

    @classmethod
    def _remote_cwd_command(cls, cwd: str, shell: str) -> str:
        quoted_cwd = cls._quote_remote_cwd(cwd)
        cwd_arg = shlex.quote(cwd)
        return (
            f"cd {quoted_cwd} || {{ "
            f"printf 'Remote cwd not found: %s; using home directory.\\n' {cwd_arg}; "
            "cd ~ || { "
            "printf 'Remote home directory not available; dropping to shell.\\n'; "
            f"exec {shell} -l; "
            "}; "
            "}"
        )

    @staticmethod
    def _quote_remote_cwd(cwd: str) -> str:
        if cwd == "~":
            return "~"
        if cwd.startswith("~/"):
            return "~/" + shlex.quote(cwd[2:])
        return shlex.quote(cwd)

    def _build_remote_launcher(self) -> list[str]:
        host, port = self._remote_ssh_target()
        user_shell = os.environ.get("SHELL", "/bin/bash")
        ssh_parts = ["ssh", "-tt"]
        ssh_parts.extend(["-o", "LogLevel=ERROR"])
        if self.remote_forward_port:
            ssh_parts.extend(
                [
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-R",
                    f"127.0.0.1:{self.remote_forward_port}:127.0.0.1:{settings.port}",
                ]
            )
        if port != 22:
            ssh_parts.extend(["-p", str(port)])
        ssh_parts.extend([host, self._build_remote_attach_command()])
        ssh_command = " ".join(shlex.quote(part) for part in ssh_parts)

        if self.remote_reconnect:
            launcher = (
                "while true; do "
                f"{ssh_command}; "
                "status=$?; "
                "printf '\\nRemote connection closed with code %s. "
                'Reconnecting in 3 seconds. Press Ctrl-C to stop.\\n\' "$status"; '
                "sleep 3; "
                "done"
            )
        else:
            launcher = f"exec {ssh_command}"

        return [user_shell, "-lc", launcher]

    async def _log_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        try:
            async for line in self.process.stderr:
                logger.warning(f"ttyd[{self.tab_id}] stderr: {line.decode().strip()}")
        except Exception as e:
            logger.debug(f"Error reading stderr for tab {self.tab_id}: {e}")

    async def _configure_tmux(self) -> None:
        """Configure tmux options for better mouse scrolling and user experience."""
        try:
            # Wait a bit for tmux session to be ready
            await asyncio.sleep(0.5)

            # Set tmux options - these will apply to all sessions
            tmux_commands = [
                ["set", "-g", "mouse", "off"],
                ["set", "-g", "history-limit", "100000"],
                ["set", "-g", "terminal-overrides", "xterm*:smcup@:rmcup@"],
                # Advertise 24-bit color so agent TUIs render their full palette
                # instead of degrading to a flat, colorless UI under tmux. The
                # tmux server inherits its global environment from whatever
                # launched it; if that parent disabled color (NO_COLOR=1 /
                # FORCE_COLOR=0, common when started from another agent CLI),
                # tmux forces those onto every new pane and overrides the env we
                # pass to new-session. Scrub them from the global environment so
                # panes can emit color.
                ["set", "-as", "terminal-features", ",xterm-256color:RGB"],
                ["setenv", "-g", "-u", "NO_COLOR"],
                ["setenv", "-g", "FORCE_COLOR", "3"],
                ["setenv", "-g", "COLORTERM", "truecolor"],
                # Allow scrolling without entering copy mode explicitly
                ["set", "-g", "mode-keys", "vi"],
                ["set", "-g", "status", "off"],
            ]

            for cmd in tmux_commands:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *tmux_command(*cmd),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                except Exception:
                    pass

            # Set options on our specific session too
            for cmd in tmux_commands:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *tmux_command("-t", self.tmux_session, *cmd),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                except Exception:
                    pass

            logger.info(f"Configured tmux options for tab {self.tab_id}")
        except Exception as e:
            logger.warning(f"Failed to configure tmux for tab {self.tab_id}: {e}")

    async def _run_remote_capture_command(self, remote_command: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *self._build_remote_ssh_command(remote_command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=_REMOTE_CAPTURE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RuntimeError("remote tmux capture timed out") from exc

        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"remote tmux capture failed with code {proc.returncode}")

        return stdout.decode("utf-8", errors="ignore")

    async def _capture_remote_history(self, lines: int = 100000) -> str:
        safe_lines = max(100, min(lines, 100000))
        start = f"-{safe_lines}"
        script = "; ".join(
            [
                self._remote_path_bootstrap(),
                "command -v tmux >/dev/null 2>&1",
                (
                    "tmux capture-pane -p -e "
                    f"-S {shlex.quote(start)} "
                    f"-t {shlex.quote(self.tmux_session)}"
                ),
            ]
        )
        return await self._run_remote_capture_command(self._remote_shell_command(script))

    async def _capture_local_history(self, lines: int = 100000) -> str:
        """Capture full terminal history from tmux for replay on reconnect.

        Captures the entire terminal content (scrollback + visible screen)
        because the client-side replay must clear and rewrite the full
        buffer when ttyd has already rendered the visible screen before
        our script can intercept.

        Returns an empty string if the tmux session does not exist yet
        (ttyd creates sessions lazily on first WebSocket connection).
        """
        if not await _tmux_session_exists_async(self.tmux_session):
            return ""
        safe_lines = max(100, min(lines, 100000))
        start = f"-{safe_lines}"

        proc = await asyncio.create_subprocess_exec(
            *tmux_command(
                "capture-pane",
                "-p",
                "-e",
                "-S",
                start,
                "-t",
                self.tmux_session,
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux capture-pane failed with code {proc.returncode}")

        return stdout.decode("utf-8", errors="ignore")

    async def capture_history(self, lines: int = 100000, prefer_remote: bool = False) -> str:
        if prefer_remote and self.target == ExecutionTarget.REMOTE:
            try:
                remote_history = await self._capture_remote_history(lines)
                if remote_history:
                    return remote_history
            except Exception as e:
                logger.debug(
                    "Falling back to local history for remote tab %s after remote capture failed: %s",
                    self.tab_id,
                    e,
                )
        return await self._capture_local_history(lines)

    async def _capture_local_cursor_position(self) -> Optional[CursorPosition]:
        """Capture tmux pane cursor position as zero-based x/y coordinates."""
        if not await _tmux_session_exists_async(self.tmux_session):
            return None

        proc = await asyncio.create_subprocess_exec(
            *tmux_command(
                "display-message",
                "-p",
                "-t",
                self.tmux_session,
                "#{cursor_x} #{cursor_y}",
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error or f"tmux display-message failed with code {proc.returncode}")

        parts = stdout.decode("utf-8", errors="ignore").strip().split()
        if len(parts) != 2:
            return None

        try:
            return {"cursor_x": int(parts[0]), "cursor_y": int(parts[1])}
        except ValueError:
            return None

    async def _capture_remote_cursor_position(self) -> Optional[CursorPosition]:
        script = "; ".join(
            [
                self._remote_path_bootstrap(),
                "command -v tmux >/dev/null 2>&1",
                (
                    "tmux display-message -p "
                    f"-t {shlex.quote(self.tmux_session)} "
                    f"{shlex.quote('#{cursor_x} #{cursor_y}')}"
                ),
            ]
        )
        stdout = await self._run_remote_capture_command(self._remote_shell_command(script))
        parts = stdout.strip().split()
        if len(parts) != 2:
            return None
        try:
            return {"cursor_x": int(parts[0]), "cursor_y": int(parts[1])}
        except ValueError:
            return None

    async def capture_cursor_position(
        self,
        prefer_remote: bool = False,
    ) -> Optional[CursorPosition]:
        if prefer_remote and self.target == ExecutionTarget.REMOTE:
            try:
                remote_cursor = await self._capture_remote_cursor_position()
                if remote_cursor:
                    return remote_cursor
            except Exception as e:
                logger.debug(
                    "Falling back to local cursor for remote tab %s after remote capture failed: %s",
                    self.tab_id,
                    e,
                )
        return await self._capture_local_cursor_position()

    async def capture_foreground_command(self) -> Optional[str]:
        """Capture the foreground command currently running in the tmux pane."""
        if not await _tmux_session_exists_async(self.tmux_session):
            return None

        proc = await asyncio.create_subprocess_exec(
            *tmux_command(
                "display-message",
                "-p",
                "-t",
                self.tmux_session,
                "#{pane_current_command}",
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        command = stdout.decode("utf-8", errors="ignore").strip()
        return command or None

    async def stop(self, kill_tmux: bool = False) -> None:
        """Stop the ttyd process. By default, KEEP tmux session alive for persistence.

        Args:
            kill_tmux: If True, also kill the tmux session. Only use this when
                      deleting a tab or rolling back an unpersisted tab startup.
        """
        if self.process and self.process.returncode is None:
            logger.info(f"Stopping ttyd for tab {self.tab_id} (PID {self.process.pid})")
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"ttyd for tab {self.tab_id} didn't terminate, killing it")
                self.process.kill()
                await self.process.wait()

        if kill_tmux:
            logger.warning(f"Killing tmux session {self.tmux_session}")
            await _tmux_kill_session(self.tmux_session)
        else:
            logger.info(f"Preserving tmux session {self.tmux_session} for future reattachment")

        self.is_active = False

    def to_dict(self) -> dict:
        return {
            "id": self.tab_id,
            "name": self.name,
            "shell": self.shell,
            "cwd": self.cwd,
            "solo_mode": self.solo_mode,
            "agent_type": (
                self.agent_type.value if isinstance(self.agent_type, AgentType) else self.agent_type
            ),
            "target": (
                self.target.value if isinstance(self.target, ExecutionTarget) else self.target
            ),
            "remote_profile_id": self.remote_profile_id,
            "remote_cwd": self.remote_cwd,
            "remote_reconnect": self.remote_reconnect,
            "env": self.env,
            "remote_forward_port": self.remote_forward_port,
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "workspace_role": (
                self.workspace_role.value
                if isinstance(self.workspace_role, WorkspaceSessionRole)
                else self.workspace_role
            ),
            "port": self.port,
            "created_at": self.created_at.isoformat(),
            "agent_session_id": self.agent_session_id,
            "resume_quarantined": bool(self.resume_quarantined),
            "shell_explicitly_provided": self._shell_explicitly_provided,
            "cursor_transport": self.cursor_transport,
            "cursor_data_dir": self.cursor_data_dir,
            "cursor_cli_version": self.cursor_cli_version,
            "cursor_transcript_path": self.cursor_transcript_path,
            "cursor_transcript_schema": self.cursor_transcript_schema,
        }

    def to_schema(self) -> TerminalTab:
        return TerminalTab(
            id=self.tab_id,
            name=self.name,
            shell=self.shell,
            cwd=self.cwd,
            solo_mode=self.solo_mode,
            agent_type=self.agent_type,
            target=self.target,
            remote_profile_id=self.remote_profile_id,
            remote_cwd=self.remote_cwd,
            remote_reconnect=self.remote_reconnect,
            env=self.env,
            port=self.port,
            created_at=self.created_at,
            is_active=self.is_active,
            workspace_id=self.workspace_id,
            workspace_name=self.workspace_name,
            workspace_role=self.workspace_role,
            agent_session_id=self.agent_session_id,
            cursor_transport=self.cursor_transport,
            cursor_data_dir=self.cursor_data_dir,
            cursor_cli_version=self.cursor_cli_version,
            cursor_transcript_path=self.cursor_transcript_path,
            cursor_transcript_schema=self.cursor_transcript_schema,
        )


class TTYDManager:
    """Manages multiple ttyd processes with tmux-backed persistence."""

    def __init__(self) -> None:
        self.processes: Dict[str, TTYDProcess] = {}
        self._next_port = settings.ttyd_base_port
        self._tab_order: List[str] = []
        self._status_snapshots: Dict[str, _AgentStatusSnapshot] = {}
        self._status_cache: Dict[str, TerminalAgentStatus] = {}
        self._start_locks: Dict[str, asyncio.Lock] = {}
        logger.info("=" * 60)
        logger.info("Initializing TTYDManager - tmux session persistence enabled")
        logger.info("=" * 60)
        # Do not touch tmux during module import. Backend ownership is acquired
        # in FastAPI lifespan before start_all_tabs performs the tmux probe;
        # probing here lets a duplicate backend hang or mutate shared runtime
        # state before the single-instance guard can reject it.
        self._load_state()
        self._load_order()
        # Ensure all loaded tabs are in the order list
        self._sync_order_with_processes()

    def _load_state(self) -> None:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    max_port = self._next_port
                    for tab_data in data:
                        agent_type_str = tab_data.get("agent_type", "claude")
                        agent_type = (
                            AgentType(agent_type_str)
                            if agent_type_str in [e.value for e in AgentType]
                            else AgentType.CLAUDE
                        )
                        target_str = tab_data.get("target", "local")
                        target = (
                            ExecutionTarget(target_str)
                            if target_str in [e.value for e in ExecutionTarget]
                            else ExecutionTarget.LOCAL
                        )
                        workspace_role_str = tab_data.get("workspace_role")
                        workspace_role = (
                            WorkspaceSessionRole(workspace_role_str)
                            if workspace_role_str in [e.value for e in WorkspaceSessionRole]
                            else None
                        )
                        process = TTYDProcess(
                            tab_id=tab_data["id"],
                            port=tab_data["port"],
                            name=tab_data["name"],
                            shell=tab_data.get("shell"),
                            cwd=tab_data.get("cwd"),
                            created_at=datetime.fromisoformat(tab_data["created_at"]),
                            solo_mode=tab_data.get("solo_mode", False),
                            agent_type=agent_type,
                            target=target,
                            remote_profile_id=tab_data.get("remote_profile_id"),
                            remote_cwd=tab_data.get("remote_cwd"),
                            remote_reconnect=tab_data.get("remote_reconnect", True),
                            remote_forward_port=tab_data.get("remote_forward_port"),
                            env=tab_data.get("env", {}),
                            workspace_id=tab_data.get("workspace_id"),
                            workspace_name=tab_data.get("workspace_name"),
                            workspace_role=workspace_role,
                            agent_session_id=tab_data.get("agent_session_id"),
                            from_persisted_state=True,
                            resume_quarantined=tab_data.get("resume_quarantined", False),
                            shell_explicitly_provided=tab_data.get("shell_explicitly_provided"),
                            cursor_transport=tab_data.get("cursor_transport", "terminal"),
                            cursor_data_dir=tab_data.get("cursor_data_dir"),
                            cursor_cli_version=tab_data.get("cursor_cli_version"),
                            cursor_transcript_path=tab_data.get("cursor_transcript_path"),
                            cursor_transcript_schema=tab_data.get("cursor_transcript_schema"),
                        )
                        self.processes[process.tab_id] = process
                        if process.port > max_port:
                            max_port = process.port
                    self._next_port = max_port + 1
                logger.info(f"Loaded {len(self.processes)} tabs from {STATE_FILE}")
                for tab_id, process in self.processes.items():
                    logger.info(
                        f"  - Tab: {process.name} (tmux: {process.tmux_session}, agent: {process.agent_type.value})"
                    )
            except Exception as e:
                logger.error(f"Failed to load state: {e}")
        else:
            logger.info(f"No state file at {STATE_FILE}, starting fresh")

    def _save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump([p.to_dict() for p in self.processes.values()], f)
            logger.debug(f"Saved {len(self.processes)} tabs to {STATE_FILE}")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _load_order(self) -> None:
        """Load tab order from file."""
        if ORDER_FILE.exists():
            try:
                with open(ORDER_FILE, "r") as f:
                    self._tab_order = json.load(f)
                logger.info(f"Loaded tab order: {self._tab_order}")
            except Exception as e:
                logger.error(f"Failed to load tab order: {e}")
                self._tab_order = []
        else:
            logger.info(f"Order file {ORDER_FILE} does not exist, starting with empty order")

    def _save_order(self) -> None:
        """Save tab order to file."""
        try:
            ORDER_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(ORDER_FILE, "w") as f:
                json.dump(self._tab_order, f)
            logger.info(f"Saved tab order to {ORDER_FILE}: {self._tab_order}")
        except Exception as e:
            logger.error(f"Failed to save tab order: {e}")

    def set_tab_order(self, tab_ids: List[str]) -> None:
        """Set the order of tabs."""
        logger.info(f"set_tab_order called with: {tab_ids}")
        logger.info(f"Current processes keys: {list(self.processes.keys())}")
        # Validate that all tab IDs exist
        valid_ids = [tid for tid in tab_ids if tid in self.processes]
        # Add any missing tabs at the end
        for tid in self.processes:
            if tid not in valid_ids:
                valid_ids.append(tid)
        self._tab_order = valid_ids
        logger.info(f"Final tab order set to: {self._tab_order}")
        self._save_order()

    def _ensure_tab_in_order(self, tab_id: str) -> None:
        """Ensure a tab is in the order list."""
        if tab_id not in self._tab_order:
            self._tab_order.append(tab_id)
            self._save_order()

    def set_tab_workspace_metadata(
        self,
        tab_id: str,
        workspace_id: Optional[str],
        workspace_name: Optional[str],
        workspace_role: Optional[WorkspaceSessionRole],
    ) -> bool:
        """Attach workspace ownership metadata to an existing tab."""
        process = self.processes.get(tab_id)
        if not process:
            return False

        if (
            process.workspace_id == workspace_id
            and process.workspace_name == workspace_name
            and process.workspace_role == workspace_role
        ):
            return True

        process.workspace_id = workspace_id
        process.workspace_name = workspace_name
        process.workspace_role = workspace_role
        self._save_state()
        return True

    def rename_tab(self, tab_id: str, name: str) -> bool:
        """Rename a tab without restarting its terminal process."""
        process = self.processes.get(tab_id)
        if not process:
            return False

        if process.name == name:
            return True

        process.name = name
        self._save_state()
        return True

    def _sync_order_with_processes(self) -> None:
        """Sync order list with current processes - add any missing tabs."""
        # Add any tabs that are in processes but not in order
        order_changed = False
        for tab_id in self.processes:
            if tab_id not in self._tab_order:
                self._tab_order.append(tab_id)
                order_changed = True
        # Remove any tab IDs that are in order but not in processes
        original_len = len(self._tab_order)
        self._tab_order = [tid for tid in self._tab_order if tid in self.processes]
        if len(self._tab_order) != original_len:
            order_changed = True
        # If order is still empty, initialize with all process IDs
        if not self._tab_order and self.processes:
            self._tab_order = list(self.processes.keys())
            order_changed = True
        if order_changed:
            logger.info(f"Synced tab order: {self._tab_order}")
            self._save_order()

    def _get_next_port(self) -> int:
        while self._next_port <= _MAX_TCP_PORT:
            port = self._next_port
            self._next_port += 1
            if _is_local_port_available(port):
                return port
            logger.warning("Skipping occupied ttyd port %s", port)
        raise RuntimeError("No available ttyd ports remain")

    @staticmethod
    async def _finish_startup_cleanup(process: TTYDProcess, *, kill_tmux: bool) -> None:
        """Finish rollback even if shutdown delivers repeated cancellation."""
        cleanup_task = asyncio.create_task(process.stop(kill_tmux=kill_tmux))
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                continue
        await cleanup_task

    async def create_tab(
        self,
        name: str,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: bool = False,
        agent_type: AgentType = AgentType.CLAUDE,
        target: ExecutionTarget = ExecutionTarget.LOCAL,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: bool = True,
        remote_forward_port: Optional[int] = None,
        workspace_id: Optional[str] = None,
        workspace_name: Optional[str] = None,
        workspace_role: Optional[WorkspaceSessionRole] = None,
        env: Optional[Dict[str, str]] = None,
        agent_session_id: Optional[str] = None,
        cursor_transport: str = "terminal",
        cursor_data_dir: Optional[str] = None,
        cursor_cli_version: Optional[str] = None,
        cursor_transcript_path: Optional[str] = None,
        cursor_transcript_schema: Optional[str] = None,
    ) -> TerminalTab:
        logger.info(
            f"create_tab called with: name={name}, solo_mode={solo_mode}, shell={shell}, cwd={cwd}, agent_type={agent_type}, target={target}, remote_profile_id={remote_profile_id}, remote_forward_port={remote_forward_port}, workspace_id={workspace_id}, workspace_role={workspace_role}, agent_session_id={agent_session_id}"
        )
        tab_id = str(uuid.uuid4())
        port = self._get_next_port()

        process = TTYDProcess(
            tab_id,
            port,
            name,
            shell,
            cwd,
            solo_mode=solo_mode,
            agent_type=agent_type,
            target=target,
            remote_profile_id=remote_profile_id,
            remote_cwd=remote_cwd,
            remote_reconnect=remote_reconnect,
            remote_forward_port=remote_forward_port,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            workspace_role=workspace_role,
            env=env,
            agent_session_id=agent_session_id,
            cursor_transport=cursor_transport,
            cursor_data_dir=cursor_data_dir,
            cursor_cli_version=cursor_cli_version,
            cursor_transcript_path=cursor_transcript_path,
            cursor_transcript_schema=cursor_transcript_schema,
        )
        logger.info(
            f"Created TTYDProcess with solo_mode={process.solo_mode}, agent_type={process.agent_type}"
        )
        tmux_created = False
        process_stopped_without_tmux = False
        try:
            # Stamp launch clocks before starting (ttyd lazy-executes: start()
            # awaits ~1 s during which the agent is already running in tmux).
            # For hot reattaches this is harmless (ensure_tmux_session is a
            # no-op when the session already exists); for cold creates we need
            # the anchor set before start() spawns the new tmux session.
            # Keep the ownership result reliable if cancellation arrives while
            # tmux is being created. Rollback must never kill a session that
            # predated this create request.
            ensure_task = asyncio.create_task(process.ensure_tmux_session())
            ensure_cancel: Optional[asyncio.CancelledError] = None
            while not ensure_task.done():
                try:
                    tmux_created = await asyncio.shield(ensure_task)
                except asyncio.CancelledError as exc:
                    ensure_cancel = ensure_cancel or exc
            if ensure_task.done():
                tmux_created = ensure_task.result()
            if ensure_cancel is not None:
                raise ensure_cancel

            for attempt in range(1, _MAX_TTYD_BIND_ATTEMPTS + 1):
                try:
                    await process.start()
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A port can be claimed after allocation but before ttyd
                    # binds. Stop the failed attempt, then retry only when the
                    # candidate is demonstrably occupied by another process.
                    await process.stop(kill_tmux=False)
                    process_stopped_without_tmux = True
                    if attempt >= _MAX_TTYD_BIND_ATTEMPTS or _is_local_port_available(process.port):
                        raise
                    old_port = process.port
                    process.port = self._get_next_port()
                    logger.warning(
                        "Retrying ttyd startup for tab %s after port %s was claimed; using %s",
                        tab_id,
                        old_port,
                        process.port,
                    )
        except asyncio.CancelledError:
            # A reload can cancel creation after ttyd has bound its port but
            # before the tab is persisted. Stop that untracked process so the
            # next create does not collide with an orphan listener.
            try:
                await self._finish_startup_cleanup(process, kill_tmux=tmux_created)
            except Exception:
                logger.exception("Failed to clean up cancelled tab %s", tab_id)
            raise
        except Exception:
            try:
                if tmux_created or not process_stopped_without_tmux:
                    await self._finish_startup_cleanup(process, kill_tmux=tmux_created)
            except Exception:
                logger.exception("Failed to clean up unsuccessful tab %s", tab_id)
            raise

        self.processes[tab_id] = process
        self._ensure_tab_in_order(tab_id)
        self._save_state()

        # Schedule post-start codex session-id discovery so newly created
        # codex tabs get a pinned UUID before the next restart.
        self._schedule_codex_discovery(process)

        return process.to_schema()

    async def ensure_tab_tmux_session(self, tab_id: str) -> bool:
        process = self.processes.get(tab_id)
        if not process:
            raise KeyError(tab_id)
        created = await process.ensure_tmux_session()
        if created:
            # If ensure_tmux_session launched codex (fresh session), schedule
            # discovery to pin its real session UUID. process.start() will
            # later reattach rather than re-launch codex, so this is our only
            # hook for pinning this launch.
            self._schedule_codex_discovery(process)
        return created

    async def duplicate_tab(self, tab_id: str) -> Optional[TerminalTab]:
        """Create a new tab by copying the source tab's launch configuration."""
        source = self.processes.get(tab_id)
        if not source:
            return None

        return await self.create_tab(
            name=f"{source.name} (copy)",
            shell=source.shell,
            cwd=source.cwd,
            solo_mode=source.solo_mode,
            agent_type=source.agent_type,
            target=source.target,
            remote_profile_id=source.remote_profile_id,
            remote_cwd=source.remote_cwd,
            remote_reconnect=source.remote_reconnect,
            remote_forward_port=source.remote_forward_port,
            env=source.env,
        )

    async def delete_tab(self, tab_id: str) -> bool:
        """Delete a tab and explicitly kill its tmux session (user requested deletion)."""
        if tab_id not in self.processes:
            return False

        process = self.processes[tab_id]
        logger.warning(
            f"User requested deletion of tab {tab_id}, killing tmux session {process.tmux_session}"
        )
        # Keep the process registered until teardown is proven.  If tmux
        # refuses to die, the workspace orphan reconciler can still see this
        # tab and retry instead of losing its final durable owner reference.
        await process.stop(kill_tmux=True)
        self.processes.pop(tab_id, None)
        if tab_id in self._tab_order:
            self._tab_order.remove(tab_id)
            self._save_order()
        self._save_state()
        return True

    def get_tab(self, tab_id: str) -> Optional[TerminalTab]:
        if tab_id not in self.processes:
            return None
        return self.processes[tab_id].to_schema()

    async def ensure_tab_running(self, tab_id: str) -> Optional[TerminalTab]:
        """Ensure the tab has a live ttyd listener while preserving tmux state."""
        process = self.processes.get(tab_id)
        if not process:
            return None

        lock = self._start_locks.setdefault(tab_id, asyncio.Lock())
        async with lock:
            if _is_local_port_listening(process.port):
                process.is_active = True
                return process.to_schema()

            if process.process and process.process.returncode is None:
                logger.warning(
                    "Tab %s has a live ttyd process object but port %s is not listening; restarting ttyd",
                    tab_id,
                    process.port,
                )
                await process.stop(kill_tmux=False)

            logger.info(
                "Starting missing ttyd listener for tab %s on port %s",
                tab_id,
                process.port,
            )
            try:
                await self._start_missing_tab_identity_safe(process)
            except Exception:
                # During uvicorn --reload an old backend may still own the
                # port briefly. If the listener exists now, let the proxy use
                # it; a later request will restart ttyd if that old listener is
                # cleaned up.
                if _is_local_port_listening(process.port):
                    logger.warning(
                        "Tab %s start failed but port %s is already listening; treating it as available",
                        tab_id,
                        process.port,
                    )
                    process.is_active = True
                    return process.to_schema()
                process.is_active = False
                raise

            return process.to_schema()

    async def _start_missing_tab_identity_safe(self, process: TTYDProcess) -> None:
        """Start an explicitly opened stopped tab without losing agent identity.

        A hot backend restart intentionally leaves persisted tabs with no live
        tmux session stopped.  Opening one is therefore a real cold recovery,
        not merely a missing ttyd listener.  Local Codex recovery must reuse
        the same attribution and reconciliation fence as bulk cold startup;
        Cursor/Claude may update their pinned id while building the recovery
        command, so their resulting state is persisted before returning.
        """
        session_exists = await _tmux_session_exists_async(process.tmux_session)
        is_local_codex = (
            not session_exists
            and process.target == ExecutionTarget.LOCAL
            and process.agent_type == AgentType.CODEX
            and not process._shell_explicitly_provided
        )
        if not is_local_codex:
            try:
                await process.start()
            finally:
                # Cursor rotates an unverifiable persisted id while building
                # its recovery command; Claude can defensively allocate a
                # missing legacy id. Persist even if ttyd startup then fails so
                # the next attempt targets the same conversation identity.
                self._save_state()
            return

        # A prior launch in this backend process may have left attribution
        # scratch fields populated even though its tmux was later killed.
        process._launch_wall = None
        process._launch_mono = None
        process._is_new_pin = None
        process._pre_scan = None
        process._pending_quarantine_reason = None

        async with GLOBAL_CODEX_LAUNCH_LOCK:
            pre_global_scan = _codex_scan_sessions()
            try:
                outcome, _ = await self._launch_one_cold_codex_locked(process)
                if outcome == "PendingQ":
                    raise RuntimeError(
                        "unresolved Codex ownership signal for explicitly opened "
                        f"tab {process.tab_id}: {process._pending_quarantine_reason}"
                    )
                await self._reconcile_codex_phase_r([process], pre_global_scan)
                if process.resume_quarantined:
                    raise RuntimeError(
                        f"Codex identity reconciliation quarantined tab {process.tab_id}"
                    )
            except asyncio.CancelledError:
                try:
                    await self._finish_explicit_codex_recovery_rollback(
                        process,
                        "explicit cold recovery cancelled before identity commit",
                    )
                except Exception as cleanup_error:
                    raise RuntimeError(
                        "Cancelled Codex explicit recovery could not prove process teardown "
                        f"for tab {process.tab_id}"
                    ) from cleanup_error
                raise
            except Exception as error:
                try:
                    await self._finish_explicit_codex_recovery_rollback(
                        process,
                        f"explicit cold recovery rollback: {error!r}",
                    )
                except Exception as cleanup_error:
                    raise RuntimeError(
                        "Codex explicit recovery could not prove process teardown "
                        f"for tab {process.tab_id}"
                    ) from cleanup_error
                raise

            # Commit the attributed SID before ttyd attaches. If ttyd binding
            # fails, the owned tmux and durable identity remain safe to retry.
            self._save_state()

        await process.start()

    async def _finish_explicit_codex_recovery_rollback(
        self,
        process: TTYDProcess,
        reason: str,
    ) -> None:
        """Shield Codex teardown/state commit from request or reload cancellation."""
        cleanup_task = asyncio.create_task(self._quarantine_codex_tab(process, reason))
        try:
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    # Repeated shutdown cancellation must not strand an
                    # unattributed Codex writer. The caller re-raises its original
                    # cancellation only after this task proves teardown.
                    continue
            await cleanup_task
        finally:
            # _quarantine_codex_tab deliberately retains its cleanup-failed
            # evidence if tmux teardown cannot be proven. Persist that evidence
            # even when the cleanup task itself raises.
            self._save_state()

    def list_tabs(self) -> list[TerminalTab]:
        # Return tabs in saved order
        logger.info(
            f"list_tabs called, _tab_order={self._tab_order}, processes={list(self.processes.keys())}"
        )
        ordered_tabs: list[TerminalTab] = []
        # First add tabs in the saved order
        for tab_id in self._tab_order:
            if tab_id in self.processes:
                ordered_tabs.append(self.processes[tab_id].to_schema())
        # Then add any tabs not in the order list
        for process in self.processes.values():
            if process.tab_id not in self._tab_order:
                ordered_tabs.append(process.to_schema())
        logger.info(f"list_tabs returning: {[t.name for t in ordered_tabs]}")
        return ordered_tabs

    async def get_tab_history(self, tab_id: str, lines: int = 100000) -> Optional[str]:
        """Get tmux terminal history for a tab (scrollback + visible screen)."""
        process = self.processes.get(tab_id)
        if not process:
            return None
        return await process.capture_history(lines, prefer_remote=True)

    async def get_tab_cursor_position(self, tab_id: str) -> Optional[CursorPosition]:
        """Get tmux cursor position for a tab."""
        process = self.processes.get(tab_id)
        if not process:
            return None
        return await process.capture_cursor_position(prefer_remote=True)

    def _classify_agent_status(
        self,
        process: TTYDProcess,
        output: str,
        output_hash: str,
        foreground_command: Optional[str],
    ) -> tuple[AgentRuntimeStatus, str, Optional[str], Optional[datetime]]:
        now = datetime.now()
        snapshot = self._status_snapshots.get(process.tab_id)
        last_hash = snapshot.get("hash") if snapshot else None
        last_changed_at = snapshot.get("last_changed_at") if snapshot else None
        frame_first_seen_at = snapshot.get("frame_first_seen_at") if snapshot else None

        if snapshot is None:
            frame_first_seen_at = now
            self._status_snapshots[process.tab_id] = {
                "hash": output_hash,
                "last_changed_at": None,
                "frame_first_seen_at": frame_first_seen_at,
            }
        elif last_hash != output_hash:
            last_changed_at = now
            frame_first_seen_at = now
            self._status_snapshots[process.tab_id] = {
                "hash": output_hash,
                "last_changed_at": last_changed_at,
                "frame_first_seen_at": frame_first_seen_at,
            }
        # else: identical frame — keep the existing frame_first_seen_at so we can
        # measure how long the screen has been frozen.

        # Aliveness is verified by the caller (``get_tab_agent_status``) before
        # classification, so we avoid a redundant per-tab ``tmux has-session``
        # subprocess here — that check previously blocked the event loop on
        # every board refresh.

        def working_or_stale() -> tuple[AgentRuntimeStatus, str, Optional[str], Optional[datetime]]:
            # A live agent repaints its spinner/elapsed counter every second, so
            # the captured frame keeps changing. If working markers are showing
            # but the frame has been frozen past the staleness window, the agent
            # has stopped behind a lingering "working" frame — flag it for
            # attention instead of reporting it as working forever.
            if (
                frame_first_seen_at is not None
                and (now - frame_first_seen_at).total_seconds() >= _WORKING_FRAME_STALE_SECONDS
            ):
                return (
                    AgentRuntimeStatus.ATTENTION,
                    "Agent may be stuck",
                    "working indicator has not changed; agent appears stopped",
                    last_changed_at,
                )
            return (
                AgentRuntimeStatus.WORKING,
                "Working",
                "agent is processing",
                last_changed_at,
            )

        # Only inspect the bottom of the visible screen — the current prompt
        # area. Historical scrollback is ignored so that words like
        # "Reading"/"editing" from past activity can't drive classification.
        non_empty = [line.strip() for line in output.splitlines() if line.strip()]
        tail_lines = non_empty[-5:]
        status_tail_lines = non_empty[-10:]
        tail = "\n".join(tail_lines).lower()
        status_tail = "\n".join(status_tail_lines).lower()
        last_line = tail_lines[-1] if tail_lines else ""

        for pattern in _ATTENTION_TAIL_PATTERNS:
            if pattern in tail:
                return (
                    AgentRuntimeStatus.ATTENTION,
                    "Agent waiting for input",
                    "needs your response",
                    last_changed_at,
                )

        # A bare shell prompt on the last line means the agent has returned to
        # an idle prompt. This takes priority over working markers higher in
        # the scrollback (e.g. a lingering "esc to interrupt" from the
        # just-finished turn), so an agent that has finished is promptly
        # classified idle instead of lingering in "working".
        if _BARE_SHELL_PROMPT_RE.search(last_line):
            return (
                AgentRuntimeStatus.IDLE,
                "Idle",
                "shell prompt visible",
                last_changed_at,
            )

        # Claude Code / Codex idle hints ("? for shortcuts", "/ for commands")
        # on the bottom lines also mean the agent is idle and waiting for
        # input. Like the shell prompt, these take priority over working
        # markers in the scrollback above.
        for hint in _IDLE_TAIL_HINTS:
            if hint in tail:
                return (
                    AgentRuntimeStatus.IDLE,
                    "Idle",
                    "agent prompt visible",
                    last_changed_at,
                )

        # Codex (GPT-5.5) renders its working indicator ("⠞ Working  4.03k
        # tokens" / "• Working (3s • esc to interrupt)") ABOVE a tall persistent
        # bottom chrome — the ›/❯ composer, a growing "Queued follow-up inputs"
        # panel, and a model footer — so the indicator falls outside the
        # bottom-10 window scanned below. Detect it against the wider frame
        # using the codex-specific marker set. Runs after the ATTENTION and
        # idle-prompt checks so a codex selection prompt or idle hint still
        # wins, and through working_or_stale() so the frozen-frame guard still
        # applies.
        if process.agent_type == AgentType.CODEX and codex_output_is_working(output):
            return working_or_stale()

        for pattern in _WORKING_TAIL_PATTERNS:
            if pattern in status_tail:
                return working_or_stale()

        if _CLAUDE_WORKING_STATUS_RE.search("\n".join(status_tail_lines)):
            return working_or_stale()

        if _CURSOR_WORKING_STATUS_RE.search("\n".join(status_tail_lines)):
            return working_or_stale()

        if foreground_command and foreground_command in {"claude", "codex", "agent"}:
            return (
                AgentRuntimeStatus.IDLE,
                "Idle",
                f"{foreground_command} is waiting",
                last_changed_at,
            )

        return (
            AgentRuntimeStatus.IDLE,
            "Idle",
            "no activity indicators",
            last_changed_at,
        )

    async def get_tab_agent_status(
        self,
        tab_id: str,
        use_cache: bool = True,
        live_sessions: Optional[set[str]] = None,
    ) -> Optional[TerminalAgentStatus]:
        """Get a best-effort terminal agent status for one tab.

        When ``live_sessions`` is provided (a snapshot of live tmux session
        names from a single ``tmux list-sessions`` call), tmux existence is
        checked against that set instead of spawning a per-tab subprocess.
        """
        process = self.processes.get(tab_id)
        if not process:
            return None

        sampled_at = datetime.now()
        cached = self._status_cache.get(tab_id)
        if (
            use_cache
            and cached
            and (sampled_at - cached.sampled_at).total_seconds() < _STATUS_CACHE_TTL_SECONDS
        ):
            return cached

        if live_sessions is not None:
            session_alive = process.tmux_session in live_sessions
        else:
            session_alive = await _tmux_session_exists_async(process.tmux_session)
        if not session_alive:
            status = TerminalAgentStatus(
                tab_id=process.tab_id,
                tab_name=process.name,
                agent_type=process.agent_type,
                status=AgentRuntimeStatus.OFFLINE,
                status_text="Offline",
                detail="tmux session is not available",
                tmux_session=process.tmux_session,
                last_changed_at=None,
                sampled_at=sampled_at,
            )
            self._status_cache[tab_id] = status
            return status

        try:
            raw_output = await process.capture_history(lines=120)
        except Exception as e:
            logger.debug(f"Unable to capture status output for tab {tab_id}: {e}")
            raw_output = ""

        foreground_command = await process.capture_foreground_command()
        output = _ANSI_ESCAPE_RE.sub("", raw_output)
        output_hash = hashlib.sha256(output.encode("utf-8", errors="ignore")).hexdigest()
        runtime_status, status_text, detail, last_changed_at = self._classify_agent_status(
            process,
            output,
            output_hash,
            foreground_command,
        )

        status = TerminalAgentStatus(
            tab_id=process.tab_id,
            tab_name=process.name,
            agent_type=process.agent_type,
            status=runtime_status,
            status_text=status_text,
            detail=detail,
            tmux_session=process.tmux_session,
            last_changed_at=last_changed_at,
            sampled_at=sampled_at,
        )
        self._status_cache[tab_id] = status
        return status

    async def list_tab_agent_statuses(
        self,
        tab_ids: Optional[Iterable[str]] = None,
    ) -> list[TerminalAgentStatus]:
        """List best-effort terminal agent statuses in tab order."""
        if tab_ids is None:
            ordered_ids = [tab_id for tab_id in self._tab_order if tab_id in self.processes]
            ordered_ids.extend(tab_id for tab_id in self.processes if tab_id not in ordered_ids)
        else:
            seen: set[str] = set()
            ordered_ids = []
            for tab_id in tab_ids:
                if tab_id in self.processes and tab_id not in seen:
                    ordered_ids.append(tab_id)
                    seen.add(tab_id)

        live_sessions = await _tmux_list_sessions()
        results = await asyncio.gather(
            *(
                self.get_tab_agent_status(tab_id, live_sessions=live_sessions)
                for tab_id in ordered_ids
            )
        )
        return [status for status in results if status]

    async def update_tab(
        self,
        tab_id: str,
        name: Optional[str] = None,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        solo_mode: Optional[bool] = None,
        agent_type: Optional[AgentType] = None,
        target: Optional[ExecutionTarget] = None,
        remote_profile_id: Optional[str] = None,
        remote_cwd: Optional[str] = None,
        remote_reconnect: Optional[bool] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> Optional[TerminalTab]:
        """Update tab settings. Note: Changing shell/cwd/solo_mode/agent_type requires restarting
        the ttyd process, but the tmux session will be PRESERVED.
        """
        if tab_id not in self.processes:
            return None

        process = self.processes[tab_id]
        needs_restart = False

        if name:
            process.name = name
        if shell is not None:
            process.shell = shell
            needs_restart = True
        if cwd is not None:
            process.cwd = cwd
            needs_restart = True
        if solo_mode is not None:
            process.solo_mode = solo_mode
            needs_restart = True
        if agent_type is not None:
            if process.agent_type != agent_type:
                # Reset agent_session_id when switching agent types: ids are
                # agent-specific (claude uuids vs codex ULID-uuid hybrids vs
                # cursor opaque hashes) and a stale id from the wrong agent
                # will corrupt resume. The new agent's launch path will
                # generate/pin a fresh id.
                process.agent_session_id = None
                if agent_type in (AgentType.CLAUDE, AgentType.CODEX):
                    process.agent_session_id = str(uuid.uuid4())
                process.cursor_transport = "terminal"
                process.cursor_data_dir = None
                process.cursor_cli_version = None
                process.cursor_transcript_path = None
                process.cursor_transcript_schema = None
                logger.info(f"reset agent_session_id for tab {tab_id} due to agent_type change")
            process.agent_type = agent_type
            needs_restart = True
        if target is not None:
            process.target = target
            needs_restart = True
        if remote_profile_id is not None:
            process.remote_profile_id = remote_profile_id
            needs_restart = True
        if remote_cwd is not None:
            process.remote_cwd = remote_cwd
            needs_restart = True
        if remote_reconnect is not None:
            process.remote_reconnect = remote_reconnect
            needs_restart = True
        if env is not None:
            process.env = TTYDProcess._clean_env(env)
            process._prepare_agent_env()
            if process.agent_type != AgentType.CLAUDE:
                process._setup_tunnel_env()
            needs_restart = True

        if process.cursor_transport == "terminal_transcript" and not (
            process.target == ExecutionTarget.LOCAL
            and cursor_terminal_transcript_provenance_valid(
                cwd=process.cwd,
                session_id=process.agent_session_id,
                cli_version=process.cursor_cli_version,
                transcript_path=process.cursor_transcript_path,
                transcript_schema=process.cursor_transcript_schema,
                data_dir=process.cursor_data_dir,
                env=process.env,
            )
        ):
            process.cursor_transport = "terminal"
            process.cursor_data_dir = None
            process.cursor_cli_version = None
            process.cursor_transcript_path = None
            process.cursor_transcript_schema = None

        if needs_restart:
            logger.info(
                f"Updating tab {tab_id}, restarting ttyd but preserving tmux session {process.tmux_session}"
            )
            await process.stop(kill_tmux=False)
            await process.start()

        self._save_state()
        return process.to_schema()

    async def switch_env(
        self,
        tab_id: str,
        env: Dict[str, str],
        solo_mode: Optional[bool] = None,
    ) -> TerminalTab:
        """Hot-swap env/solo_mode on a live local Claude/Codex tab via tmux respawn-pane.

        Unlike ``update_tab`` this does NOT restart ttyd; it rewrites the launch
        files and respawns the foreground process in-place so the WebSocket
        connection, pane scrollback, and conversation (via resume flags) survive.
        connection, pane scrollback, and conversation (via --resume) survive.
        """
        process = self.processes.get(tab_id)
        if not process:
            raise KeyError(tab_id)

        await process.switch_env(env, solo_mode=solo_mode)

        self._save_state()
        # Invalidate any cached agent status so the next poll re-samples.
        self._status_cache.pop(tab_id, None)
        return process.to_schema()

    def _backfill_agent_session_ids(self) -> None:
        """Conservatively pin ``agent_session_id`` for pre-feature claude tabs.

        Tabs that were already running before session-id pinning shipped have
        ``agent_session_id is None`` and cannot resume their real conversation
        after a reboot. While their tmux sessions are still alive (this runs at
        startup, before any relaunch), we correlate each such tab's tmux
        ``session_created`` time with the start time of conversations Claude
        logged for the tab's cwd. We pin an id ONLY when the match is
        unambiguous — cross-wiring a tab to the wrong conversation is worse than
        a fresh start, so anything uncertain is logged and skipped.

        Defensive throughout: any per-tab error is logged and never aborts
        startup. Each jsonl is read only up to its first timestamped line.
        """
        backfilled = False
        for process in list(self.processes.values()):
            try:
                if not (
                    process.from_persisted_state
                    and process.agent_type == AgentType.CLAUDE
                    and process.target == ExecutionTarget.LOCAL
                    and process.agent_session_id is None
                    and process.cwd
                ):
                    continue

                label = f"tab {process.tab_id} ({process.name})"

                session_created = _tmux_session_created(process.tmux_session)
                if session_created is None:
                    logger.info(f"skipped session-id backfill for {label}: no live session")
                    continue

                project_dir = _claude_project_dir_for_cwd(process.cwd)
                if not project_dir.is_dir():
                    logger.info(f"skipped session-id backfill for {label}: dir missing")
                    continue

                candidates: List[Tuple[float, str, str]] = []
                for jsonl_path in glob.glob(os.path.join(str(project_dir), "*.jsonl")):
                    start_epoch = _jsonl_start_epoch(jsonl_path)
                    if start_epoch is None:
                        continue
                    session_id = os.path.basename(jsonl_path)[: -len(".jsonl")]
                    candidates.append((abs(start_epoch - session_created), session_id, jsonl_path))

                within_window = [c for c in candidates if c[0] <= _BACKFILL_MATCH_WINDOW_S]
                chosen = _pick_backfill_session(session_created, candidates)
                if chosen is None:
                    if not within_window:
                        reason = "best delta too large"
                    else:
                        reason = f"{len(within_window)} candidates within window, ambiguous"
                    logger.info(f"skipped session-id backfill for {label}: {reason}")
                    continue

                process.agent_session_id = chosen
                backfilled = True
                logger.info(f"backfilled agent_session_id for {label} -> {chosen}")
            except Exception as e:  # never abort startup
                logger.warning(f"error during session-id backfill for tab {process.tab_id}: {e}")

        if backfilled:
            self._save_state()

    def _backfill_codex_session_ids(self) -> None:
        """Conservatively pin ``agent_session_id`` for codex tabs.

        Analogous to ``_backfill_agent_session_ids`` for Claude but operating on
        codex's rollout store under ``~/.codex/sessions/``. Runs at startup
        while tmux sessions are still alive so we can anchor on tmux
        ``session_created`` time. Pinning is conservative: cross-wiring a tab
        to the wrong conversation is worse than leaving it unpinned (in which
        case cold recovery starts fresh and attributes the new rollout).

        Also handles the upgrade case: tabs created before codex pinning
        shipped have a freshly-generated placeholder uuid at construction
        time; this backfill overwrites it with the codex-discovered real id
        when an unambiguous match is found.
        """
        backfilled = False
        for process in list(self.processes.values()):
            try:
                if not (
                    process.from_persisted_state
                    and process.agent_type == AgentType.CODEX
                    and process.target == ExecutionTarget.LOCAL
                    and process.cwd
                ):
                    continue
                # Skip tabs that already have a real codex session id (exists
                # in session_index). Placeholder uuids generated at __init__
                # are not in the index and still need backfill.
                if process.agent_session_id and _codex_id_in_index(process.agent_session_id):
                    continue

                label = f"tab {process.tab_id} ({process.name})"

                session_created = _tmux_session_created(process.tmux_session)
                if session_created is None:
                    logger.info(f"skipped codex session-id backfill for {label}: no live session")
                    continue

                # Collect codex rollouts in this cwd and compute |start - session_created|.
                candidates: List[Tuple[float, str, str]] = []
                for start_epoch, sid, path in _codex_candidates_for_cwd(process.cwd):
                    candidates.append((abs(start_epoch - session_created), sid, path))

                within_window = [c for c in candidates if c[0] <= _BACKFILL_MATCH_WINDOW_S]
                chosen = _pick_backfill_session(session_created, candidates)
                if chosen is None:
                    if not within_window:
                        reason = "best delta too large"
                    else:
                        reason = f"{len(within_window)} candidates within window, ambiguous"
                    logger.info(f"skipped codex session-id backfill for {label}: {reason}")
                    continue

                process.agent_session_id = chosen
                backfilled = True
                logger.info(f"backfilled codex agent_session_id for {label} -> {chosen}")
            except Exception as e:  # never abort startup
                logger.warning(
                    f"error during codex session-id backfill for tab {process.tab_id}: {e}"
                )

        if backfilled:
            self._save_state()

    async def _quarantine_codex_tab(self, tab: TTYDProcess, reason: str) -> None:
        """Fail-closed quarantine for a codex tab whose attribution failed.

        Kills the partially-started tmux session (so no stale codex process
        keeps writing to an unattributed rollout), clears ``agent_session_id``,
        resets all per-launch runtime fields, and sets
        ``resume_quarantined=True`` so the next launch starts fresh. The tab
        is NOT removed from the manager — it stays in processes so the UI
        still shows it; when the user next opens it (or the next restart),
        it will get a fresh codex session with a correctly-attributed sid.
        """
        logger.warning(
            "codex quarantine for tab %s (sid=%s): %s",
            tab.tab_id,
            tab.agent_session_id,
            reason,
        )
        # Kill the tmux session to stop a possibly-misattributed codex from
        # continuing to write to a rollout we no longer trust.
        stop_error: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                await tab.stop(kill_tmux=True)
                stop_error = None
                break
            except Exception as e:
                stop_error = e
                logger.error(
                    "quarantine: stop(kill_tmux=True) attempt %d for %s failed: %s",
                    attempt,
                    tab.tab_id,
                    e,
                )
        if stop_error is not None:
            # Do not erase ownership metadata while an unattributed writer may
            # still be alive.  Persist quarantine with the last known SID and
            # surface the cleanup failure to the caller/reviewer.
            tab.resume_quarantined = True
            tab._pending_quarantine_reason = f"{reason}; cleanup failed: {stop_error!r}"
            raise RuntimeError(
                f"failed to stop owned tmux/ttyd for quarantined tab {tab.tab_id}"
            ) from stop_error
        # Reset all per-launch state; clear agent_session_id so we don't carry
        # forward a now-untrustworthy sid. Phase 0 of the next start_all_tabs
        # will assign a fresh uuid4 for codex just like it does for legacy
        # cursor tabs.
        tab.agent_session_id = None
        tab._is_new_pin = None
        tab._pre_scan = None
        tab._launch_wall = None
        tab._launch_mono = None
        tab._pending_quarantine_reason = None
        tab.resume_quarantined = True
        tab.is_active = False

    async def _codex_poll_signal(self, tab: TTYDProcess) -> Dict[str, str]:
        """Fence-poll after launching a codex tab until activity is observed
        plus ``_CODEX_FENCE_SILENCE_S`` of silence.

        Returns the ``_diff_scans`` result against ``tab._pre_scan`` at the
        moment of the final post-fence snapshot, filtered to same-cwd
        ``new``/``appended`` entries (so cross-cwd activity from unrelated
        codex processes cannot leak into the per-tab signal). Polls at
        ``_CODEX_POLL_INTERVAL_S`` up to ``_CODEX_POLL_MAX_ATTEMPTS`` times
        (~6 s wall budget per tab).
        """
        assert tab._pre_scan is not None
        # ``tab._pre_scan`` is the immutable attribution baseline.  A second,
        # rolling snapshot is required for the silence fence: comparing every
        # poll to the immutable baseline makes the same already-observed append
        # look "new" forever and forces every launch to consume the full timeout.
        previous_post = tab._pre_scan
        saw_signal = False
        last_activity_at: Optional[float] = None
        try:
            target_cwd = os.path.realpath(tab.cwd) if tab.cwd else ""
        except OSError:
            target_cwd = tab.cwd or ""

        def _filter_same_cwd(post: Dict[str, ScanEntry], diff: Dict[str, str]) -> Dict[str, str]:
            out: Dict[str, str] = {}
            for s, ct in diff.items():
                if ct not in ("new", "appended"):
                    continue
                entry = post.get(s)
                if not entry or not entry.cwd:
                    continue
                if not target_cwd:
                    continue
                try:
                    if os.path.realpath(entry.cwd) != target_cwd:
                        continue
                except OSError:
                    continue
                out[s] = ct
            return out

        for _ in range(_CODEX_POLL_MAX_ATTEMPTS):
            await asyncio.sleep(_CODEX_POLL_INTERVAL_S)
            post = _codex_scan_sessions()
            signal = _filter_same_cwd(post, _diff_scans(tab._pre_scan, post))
            incremental = _filter_same_cwd(post, _diff_scans(previous_post, post))
            now = time.monotonic()
            if signal:
                saw_signal = True
            if incremental:
                last_activity_at = now
            previous_post = post
            if (
                saw_signal
                and last_activity_at is not None
                and now - last_activity_at >= _CODEX_FENCE_SILENCE_S
            ):
                final_post = _codex_scan_sessions()
                final_incremental = _filter_same_cwd(
                    final_post, _diff_scans(previous_post, final_post)
                )
                if final_incremental:
                    # Activity raced with the settling poll; restart the silence
                    # window instead of accepting a moving target.
                    previous_post = final_post
                    last_activity_at = time.monotonic()
                    continue
                return _filter_same_cwd(final_post, _diff_scans(tab._pre_scan, final_post))
        # Timeout: return last same-cwd diff we saw (caller treats empty as SignalZero)
        if saw_signal:
            # Re-scan once more to get a stable final view
            final_post = _codex_scan_sessions()
            return _filter_same_cwd(final_post, _diff_scans(tab._pre_scan, final_post))
        return {}

    def _salvage_pending_codex_signal(self, tab: TTYDProcess) -> Optional[Tuple[str, str]]:
        """Resolve a failed signal before any sibling launch can begin.

        This runs while the global Codex launch lock is held and before the
        next same-cwd tab is started, so a candidate cannot belong to a later
        sibling.  Only exact append growth of the persisted target or exactly
        one new same-cwd rollout in this tab's timestamp window is salvageable.
        """
        assert tab._pre_scan is not None
        post = _codex_scan_sessions()
        try:
            target_cwd = os.path.realpath(tab.cwd) if tab.cwd else ""
        except OSError:
            target_cwd = tab.cwd or ""

        def _same_cwd(entry: ScanEntry) -> bool:
            if not target_cwd or not entry.cwd:
                return False
            try:
                return bool(os.path.realpath(entry.cwd) == target_cwd)
            except OSError:
                return bool(entry.cwd == target_cwd)

        target_sid = tab.agent_session_id
        if target_sid:
            pre_target = tab._pre_scan.get(target_sid)
            post_target = post.get(target_sid)
            if (
                pre_target is not None
                and post_target is not None
                and _same_cwd(post_target)
                and post_target.size > pre_target.size
                and post_target.mtime_ns > pre_target.mtime_ns
            ):
                tab._is_new_pin = False
                tab.resume_quarantined = False
                tab._pending_quarantine_reason = None
                return "SALVAGE_RESUME", target_sid

        new_candidates: List[str] = []
        for sid, entry in post.items():
            if sid in tab._pre_scan or not _same_cwd(entry):
                continue
            if entry.ts is None or tab._launch_wall is None:
                continue
            delta = entry.ts - tab._launch_wall
            if -_CODEX_NEW_PIN_TS_EARLY_S <= delta <= _CODEX_NEW_PIN_TS_LATE_S:
                new_candidates.append(sid)
        if len(new_candidates) != 1:
            return None

        new_sid = new_candidates[0]
        tab.agent_session_id = new_sid
        tab._is_new_pin = True
        tab.resume_quarantined = False
        tab._pending_quarantine_reason = None
        return "SALVAGE_FRESH", new_sid

    async def _launch_one_cold_codex_locked(self, tab: TTYDProcess) -> Tuple[str, Optional[str]]:
        """Launch ONE cold codex tab under the global lock, attribute its
        session via Phase 1C signal logic, and pin ``tab.agent_session_id``.

        Returns ``(outcome, pinned_sid)`` where outcome is one of:
          RESUME_OK     — verified id resumed (file appended or new pin matched)
          FRESH_PIN     — no/quarantined/unverified id; started fresh; new sid pinned
          FALLBACK      — verified id FAILED to resume (new rollout with different sid);
                          pinned the new sid instead; quarantine cleared
          SALVAGE_*     — a signal race was resolved before any sibling launch
          PendingQ      — signal failure could not be proven; caller rolls back

        Launch exceptions propagate to the cwd-group rollback.  Signal failures
        get one immediate, launch-isolated salvage attempt; an unresolved signal
        returns PendingQ and the caller rolls back the whole cwd group before a
        sibling can start.
        """
        assert tab._launch_wall is None
        assert tab._pre_scan is None
        tab._pending_quarantine_reason = None  # type: ignore[attr-defined]
        target_sid = tab.agent_session_id
        is_verified = bool(
            not tab.resume_quarantined and target_sid and _codex_id_exists(target_sid, tab.cwd)
        )
        tab._pre_scan = _codex_scan_sessions()
        # ensure_tmux_session stamps _launch_wall/_launch_mono and spawns Codex.
        # The ttyd process itself is intentionally started only after all SID
        # attribution/reconciliation completes: ttyd does not participate in
        # rollout ownership, and serializing its configure/safety waits adds
        # ~1.5 s per tab without improving correctness.
        await tab.ensure_tmux_session()
        diff = await self._codex_poll_signal(tab)
        # Filter to same-cwd new/appended entries
        post = _codex_scan_sessions()
        changed: Dict[str, str] = {}
        for s, ct in diff.items():
            if ct not in ("new", "appended"):
                continue
            entry = post.get(s)
            if not entry or not entry.cwd:
                continue
            if not tab.cwd:
                continue
            try:
                if os.path.realpath(entry.cwd) != os.path.realpath(tab.cwd):
                    continue
            except OSError:
                continue
            changed[s] = ct

        if not changed:
            reason = f"SignalZero (no same-cwd rollout activity within {_CODEX_POLL_MAX_ATTEMPTS * _CODEX_POLL_INTERVAL_S:.1f}s budget)"
            logger.warning("codex Phase 1C tab %s: %s", tab.tab_id, reason)
            tab._pending_quarantine_reason = reason  # type: ignore[attr-defined]
            return self._salvage_pending_codex_signal(tab) or ("PendingQ", None)

        if len(changed) != 1:
            reason = (
                f"SignalAmbiguous ({len(changed)} same-cwd changed sids: {sorted(changed.keys())})"
            )
            logger.warning("codex Phase 1C tab %s: %s", tab.tab_id, reason)
            tab._pending_quarantine_reason = reason  # type: ignore[attr-defined]
            return self._salvage_pending_codex_signal(tab) or ("PendingQ", None)

        ((new_sid, ct),) = changed.items()
        is_appended = ct == "appended"
        entry = post[new_sid]

        if is_appended:
            # Successful resume appended to the target file.
            if is_verified and new_sid == target_sid:
                tab._is_new_pin = False
                tab.resume_quarantined = False
                logger.info(
                    "codex Phase 1C tab %s: RESUME_OK append-resume to %s",
                    tab.tab_id,
                    new_sid,
                )
                return "RESUME_OK", new_sid
            # Appending to a non-target sid: unexpected, defer to Phase R.
            reason = f"SignalBadDiscriminator: append to {new_sid} but target was {target_sid}"
            logger.warning("codex Phase 1C tab %s: %s", tab.tab_id, reason)
            tab._pending_quarantine_reason = reason  # type: ignore[attr-defined]
            return self._salvage_pending_codex_signal(tab) or ("PendingQ", None)

        # New file: codex created a fresh rollout.
        if entry.ts is not None:
            delta = entry.ts - (tab._launch_wall or time.time())
            if delta < -_CODEX_NEW_PIN_TS_EARLY_S or delta > _CODEX_NEW_PIN_TS_LATE_S:
                reason = f"SignalBadDiscriminator: new rollout {new_sid} ts delta {delta:.1f}s outside [-{_CODEX_NEW_PIN_TS_EARLY_S:.0f}s, +{_CODEX_NEW_PIN_TS_LATE_S:.0f}s] window"
                logger.warning("codex Phase 1C tab %s: %s", tab.tab_id, reason)
                tab._pending_quarantine_reason = reason  # type: ignore[attr-defined]
                return self._salvage_pending_codex_signal(tab) or ("PendingQ", None)
        if is_verified and new_sid == target_sid:
            # Verified resume that also created new file — shouldn't happen for a
            # resume, but if sids match, trust it.
            tab._is_new_pin = False
            tab.resume_quarantined = False
            logger.info(
                "codex Phase 1C tab %s: RESUME_OK (new file but sid matches %s)",
                tab.tab_id,
                new_sid,
            )
            return "RESUME_OK", new_sid
        # Either: verified resume failed and codex fell through to fresh (FALLBACK),
        # or unverified/quarantined tab started fresh (FRESH_PIN). Both pin new_sid.
        tab.agent_session_id = new_sid
        tab._is_new_pin = True
        tab.resume_quarantined = False
        outcome = "FALLBACK" if is_verified else "FRESH_PIN"
        logger.info(
            "codex Phase 1C tab %s: %s -> %s",
            tab.tab_id,
            outcome,
            new_sid,
        )
        return outcome, new_sid

    def _schedule_codex_discovery(
        self, process: TTYDProcess, delay: float = _SINGLE_TAB_DISCOVERY_DELAY_S
    ) -> None:
        """Fire-and-forget: discover and pin codex session id after ``delay``.

        Used when a SINGLE codex tab is launched outside the bulk
        ``start_all_tabs`` path (create_tab, ensure_tab_tmux_session). Single
        launches never run under GLOBAL_CODEX_LAUNCH_LOCK and skip Phase-R
        because there is only one codex writing to disk at a time, so the
        legacy timestamp-window discovery is sufficient.
        """
        if process.agent_type != AgentType.CODEX:
            return
        if process.target != ExecutionTarget.LOCAL:
            return
        if not process.cwd:
            return

        async def _discover() -> None:
            await asyncio.sleep(delay)
            try:
                launch_epoch = (
                    process._launch_wall
                    if process._launch_wall is not None
                    else time.time() - delay
                )
                if process.agent_session_id and _codex_id_exists(
                    process.agent_session_id, process.cwd
                ):
                    return
                discovered = process._discover_codex_session_id(launch_epoch)
                if discovered and discovered != process.agent_session_id:
                    process.agent_session_id = discovered
                    process.resume_quarantined = False
                    self._save_state()
            except Exception as e:
                logger.warning(f"error during single-tab codex discovery for {process.tab_id}: {e}")

        asyncio.create_task(_discover())

    async def start_all_tabs(self) -> None:
        """Start all saved tabs on startup. Five-phase lifecycle:

        Phase 0 — Snapshot and backfill. Backfill codex sids against live
        tmux sessions (where unambiguous) and assign new uuid4 sids to any
        cursor tabs missing them (Cursor is a constructive pin; V0 verified
        ``agent --resume <uuid>`` creates the store immediately).

        Phase 1 (hot) — If at least one persisted tab still has its exact tmux
        session, treat startup as a backend-only restart and reattach only the
        tabs whose tmux sessions survived. Persisted tabs without a surviving
        session stay stopped and can be resumed explicitly when opened. This
        prevents a source reload from cold-launching every historical tab.

        Phase 1 (cold) — If none of the persisted tabs has a surviving tmux
        session, treat startup as a machine/runtime restart and recover all
        saved tabs using the existing pinned-session recovery rules.

        Phase 1S — Serialize codex cold launches under GLOBAL_CODEX_LAUNCH_LOCK.
        Group tabs by cwd; within each cwd run VerifiedCodex (verified sid,
        not quarantined) BEFORE FreshCodex (new / unverified / quarantined) —
        this ordering guarantees that a resumed-verified tab appending to an
        existing rollout is observed before any fresh codex in that cwd
        starts, preventing R8 extra-sid misattribution. Across cwds the
        ordering also prefers fewer-cwd keys first (cwd-first sort) for
        determinism. Each tab's launch uses the per-tab snapshot+signal
        logic (_launch_one_cold_codex_locked) with atomic whole-cwd rollback:
        if any tab in a cwd group throws, stop(kill_tmux=True) every tab in
        that cwd and mark them quarantine=True.

        Phase R — After all successfully-attributed codex tabs are launched,
        re-scan and enforce R1-R8 bijection:
          R1 every expected pinned sid exists in post scan
          R2 every entry.cwd realpath matches the tab's cwd realpath
          R3 new-pin sids fall in ts window; append-resume sids grew
             (size+mtime_ns vs _pre_scan)
          R4 no empty entries
          R5 no duplicate sid assignments across tabs
          R6 every actual new-pin sid in this batch is assigned to exactly one tab
          R7 every append-resume sid was present in that tab's exact _pre_scan
          R8 extras: any new same-cwd sid relative to the immutable global
             pre-launch scan is rejected
        Any R6-R8 failure quarantines the whole live cwd batch rather than
        risking cross-wiring. Signal failures are salvaged, if provable, while
        the lock is still held and before the next sibling launches; unresolved
        ownership rolls back the cwd immediately.

        Phase F — Persist final state to tabs.json so the next restart can
        resume the correctly-pinned sids.
        """
        logger.info("Ensuring tmux server is running...")
        _ensure_tmux_server()

        # Repair raw tmux sessions that survived an older failed/cancelled tab
        # lifecycle and no longer have a durable tabs.json owner.  This runs
        # before the API accepts requests and after the backend instance lock
        # is acquired, so the grace window only needs to cover historical
        # create crashes rather than a competing live backend.
        await self._prune_orphan_tmux_sessions()

        # Phase 0: backfill + assign sids for cursor legacy tabs.
        self._backfill_agent_session_ids()
        self._backfill_codex_session_ids()
        for p in self.processes.values():
            if (
                p.agent_type == AgentType.CURSOR
                and p.target == ExecutionTarget.LOCAL
                and not p.agent_session_id
            ):
                p.agent_session_id = str(uuid.uuid4())
                p.resume_quarantined = False

        try:
            listed = subprocess.run(
                tmux_command("ls"),
                capture_output=True,
                text=True,
            )
            result = listed.stdout or listed.stderr
            logger.info("Existing tmux sessions before starting tabs:\n%s", result)
        except Exception as e:
            logger.debug("Could not list tmux sessions: %s", e)

        processes = list(self.processes.values())
        logger.info("Starting %d saved tabs", len(processes))

        # Partition into hot (session exists) / cold-codex / cold-non-codex.
        # A mixed snapshot means the tmux server survived this backend restart.
        # Missing sessions in that case are historical/stopped tabs, not proof
        # that every one of them should be relaunched. They remain available in
        # the UI and ``ensure_tab_running`` resumes one on explicit access.
        async def _session_exists(p: TTYDProcess) -> bool:
            return await _tmux_session_exists_async(p.tmux_session)

        exists = await asyncio.gather(*(_session_exists(p) for p in processes))
        hot_restart = any(exists)
        hot: List[TTYDProcess] = []
        cold_non_codex: List[TTYDProcess] = []
        cold_codex: List[TTYDProcess] = []
        for p, ex in zip(processes, exists):
            if ex:
                hot.append(p)
            elif hot_restart:
                p.is_active = False
            elif p.agent_type == AgentType.CODEX and p.target == ExecutionTarget.LOCAL:
                cold_codex.append(p)
            else:
                cold_non_codex.append(p)

        if hot_restart:
            skipped = len(processes) - len(hot)
            logger.info(
                "Hot backend restart detected: reattaching %d live tabs and "
                "leaving %d tabs stopped until explicitly opened",
                len(hot),
                skipped,
            )
        else:
            logger.info(
                "Cold runtime restart detected: recovering all %d saved tabs",
                len(processes),
            )

        # Phase 1: launch hot + cold non-codex in parallel.
        async def _start_one(process: TTYDProcess) -> None:
            try:
                await process.start()
                logger.info(
                    "Tab %s started (tmux session %s %s)",
                    process.tab_id,
                    process.tmux_session,
                    "reattached" if await _session_exists(process) else "created",
                )
            except Exception as e:
                logger.error("Failed to start tab %s: %s", process.tab_id, e)

        async def _launch_cold_non_codex(p: TTYDProcess) -> None:
            # For claude/cursor/terminal cold launches we stamp _launch_wall via
            # ensure_tmux_session so the clock is captured consistently.
            # start() uses tmux new-session -A; calling ensure first ensures we
            # create the session and stamp clocks before ttyd binds the port.
            if not await _tmux_session_exists_async(p.tmux_session):
                try:
                    await p.ensure_tmux_session()
                except Exception as e:
                    logger.warning(
                        "ensure_tmux_session for non-codex tab %s failed: %s", p.tab_id, e
                    )
            await _start_one(p)

        await asyncio.gather(
            *(_start_one(p) for p in hot),
            *(_launch_cold_non_codex(p) for p in cold_non_codex),
        )

        # Phase 1S: cold codex, per-cwd serial under global lock.
        # Group cold codex by cwd and sort: (is_verified, tab_id) within each cwd,
        # sorted cwd keys across cwds (deterministic). Verified-before-Fresh
        # guarantees resumed-verified tabs append before any fresh codex starts
        # in the same cwd, preventing R8 extra-sid misattribution.
        from collections import defaultdict

        by_cwd: Dict[str, List[TTYDProcess]] = defaultdict(list)
        for p in cold_codex:
            key = os.path.realpath(p.cwd) if p.cwd else ""
            by_cwd[key].append(p)
        for cwd_key in list(by_cwd.keys()):
            tabs = by_cwd[cwd_key]
            # Verified-before-Fresh: tabs with a verified, non-quarantined sid
            # go first so that a successful append-resume is observed before
            # any fresh codex in this cwd starts writing.
            tabs.sort(
                key=lambda t: (
                    (
                        0
                        if (
                            not t.resume_quarantined
                            and t.agent_session_id
                            and _codex_id_exists(t.agent_session_id, t.cwd)
                        )
                        else 1
                    ),
                    t.tab_id,
                )
            )

        async with GLOBAL_CODEX_LAUNCH_LOCK:
            pre_global_scan = _codex_scan_sessions()
            launched_codex: List[TTYDProcess] = []
            for cwd_key in sorted(by_cwd.keys()):
                tabs = by_cwd[cwd_key]
                started_this_cwd: List[TTYDProcess] = []
                try:
                    for tab in tabs:
                        outcome, pinned = await self._launch_one_cold_codex_locked(tab)
                        started_this_cwd.append(tab)
                        if outcome == "PendingQ":
                            raise RuntimeError(
                                f"unresolved Codex ownership signal for tab {tab.tab_id}: "
                                f"{tab._pending_quarantine_reason}"
                            )
                        launched_codex.append(tab)
                except Exception as e:
                    logger.error(
                        "Cold codex launch cwd-group rollback for cwd %s: %s",
                        cwd_key,
                        e,
                    )
                    # Atomic rollback: _quarantine_codex_tab kills tmux, clears SID,
                    # resets runtime fields, and sets resume_quarantined=True.
                    rollback_failures: List[str] = []
                    for t in tabs:
                        try:
                            await self._quarantine_codex_tab(
                                t, f"cwd-group exception rollback: {e!r}"
                            )
                        except Exception as qe:
                            logger.error("rollback quarantine for %s failed: %s", t.tab_id, qe)
                            rollback_failures.append(f"{t.tab_id}: {qe!r}")
                    self._save_state()
                    # Remove all tabs in this cwd from launched_codex; Phase R skips them.
                    launched_codex = [x for x in launched_codex if x not in tabs]
                    if rollback_failures:
                        raise RuntimeError(
                            "Codex cwd rollback could not prove process teardown: "
                            + "; ".join(rollback_failures)
                        ) from e
                    continue

            # Phase R must stay inside the same global lock as launch. Releasing
            # the lock first would let another cold-recovery batch create rollout
            # activity between our final scan and ownership decision.
            await self._reconcile_codex_phase_r(launched_codex, pre_global_scan)

        # Attribution is complete and the global lock has excluded competing
        # cold Codex recovery.  Start the independent ttyd frontends in
        # parallel; quarantined tabs have no owned tmux and remain stopped.
        await asyncio.gather(
            *(_start_one(tab) for tab in launched_codex if not tab.resume_quarantined)
        )

        # Final persist.
        self._save_state()

    async def _prune_orphan_tmux_sessions(
        self,
        *,
        grace_seconds: float = _ORPHAN_TMUX_PRUNE_GRACE_SECONDS,
    ) -> list[str]:
        """Kill old managed-prefix tmux sessions absent from ``tabs.json``.

        Manual/non-Hub tmux sessions and recently-created managed sessions are
        preserved.  Failed kills remain live and are reported for a later
        restart instead of being counted as successfully pruned.
        """
        owned = {process.tmux_session for process in self.processes.values()}
        now = time.time()
        pruned: list[str] = []
        sessions = await _tmux_list_session_created()
        for session_name, created_at in sorted(sessions.items()):
            if not _MANAGED_TMUX_SESSION_RE.fullmatch(session_name):
                continue
            if session_name in owned:
                continue
            if now - created_at < grace_seconds:
                continue
            logger.warning(
                "Pruning orphan tmux session with no tabs.json owner session=%s age_seconds=%.1f",
                session_name,
                now - created_at,
            )
            try:
                await _tmux_kill_session(session_name)
            except Exception:
                logger.exception("Failed to prune orphan tmux session %s", session_name)
                continue
            pruned.append(session_name)
        return pruned

    async def _reconcile_codex_phase_r(
        self, launched: List[TTYDProcess], pre_global_scan: Dict[str, ScanEntry]
    ) -> None:
        """Phase R: enforce the final R1-R8 ownership bijection.

        Any failure calls ``_quarantine_codex_tab`` (which kills tmux, clears
        the SID only after teardown is proven, resets runtime fields, and sets
        ``resume_quarantined=True``) rather than risking cross-wiring.
        """
        if not launched:
            return
        post = _codex_scan_sessions()
        problems: List[str] = []
        quarantine_failures: List[str] = []
        cwd_to_tabs: Dict[str, List[TTYDProcess]] = {}
        for t in launched:
            key = os.path.realpath(t.cwd) if t.cwd else ""
            cwd_to_tabs.setdefault(key, []).append(t)

        def _same_cwd(entry: ScanEntry, cwd_key: str) -> bool:
            if not entry.cwd or not cwd_key:
                return False
            try:
                return bool(os.path.realpath(entry.cwd) == cwd_key)
            except OSError:
                return bool(entry.cwd == cwd_key)

        # ---- R1-R5 per-tab bijection ----
        quarantine_reasons: Dict[str, List[str]] = {}

        def _queue_quarantine(tab: TTYDProcess, reason: str) -> None:
            quarantine_reasons.setdefault(tab.tab_id, []).append(reason)

        sid_claims: Dict[str, List[TTYDProcess]] = {}
        for t in launched:
            if t._pending_quarantine_reason is not None:
                _queue_quarantine(t, f"R-fail signal/launch: {t._pending_quarantine_reason}")
            elif not t.agent_session_id:
                _queue_quarantine(t, f"tab {t.tab_id} R4 empty agent_session_id")
            else:
                sid_claims.setdefault(t.agent_session_id, []).append(t)

        # A duplicate invalidates every claimant, not only whichever tab happens
        # to be visited second.
        for claimed_sid, claimants in sid_claims.items():
            if len(claimants) > 1:
                labels = sorted(t.tab_id for t in claimants)
                for t in claimants:
                    _queue_quarantine(t, f"tab {t.tab_id} R5 duplicate sid {claimed_sid}: {labels}")

        for t in launched:
            label = f"tab {t.tab_id}"
            if quarantine_reasons.get(t.tab_id):
                continue
            sid: Optional[str] = t.agent_session_id
            assert sid is not None
            post_entry: Optional[ScanEntry] = post.get(sid)
            # R1 exists
            if post_entry is None:
                _queue_quarantine(t, f"{label} R1 sid {sid} missing from post-scan")
                continue
            # R2 cwd realpath match
            if t.cwd and not post_entry.cwd:
                _queue_quarantine(t, f"{label} R2 rollout cwd missing")
                continue
            if t.cwd and post_entry.cwd:
                try:
                    if os.path.realpath(post_entry.cwd) != os.path.realpath(t.cwd):
                        _queue_quarantine(
                            t, f"{label} R2 cwd mismatch: {post_entry.cwd} vs {t.cwd}"
                        )
                except OSError:
                    if post_entry.cwd != t.cwd:
                        _queue_quarantine(
                            t, f"{label} R2 cwd mismatch: {post_entry.cwd} vs {t.cwd}"
                        )
            if quarantine_reasons.get(t.tab_id):
                continue
            # R3 check
            pre_entry = t._pre_scan.get(sid) if t._pre_scan is not None else None
            r3_fail: Optional[str] = None
            if t._is_new_pin is True:
                if post_entry.ts is None or t._launch_wall is None:
                    r3_fail = f"{label} R3-new missing ts"
                else:
                    delta = post_entry.ts - t._launch_wall
                    if delta < -_CODEX_NEW_PIN_TS_EARLY_S or delta > _CODEX_NEW_PIN_TS_LATE_S:
                        r3_fail = f"{label} R3-new ts delta {delta:.1f}s outside window"
            elif t._is_new_pin is False:
                # append-resume: must have grown size+mtime vs pre
                if pre_entry is None:
                    r3_fail = f"{label} R3-append no pre_entry for resumed sid {sid}"
                else:
                    size_grew = post_entry.size > pre_entry.size
                    mtime_grew = post_entry.mtime_ns > pre_entry.mtime_ns
                    if not (size_grew and mtime_grew):
                        r3_fail = (
                            f"{label} R3-append no grow: size {pre_entry.size}->{post_entry.size}, "
                            f"mtime {pre_entry.mtime_ns}->{post_entry.mtime_ns}"
                        )
            else:
                r3_fail = f"{label} R3 unknown pin kind"
            if r3_fail:
                _queue_quarantine(t, r3_fail)

        # Quarantine R1-R5 failures (kill_tmux + SID clear).
        quarantined_ids: set[str] = set()
        for t in launched:
            reasons = quarantine_reasons.get(t.tab_id)
            if not reasons:
                continue
            reason = "; ".join(reasons)
            problems.append(reason)
            logger.warning("Phase R: quarantining %s — %s", t.tab_id, reason)
            try:
                await self._quarantine_codex_tab(t, reason)
            except Exception as qe:
                logger.error("Phase R quarantine for %s failed: %s", t.tab_id, qe)
                quarantine_failures.append(f"{t.tab_id}: {qe!r}")
            quarantined_ids.add(t.tab_id)

        # ---- R6/R7/R8 global/per-cwd bijection ----
        # All activity is classified relative to the ONE global snapshot taken
        # before any cold Codex launch.  A later tab's per-tab pre-scan may
        # already contain an earlier tab's new sid, so using ``any(per_tab_pre)``
        # here would hide extras and make the result order-dependent.
        for cwd_key, tabs in cwd_to_tabs.items():
            live_tabs = [t for t in tabs if t.tab_id not in quarantined_ids]
            if not live_tabs:
                continue
            expected_new = {
                t.agent_session_id
                for t in live_tabs
                if t._is_new_pin is True and t.agent_session_id
            }
            expected_append = {
                t.agent_session_id
                for t in live_tabs
                if t._is_new_pin is False and t.agent_session_id
            }
            expected_all = expected_new | expected_append
            actual_new: set[str] = set()
            actual_append: set[str] = set()
            for sid, entry in post.items():
                if not _same_cwd(entry, cwd_key):
                    continue
                pre_entry = pre_global_scan.get(sid)
                if pre_entry is None:
                    actual_new.add(sid)
                elif entry.size > pre_entry.size and entry.mtime_ns > pre_entry.mtime_ns:
                    actual_append.add(sid)

            # R6 every expected_new is in actual_new
            missing_new = expected_new - actual_new
            missing_append = expected_append - actual_append
            extras = (actual_new | actual_append) - expected_all
            batch_failures: List[str] = []
            if missing_new:
                batch_failures.append(
                    f"R6 expected new sids missing from global diff: {sorted(missing_new)}"
                )
            if missing_append:
                batch_failures.append(
                    f"R7 expected append sids missing growth from global pre-scan: {sorted(missing_append)}"
                )
            if extras:
                batch_failures.append(f"R8 extra changed sids: {sorted(extras)}")

            if batch_failures:
                batch_reason = f"cwd {cwd_key} bijection failure: " + "; ".join(batch_failures)
                problems.append(batch_reason)
                # Ownership for the cwd batch is no longer bijective.  Kill and
                # clear every still-live claimant rather than preserving a tab
                # based on launch order or a merely plausible timestamp.
                for t in live_tabs:
                    logger.warning("Phase R batch quarantine for %s — %s", t.tab_id, batch_reason)
                    try:
                        await self._quarantine_codex_tab(t, batch_reason)
                    except Exception as qe:
                        logger.error("Phase R batch quarantine for %s failed: %s", t.tab_id, qe)
                        quarantine_failures.append(f"{t.tab_id}: {qe!r}")
                    quarantined_ids.add(t.tab_id)

        if problems:
            logger.warning("Phase R reconciliation problems: %s", "; ".join(problems))
            self._save_state()
        else:
            logger.info("Phase R reconciliation OK for %d codex tabs", len(launched))
        if quarantine_failures:
            self._save_state()
            raise RuntimeError(
                "Codex reconciliation could not prove process teardown: "
                + "; ".join(quarantine_failures)
            )

    async def cleanup(self) -> None:
        """Stop ttyd processes but keep tmux sessions alive for next startup."""
        logger.info("=" * 60)
        logger.info("CLEANING UP - tmux sessions WILL BE PRESERVED")
        logger.info("=" * 60)
        for process in list(self.processes.values()):
            logger.info(
                f"Will preserve tmux session: {process.tmux_session} for tab: {process.name}"
            )
            await process.stop(kill_tmux=False)
        logger.info("Cleanup complete - all tmux sessions preserved")


# Global manager instance
ttyd_manager = TTYDManager()
