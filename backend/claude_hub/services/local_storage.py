"""One SQLite WAL engine for Hub-owned durable documents and stream events.

Logical legacy paths identify records; JSON is read only for first import. Sources
are retained as recovery evidence, never rewritten. A committed document marker
prevents stale sources from being imported again, including after deletion.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .runtime_isolation import resolve_runtime_home, resolve_state_root

_SCHEMA_LOCK = threading.Lock()


def storage_location(path: Path) -> tuple[Path, str]:
    path = path.absolute()
    home = resolve_runtime_home().absolute()
    state = resolve_state_root().absolute()
    # Canonicalize directory aliases without following a symlink at the final
    # document name: lifecycle ownership belongs to that logical file name.
    canonical_path = path.parent.resolve() / path.name
    canonical_home = home.resolve()
    canonical_state = state.resolve()
    if canonical_path.is_relative_to(canonical_home):
        return home / "storage.sqlite3", str(canonical_path.relative_to(canonical_home))
    if canonical_path.is_relative_to(canonical_state):
        # Changing an external workspace-root override selects different state,
        # even when top-level metadata keeps the same runtime home/database.
        identity = hashlib.sha256(str(canonical_state).encode()).hexdigest()
        name = (
            "workspace-roots/" + identity + "/" + str(canonical_path.relative_to(canonical_state))
        )
        return home / "storage.sqlite3", name
    root = path.parent
    for parent in path.parents:
        if parent.name == "workspaces":
            root = parent.parent
            break
    if root.name == "secrets":
        root = root.parent
    # Apply the existing linked-worktree live-home guard to explicit legacy
    # paths too; a forgotten caller override must not bootstrap production.
    resolve_runtime_home(environ={**os.environ, "CLAUDE_HUB_HOME": str(root)})
    return root / "storage.sqlite3", str(path.relative_to(root))


@contextmanager
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    database, _ = storage_location(path)
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        db = sqlite3.connect(database, timeout=30)
    except sqlite3.Error as exc:
        raise OSError(f"Cannot open SQLite storage: {database}: {exc}") from exc
    try:
        with _SCHEMA_LOCK:
            database.chmod(0o600)
            deadline = time.monotonic() + 30
            while True:
                try:
                    if db.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                        break
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            db.execute("PRAGMA synchronous=FULL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise OSError(f"Unsupported SQLite storage version: {version}")
            if version == 0:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS documents (
                        name TEXT PRIMARY KEY, layout BLOB NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS document_rows (
                        name TEXT NOT NULL, key TEXT NOT NULL, digest BLOB NOT NULL, body BLOB NOT NULL,
                        PRIMARY KEY(name,key)
                    ) WITHOUT ROWID;
                    CREATE TABLE IF NOT EXISTS streams (
                        id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, generation INTEGER NOT NULL DEFAULT 0, dictionary BLOB NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS stream_events (
                        name INTEGER NOT NULL, sequence INTEGER NOT NULL, turn_id TEXT,
                        kind TEXT NOT NULL, visible INTEGER NOT NULL, identity TEXT, body BLOB NOT NULL,
                        PRIMARY KEY(name,sequence)
                    );
                    CREATE TABLE IF NOT EXISTS stream_errors (
                        name INTEGER NOT NULL, line INTEGER NOT NULL, raw BLOB NOT NULL,
                        PRIMARY KEY(name,line)
                    ) WITHOUT ROWID;
                    CREATE INDEX IF NOT EXISTS stream_turn ON stream_events(name,turn_id,sequence);
                    CREATE INDEX IF NOT EXISTS stream_kind ON stream_events(name,kind,sequence);
                    CREATE INDEX IF NOT EXISTS stream_identity ON stream_events(name,identity) WHERE turn_id IS NOT NULL AND identity IS NOT NULL;
                """)
                db.execute("PRAGMA user_version=1")
        yield db
    except sqlite3.Error as exc:
        raise OSError(f"SQLite storage failed: {database}: {exc}") from exc
    finally:
        db.close()


def pack(value: Any, dictionary: bytes = b"") -> bytes:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    compressor = zlib.compressobj(6, zdict=dictionary)
    return compressor.compress(raw) + compressor.flush()


def unpack(value: bytes, dictionary: bytes = b"") -> Any:
    decompressor = zlib.decompressobj(zdict=dictionary)
    return json.loads(decompressor.decompress(value) + decompressor.flush())


def _parts(payload: Any) -> tuple[bytes, dict[str, bytes]]:
    """Partition collection entries so unchanged records never enter the WAL."""
    rows: dict[str, bytes] = {}
    root_list = isinstance(payload, list)
    if root_list:
        payload = {"": payload}
    if not isinstance(payload, dict):
        return pack(["value", payload]), rows
    layout: dict[str, Any] = {}
    for field, value in payload.items():
        if isinstance(value, (list, dict)):
            keys = []
            items = enumerate(value) if isinstance(value, list) else value.items()
            for index, item in items:
                # Lists with stable ids avoid rewriting subsequent rows on delete.
                identity = (
                    str(item["id"]) if isinstance(item, dict) and "id" in item else str(index)
                )
                key = json.dumps([field, identity])
                if key in rows:
                    key = json.dumps([field, str(index), identity])
                rows[key] = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()
                keys.append([index, key])
            layout[field] = ["list" if isinstance(value, list) else "dict", keys]
        else:
            layout[field] = ["value", value]
    return pack(["root-list" if root_list else "object", layout]), rows


