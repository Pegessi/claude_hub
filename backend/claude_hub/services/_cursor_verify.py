"""Cursor on-disk session verification.

Cursor CLI (``agent``) persists per-workspace chat state under
``<home>/.cursor/chats/<md5(realpath(cwd))>/<chatId>/store.db``.  Each tab that
launches Cursor with ``agent --resume <uuid>`` creates ``store.db`` immediately
(Cursor is a *constructive pin* — V0 verified: passing an arbitrary uuid causes
Cursor to create a fresh chat store rather than erroring out), so we only need
this helper for verifying whether a previously-persisted ``agent_session_id``
already has a matching on-disk store to resume.

Verification is DB-authoritative when ``store.db`` exists: the very first
record in the meta table (key=0) is a hex-encoded JSON blob whose ``agentId``
field carries the stable session UUID. When the DB is absent we fall back to
``meta.json`` (written after the first user message) whose ``cwd`` field holds
the workspace realpath.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


def _expand_with_home(raw_path: str, home: Path, cwd: Optional[str] = None) -> Path:
    stripped = raw_path.strip()
    if stripped == "~":
        return home
    if stripped.startswith("~/"):
        return home / stripped[2:]
    path = Path(stripped).expanduser()
    if not path.is_absolute():
        return Path(cwd or ".") / path
    return path


def resolve_cursor_roots(
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[str] = None,
    home: Optional[str] = None,
) -> Tuple[Path, Path]:
    """Return ``(chats_root, acp_config_root)`` honoring ``env`` and ``home``.

    * ``chats_root`` owns ``chats/<md5(realpath(cwd))>/<chatId>/store.db``.
    * ``acp_config_root`` owns ``acp-sessions/<sid>/store.db`` (honoring
      ``CURSOR_CONFIG_DIR`` and ``XDG_CONFIG_HOME/cursor`` before falling back
      to ``chats_root``).
    """
    source_env: Mapping[str, str] = env if env is not None else os.environ
    raw_home = home or source_env.get("HOME")
    effective_home = Path(raw_home).expanduser() if raw_home else Path.home()

    cursor_home_raw = source_env.get("CURSOR_HOME")
    if isinstance(cursor_home_raw, str) and cursor_home_raw.strip():
        chats_root = _expand_with_home(cursor_home_raw, effective_home, cwd)
    else:
        chats_root = effective_home / ".cursor"

    config_dir_raw = source_env.get("CURSOR_CONFIG_DIR")
    xdg_raw = source_env.get("XDG_CONFIG_HOME")
    if isinstance(config_dir_raw, str) and config_dir_raw.strip():
        acp_root = _expand_with_home(config_dir_raw, effective_home, cwd)
    elif isinstance(xdg_raw, str) and xdg_raw.strip():
        acp_root = _expand_with_home(xdg_raw, effective_home, cwd) / "cursor"
    else:
        acp_root = chats_root
    return chats_root, acp_root


def _cursor_home_dir(home: Optional[str] = None) -> Path:
    """Cursor config root (respects $CURSOR_HOME / $HOME override for tests)."""
    chats_root, _ = resolve_cursor_roots(home=home)
    return chats_root


def _cwd_hash(cwd: str) -> str:
    """MD5 of the realpath of cwd — matches Cursor's on-disk sharding key."""
    return hashlib.md5(os.path.realpath(cwd).encode("utf-8")).hexdigest()


def _cursor_id_exists(sid: str, cwd: str, home: Optional[str] = None) -> bool:
    """Return True iff ``sid`` is a known Cursor session id for ``cwd``.

    Tier 1 (authoritative): scan every ``<chats>/<cwdhash>/<chatId>/store.db``,
    open it read-only, read meta key=0 (hex-encoded JSON) and compare its
    ``agentId`` field to ``sid``. Tier 2 (fallback for legacy stores where the
    DB hasn't been created yet or is corrupted): walk sibling ``meta.json``
    files whose cwd realpath matches and return True if any ``chatId`` directory
    name matches ``sid`` (the directory name IS the session id on older
    Cursor versions).
    """
    if not sid or not cwd:
        return False
    root, acp_root = resolve_cursor_roots(cwd=cwd, home=home)
    try:
        target_real = os.path.realpath(cwd)
    except OSError:
        target_real = cwd

    if "/" not in sid and "\\" not in sid and sid not in {".", ".."}:
        acp_sessions = acp_root / "acp-sessions"
        acp_dir = acp_sessions / sid
        acp_db = acp_dir / "store.db"
        acp_meta = acp_dir / "meta.json"
        try:
            if (
                not acp_sessions.is_symlink()
                and not acp_dir.is_symlink()
                and not acp_db.is_symlink()
                and acp_db.is_file()
                and acp_db.resolve().parent.parent == acp_sessions.resolve()
                and _read_agent_id_from_db(acp_db) == sid
            ):
                if acp_meta.is_file() and not acp_meta.is_symlink():
                    meta = json.loads(acp_meta.read_text(encoding="utf-8"))
                    meta_cwd = meta.get("cwd")
                    if isinstance(meta_cwd, str) and meta_cwd:
                        if os.path.realpath(meta_cwd) == target_real:
                            return True
                    else:
                        return True
                else:
                    return True
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    chats = root / "chats"
    if not chats.is_dir():
        return False
    target_hash = _cwd_hash(cwd)

    cwd_bucket = chats / target_hash
    if cwd_bucket.is_dir():
        try:
            for chat_id in os.listdir(cwd_bucket):
                chat_dir = cwd_bucket / chat_id
                if not chat_dir.is_dir():
                    continue
                db_path = chat_dir / "store.db"
                # Tier 1: authoritative DB check
                if db_path.is_file():
                    agent_id = _read_agent_id_from_db(db_path)
                    if agent_id == sid:
                        return True
                    # Even if the DB doesn't match, try Tier 2 below for this chat_dir.
                # Tier 2: meta.json realpath match AND chatId matches sid
                meta_path = chat_dir / "meta.json"
                if chat_id == sid and meta_path.is_file():
                    try:
                        with open(meta_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        meta_cwd = meta.get("cwd")
                        if meta_cwd and os.path.realpath(str(meta_cwd)) == target_real:
                            return True
                    except (OSError, ValueError, json.JSONDecodeError):
                        pass
        except OSError as e:
            logger.debug("cursor _cursor_id_exists: failed walking %s: %s", cwd_bucket, e)
    return False


def _read_agent_id_from_db(db_path: Path) -> Optional[str]:
    """Read meta key=0 from ``store.db`` and return the agentId field, or None."""
    try:
        # open in read-only mode to avoid interfering with a running Cursor
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            cur = conn.execute("SELECT value FROM meta WHERE key = 0 LIMIT 1")
            row = cur.fetchone()
        if not row:
            return None
        raw = row[0]
        if not isinstance(raw, (str, bytes)):
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        # meta value is hex-encoded JSON in current builds
        decoded = raw
        try:
            decoded = bytes.fromhex(raw.strip()).decode("utf-8", errors="ignore")
        except ValueError:
            # already plain JSON (older builds)
            pass
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError:
            return None
        agent_id = payload.get("agentId")
        if isinstance(agent_id, str) and agent_id:
            return agent_id
        # Fallback: some builds store the session id under a different key
        for k in ("sessionId", "session_id", "chatId", "id"):
            v = payload.get(k)
            if isinstance(v, str) and v:
                return v
        return None
    except sqlite3.Error as e:
        logger.debug("cursor _read_agent_id_from_db: %s: %s", db_path, e)
        return None
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("cursor _read_agent_id_from_db unexpected: %s: %s", db_path, e)
        return None
