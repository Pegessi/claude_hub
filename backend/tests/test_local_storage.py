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


def test_legacy_import_is_once_and_delete_tombstone_cannot_revive_source(
    tmp_path: Path,
) -> None:
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


def test_corrupt_database_fails_closed_instead_of_reimporting_legacy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "metadata.json"
    path.write_text('{"stale": "must not revive"}')
    database, _ = storage.storage_location(path)
    database.write_bytes(b"not a SQLite database")
    with pytest.raises(OSError):
        storage.read_document(path)
    assert database.read_bytes() == b"not a SQLite database"
    assert path.read_text() == '{"stale": "must not revive"}'


def test_custom_state_root_uses_runtime_database(tmp_path, monkeypatch):
    home = tmp_path / "runtime"
    state = tmp_path / "custom-state"
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(home))
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(state))
    expected = home / "storage.sqlite3"
    for path in (
        home / "tabs.json",
        state / "index.json",
        state / "ws" / "state.json",
        state / "ws" / "agent_streams" / "session.jsonl",
    ):
        assert storage.storage_location(path)[0] == expected


def test_stream_wal_reader_does_not_reserve_writer(tmp_path, monkeypatch):
    import importlib

    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    from claude_hub.services.agent_stream.store import AgentStreamStore

    root = tmp_path / "workspaces"
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", root)
    store = AgentStreamStore("ws", "session")
    # First read imports only once; subsequent snapshots must coexist with writes.
    store._read_rows(limit=0)
    document = tmp_path / "tabs.json"
    with store._read_database():
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(storage.write_document, document, {"value": 1}).result(timeout=2)
    assert storage.read_document(document) == {"value": 1}


def test_verified_archive_preserves_legacy_and_reopens_sqlite(tmp_path):
    import gzip

    from claude_hub.services.storage_maintenance import archive_sources

    path = tmp_path / "sessions.json"
    raw = b'{"session":{"value":"original"}}'
    path.write_bytes(raw)
    assert storage.read_document(path) == json.loads(raw)
    archive_sources([path])
    assert not path.exists()
    assert gzip.decompress(path.with_name(path.name + ".gz").read_bytes()) == raw
    assert storage.read_document(path) == json.loads(raw)
    assert archive_sources([]) == 0


def test_archive_refuses_conflicting_backup_without_deleting_source(tmp_path):
    import gzip

    from claude_hub.services.storage_maintenance import archive_sources

    path = tmp_path / "sessions.json"
    path.write_text('{"value":1}')
    storage.read_document(path)
    path.with_name(path.name + ".gz").write_bytes(gzip.compress(b'{"value":2}'))
    with pytest.raises(ValueError, match="archive does not match"):
        archive_sources([path])
    assert path.read_text() == '{"value":1}'


def test_external_workspace_roots_and_aliases_keep_independent_namespaces(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from claude_hub.models import AgentStreamEvent
    from claude_hub.services.agent_stream.store import AgentStreamStore

    home = tmp_path / "runtime"
    root_a = tmp_path / "state-a"
    root_b = tmp_path / "state-b"
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(home))
    stores = {}
    keys = {}
    for label, root in (("A", root_a), ("B", root_b)):
        root.mkdir()
        monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(root))
        (root / "index.json").write_text(json.dumps({"label": label}))
        assert storage.read_document(root / "index.json") == {"label": label}
        storage.write_document(root / "ws" / "state.json", {"label": label})
        store = AgentStreamStore("ws", "same-session")
        store._path = root / "ws" / "agent_streams" / "same-session.jsonl"
        store._dir = store._path.parent
        store._replace_rows(
            [
                AgentStreamEvent(
                    stream_sequence=0,
                    session_id="same-session",
                    tab_id="tab",
                    agent_type="codex",
                    type="text_delta",
                    payload={"text": label},
                    created_at=datetime.now(timezone.utc),
                )
            ]
        )
        stores[label] = store
        database, keys[label] = storage.storage_location(root / "index.json")
        assert database == home / "storage.sqlite3"
    assert keys["A"] != keys["B"]
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(root_a))
    assert storage.read_document(root_a / "index.json") == {"label": "A"}
    assert stores["A"]._read_rows()[0].payload["text"] == "A"
    alias = tmp_path / "state-alias"
    alias.symlink_to(root_a, target_is_directory=True)
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(alias))
    assert storage.storage_location(alias / "index.json")[1] == keys["A"]
    assert storage.storage_location(root_a / "index.json")[1] == keys["A"]
    assert storage.read_document(alias / "index.json") == {"label": "A"}
    storage.retire_namespace(alias / "ws")
    assert stores["A"]._read_rows() == []
    assert not storage.document_exists(alias / "ws" / "state.json")
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(root_b))
    assert storage.read_document(root_b / "index.json") == {"label": "B"}
    assert storage.read_document(root_b / "ws" / "state.json") == {"label": "B"}
    assert stores["B"]._read_rows()[0].payload["text"] == "B"


def test_offline_archive_honors_external_workspace_root(tmp_path, monkeypatch):
    from claude_hub.services import storage_maintenance

    home = tmp_path / "runtime"
    selected = tmp_path / "selected-state"
    other = tmp_path / "other-state"
    selected.mkdir()
    other.mkdir()
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(home))
    monkeypatch.setenv("CLAUDE_HUB_STATE_ROOT", str(selected))
    for path in (selected / "index.json", other / "index.json"):
        path.write_text('{"workspaces":[]}')
    storage.read_document(selected / "index.json")
    monkeypatch.setattr("sys.argv", ["storage_maintenance", "--root", str(home)])
    storage_maintenance.main()
    assert not (selected / "index.json").exists()
    assert (selected / "index.json.gz").is_file()
    assert (other / "index.json").read_text() == '{"workspaces":[]}'
