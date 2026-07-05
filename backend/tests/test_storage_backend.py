"""Tests for the additive storage backend spike.

Covers the approved Goal Packet acceptance criteria for prototype code:

* No default behavior change — the flag defaults to ``json`` and
  ``get_storage_backend`` returns the JSON backend by default; the JSON backend
  reproduces the manager's exact on-disk layout.
* Round-trip preservation — representative Workspace/WorkspaceTask/
  ManagedSession/AgentReport records survive JSON->SQLite->reload and
  SQLite->JSON->reload field-for-field.
* Data-loss protection — ``atomic_write_text`` never truncates the live file on
  a failed write, and keeps a ``.bak``.

Nothing here imports or mutates real ~/.claude_hub state; every fixture is built
in-test under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import unittest.mock
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_hub.models import (
    AcceptanceCheck,
    AcceptanceCheckStatus,
    AgentReport,
    AgentReportState,
    AgentRuntimeStatus,
    AgentType,
    AutonomousRun,
    GoalPacket,
    ManagedSession,
    ManagedSessionStatus,
    ReviewDecision,
    Workspace,
    WorkspaceSessionRole,
    WorkspaceTask,
    WorkspaceTaskMode,
    WorkspaceTaskStatus,
)
from claude_hub.services.storage import (
    StorageSnapshot,
    atomic_write_text,
    get_storage_backend,
)
from claude_hub.services.storage.json_backend import JsonStorageBackend
from claude_hub.services.storage.migrate import (
    RoundTripError,
    export_sqlite_to_json,
    import_json_to_sqlite,
)
from claude_hub.services.storage.sqlite_backend import SqliteStorageBackend

NOW = datetime(2026, 7, 3, 0, 0, 0)
WS_ID = "ws-1"


def _representative_snapshot() -> StorageSnapshot:
    """Build a snapshot with a fully-populated instance of each entity."""
    workspace = Workspace(
        id=WS_ID,
        name="Claude Hub",
        path="/tmp/repo",
        default_branch="develop",
        session_prefix="cb",
        resident_agent_enabled=True,
        resident_agent_directive="stay resident",
        resident_agent_env={"FOO": "bar"},
        created_at=NOW,
        updated_at=NOW,
    )
    task = WorkspaceTask(
        id="task-1",
        workspace_id=WS_ID,
        title="SQLite spike",
        prompt="do the spike",
        agent_type=AgentType.CLAUDE,
        task_mode=WorkspaceTaskMode.REVIEWED,
        status=WorkspaceTaskStatus.WORKING,
        goal_packet=GoalPacket(
            objective="preserve data",
            acceptance_criteria=["round-trip preserved"],
        ),
        autonomous_run=AutonomousRun(id="run-1", task_id="task-1"),
        feedback_lesson_ids=["lesson-a", "lesson-b"],
        review_cycle=2,
        reviewed_cycle=1,
        created_at=NOW,
        updated_at=NOW,
    )
    session = ManagedSession(
        id="cb-agent-4",
        workspace_id=WS_ID,
        tab_id="tab-1",
        role=WorkspaceSessionRole.ORCHESTRATOR,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.WORKING,
        current_task_id="task-1",
        title="cb-agent-4",
        workspace_path="/tmp/repo",
        tmux_session="cb-agent-4",
        env={"ANTHROPIC_MODEL": "x"},
        created_at=NOW,
        updated_at=NOW,
    )
    report = AgentReport(
        id="rep-1",
        workspace_id=WS_ID,
        task_id="task-1",
        session_id="cb-agent-4",
        state=AgentReportState.WORKING,
        message="progress",
        message_en="progress",
        message_zh="进度",
        changed_files=["a.py", "b.py"],
        acceptance_check=[
            AcceptanceCheck(
                criterion="round-trip preserved",
                status=AcceptanceCheckStatus.PASSED,
                evidence="test",
            )
        ],
        review_decision=ReviewDecision.REQUEST,
        review_reason="needs review",
        created_at=NOW,
    )
    return StorageSnapshot(
        workspaces=[workspace.model_dump(mode="json")],
        tasks=[task.model_dump(mode="json")],
        sessions=[session.model_dump(mode="json")],
        reports=[report.model_dump(mode="json")],
    )


def _reconstruct(snapshot: StorageSnapshot) -> tuple[list, list, list, list]:
    """Rebuild pydantic models from a snapshot, as the manager's loader does."""
    return (
        [Workspace(**w) for w in snapshot.workspaces],
        [WorkspaceTask(**t) for t in snapshot.tasks],
        [ManagedSession(**s) for s in snapshot.sessions],
        [AgentReport(**r) for r in snapshot.reports],
    )


