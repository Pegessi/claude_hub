"""Session discovery operations with explicit runtime dependencies."""

from __future__ import annotations

import glob
import itertools
import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import catalog as _terminal_session_catalog
from .bindings import RuntimeBindings
from .defaults import _BACKFILL_ISOLATION_S, _BACKFILL_MATCH_WINDOW_S, _BACKFILL_MTIME_SLACK_S
from .records import ScanEntry

logger = logging.getLogger("claude_hub.services.ttyd_manager")


def _claude_project_dir_for_cwd(runtime: RuntimeBindings, cwd: str) -> Path:
    """Map a cwd to the Claude project log dir under ``~/.claude/projects``.

    Claude derives the dir name by dropping the leading slash and replacing
    every ``/``, ``_`` and ``.`` with ``-``, then prefixing ``-``. Example:
    ``/Users/bytedance/claude_hub`` -> ``-Users-bytedance-claude-hub``.
    """
    key = re.sub(r"[/_.]", "-", cwd.lstrip("/"))
    return Path.home() / ".claude" / "projects" / f"-{key}"


def _jsonl_start_epoch(runtime: RuntimeBindings, path: str) -> Optional[float]:
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
                    return runtime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
    except OSError:
        return None
    return None


def _pick_backfill_session(
    runtime: RuntimeBindings,
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


def _codex_home_dir(runtime: RuntimeBindings) -> Path:
    """Codex CLI home directory (respects $CODEX_HOME if set)."""
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def _codex_roots(runtime: RuntimeBindings) -> List[Tuple[Path, bool]]:
    """Return [(root, is_archived)] for active + archived codex rollout dirs."""
    home = runtime._codex_home_dir()
    roots: List[Tuple[Path, bool]] = []
    active = home / "sessions"
    if active.is_dir():
        roots.append((active, False))
    archived = home / "archived_sessions"
    if archived.is_dir():
        roots.append((archived, True))
    return roots


def _codex_scan_sessions(
    runtime: RuntimeBindings, with_title: bool = False, skip_title_sids: Optional[set] = None
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
    for root, is_archived in runtime._codex_roots():
        pattern = "**/rollout-*.jsonl"
        for path_str in glob.iglob(str(root / pattern), recursive=True):
            try:
                st = os.stat(path_str)
            except OSError:
                continue
            try:
                with open(path_str, "r", encoding="utf-8", errors="ignore") as f:
                    first = f.readline()
                    sid, ts_epoch, cwd, thread_source = runtime._parse_session_meta(first)
                    if sid is None:
                        logger.warning("codex scan: cannot parse session_meta from %s", path_str)
                        continue
                    if with_title and (skip_title_sids is None or sid not in skip_title_sids):
                        # first line already consumed; read up to N-1 more for
                        # title extraction, then prepend the session_meta line.
                        rest = list(itertools.islice(f, runtime._CODEX_TITLE_SCAN_MAX_LINES - 1))
                        title = runtime._codex_title_from_lines([first] + rest)
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


def _codex_id_exists(runtime: RuntimeBindings, sid: str, cwd: Optional[str] = None) -> bool:
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
    for scan_sid, entry in runtime._codex_scan_sessions().items():
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


def _codex_candidates_for_cwd(
    runtime: RuntimeBindings, cwd: str, scan: Optional[Dict[str, ScanEntry]] = None
) -> List[Tuple[float, str, str]]:
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
    for sid, entry in (scan if scan is not None else runtime._codex_scan_sessions()).items():
        if not entry.cwd:
            continue
        try:
            entry_cwd = os.path.realpath(entry.cwd)
        except OSError:
            entry_cwd = entry.cwd
        if entry_cwd == target and entry.ts is not None:
            out.append((entry.ts, sid, entry.path))
    return out


def _codex_session_title(runtime: RuntimeBindings, path: str) -> str:
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
            return runtime._codex_title_from_lines(
                itertools.islice(f, runtime._CODEX_TITLE_SCAN_MAX_LINES)
            )
    except OSError:
        return ""


def _codex_load_session_index(runtime: RuntimeBindings) -> Dict[str, str]:
    """Load ``~/.codex/session_index.jsonl`` as ``{session_id: thread_name}``.

    ``thread_name`` is the conversation title shown in the Codex app / IDE
    extension — exactly what the user expects to see in the session picker.
    The index is small (a few hundred KB) so loading it is cheap and lets us
    avoid scanning the (potentially multi-GB) rollout files for titles.
    Returns an empty dict if the file is missing or unreadable.
    """
    index_path = runtime._codex_home_dir() / "session_index.jsonl"
    try:
        with open(index_path, "r", encoding="utf-8", errors="ignore") as f:
            return _terminal_session_catalog.parse_session_index(f)
    except OSError:
        return {}


def _codex_sessions_cache_key(runtime: RuntimeBindings) -> Tuple[int, int]:
    """Return a cache key for the codex sessions listing.

    The key is ``(session_index_mtime_ns, max_rollout_mtime_ns)``. When either
    changes (user renamed a thread, or a new rollout was written), the cache is
    invalidated. We stat the index and every date-level directory under the
    codex session roots; since rollout files are append-only and never modified
    in place, directory mtime changes (file additions/removals) are sufficient
    to detect new sessions.
    """
    index_path = runtime._codex_home_dir() / "session_index.jsonl"
    try:
        index_mtime = index_path.stat().st_mtime_ns
    except OSError:
        index_mtime = 0

    max_rollout_mtime = 0
    for root, _ in runtime._codex_roots():
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
