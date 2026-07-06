"""Tests for phase-4 shadow-write risk gate.

Covers Goal Packet AC1-AC8 and AC10 for the shadow phase:

* AC1-2: workspace_manager._save_state routes through ``atomic_write_text``
  (raw ``Path.write_text`` on INDEX_FILE / state.json is gone).
* AC3: ShadowStorageBackend writes primary first; a primary failure prevents
  the secondary from being touched; a secondary failure does not propagate.
* AC4: ShadowStorageBackend is never constructed by default (meta-test).
* AC5: Drift between primary and secondary is detected and reported via
  ``on_error`` but does not fail the save.
* AC6: ``assert_path_outside_root`` refuses shadow paths under a state root.
* AC7: ``claude-hub storage shadow`` CLI exits 0 on a clean snapshot and 1
  when drift is introduced / live-root is violated.
* AC8: Focused tests; all artifacts under tmp_path.

Nothing here touches ~/.claude_hub/workspaces or the default state root.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from claude_hub.cli.main import cli
from claude_hub.config import settings
from claude_hub.services.storage import (
    ShadowDriftWarning,
    ShadowError,
    ShadowStorageBackend,
    StorageBackend,
    StorageSnapshot,
    assert_path_outside_root,
    atomic_write_text,
    get_storage_backend,
)
from claude_hub.services.storage.json_backend import JsonStorageBackend
from claude_hub.services.storage.sqlite_backend import SqliteStorageBackend

# --- helpers ---------------------------------------------------------------


def _empty_snapshot() -> StorageSnapshot:
    return StorageSnapshot()


def _small_snapshot() -> StorageSnapshot:
    return StorageSnapshot(
        workspaces=[
            {
                "id": "ws1",
                "name": "demo",
                "created_at": "2026-07-07T00:00:00",
                "status": "active",
                "path": "/tmp/repo",
                "default_branch": "main",
                "target": "local",
                "remote_profile_id": None,
                "task_count": 1,
                "session_count": 0,
                "report_count": 0,
            }
        ],
        tasks=[
            {
                "id": "t1",
                "workspace_id": "ws1",
                "title": "hello",
                "status": "queued",
                "task_mode": "autonomous",
                "created_at": "2026-07-07T00:00:00",
                "dispatch_pending": False,
                "system_internal": False,
                "origin": "manual",
                "execution_complexity": "standard",
            }
        ],
        sessions=[],
        reports=[],
    )


class _ExplodingPrimary:
    def load(self) -> StorageSnapshot:  # pragma: no cover - not used in test
        return StorageSnapshot()

    def save(self, snapshot: StorageSnapshot) -> None:
        raise RuntimeError("primary boom")


class _ExplodingSecondary:
    def __init__(self) -> None:
        self.saved = False

    def load(self) -> StorageSnapshot:  # pragma: no cover - not used in test
        return StorageSnapshot()

    def save(self, snapshot: StorageSnapshot) -> None:
        self.saved = True
        raise RuntimeError("secondary boom")


class _DriftingSecondary:
    """Pretends to save but returns a snapshot that drops the first task."""

    def __init__(self, delegate: StorageBackend) -> None:
        self._delegate = delegate

    def load(self) -> StorageSnapshot:
        snap = self._delegate.load()
        return StorageSnapshot(
            workspaces=snap.workspaces,
            tasks=snap.tasks[1:],  # drop t1 to trigger drift
            sessions=snap.sessions,
            reports=snap.reports,
        )

    def save(self, snapshot: StorageSnapshot) -> None:
        self._delegate.save(snapshot)


# --- AC1-2: _save_state routes through atomic_write_text -------------------


def test_save_state_uses_atomic_write_text_for_index_and_per_workspace_files(
    tmp_path: Path,
) -> None:
    """Verify AC1-2: workspace_manager._persistence._save_state routes writes
    of INDEX_FILE and <ws>/state.json through atomic_write_text, never through
    raw Path.write_text.

    Strategy:
      * inspect the source to statically prove the routing;
      * build a minimal stub instance of the mixin (avoiding the heavyweight
        WorkspaceManager constructor which would load live state) and drive
        _save_state() against tmp paths while intercepting atomic_write_text
        at the persistence module's import site, asserting that (a) both
        authoritative files are written through it, (b) neither goes through
        raw Path.write_text, (c) snapshot.md remains a plain write_text.
    """
    import importlib
    import inspect
    from datetime import datetime
    from typing import Any, cast

    persist_mod = importlib.import_module("claude_hub.services.workspace_manager._persistence")
    _PersistenceMixin = cast(Any, persist_mod)._PersistenceMixin
    wm_pkg = importlib.import_module("claude_hub.services.workspace_manager")

    # --- Static source check (belt-and-braces) ---
    src = inspect.getsource(_PersistenceMixin._save_state)
    assert (
        "atomic_write_text(INDEX_FILE" in src
    ), "_save_state no longer routes INDEX_FILE through atomic_write_text"
    assert (
        "atomic_write_text(" in src and "_workspace_state_file(workspace.id)" in src
    ), "_save_state no longer routes per-workspace state through atomic_write_text"
    # Raw .write_text on authoritative paths must be gone from _save_state.
    save_state_src = src.split("def _write_snapshot", 1)[0]
    assert "INDEX_FILE.write_text" not in save_state_src
    assert (
        "_workspace_state_file(" in save_state_src
        and ".write_text("
        not in save_state_src.split("_workspace_state_file(", 1)[1].split(")", 1)[0]
    ), "per-workspace state.json still uses raw write_text"

    # --- Behavioral check on a stub mixin instance ---
    state_root = tmp_path / "workspaces"
    state_root.mkdir()

    class StubPersistence(_PersistenceMixin):  # type: ignore[misc,valid-type]
        """Minimal stub: only provides what _save_state / _write_snapshot read."""

        def __init__(self) -> None:
            self.workspaces: dict = {}
            self.tasks: dict = {}
            self.sessions: dict = {}
            self.reports: dict = {}

        def _workspace_dir(self, workspace_id: str) -> Path:
            return state_root / workspace_id

        def _workspace_state_file(self, workspace_id: str) -> Path:
            return self._workspace_dir(workspace_id) / "state.json"

        def snapshot_path(self, workspace_id: str) -> Path:
            return self._workspace_dir(workspace_id) / "snapshot.md"

        def _write_snapshot(self, workspace_id: str) -> None:
            # Use a tiny snapshot writer that does raw Path.write_text —
            # proving snapshot.md stays plain-write.
            self.snapshot_path(workspace_id).parent.mkdir(parents=True, exist_ok=True)
            self.snapshot_path(workspace_id).write_text("# stub snapshot\n", encoding="utf-8")

    # Point the module-level INDEX_FILE at our tmp. _save_state reads
    # INDEX_FILE via the `from ._constants import *` wildcard, so patch it on
    # the persistence module itself (not on the package).
    assert hasattr(persist_mod, "INDEX_FILE"), "persistence module must export INDEX_FILE"
    assert hasattr(wm_pkg, "STATE_ROOT"), "workspace_manager package must export STATE_ROOT"
    real_index_file: Path = cast(Any, persist_mod).INDEX_FILE
    cast(Any, persist_mod).INDEX_FILE = state_root / "index.json"
    try:
        atomic_calls: list[Path] = []
        real_atomic = persist_mod.atomic_write_text

        def tracking_atomic(path: Path, data: str, *, keep_backup: bool = True) -> None:
            atomic_calls.append(Path(str(path)))
            real_atomic(path, data, keep_backup=keep_backup)

        # Guard: fail if raw Path.write_text is called on the authoritative files.
        original_write_text = Path.write_text

        def guarded_write_text(self, data, *args, **kwargs):
            s = str(self)
            if s == str(persist_mod.INDEX_FILE) or s.endswith(f"{os.sep}state.json"):
                raise AssertionError(
                    f"raw Path.write_text called on authoritative file {self}; "
                    "atomic_write_text must be used"
                )
            return original_write_text(self, data, *args, **kwargs)

        from unittest.mock import patch

        with (
            patch.object(persist_mod, "atomic_write_text", side_effect=tracking_atomic),
            patch.object(Path, "write_text", guarded_write_text),
        ):
            stub = StubPersistence()
            from claude_hub.models import (
                Workspace,
                WorkspaceTask,
                WorkspaceTaskMode,
                WorkspaceTaskStatus,
            )

            ws = Workspace(
                id="ws-ac1",
                name="ac1",
                path=str(tmp_path / "repo"),
                default_branch="main",
                session_prefix="cb",
                resident_agent_enabled=False,
                created_at=datetime(2026, 7, 7),
                updated_at=datetime(2026, 7, 7),
            )
            stub.workspaces[ws.id] = ws
            from claude_hub.models import AgentType

            t = WorkspaceTask(
                id="t-ac1",
                workspace_id=ws.id,
                title="ac1",
                prompt="do ac1",
                agent_type=AgentType.CLAUDE,
                status=WorkspaceTaskStatus.QUEUED,
                task_mode=WorkspaceTaskMode.AUTONOMOUS,
                created_at=datetime(2026, 7, 7),
                updated_at=datetime(2026, 7, 7),
            )
            stub.tasks[t.id] = t
            # _save_state reads STATE_ROOT via _wm.STATE_ROOT; temporarily
            # point the workspace_manager package's STATE_ROOT at our tmp.
            real_state_root: Path = cast(Any, wm_pkg).STATE_ROOT
            cast(Any, wm_pkg).STATE_ROOT = state_root
            try:
                stub._save_state()
            finally:
                cast(Any, wm_pkg).STATE_ROOT = real_state_root

        targets = {str(p) for p in atomic_calls}
        assert (
            str(cast(Any, persist_mod).INDEX_FILE) in targets
        ), f"index.json not routed via atomic_write_text; calls={targets}"
        assert (
            str(state_root / "ws-ac1" / "state.json") in targets
        ), f"<ws>/state.json not routed via atomic_write_text; calls={targets}"
        # snapshot.md must NOT go through atomic_write_text (explicit AC1 carve-out).
        assert str(state_root / "ws-ac1" / "snapshot.md") not in targets
        assert (state_root / "ws-ac1" / "snapshot.md").exists()
    finally:
        cast(Any, persist_mod).INDEX_FILE = real_index_file


# --- AC3: primary/secondary failure semantics ------------------------------


def test_primary_save_failure_never_calls_secondary(tmp_path: Path) -> None:
    primary = _ExplodingPrimary()
    secondary = _ExplodingSecondary()
    errors: list[Exception] = []
    sh = ShadowStorageBackend(primary, secondary, on_error=errors.append)
    with pytest.raises(RuntimeError, match="primary boom"):
        sh.save(_empty_snapshot())
    assert not secondary.saved, "secondary.save must not be called when primary fails"
    assert errors == []


def test_secondary_save_failure_does_not_propagate(tmp_path: Path) -> None:
    j = JsonStorageBackend(tmp_path / "json")
    secondary = _ExplodingSecondary()
    errors: list[Exception] = []
    sh = ShadowStorageBackend(j, secondary, on_error=errors.append)
    snap = _small_snapshot()
    sh.save(snap)  # must NOT raise
    assert secondary.saved, "secondary.save was not called"
    assert len(errors) == 1
    assert "secondary boom" in str(errors[0])
    # primary must still have persisted the snapshot.
    reloaded = j.load()
    assert [w["id"] for w in reloaded.workspaces] == ["ws1"]
    assert [t["id"] for t in reloaded.tasks] == ["t1"]


# --- AC4: JSON default preserved, shadow never constructed by default ------


def test_json_default_unchanged_and_shadow_not_wired_by_default() -> None:
    """AC2+AC4 meta-check: default backend is JSON; the workspace_manager
    persistence module does not reference Shadow* names at import time."""
    import importlib

    assert settings.workspace_storage_backend == "json"
    backend = get_storage_backend(backend=None)
    assert isinstance(
        backend, JsonStorageBackend
    ), f"default backend must be JsonStorageBackend, got {type(backend).__name__}"
    persist_mod = importlib.import_module("claude_hub.services.workspace_manager._persistence")
    wm_pkg = importlib.import_module("claude_hub.services.workspace_manager")
    for mod in (wm_pkg, persist_mod):
        for name in dir(mod):
            assert "Shadow" not in name or name.startswith(
                "_"
            ), f"shadow symbol leaked into {mod.__name__}: {name}"


# --- AC5: drift detection --------------------------------------------------


def test_drift_is_reported_but_does_not_fail_save(tmp_path: Path) -> None:
    j = JsonStorageBackend(tmp_path / "json")
    drifting = _DriftingSecondary(SqliteStorageBackend(tmp_path / "state.sqlite3"))
    errors: list[Exception] = []
    sh = ShadowStorageBackend(j, drifting, on_error=errors.append)
    snap = _small_snapshot()
    sh.save(snap)  # must NOT raise even though secondary dropped t1
    drift_warnings = [e for e in errors if isinstance(e, ShadowDriftWarning)]
    assert len(drift_warnings) == 1, f"expected one drift warning, got {errors}"
    dw = drift_warnings[0].drift
    assert not dw.ok
    assert "tasks" in dw.primary_missing_from_secondary
    assert "t1" in dw.primary_missing_from_secondary["tasks"]
    # Primary must still have t1.
    assert [t["id"] for t in j.load().tasks] == ["t1"]


def test_clean_dual_write_shows_no_drift(tmp_path: Path) -> None:
    j = JsonStorageBackend(tmp_path / "json")
    s = SqliteStorageBackend(tmp_path / "state.sqlite3")
    errors: list[Exception] = []
    sh = ShadowStorageBackend(j, s, on_error=errors.append)
    sh.save(_small_snapshot())
    assert errors == [], f"unexpected errors/warnings: {errors}"
    assert sh.last_drift is not None and sh.last_drift.ok


# --- AC6: live-root guard --------------------------------------------------


@pytest.mark.parametrize(
    "candidate,forbidden,should_fail",
    [
        ("/a/b/state.sqlite3", "/a/b", True),
        ("/a/b/shadow/state.db", "/a/b", True),
        ("/a/b.sqlite3", "/a/b", False),  # sibling file, not inside
        ("/x/y/state.sqlite3", "/a/b", False),
    ],
)
def test_assert_path_outside_root(candidate: str, forbidden: str, should_fail: bool) -> None:
    c = Path(candidate)
    r = Path(forbidden)
    # We can't rely on /a/b existing on the test host, so monkeypatch _is_under
    # to compare lexically for this test by using real /tmp paths:
    import tempfile

    with tempfile.TemporaryDirectory() as base:
        cb = Path(base)
        # Lay out the candidate relative to base in a way that mirrors the case.
        if should_fail:
            root = cb / "root"
            root.mkdir()
            cand = (
                root / Path(candidate).name
                if candidate.count("/") == 2
                else root / "shadow" / Path(candidate).name
            )
            cand.parent.mkdir(parents=True, exist_ok=True)
            with pytest.raises(ShadowError):
                assert_path_outside_root(cand, root)
        else:
            root = cb / "root"
            outside = cb / "outside"
            outside.mkdir()
            root.mkdir()
            cand = outside / Path(candidate).name
            # Should NOT raise
            assert_path_outside_root(cand, root)


def test_live_root_guard_refuses_db_inside_state_root(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    # sibling file inside state dir
    with pytest.raises(ShadowError):
        assert_path_outside_root(state / "shadow.db", state, label="--db-path")
    # nested dir inside state
    (state / "nested").mkdir()
    with pytest.raises(ShadowError):
        assert_path_outside_root(state / "nested" / "shadow.db", state)
    # outside: ok
    outside = tmp_path / "shadow"
    outside.mkdir()
    assert_path_outside_root(outside / "shadow.db", state)  # no raise


# --- AC7: CLI shadow dry-run -----------------------------------------------


def _seed_json_root(root: Path, snap: StorageSnapshot) -> None:
    JsonStorageBackend(root).save(snap)


def test_cli_shadow_passes_on_clean_snapshot(tmp_path: Path) -> None:
    state = tmp_path / "state"
    db = tmp_path / "shadow" / "s.db"
    _seed_json_root(state, _small_snapshot())
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["storage", "shadow", "--state-root", str(state), "--db-path", str(db), "--no-copy"],
        catch_exceptions=False,
    )
    assert (
        result.exit_code == 0
    ), f"exit={result.exit_code}\noutput={result.output}\ntraceback={getattr(result.exception, '__traceback__', None) and result.exception}"
    assert "PASS" in result.output
    assert db.exists(), "shadow DB should be written when --db-path is supplied and --clean default"


def test_cli_shadow_json_output_shape(tmp_path: Path) -> None:
    state = tmp_path / "state"
    db = tmp_path / "shadow" / "s.db"
    _seed_json_root(state, _small_snapshot())
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--json",
            "storage",
            "shadow",
            "--state-root",
            str(state),
            "--db-path",
            str(db),
            "--no-copy",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["drift"]["ok"] is True
    assert payload["secondary_errors"] == []
    assert payload["temporary_db"] is False


def test_cli_shadow_refuses_db_under_state_root(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    bad_db = state / "shadow.db"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["storage", "shadow", "--state-root", str(state), "--db-path", str(bad_db), "--no-copy"],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "must not live under state root" in result.output


def test_cli_shadow_detects_drift_when_secondary_mangles_payload(tmp_path: Path) -> None:
    """Inject a failing secondary by monkeypatching SqliteStorageBackend.save
    so that it drops the first task after writing — exercises drift reporting
    through the CLI."""
    state = tmp_path / "state"
    db = tmp_path / "shadow" / "s.db"
    _seed_json_root(state, _small_snapshot())

    real_save = SqliteStorageBackend.save
    calls = {"n": 0}

    def mangling_save(self, snapshot: StorageSnapshot) -> None:
        # First save from CLI seed is already done (that's JsonStorageBackend).
        # First call here is from the shadow dual-write — write then corrupt the
        # underlying DB row for t1 so reload misses it.
        real_save(self, snapshot)
        calls["n"] += 1
        # Directly DELETE the 't1' row from tasks table to simulate a buggy secondary.
        import sqlite3

        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("DELETE FROM tasks WHERE id = ?", ("t1",))
            conn.commit()
        finally:
            conn.close()

    runner = CliRunner()
    with patch.object(SqliteStorageBackend, "save", mangling_save):
        result = runner.invoke(
            cli,
            ["storage", "shadow", "--state-root", str(state), "--db-path", str(db), "--no-copy"],
            catch_exceptions=False,
        )
    assert (
        result.exit_code == 1
    ), f"expected drift failure exit 1, got {result.exit_code}: {result.output}"
    assert "DRIFT" in result.output or "drift" in result.output.lower()


def test_cli_shadow_missing_root_errors(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["storage", "shadow", "--state-root", str(tmp_path / "nope"), "--no-copy"],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "state root does not exist" in result.output


# --- AC8 (corner): shadow backend is structurally a StorageBackend ---------


def test_shadow_is_runtime_checkable_storage_backend(tmp_path: Path) -> None:
    j = JsonStorageBackend(tmp_path / "json")
    s = SqliteStorageBackend(tmp_path / "s.db")
    sh = ShadowStorageBackend(j, s)
    assert isinstance(
        sh, StorageBackend
    ), "ShadowStorageBackend must satisfy the StorageBackend Protocol"