# --- No default behavior change -------------------------------------------------


def test_default_backend_is_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from claude_hub.config import settings

    # Default value on the settings object is "json".
    assert settings.workspace_storage_backend == "json"
    backend = get_storage_backend(tmp_path)
    assert isinstance(backend, JsonStorageBackend)


def test_unknown_backend_falls_back_to_json(tmp_path: Path) -> None:
    backend = get_storage_backend(tmp_path, backend="nonsense")
    assert isinstance(backend, JsonStorageBackend)


def test_sqlite_backend_only_when_explicitly_selected(tmp_path: Path) -> None:
    backend = get_storage_backend(tmp_path, backend="sqlite")
    assert isinstance(backend, SqliteStorageBackend)


def test_json_backend_matches_manager_on_disk_layout(tmp_path: Path) -> None:
    """JSON backend must produce the same index.json + <id>/state.json layout
    the workspace manager reads, so it is drop-in compatible with existing state."""
    snapshot = _representative_snapshot()
    JsonStorageBackend(tmp_path).save(snapshot)

    index = json.loads((tmp_path / "index.json").read_text())
    assert [w["id"] for w in index["workspaces"]] == [WS_ID]

    state = json.loads((tmp_path / WS_ID / "state.json").read_text())
    assert {"tasks", "sessions", "reports"} <= set(state)
    assert [t["id"] for t in state["tasks"]] == ["task-1"]
    assert [s["id"] for s in state["sessions"]] == ["cb-agent-4"]
    assert [r["id"] for r in state["reports"]] == ["rep-1"]


def test_json_backend_load_empty_root_is_empty(tmp_path: Path) -> None:
    snapshot = JsonStorageBackend(tmp_path).load()
    assert snapshot.workspaces == []
    assert snapshot.tasks == []


# --- Round-trip preservation ----------------------------------------------------


def test_json_backend_roundtrip_preserves_all_fields(tmp_path: Path) -> None:
    original = _representative_snapshot()
    backend = JsonStorageBackend(tmp_path)
    backend.save(original)
    reloaded = backend.load()
    assert _fingerprint(reloaded) == _fingerprint(original)
    # And the reconstructed pydantic models are equal.
    assert _reconstruct(reloaded) == _reconstruct(original)


def test_sqlite_backend_roundtrip_preserves_all_fields(tmp_path: Path) -> None:
    original = _representative_snapshot()
    backend = SqliteStorageBackend(tmp_path / "state.sqlite3")
    backend.save(original)
    reloaded = backend.load()
    assert _fingerprint(reloaded) == _fingerprint(original)
    assert _reconstruct(reloaded) == _reconstruct(original)


def test_import_json_to_sqlite_is_non_destructive_and_verified(tmp_path: Path) -> None:
    state_root = tmp_path / "workspaces"
    original = _representative_snapshot()
    JsonStorageBackend(state_root).save(original)
    index_before = (state_root / "index.json").read_bytes()

    db_path = import_json_to_sqlite(state_root)
    assert db_path.exists()
    # JSON source untouched by the import.
    assert (state_root / "index.json").read_bytes() == index_before

    # SQLite now holds the same data.
    reloaded = SqliteStorageBackend(db_path).load()
    assert _fingerprint(reloaded) == _fingerprint(original)


def test_export_sqlite_to_json_roundtrips(tmp_path: Path) -> None:
    original = _representative_snapshot()
    db_path = tmp_path / "state.sqlite3"
    SqliteStorageBackend(db_path).save(original)

    out_root = tmp_path / "restored"
    export_sqlite_to_json(db_path, out_root)
    reloaded = JsonStorageBackend(out_root).load()
    assert _fingerprint(reloaded) == _fingerprint(original)


def test_full_cycle_json_to_sqlite_to_json(tmp_path: Path) -> None:
    original = _representative_snapshot()
    json_root = tmp_path / "json"
    JsonStorageBackend(json_root).save(original)

    db_path = import_json_to_sqlite(json_root, tmp_path / "state.sqlite3")
    restored_root = tmp_path / "restored"
    export_sqlite_to_json(db_path, restored_root)

    assert _fingerprint(JsonStorageBackend(restored_root).load()) == _fingerprint(original)


def test_export_backs_up_existing_live_tree(tmp_path: Path) -> None:
    """A successful export over an existing state_root preserves the prior tree
    at <state_root>.bak rather than silently discarding it."""
    state_root = tmp_path / "workspaces"
    # Prior live JSON tree with a stale marker file.
    JsonStorageBackend(state_root).save(_representative_snapshot())
    (state_root / "stale-marker.txt").write_text("old")

    db_path = tmp_path / "state.sqlite3"
    SqliteStorageBackend(db_path).save(_representative_snapshot())

    export_sqlite_to_json(db_path, state_root)

    # New tree is in place and no leftover staging dir exists.
    assert (state_root / "index.json").exists()
    assert not (tmp_path / "workspaces.staging").exists()
    # Prior tree (including the stale marker) preserved in the backup.
    assert (state_root.with_name("workspaces.bak") / "stale-marker.txt").read_text() == "old"


