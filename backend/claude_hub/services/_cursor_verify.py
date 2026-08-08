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
from typing import Optional

logger = logging.getLogger(__name__)


def _cursor_home_dir(home: Optional[str] = None) -> Path:
    """Cursor config root (respects $CURSOR_HOME / $HOME override for tests)."""
    override = os.environ.get("CURSOR_HOME")
    if override:
        return Path(override).expanduser()
    base = Path(home) if home else Path.home()
    return base / ".cursor"


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
    root = _cursor_home_dir(home)
    chats = root / "chats"
    if not chats.is_dir():
        return False
    target_hash = _cwd_hash(cwd)
    try:
        target_real = os.path.realpath(cwd)
    except OSError:
        target_real = cwd

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
        uri = f"file:{db_path}?mode=ro"
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
