#!/usr/bin/env python3
"""Copy bounded Hub persistence inputs, migrate, and benchmark without live writes.

Run with the feature backend interpreter; output contains sizes/timings only.
Legacy sources remain recovery evidence. --archive compresses sources in the
COPY after verified import, making backup overhead explicit rather than hiding it.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import statistics
import time
from itertools import zip_longest
from pathlib import Path
from typing import Any, Callable

from claude_hub.models import AgentStreamEvent
from claude_hub.services.agent_stream.store import AgentStreamStore
from claude_hub.services.local_storage import (
    connection,
    read_document,
    storage_location,
    write_document,
)
from claude_hub.services.storage_maintenance import archive_sources, legacy_sources


def elapsed(operation: Callable[[], Any], repeats: int = 7) -> float:
    values = []
    for _ in range(repeats):
        start = time.perf_counter()
        operation()
        values.append((time.perf_counter() - start) * 1000)
    return statistics.median(values)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--archive", action="store_true")
    parser.add_argument("--reuse-copy", action="store_true")
    parser.add_argument("--verify-replay", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve()
    if not destination.is_relative_to(Path("/tmp")) or destination.is_relative_to(source):
        parser.error("destination must be below /tmp outside source")
    manifest_path = destination / "snapshot-manifest.json"
    copied = []
    if args.reuse_copy:
        manifest = json.loads(manifest_path.read_text())
        for item in manifest:
            target = destination / item["path"]
            if not target.resolve().is_relative_to(destination):
                parser.error("invalid snapshot path")
            if not target.exists():
                with (
                    gzip.open(target.with_name(target.name + ".gz"), "rb") as src,
                    target.open("xb") as dst,
                ):
                    shutil.copyfileobj(src, dst)
                target.chmod(0o600)
            with target.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != item["sha256"]:
                    parser.error("snapshot source changed")
            copied.append(target)
    else:
        destination.mkdir(mode=0o700)
        manifest = []
        for path in legacy_sources(source):
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, target)
            target.chmod(0o600)
            copied.append(target)
            with target.open("rb") as handle:
                manifest.append(
                    {
                        "path": str(target.relative_to(destination)),
                        "sha256": hashlib.file_digest(handle, "sha256").hexdigest(),
                    }
                )
        manifest_path.write_text(json.dumps(manifest, indent=2))
    before = sum(path.stat().st_size for path in copied)
    documents = [path for path in copied if path.suffix == ".json"]
    streams = [path for path in copied if path.suffix == ".jsonl"]
    start = time.perf_counter()
    for path in documents:
        read_document(path)
    errors = 0
    total_events = 0
    for index, path in enumerate(streams):
        store = AgentStreamStore(path.parent.parent.name, path.stem)
        store._path = path
        store._dir = path.parent
        store._read_rows(limit=0)
        with connection(path) as db:
            _, logical_name = storage_location(path)
            name = db.execute("SELECT id FROM streams WHERE name=?", (logical_name,)).fetchone()[0]
            total_events += db.execute(
                "SELECT count(*) FROM stream_events WHERE name=?", (name,)
            ).fetchone()[0]
            errors += db.execute(
                "SELECT count(*) FROM stream_errors WHERE name=?", (name,)
            ).fetchone()[0]
        if index % 25 == 0:
            print(json.dumps({"migrated_streams": index + 1}), flush=True)
    migration_seconds = time.perf_counter() - start
    replay_verified = 0
    if args.verify_replay:
        for path in streams:
            candidate = AgentStreamStore(path.parent.parent.name, path.stem)
            candidate._path = path

            def expected_events():
                seen = set()
                with path.open("rb") as handle:
                    for line in handle:
                        try:
                            event = AgentStreamEvent.model_validate_json(line)
                        except ValueError:
                            continue
                        if event.stream_sequence < 0 or event.stream_sequence in seen:
                            continue
                        seen.add(event.stream_sequence)
                        yield event

            for expected, actual in zip_longest(expected_events(), candidate.iter_events()):
                if expected != actual:
                    raise ValueError(f"replay differs from source: {path}")
                replay_verified += 1
    largest = max(
        (path for path in documents if path.name == "state.json"),
        key=lambda path: path.stat().st_size,
    )
    payload = json.loads(largest.read_text())
    baseline = destination / "baseline-state.json"
    baseline.write_text(largest.read_text())

    revision = 0
    original_title = payload["tasks"][0]["title"]

    def change_one_task() -> None:
        nonlocal revision
        revision += 1
        payload["tasks"][0]["title"] = f"{original_title} [benchmark {revision}]"

    def json_write() -> None:
        change_one_task()
        shutil.copyfile(baseline, baseline.with_suffix(".bak"))
        with baseline.open("w") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())

    def sqlite_write() -> None:
        change_one_task()
        write_document(largest, payload)

    table = {
        "workspace_read_json_ms": elapsed(lambda: json.loads(largest.read_text())),
        "workspace_read_sqlite_ms": elapsed(lambda: read_document(largest)),
        "workspace_save_json_ms": elapsed(json_write),
        "workspace_save_sqlite_ms": elapsed(sqlite_write),
        "workspace_unchanged_sqlite_ms": elapsed(lambda: write_document(largest, payload)),
    }
    payload["tasks"][0]["title"] = original_title
    write_document(largest, payload)
    largest_stream = max(streams, key=lambda path: path.stat().st_size)
    store = AgentStreamStore(largest_stream.parent.parent.name, largest_stream.stem)
    store._path = largest_stream
    store._dir = largest_stream.parent
    last = store._read_rows()
    cursor = max(-1, last[-1].stream_sequence - 100) if last else -1
    del last

    def json_page() -> None:
        page = []
        for line in largest_stream.open("rb"):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("stream_sequence", -1) > cursor:
                page.append(item)
            if len(page) >= 100:
                break

    table["stream_tail_json_ms"] = elapsed(json_page, 3)
    table["stream_tail_sqlite_ms"] = elapsed(lambda: store._read_rows(cursor, 100))
    # Reimport is no-op even if source content is later unavailable.
    start = time.perf_counter()
    for path in streams:
        candidate = AgentStreamStore(path.parent.parent.name, path.stem)
        candidate._path = path
        candidate._read_rows(limit=0)
    rerun_seconds = time.perf_counter() - start
    database, _ = storage_location(largest)
    with connection(largest) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    baseline.unlink()
    baseline.with_suffix(".bak").unlink()
    active_bytes = database.stat().st_size
    archived = 0
    if args.archive:
        archived = archive_sources(copied)
    result = {
        "source_bytes": before,
        "active_sqlite_bytes": active_bytes,
        "retained_source_bytes": 0 if args.archive else before,
        "compressed_backup_bytes": archived,
        "total_after_bytes": active_bytes + (archived if args.archive else before),
        "total_reduction_percent": round(
            (1 - (active_bytes + (archived if args.archive else before)) / before) * 100,
            2,
        ),
        "active_reduction_percent": round((1 - active_bytes / before) * 100, 2),
        "valid_events": total_events,
        "replay_verified_events": replay_verified,
        "malformed_preserved_rows": errors,
        "migration_seconds": migration_seconds,
        "idempotent_rerun_seconds": rerun_seconds,
        "integrity_check": integrity,
        "latency": table,
    }
    (destination / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