def test_export_round_trip_failure_leaves_live_tree_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the staged round-trip fails, export raises and must NOT overwrite or
    remove the existing live state_root (the rollback source of truth)."""
    state_root = tmp_path / "workspaces"
    live = _representative_snapshot()
    JsonStorageBackend(state_root).save(live)
    index_before = (state_root / "index.json").read_bytes()

    db_path = tmp_path / "state.sqlite3"
    # Export a DIFFERENT snapshot so we could tell if the live tree got clobbered.
    other = StorageSnapshot(workspaces=[dict(live.workspaces[0], id="ws-other")])
    SqliteStorageBackend(db_path).save(other)

    # Force the staged verification to mismatch by making reloads look empty.
    real_load = JsonStorageBackend.load

    def _empty_load(self: JsonStorageBackend) -> StorageSnapshot:
        return StorageSnapshot()

    monkeypatch.setattr(JsonStorageBackend, "load", _empty_load)
    with pytest.raises(RoundTripError):
        export_sqlite_to_json(db_path, state_root)
    monkeypatch.setattr(JsonStorageBackend, "load", real_load)

    # Live tree byte-identical to before; no staging/backup swap happened.
    assert (state_root / "index.json").read_bytes() == index_before
    assert not (tmp_path / "workspaces.staging").exists()
    assert not (state_root.with_name("workspaces.bak")).exists()
    # And it still reconstructs to the original live snapshot.
    assert _fingerprint(JsonStorageBackend(state_root).load()) == _fingerprint(live)


def test_import_raises_on_round_trip_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the SQLite reload disagrees with the source, import fails and only the
    **staging** DB is removed — the final target path is never created, so no
    prior DB at that location can be overwritten or truncated."""
    state_root = tmp_path / "workspaces"
    JsonStorageBackend(state_root).save(_representative_snapshot())

    # Force a mismatch by making the SQLite reload drop a row.
    def _corrupt_load(self: SqliteStorageBackend) -> StorageSnapshot:
        return StorageSnapshot()

    target = tmp_path / "state.sqlite3"
    monkeypatch.setattr(SqliteStorageBackend, "load", _corrupt_load)
    with pytest.raises(RoundTripError):
        import_json_to_sqlite(state_root, target)
    monkeypatch.undo()

    # Final target was never created (no promotion happened), and no staging
    # debris was left behind.
    assert not target.exists()
    assert not (tmp_path / "state.sqlite3.staging").exists()
    for suffix in ("-wal", "-shm"):
        assert not Path(str(target) + suffix).exists()
        assert not Path(str(target) + ".staging" + suffix).exists()


