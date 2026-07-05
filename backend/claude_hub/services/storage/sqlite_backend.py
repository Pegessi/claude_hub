"""SQLite storage backend prototype (spike / opt-in only).

Stores one JSON-blob row per entity, keyed by ``id`` and ``workspace_id`` — the
two keys the workspace manager queries by. Model evolution therefore needs **no**
DDL change: the row payload is the same ``model_dump(mode="json")`` dict the JSON
backend stores. A ``schema_meta`` table records the on-disk ``schema_version``.

Durability / crash-safety:

* WAL journal mode + ``synchronous=FULL``.
* ``busy_timeout=5000`` so multi-process / multi-tab lock contention waits
  instead of raising ``OperationalError: database is locked`` immediately.
* Each :meth:`save` runs in a single transaction — a crash rolls back to the last
  commit, so a partial write can never corrupt committed state (contrast the
  current JSON path, whose non-atomic full-file rewrite can truncate on crash).
* Orphan parity: rows without ``workspace_id`` are skipped on save for
  tasks/sessions/reports, matching ``JsonStorageBackend`` semantics where such
  items cannot be placed in any per-workspace ``state.json`` and are therefore
  not round-trippable.

This backend is **opt-in** (``WORKSPACE_STORAGE_BACKEND=sqlite``) and is not wired
into the running manager. It exists to prove a safe round-trip.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import SCHEMA_VERSION, StorageSnapshot

_ENTITY_TABLES = ("workspaces", "tasks", "sessions", "reports")

# Milliseconds SQLite will wait on a locked database before raising
# OperationalError. 5s gives concurrent readers/writers (e.g., a migration CLI
# running alongside the server) a chance to serialize without failing fast.
_BUSY_TIMEOUT_MS = 5000


def _run_integrity_check(db_path: Path) -> None:
    """Run ``PRAGMA integrity_check`` against ``db_path`` and raise on any defect.

    The pragma returns one row with value ``'ok'`` when the database is
    consistent; any other rows describe individual defects. We treat any
    non-ok result as fatal — callers should refuse to migrate or export a
    corrupt database rather than propagate corruption.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    messages = [r[0] for r in rows if r and r[0] != "ok"]
    if messages:
        raise sqlite3.DatabaseError(
            f"SQLite integrity check failed for {db_path}: {'; '.join(messages[:5])}"
            + (" ..." if len(messages) > 5 else "")
        )


class SqliteStorageBackend:
    """JSON-blob-per-row SQLite backend."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (" "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        # workspaces has no workspace_id foreign key (it *is* the workspace).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS workspaces (" "id TEXT PRIMARY KEY, json TEXT NOT NULL)"
        )
        for table in ("tasks", "sessions", "reports"):
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                "id TEXT PRIMARY KEY, workspace_id TEXT, json TEXT NOT NULL)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_workspace " f"ON {table}(workspace_id)"
            )
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        else:
            on_disk = int(row[0])
            if on_disk > SCHEMA_VERSION:
                # Fail closed — never silently operate on a newer layout than we
                # understand. A downgrade would risk dropping fields on rewrite.
                raise RuntimeError(
                    f"SQLite state schema_version={on_disk} is newer than "
                    f"supported {SCHEMA_VERSION}; refusing to open."
                )
            # on_disk < SCHEMA_VERSION would run forward migrations here; none
            # exist yet at version 1.
        conn.commit()

    def load(self) -> StorageSnapshot:
        if not self.db_path.exists():
            return StorageSnapshot()
        conn = self._connect()
        try:
            snapshot = StorageSnapshot()
            snapshot.workspaces = [
                json.loads(r[0]) for r in conn.execute("SELECT json FROM workspaces")
            ]
            snapshot.tasks = [json.loads(r[0]) for r in conn.execute("SELECT json FROM tasks")]
            snapshot.sessions = [
                json.loads(r[0]) for r in conn.execute("SELECT json FROM sessions")
            ]
            snapshot.reports = [json.loads(r[0]) for r in conn.execute("SELECT json FROM reports")]
            return snapshot
        finally:
            conn.close()

    def save(self, snapshot: StorageSnapshot) -> None:
        conn = self._connect()
        try:
            with conn:  # single transaction: commit on success, rollback on error
                for table in _ENTITY_TABLES:
                    conn.execute(f"DELETE FROM {table}")
                for item in snapshot.workspaces:
                    conn.execute(
                        "INSERT INTO workspaces(id, json) VALUES (?, ?)",
                        (item["id"], json.dumps(item)),
                    )
                for table, items in (
                    ("tasks", snapshot.tasks),
                    ("sessions", snapshot.sessions),
                    ("reports", snapshot.reports),
                ):
                    for item in items:
                        workspace_id = item.get("workspace_id")
                        # Orphan parity with JsonStorageBackend: items without a
                        # workspace_id cannot be placed in any per-workspace
                        # state.json on the JSON side, so skip them here too to
                        # prevent a JSON -> SQLite -> JSON round-trip from
                        # gaining orphan rows that JSON cannot represent.
                        if not workspace_id:
                            continue
                        conn.execute(
                            f"INSERT INTO {table}(id, workspace_id, json) " "VALUES (?, ?, ?)",
                            (item["id"], workspace_id, json.dumps(item)),
                        )
        finally:
            conn.close()