def _write(db: sqlite3.Connection, name: str, payload: Any) -> None:
    layout, rows = _parts(payload)
    old = dict(db.execute("SELECT key,digest FROM document_rows WHERE name=?", (name,)))
    db.executemany(
        "DELETE FROM document_rows WHERE name=? AND key=?",
        [(name, k) for k in old.keys() - rows.keys()],
    )
    changed = []
    for key, value in rows.items():
        digest = hashlib.sha256(value).digest()
        if old.get(key) != digest:
            changed.append((name, key, digest, zlib.compress(value, 6)))
    db.executemany(
        "INSERT INTO document_rows VALUES (?,?,?,?) ON CONFLICT(name,key) DO UPDATE SET digest=excluded.digest, body=excluded.body",
        changed,
    )
    db.execute(
        "INSERT INTO documents VALUES (?,?) ON CONFLICT(name) DO UPDATE SET layout=excluded.layout WHERE layout<>excluded.layout",
        (name, layout),
    )


def document_exists(path: Path) -> bool:
    database, name = storage_location(path)
    if not database.exists():
        return path.exists()
    with connection(path) as db:
        row = db.execute("SELECT layout FROM documents WHERE name=?", (name,)).fetchone()
        return unpack(row[0])[0] != "deleted" if row else path.exists()


def read_document(path: Path) -> Any:
    _, name = storage_location(path)
    with connection(path) as db:
        with db:
            db.execute("BEGIN")
            record = db.execute("SELECT layout FROM documents WHERE name=?", (name,)).fetchone()
            if record is None:
                db.rollback()
                db.execute("BEGIN IMMEDIATE")
                record = db.execute("SELECT layout FROM documents WHERE name=?", (name,)).fetchone()
            if record is None:
                payload = json.loads(path.read_text(encoding="utf-8"))
                _write(db, name, payload)
                return payload
            kind, layout = unpack(record[0])
            if kind == "deleted":
                raise FileNotFoundError(path)
            if kind == "value":
                return layout
            rows = {
                key: unpack(body)
                for key, body in db.execute(
                    "SELECT key,body FROM document_rows WHERE name=?", (name,)
                )
            }
            result: dict[str, Any] = {}
            for field, (shape, value) in layout.items():
                if shape == "value":
                    result[field] = value
                elif shape == "list":
                    result[field] = [rows[key] for _, key in value]
                else:
                    result[field] = {index: rows[key] for index, key in value}
            return result[""] if kind == "root-list" else result


def write_document(path: Path, payload: Any) -> None:
    _, name = storage_location(path)
    with connection(path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _write(db, name, payload)


def has_stored_document(path: Path) -> bool:
    database, name = storage_location(path)
    if not database.exists():
        return False
    with connection(path) as db:
        return db.execute("SELECT 1 FROM documents WHERE name=?", (name,)).fetchone() is not None


def delete_document(path: Path) -> None:
    """Retire a logical document without allowing an old JSON source to revive it."""
    _, name = storage_location(path)
    with connection(path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM document_rows WHERE name=?", (name,))
        db.execute(
            "INSERT INTO documents VALUES (?,?) ON CONFLICT(name) DO UPDATE SET layout=excluded.layout",
            (name, pack(["deleted", None])),
        )
    path.unlink(missing_ok=True)


def retire_namespace(directory: Path) -> None:
    """Retire DB rows after a workspace owner has fenced all session resources."""
    _, prefix = storage_location(directory / ".namespace")
    prefix = prefix.removesuffix(".namespace")
    with connection(directory / ".namespace") as db, db:
        db.execute("BEGIN IMMEDIATE")
        names = [
            row[0]
            for row in db.execute(
                "SELECT name FROM documents WHERE substr(name,1,?)=?",
                (len(prefix), prefix),
            )
        ]
        for name in names:
            db.execute("DELETE FROM document_rows WHERE name=?", (name,))
            db.execute(
                "UPDATE documents SET layout=? WHERE name=?",
                (pack(["deleted", None]), name),
            )
        streams = [
            row[0]
            for row in db.execute(
                "SELECT id FROM streams WHERE substr(name,1,?)=?", (len(prefix), prefix)
            )
        ]
        for stream in streams:
            db.execute("DELETE FROM stream_events WHERE name=?", (stream,))
            db.execute("DELETE FROM stream_errors WHERE name=?", (stream,))
            db.execute("UPDATE streams SET generation=generation+1 WHERE id=?", (stream,))