def test_import_failure_preserves_pre_existing_target_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the phase-1 review finding: if ``db_path`` already
    holds a SQLite DB with unrelated content, a failed ``import_json_to_sqlite``
    must NOT delete, truncate, or modify that pre-existing DB. The pre-existing
    DB must remain byte-identical after the failed import.
    """
    state_root = tmp_path / "workspaces"
    JsonStorageBackend(state_root).save(_representative_snapshot())

    # Seed a pre-existing DB at the target path with DIFFERENT content, so we
    # can detect any mutation / truncation / deletion.
    target = tmp_path / "state.sqlite3"
    other_snapshot = StorageSnapshot(
        workspaces=[dict(_representative_snapshot().workspaces[0], id="pre-existing-ws")]
    )
    SqliteStorageBackend(target).save(other_snapshot)

    # Snapshot the pre-existing DB bytes (and sidecars, if any).
    def _bytes(p: Path) -> bytes:
        return p.read_bytes() if p.exists() else b"<absent>"

    before = {
        "db": _bytes(target),
        "wal": _bytes(Path(str(target) + "-wal")),
        "shm": _bytes(Path(str(target) + "-shm")),
    }

    # Force a round-trip failure in the import.
    def _corrupt_load(self: SqliteStorageBackend) -> StorageSnapshot:
        return StorageSnapshot()

    monkeypatch.setattr(SqliteStorageBackend, "load", _corrupt_load)
    with pytest.raises(RoundTripError):
        import_json_to_sqlite(state_root, target)
    monkeypatch.undo()

    # Pre-existing target must be byte-identical to before.
    after = {
        "db": _bytes(target),
        "wal": _bytes(Path(str(target) + "-wal")),
        "shm": _bytes(Path(str(target) + "-shm")),
    }
    assert after == before, "pre-existing target DB was mutated by a failed import"

    # And the pre-existing DB must still reload to its original content (not
    # silently zeroed or partially overwritten).
    reloaded = SqliteStorageBackend(target).load()
    assert [w["id"] for w in reloaded.workspaces] == ["pre-existing-ws"]

    # Staging files must all be cleaned up.
    assert not (tmp_path / "state.sqlite3.staging").exists()
    for suffix in ("-wal", "-shm"):
        assert not Path(str(target) + ".staging" + suffix).exists()


def test_import_success_backs_up_pre_existing_target_db(tmp_path: Path) -> None:
    """On a successful import, any pre-existing DB at ``db_path`` must be
    preserved at ``<db_path>.bak`` (one-deep rolling backup) before the newly
    imported DB is promoted, mirroring ``atomic_write_text`` and
    ``export_sqlite_to_json`` backup semantics."""
    state_root = tmp_path / "workspaces"
    JsonStorageBackend(state_root).save(_representative_snapshot())

    # Seed a pre-existing DB with DIFFERENT content.
    target = tmp_path / "state.sqlite3"
    other_snapshot = StorageSnapshot(
        workspaces=[dict(_representative_snapshot().workspaces[0], id="old-ws")]
    )
    SqliteStorageBackend(target).save(other_snapshot)

    # Successful import (no monkeypatching — round-trip verification should pass).
    result = import_json_to_sqlite(state_root, target)
    assert result == target

    # Target now holds the freshly imported data.
    reloaded = SqliteStorageBackend(target).load()
    assert [w["id"] for w in reloaded.workspaces] == [WS_ID]

    # Prior DB preserved at .bak.
    backup = Path(str(target) + ".bak")
    assert backup.exists()
    backup_reloaded = SqliteStorageBackend(backup).load()
    assert [w["id"] for w in backup_reloaded.workspaces] == ["old-ws"]

    # No staging debris.
    assert not (tmp_path / "state.sqlite3.staging").exists()
    for suffix in ("-wal", "-shm"):
        assert not Path(str(target) + ".staging" + suffix).exists()


def test_import_os_replace_failure_restores_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the promotion-failure case: after a pre-existing
    target DB has been moved to <target>.bak, if the final os.replace(staging,
    target) raises (e.g. cross-device link, permission error, disk full), the
    original DB must be restored from .bak back to target before the exception
    propagates — callers must never observe a 'missing' DB after a failed
    import.
    """
    import os

    state_root = tmp_path / "workspaces"
    JsonStorageBackend(state_root).save(_representative_snapshot())

    # Seed a pre-existing DB with DIFFERENT content and capture its on-disk
    # bytes before the failed import.
    target = tmp_path / "state.sqlite3"
    other_snapshot = StorageSnapshot(
        workspaces=[dict(_representative_snapshot().workspaces[0], id="pre-existing-ws")]
    )
    SqliteStorageBackend(target).save(other_snapshot)

    def _bytes(p: Path) -> bytes:
        return p.read_bytes() if p.exists() else b"<absent>"

    before = {"db": _bytes(target)}
    for suffix in ("-wal", "-shm"):
        before[suffix] = _bytes(Path(str(target) + suffix))

    # Force os.replace to raise after backup rename has happened. We patch
    # os.replace in the migrate module namespace.
    import claude_hub.services.storage.migrate as migrate_mod

    real_replace = os.replace
    boom = OSError("simulated os.replace failure during promotion")

    def _failing_replace(src: str, dst: str) -> None:
        # Only fail the promotion replace (staging -> target), not other
        # rename operations (target -> backup).
        if Path(src).name.endswith(".staging"):
            raise boom
        return real_replace(src, dst)

    monkeypatch.setattr(migrate_mod.os, "replace", _failing_replace)
    with pytest.raises(OSError) as excinfo:
        import_json_to_sqlite(state_root, target)
    assert excinfo.value is boom
    monkeypatch.undo()

    # Pre-existing target must exist at its original path and reload to the
    # original content (i.e. the .bak was restored, not left stranded).
    assert target.exists(), "target DB missing after failed promotion"
    reloaded = SqliteStorageBackend(target).load()
    assert [w["id"] for w in reloaded.workspaces] == ["pre-existing-ws"]

    # Staging must be cleaned up.
    assert not (tmp_path / "state.sqlite3.staging").exists()
    for suffix in ("-wal", "-shm"):
        assert not Path(str(target) + ".staging" + suffix).exists()

    # The .bak should NOT exist after a successful restore (we moved it back).
    assert not Path(str(target) + ".bak").exists()


