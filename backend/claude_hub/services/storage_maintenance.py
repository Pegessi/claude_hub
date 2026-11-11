"""Offline, verified legacy-source archival for the unified SQLite store.

Automatic imports retain original bytes for recovery. This explicit maintenance
step replaces those stale sources with owner-only gzip archives after SQLite has
committed them. It never operates while a backend owns the runtime directory.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable

from ..models import AgentStreamEvent
from .agent_stream.history import conversation_event_visible, replay_tool_identity
from .backend_instance_lock import BackendInstanceLock
from .local_storage import (
    connection,
    document_exists,
    has_stored_document,
    pack,
    storage_location,
    unpack,
    write_document,
)
from .runtime_isolation import resolve_state_root

TOP_LEVEL_DOCUMENTS = (
    "tabs.json",
    "tab_order.json",
    "sessions.json",
    "chat_messages.json",
    "goal_runs.json",
    "subagents.json",
    "feishu_bot.json",
    "secrets/feishu_bot_pool.json",
    "secrets/feishu_bot.json",
)


def legacy_sources(root: Path, *, workspace_root: Path | None = None) -> list[Path]:
    """Bounded inventory of migrated stores, including old rotating backups."""
    workspace_root = workspace_root or root / "workspaces"
    documents = [root / name for name in TOP_LEVEL_DOCUMENTS]
    documents += [workspace_root / "index.json", workspace_root / "scheduled_tasks.json"]
    documents += sorted(workspace_root.glob("*/state.json"))
    documents += sorted(workspace_root.glob("*/agent_streams/*.cursor.json"))
    backups = [path.with_name(path.name + ".bak") for path in documents]
    backups += sorted(workspace_root.glob("*/state.json.bak"))
    backups += sorted(workspace_root.glob("*/agent_streams/*.cursor.json.bak"))
    paths = list(dict.fromkeys([*documents, *backups]))
    paths += sorted(workspace_root.glob("*/agent_streams/*.jsonl"))
    return [path for path in paths if path.is_file()]


def _digest(path: Path, *, compressed: bool = False) -> bytes:
    digest = hashlib.sha256()
    opener = gzip.open if compressed else open
    with opener(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def archive_sources(paths: Iterable[Path]) -> int:
    """Archive committed sources without silent overwrite or uncertain deletion.

    Caller holds the runtime's backend lock. Corrupt stream lines are included
    byte-for-byte in the archive as well as SQLite's stream_errors table.
    """
    total = 0
    for path in paths:
        primary = (
            path.with_name(path.name.removesuffix(".bak")) if path.name.endswith(".bak") else path
        )
        if primary.suffix == ".jsonl":
            with connection(primary) as db:
                _, name = storage_location(primary)
                if db.execute("SELECT 1 FROM streams WHERE name=?", (name,)).fetchone() is None:
                    raise ValueError(f"stream has not been imported: {primary}")
        elif not has_stored_document(primary):
            raise ValueError(f"document has not been imported: {primary}")
        target = path.with_name(path.name + ".gz")
        original = _digest(path)
        if not target.exists():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=target.name + ".", suffix=".tmp", dir=target.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as raw:
                    with (
                        gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as archive,
                        path.open("rb") as source,
                    ):
                        shutil.copyfileobj(source, archive)
                    raw.flush()
                    os.fsync(raw.fileno())
                if _digest(temporary, compressed=True) != original or _digest(path) != original:
                    raise ValueError(f"source changed during archive: {path}")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        if _digest(target, compressed=True) != original or _digest(path) != original:
            raise ValueError(f"archive does not match source: {path}")
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
            path.unlink()
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        total += target.stat().st_size
    return total


def _last_complete_line(path: Path) -> bytes | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    with path.open("rb") as handle:
        pos = size
        buf = b""
        while pos > 0:
            step = min(65536, pos)
            pos -= step
            handle.seek(pos)
            buf = handle.read(step) + buf
            if not buf.endswith(b"\n"):
                idx = buf.rfind(b"\n")
                if idx == -1:
                    buf = b""
                    continue
                buf = buf[: idx + 1]
            lines = [line for line in buf.splitlines() if line.strip()]
            if pos == 0 and lines:
                return lines[-1]
            if len(lines) >= 2 or (lines and buf.startswith(b"\n")):
                return lines[-1]
    return None


def _insert_stream_event(
    db: sqlite3.Connection, stream_id: int, dictionary: bytes, event: AgentStreamEvent
) -> None:
    identity = replay_tool_identity(event)
    replay = (
        not event.turn_id
        and identity
        and db.execute(
            "SELECT 1 FROM stream_events WHERE name=? AND identity=? AND turn_id IS NOT NULL LIMIT 1",
            (stream_id, identity),
        ).fetchone()
    )
    db.execute(
        "INSERT INTO stream_events VALUES (?,?,?,?,?,?,?)",
        (
            stream_id,
            event.stream_sequence,
            event.turn_id,
            event.type.value,
            int(conversation_event_visible(event) and not replay),
            identity,
            pack(
                event.model_dump(mode="json", exclude_defaults=True, exclude_none=True),
                dictionary,
            ),
        ),
    )


def _sync_stream_file(path: Path) -> int:
    """Import or delta-sync a legacy JSONL stream into SQLite without deleting the source."""
    _, logical_name = storage_location(path)
    last_line = _last_complete_line(path)
    last_valid: AgentStreamEvent | None = None
    if last_line is not None:
        try:
            candidate = AgentStreamEvent.model_validate_json(last_line)
            if candidate.stream_sequence >= 0:
                last_valid = candidate
        except ValueError:
            last_valid = None

    with connection(path) as db:
        row = db.execute(
            "SELECT id,generation,dictionary FROM streams WHERE name=?", (logical_name,)
        ).fetchone()
        if row is not None:
            stream_id, generation, dictionary = int(row[0]), int(row[1]), bytes(row[2])
            if generation > 0:
                return 0
            max_row = db.execute(
                "SELECT sequence,body FROM stream_events WHERE name=? ORDER BY sequence DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
            err_count = int(
                db.execute(
                    "SELECT count(*) FROM stream_errors WHERE name=?", (stream_id,)
                ).fetchone()[0]
            )
            if max_row is None and err_count == 0 and last_line is None:
                return 0
            if max_row is not None and last_valid is not None:
                db_max_seq, db_last_body = int(max_row[0]), bytes(max_row[1])
                expected_dump = last_valid.model_dump(
                    mode="json", exclude_defaults=True, exclude_none=True
                )
                if (
                    last_valid.stream_sequence == db_max_seq
                    and unpack(db_last_body, dictionary) == expected_dump
                ):
                    return 0

        try:
            with path.open("rb") as source:
                raw_dict = source.read(32768)
                source.seek(0)
                complete_lines: list[tuple[int, bytes]] = []
                for number, line in enumerate(source, 1):
                    if not line.endswith(b"\n"):
                        break
                    if line.strip():
                        complete_lines.append((number, line))
        except OSError:
            return 0

        parsed_lines: list[tuple[int, AgentStreamEvent | None, bytes]] = []
        by_seq: dict[int, AgentStreamEvent] = {}
        for number, line in complete_lines:
            try:
                parsed = AgentStreamEvent.model_validate_json(line)
                if parsed.stream_sequence < 0 or parsed.stream_sequence in by_seq:
                    raise ValueError("invalid or duplicate stream sequence")
                by_seq[parsed.stream_sequence] = parsed
                parsed_lines.append((number, parsed, line))
            except ValueError:
                parsed_lines.append((number, None, line))

        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,generation,dictionary FROM streams WHERE name=?",
                (logical_name,),
            ).fetchone()
            if row is None:
                dictionary = raw_dict or path.stem.encode()
                stream_id = int(
                    db.execute(
                        "INSERT INTO streams(name,dictionary) VALUES (?,?)",
                        (logical_name, dictionary),
                    ).lastrowid
                    or 0
                )
                written = 0
                for number, ev, line in parsed_lines:
                    if ev is not None:
                        _insert_stream_event(db, stream_id, dictionary, ev)
                    else:
                        db.execute(
                            "INSERT INTO stream_errors VALUES (?,?,?)",
                            (stream_id, number, line),
                        )
                    written += 1
                return written

            stream_id, generation, dictionary = int(row[0]), int(row[1]), bytes(row[2])
            if generation > 0:
                return 0
            max_row = db.execute(
                "SELECT sequence,body FROM stream_events WHERE name=? ORDER BY sequence DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
            max_err_line = int(
                db.execute(
                    "SELECT coalesce(max(line),0) FROM stream_errors WHERE name=?",
                    (stream_id,),
                ).fetchone()[0]
            )
            db_count = int(
                db.execute(
                    "SELECT count(*) FROM stream_events WHERE name=?", (stream_id,)
                ).fetchone()[0]
            ) + int(
                db.execute(
                    "SELECT count(*) FROM stream_errors WHERE name=?", (stream_id,)
                ).fetchone()[0]
            )

            append_only = False
            if max_row is None:
                append_only = db_count == 0 or (
                    db_count <= len(parsed_lines)
                    and all(ev is None for _, ev, _ in parsed_lines[:db_count])
                )
                db_max_seq = -1
            else:
                db_max_seq, db_last_body = int(max_row[0]), bytes(max_row[1])
                matching = by_seq.get(db_max_seq)
                if matching is not None and db_count <= len(parsed_lines):
                    expected = matching.model_dump(
                        mode="json", exclude_defaults=True, exclude_none=True
                    )
                    append_only = unpack(db_last_body, dictionary) == expected

            if append_only:
                written = 0
                for number, ev, line in parsed_lines:
                    if ev is not None:
                        if ev.stream_sequence > db_max_seq:
                            _insert_stream_event(db, stream_id, dictionary, ev)
                            written += 1
                    elif number > max_err_line:
                        db.execute(
                            "INSERT OR IGNORE INTO stream_errors VALUES (?,?,?)",
                            (stream_id, number, line),
                        )
                        written += 1
                return written

            # Legacy file was rewritten/truncated on disk prior to SQLite cutover.
            dictionary = raw_dict or path.stem.encode()
            db.execute("UPDATE streams SET dictionary=? WHERE id=?", (dictionary, stream_id))
            db.execute("DELETE FROM stream_events WHERE name=?", (stream_id,))
            db.execute("DELETE FROM stream_errors WHERE name=?", (stream_id,))
            written = 0
            for number, ev, line in parsed_lines:
                if ev is not None:
                    _insert_stream_event(db, stream_id, dictionary, ev)
                else:
                    db.execute(
                        "INSERT INTO stream_errors VALUES (?,?,?)",
                        (stream_id, number, line),
                    )
                written += 1
            return written


def sync_legacy_sources(paths: Iterable[Path]) -> dict[str, int]:
    """Non-destructively import or delta-sync legacy JSON/JSONL files into SQLite."""
    source_list = list(paths)
    primary_docs = [p for p in source_list if p.suffix == ".json" and not p.name.endswith(".bak")]
    backup_docs = [p for p in source_list if p.name.endswith(".json.bak")]
    stream_files = [p for p in source_list if p.suffix == ".jsonl"]

    synced_docs = 0
    for path in primary_docs:
        if has_stored_document(path) and not document_exists(path):
            continue
        payload: Any = None
        loaded = False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            loaded = True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            bak = path.with_name(path.name + ".bak")
            if not has_stored_document(path) and bak.is_file():
                try:
                    payload = json.loads(bak.read_text(encoding="utf-8"))
                    loaded = True
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    loaded = False
        if loaded:
            write_document(path, payload)
            synced_docs += 1

    for bak_path in backup_docs:
        primary = bak_path.with_name(bak_path.name.removesuffix(".bak"))
        if primary.exists() or has_stored_document(primary):
            continue
        try:
            payload = json.loads(bak_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        write_document(primary, payload)
        synced_docs += 1

    synced_streams = 0
    synced_events = 0
    for path in stream_files:
        written = _sync_stream_file(path)
        synced_streams += 1
        synced_events += written

    return {
        "documents": synced_docs,
        "streams": synced_streams,
        "stream_rows_written": synced_events,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--pre-import",
        "--sync-legacy",
        dest="sync_legacy",
        action="store_true",
        help="Import or delta-sync legacy JSON/JSONL files into SQLite without deleting sources.",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    os.environ["CLAUDE_HUB_HOME"] = str(root)
    workspace_root = resolve_state_root()
    sources = legacy_sources(root, workspace_root=workspace_root)
    if args.sync_legacy:
        summary = sync_legacy_sources(sources)
        print(
            f"Synced documents: {summary['documents']}, "
            f"streams: {summary['streams']}, "
            f"stream_rows_written: {summary['stream_rows_written']}"
        )
        return
    with BackendInstanceLock(root / "backend.lock"):
        print(f"Archived bytes: {archive_sources(sources)}")


if __name__ == "__main__":
    main()
