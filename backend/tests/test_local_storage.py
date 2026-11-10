"""SQLite's durable contract, independent of individual metadata consumers."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from claude_hub.services import local_storage as storage


@pytest.mark.parametrize(
    "payload",
    [
        None,
        True,
        42,
        1.25,
        "多语言\ntext",
        [],
        [{"id": "same", "value": 1}, {"id": "same", "value": 2}, "tail"],
        {},
        {
            "plain": None,
            "list": [0, False, {"id": "a", "nested": [1, {"你好": "世界"}]}],
            "mapping": {"a": [1, 2], "b": {"value": True}},
        },
    ],
)
def test_round_trip_preserves_document_shapes(tmp_path: Path, payload: Any) -> None:
    path = tmp_path / "metadata.json"
    storage.write_document(path, payload)
    assert storage.document_exists(path)
    assert storage.read_document(path) == payload
    assert not path.exists(), "SQLite writes must not maintain a JSON mirror"


def test_legacy_import_is_once_and_delete_tombstone_cannot_revive_source(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"original": [1, 2]}))
    assert storage.read_document(path) == {"original": [1, 2]}
    path.write_text(json.dumps({"stale": "external edit"}))
    assert storage.read_document(path) == {"original": [1, 2]}
    storage.delete_document(path)
    path.write_text(json.dumps({"stale": "restored backup"}))
    assert not storage.document_exists(path)
    with pytest.raises(FileNotFoundError):
        storage.read_document(path)
    storage.write_document(path, {"new": "explicit creation"})
    assert storage.read_document(path) == {"new": "explicit creation"}


def test_migration_interruption_rolls_back_and_can_retry(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "metadata.json"
    payload = {"rows": [{"id": "one", "value": "recover me"}]}
    source = json.dumps(payload)
    path.write_text(source)
    original = storage._write

    def interrupt(db, name, value):
        original(db, name, value)
        raise OSError("interrupted before import commit")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_write", interrupt)
        with pytest.raises(OSError, match="interrupted"):
            storage.read_document(path)
    assert not storage.has_stored_document(path)
    with storage.connection(path) as db:
        assert db.execute("SELECT count(*) FROM document_rows").fetchone()[0] == 0
    assert path.read_text() == source
    assert storage.read_document(path) == payload


def test_failed_write_does_not_publish_partial_rows(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "metadata.json"
    previous = {"rows": [{"id": "first", "value": 1}]}
    storage.write_document(path, previous)
    original = storage._write

    def interrupt(db, name, value):
        original(db, name, value)
        raise OSError("interrupted write")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_write", interrupt)
        with pytest.raises(OSError, match="interrupted"):
            storage.write_document(path, {"rows": [{"id": "second", "value": 2}]})
    assert storage.read_document(path) == previous


def test_identical_write_adds_no_wal_frames(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    payload = {"rows": [{"id": str(index), "body": "unchanged" * 100} for index in range(100)]}
    storage.write_document(path, payload)
    # Keep a connection alive so closing each writer cannot erase the WAL.
    with storage.connection(path) as observer:
        before_version = observer.execute("PRAGMA data_version").fetchone()[0]
        database, _ = storage.storage_location(path)
        wal = Path(str(database) + "-wal")
        before_size = wal.stat().st_size if wal.exists() else 0
        for _ in range(3):
            storage.write_document(path, payload)
        assert observer.execute("PRAGMA data_version").fetchone()[0] == before_version
        assert (wal.stat().st_size if wal.exists() else 0) == before_size


def test_concurrent_readers_never_observe_torn_document(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    storage.write_document(path, {"generation": 0, "rows": [{"generation": 0}] * 8})

    def writer(identity: int) -> None:
        for generation in range(1, 15):
            stamp = identity * 100 + generation
            storage.write_document(path, {"generation": stamp, "rows": [{"generation": stamp}] * 8})

    def reader() -> None:
        for _ in range(30):
            payload = storage.read_document(path)
            assert all(row["generation"] == payload["generation"] for row in payload["rows"])

    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(writer, identity) for identity in (1, 2)]
        futures.extend(workers.submit(reader) for _ in range(2))
        for future in futures:
            future.result(timeout=30)


def test_corrupt_database_fails_closed_instead_of_reimporting_legacy(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    path.write_text('{"stale": "must not revive"}')
    database, _ = storage.storage_location(path)
    database.write_bytes(b"not a SQLite database")
    with pytest.raises(OSError):
        storage.read_document(path)
    assert database.read_bytes() == b"not a SQLite database"
    assert path.read_text() == '{"stale": "must not revive"}'