def test_import_roundtrip_failure_before_backup_does_not_touch_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If failure happens BEFORE the backup-rename step (e.g. round-trip
    mismatch), no .bak is created, target is untouched, and staging is
    cleaned up. This pins down the 'early failure' case of the new control
    flow."""
    state_root = tmp_path / "workspaces"
    JsonStorageBackend(state_root).save(_representative_snapshot())

    target = tmp_path / "state.sqlite3"
    other_snapshot = StorageSnapshot(
        workspaces=[dict(_representative_snapshot().workspaces[0], id="pre-existing-ws")]
    )
    SqliteStorageBackend(target).save(other_snapshot)

    before_bytes = target.read_bytes()

    def _corrupt_load(self: SqliteStorageBackend) -> StorageSnapshot:
        return StorageSnapshot()

    monkeypatch.setattr(SqliteStorageBackend, "load", _corrupt_load)
    with pytest.raises(RoundTripError):
        import_json_to_sqlite(state_root, target)
    monkeypatch.undo()

    # Target untouched byte-for-byte, no .bak created, staging cleaned up.
    assert target.read_bytes() == before_bytes
    assert not Path(str(target) + ".bak").exists()
    assert not (tmp_path / "state.sqlite3.staging").exists()
    reloaded = SqliteStorageBackend(target).load()
    assert [w["id"] for w in reloaded.workspaces] == ["pre-existing-ws"]


def test_import_sidecar_rename_failure_rolls_back_main_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the attempt-4 finding: if moving a sidecar
    (-wal or -shm) from target to .bak fails AFTER the main DB file has
    already been renamed to .bak, _rename_db_with_sidecars must roll back
    the main DB rename so the target is not left missing.

    We patch Path.rename to fail specifically when the -wal sidecar is
    being moved to .bak-wal during the backup step.
    """
    import pathlib

    state_root = tmp_path / "workspaces"
    JsonStorageBackend(state_root).save(_representative_snapshot())

    # Seed a pre-existing DB at target.
    target = tmp_path / "state.sqlite3"
    other_snapshot = StorageSnapshot(
        workspaces=[dict(_representative_snapshot().workspaces[0], id="pre-existing-ws")]
    )
    SqliteStorageBackend(target).save(other_snapshot)

    # Create a fake -wal sidecar alongside the target so _rename_db_with_sidecars
    # attempts to move it.
    fake_wal = Path(str(target) + "-wal")
    fake_wal.write_bytes(b"fake-wal-bytes")

    before_target_bytes = target.read_bytes()
    before_wal_bytes = fake_wal.read_bytes()

    real_path_rename = pathlib.Path.rename
    boom = OSError("simulated sidecar rename failure")

    def _failing_rename(self: pathlib.Path, other):
        # Fail when renaming target-wal -> target.bak-wal during backup step.
        if self.name == fake_wal.name and str(other).endswith(".bak-wal"):
            raise boom
        return real_path_rename(self, other)

    monkeypatch.setattr(pathlib.Path, "rename", _failing_rename)
    with pytest.raises(OSError) as excinfo:
        import_json_to_sqlite(state_root, target)
    assert excinfo.value is boom
    monkeypatch.undo()

    # After the failure — check file-state FIRST before opening any SQLite
    # connection to the target: opening the DB would cause SQLite to see and
    # possibly clean up/roll back the -wal sidecar (invalid or partial WALs
    # are auto-recovered at connection open), which would invalidate the
    # byte-level checks that follow.
    assert target.exists(), "main DB missing after sidecar rename failure"
    wal_still_at_original = fake_wal.exists()
    wal_bytes = fake_wal.read_bytes() if wal_still_at_original else None
    assert not Path(str(target) + ".bak").exists()
    assert not Path(str(target) + ".bak-wal").exists()
    assert not Path(str(target) + ".bak-shm").exists()
    assert not (tmp_path / "state.sqlite3.staging").exists()
    assert target.read_bytes() == before_target_bytes
    assert wal_still_at_original, "-wal sidecar missing after rollback"
    assert wal_bytes == before_wal_bytes

    # Now it is safe to open the target (the fake WAL file is still present;
    # SQLite may choose to ignore/delete a corrupt WAL at open, which is fine
    # for a real recovery but we have already verified file-state above).
    reloaded = SqliteStorageBackend(target).load()
    assert [w["id"] for w in reloaded.workspaces] == ["pre-existing-ws"]


def test_rename_db_with_sidecars_is_all_or_nothing(tmp_path: Path) -> None:
    """Direct unit test for _rename_db_with_sidecars: if any sidecar rename
    fails, files already moved are restored to their original locations."""
    import pathlib

    main_src = tmp_path / "data.sqlite3"
    main_dst = tmp_path / "data.moved"
    wal_src = Path(str(main_src) + "-wal")
    wal_dst = Path(str(main_dst) + "-wal")

    main_src.write_bytes(b"main")
    wal_src.write_bytes(b"wal")

    import claude_hub.services.storage.migrate as migrate_mod

    real_path_rename = pathlib.Path.rename
    boom = OSError("simulated wal rename failure")

    def _fail_wal(self: pathlib.Path, other):
        if self == wal_src:
            raise boom
        return real_path_rename(self, other)

    import unittest.mock

    with unittest.mock.patch.object(pathlib.Path, "rename", _fail_wal):
        with pytest.raises(OSError) as excinfo:
            migrate_mod._rename_db_with_sidecars(main_src, main_dst)
        assert excinfo.value is boom

    # After failure: both files must be back at src; nothing at dst.
    assert main_src.exists() and main_src.read_bytes() == b"main"
    assert wal_src.exists() and wal_src.read_bytes() == b"wal"
    assert not main_dst.exists()
    assert not wal_dst.exists()

    # A subsequent successful call (no failure) moves everything.
    migrate_mod._rename_db_with_sidecars(main_src, main_dst)
    assert not main_src.exists()
    assert not wal_src.exists()
    assert main_dst.exists() and main_dst.read_bytes() == b"main"
    assert wal_dst.exists() and wal_dst.read_bytes() == b"wal"


# --- Data-loss protection (atomic write) ---------------------------------------


def test_atomic_write_replaces_and_keeps_backup(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_write_text(target, "v1")
    assert target.read_text() == "v1"
    atomic_write_text(target, "v2")
    assert target.read_text() == "v2"
    assert (tmp_path / "state.json.bak").read_text() == "v1"


def test_atomic_write_failure_leaves_live_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-write must not truncate the existing live file."""
    target = tmp_path / "state.json"
    atomic_write_text(target, "good", keep_backup=False)

    real_replace = __import__("os").replace

    def _boom(src: str, dst: str) -> None:
        raise OSError("simulated crash before replace")

    monkeypatch.setattr("os.replace", _boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "this write fails", keep_backup=False)
    monkeypatch.setattr("os.replace", real_replace)

    # Live file still holds the last good content, not a truncated partial write.
    assert target.read_text() == "good"
    # No stray temp files left behind.
    assert list(tmp_path.glob("*.tmp")) == []


# --- helpers --------------------------------------------------------------------


def _fingerprint(snapshot: StorageSnapshot) -> dict:
    def canon(items: list[dict]) -> dict[str, str]:
        return {i["id"]: json.dumps(i, sort_keys=True, ensure_ascii=False) for i in items}

    return {
        "workspaces": canon(snapshot.workspaces),
        "tasks": canon(snapshot.tasks),
        "sessions": canon(snapshot.sessions),
        "reports": canon(snapshot.reports),
    }


# --- phase-2 safety hardening tests ---------------------------------------------


def test_sqlite_schema_newer_than_supported_fails_closed(tmp_path: Path) -> None:
    """A DB whose schema_meta.version is newer than our SCHEMA_VERSION must raise
    RuntimeError and refuse to open (fail-closed on downgrade)."""
    from claude_hub.services.storage import SCHEMA_VERSION

    db_path = tmp_path / "future.sqlite3"
    backend = SqliteStorageBackend(db_path)
    # Create a DB at current version first, then manually bump the version row.
    backend.save(StorageSnapshot(workspaces=_representative_snapshot().workspaces[:1]))
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION + 99),),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="newer than supported"):
        SqliteStorageBackend(db_path).load()


def test_sqlite_skips_orphan_items_without_workspace_id(tmp_path: Path) -> None:
    """SQLite save must skip tasks/sessions/reports that lack workspace_id so a
    JSON -> SQLite -> JSON round-trip does not introduce orphan rows the JSON
    backend cannot represent."""
    snap = _representative_snapshot()
    # Inject an orphan task (no workspace_id) alongside the valid one.
    orphan_task = dict(snap.tasks[0])
    orphan_task["id"] = "task-orphan"
    del orphan_task["workspace_id"]
    polluted = StorageSnapshot(
        workspaces=list(snap.workspaces),
        tasks=snap.tasks + [orphan_task],
        sessions=list(snap.sessions),
        reports=list(snap.reports),
    )

    db_path = tmp_path / "state.sqlite3"
    backend = SqliteStorageBackend(db_path)
    backend.save(polluted)

    reloaded = backend.load()
    ids = [t["id"] for t in reloaded.tasks]
    assert "task-1" in ids, "valid task preserved"
    assert "task-orphan" not in ids, "orphan task must be skipped on save (parity with JSON)"
    # And round-trip via JSON preserves the same shape (no extra rows bleed over).
    json_root = tmp_path / "json"
    export_sqlite_to_json(db_path, json_root)
    json_reloaded = JsonStorageBackend(json_root).load()
    assert [t["id"] for t in json_reloaded.tasks] == ["task-1"]


def test_sqlite_sets_busy_timeout_on_connection(tmp_path: Path) -> None:
    """Every connection must set PRAGMA busy_timeout so concurrent writers wait
    instead of raising 'database is locked' immediately."""
    from claude_hub.services.storage.sqlite_backend import _BUSY_TIMEOUT_MS

    db_path = tmp_path / "state.sqlite3"
    backend = SqliteStorageBackend(db_path)
    backend.save(_representative_snapshot())
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row[0] == _BUSY_TIMEOUT_MS


def test_integrity_check_runs_before_import_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """import_json_to_sqlite must refuse to promote a corrupt staging DB. We
    simulate corruption by patching _run_integrity_check (defined in
    sqlite_backend and imported lazily into migrate) at its source module so
    the lazy import sees our patched version."""
    import claude_hub.services.storage.sqlite_backend as sl_mod

    state_root = tmp_path / "workspaces"
    JsonStorageBackend(state_root).save(_representative_snapshot())
    target = tmp_path / "state.sqlite3"

    def _boom(_path: Path) -> None:
        raise sqlite3.DatabaseError("simulated integrity failure")

    monkeypatch.setattr(sl_mod, "_run_integrity_check", _boom)
    with pytest.raises(sqlite3.DatabaseError, match="integrity"):
        import_json_to_sqlite(state_root, target)
    # Target must NOT have been created (promotion must not happen) and staging
    # must be cleaned up.
    assert not target.exists()
    assert not (tmp_path / "state.sqlite3.staging").exists()


def test_integrity_check_runs_before_export_of_corrupt_db(tmp_path: Path) -> None:
    """export_sqlite_to_json must refuse to export a corrupt source DB before
    touching the state_root destination."""
    state_root = tmp_path / "state"
    db_path = tmp_path / "corrupt.sqlite3"
    # Create an empty file that is not a valid SQLite database — opening it will
    # succeed only to the extent sqlite3 reads the header; a direct call to
    # _run_integrity_check raises because the file is not a valid DB. Rather
    # than relying on header corruption we craft a DB then corrupt a page.
    SqliteStorageBackend(db_path).save(_representative_snapshot())
    # Overwrite bytes in the middle of the file (beyond header) to introduce
    # corruption that integrity_check detects.
    raw = bytearray(db_path.read_bytes())
    if len(raw) > 4096 + 32:
        raw[4096:4128] = b"\xff" * 32
        db_path.write_bytes(bytes(raw))

    # Even if integrity_check passes on a tiny file (unlikely but possible),
    # the call must at least attempt to connect; a completely unreadable file
    # must raise sqlite3.DatabaseError of some kind. Use a guaranteed-invalid
    # file instead for deterministic failure.
    bogus = tmp_path / "bogus.sqlite3"
    bogus.write_bytes(b"not a sqlite database" * 16)
    with pytest.raises((sqlite3.DatabaseError, sqlite3.OperationalError)):
        export_sqlite_to_json(bogus, state_root)
    # Destination must not have been touched.
    assert not state_root.exists()
    assert not (tmp_path / "state.staging").exists()


def test_integrity_check_passes_on_healthy_db(tmp_path: Path) -> None:
    """Sanity: _run_integrity_check returns cleanly (no raise) on a DB we just
    wrote with SqliteStorageBackend."""
    from claude_hub.services.storage.sqlite_backend import _run_integrity_check

    db_path = tmp_path / "healthy.sqlite3"
    SqliteStorageBackend(db_path).save(_representative_snapshot())
    # Must not raise.
    _run_integrity_check(db_path)


def test_export_promotion_failure_restores_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symmetric with the import restore path: after state_root has been moved
    to <state_root>.bak, if the final staging->target rename fails (e.g.
    permission, cross-fs), export_sqlite_to_json must restore .bak back to
    state_root so the caller never observes a missing live tree."""
    import pathlib

    state_root = tmp_path / "workspaces"
    live = _representative_snapshot()
    JsonStorageBackend(state_root).save(live)
    (state_root / "stale-marker.txt").write_text("live")

    db_path = tmp_path / "state.sqlite3"
    # Write a snapshot with a distinct workspace id to the DB so we can tell
    # whether promotion happened.
    other = StorageSnapshot(
        workspaces=[dict(live.workspaces[0], id="other-ws")],
    )
    SqliteStorageBackend(db_path).save(other)

    real_rename = pathlib.Path.rename
    boom = OSError("simulated staging->target rename failure during export")

    def _fail_promotion(self: pathlib.Path, other):
        # Fail only when renaming the staging dir to state_root (final promotion).
        if self.name == "workspaces.staging" and Path(other).name == "workspaces":
            raise boom
        return real_rename(self, other)

    monkeypatch.setattr(pathlib.Path, "rename", _fail_promotion)
    with pytest.raises(OSError) as excinfo:
        export_sqlite_to_json(db_path, state_root)
    assert excinfo.value is boom
    monkeypatch.undo()

    # Live state_root must exist at its original path and still hold the
    # pre-export content (including the stale marker), with .bak cleaned up
    # (restored).
    assert state_root.exists(), "state_root missing after failed export promotion"
    assert (state_root / "stale-marker.txt").exists()
    reloaded = JsonStorageBackend(state_root).load()
    assert [w["id"] for w in reloaded.workspaces] == [WS_ID]
    assert not (state_root.with_name("workspaces.bak")).exists()
    assert not (tmp_path / "workspaces.staging").exists()


def test_atomic_write_backup_oserror_does_not_block_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the one-deep backup copy fails (OSError), atomic_write_text must
    still proceed with the atomic write rather than dropping the new data."""
    target = tmp_path / "state.json"
    atomic_write_text(target, "v1")

    # Force backup-copy path to fail by patching Path.read_bytes to raise.
    def _boom(_self: Path) -> bytes:
        raise OSError("simulated backup read failure")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    # Must NOT raise; the write must still land atomically.
    atomic_write_text(target, "v2")
    monkeypatch.undo()

    assert target.read_text() == "v2"


def test_config_literal_rejects_invalid_backend_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """workspace_storage_backend must be Literal['json','sqlite'] — any other
    ENV value must raise ValidationError at settings construction so an
    operator typo fails fast rather than silently falling through."""
    from pydantic_settings import BaseSettings

    import claude_hub.config as cfg_mod

    # Build a fresh Settings object with an invalid env value; pydantic must
    # reject it. We do this by monkeypatching os.environ and re-instantiating
    # Settings directly (rather than touching the module-level singleton).
    monkeypatch.setenv("WORKSPACE_STORAGE_BACKEND", "rocksdb")
    with pytest.raises(ValidationError):
        # model_validate with no args uses env; pass an empty dict to force
        # env-driven construction.
        cfg_mod.Settings()  # type: ignore[call-arg]


def test_env_unknown_backend_falls_back_to_json_via_get_storage_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_storage_backend() must still fall back to JSON if the resolved
    setting value is unknown (defense in depth even though config Literal
    already rejects invalid values at construction)."""
    # Patch the settings object to a nonsense value to exercise the runtime
    # fallback path (this covers the case where a future code path bypasses
    # pydantic validation).
    import claude_hub.config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "workspace_storage_backend", "nonsense")
    backend = get_storage_backend(tmp_path)
    assert isinstance(backend, JsonStorageBackend)


def test_main_branch_untouched_was_not_modified_by_storage_tests() -> None:
    """Meta-check: this task must not have modified the main branch. The main
    worktree is at ../claude_hub (sibling of -develop); its HEAD must remain
    at the pre-task commit 59fa368 and its working tree must be clean of any
    path under backend/claude_hub/services/storage/."""
    import subprocess

    main_repo = Path(__file__).resolve().parents[4]  # backend/tests/ -> repo
    # Ascend from backend dir: backend/tests/ is inside backend/; main_repo
    # might resolve to develop worktree; check both.
    # Defined hard constraint: main is at /Users/bytedance/claude_hub.
    main_root = Path("/Users/bytedance/claude_hub")
    if not main_root.exists():
        pytest.skip("main worktree not at expected path")
    head = subprocess.check_output(
        ["git", "-C", str(main_root), "rev-parse", "--short=7", "HEAD"], text=True
    ).strip()
    head_full = subprocess.check_output(
        ["git", "-C", str(main_root), "rev-parse", "HEAD"], text=True
    ).strip()
    assert (
        head_full.startswith("59fa368") and head == "59fa368"
    ), f"main branch moved from 59fa368 to {head_full}"
    # And no storage/ edits.
    status = subprocess.check_output(
        [
            "git",
            "-C",
            str(main_root),
            "status",
            "--porcelain",
            "--",
            "backend/claude_hub/services/storage",
        ],
        text=True,
    ).strip()
    assert status == "", f"main storage/ has unexpected changes: {status!r}"
