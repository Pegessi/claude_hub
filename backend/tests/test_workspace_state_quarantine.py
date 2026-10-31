"""Fail-closed durable-state loading: quarantine, backup recovery, evidence.

Covers every durable JSON store loaded through the workspace-manager
persistence layer:

* ``index.json`` (workspace index)
* ``<workspace-id>/state.json`` (per-workspace tasks/sessions/reports/mailbox)
* ``scheduled_tasks.json``

All tests redirect persistence into ``tmp_path`` (see the ``state_root``
fixture); no live Hub home, tmux socket or service process is touched.
"""

from __future__ import annotations

import hashlib
import json
import logging
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from claude_hub.models import (
    ScheduledTaskCreate,
    ScheduledTaskKind,
    WorkspaceCreate,
    WorkspaceTaskCreate,
    WorkspaceTaskMode,
)
from claude_hub.services.ttyd_manager import ttyd_manager
from claude_hub.services.workspace_manager import WorkspaceManager

_wm = import_module("claude_hub.services.workspace_manager")
_PERSISTENCE_LOGGER = "claude_hub.services.workspace_manager._constants"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def state_root(monkeypatch: MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect all workspace + scheduled-task persistence to a tmp directory."""

    root = tmp_path / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    index_file = root / "index.json"
    sched_file = root / "scheduled_tasks.json"
    monkeypatch.setattr(_wm, "STATE_ROOT", root)
    monkeypatch.setattr(_wm, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm, "SCHEDULED_TASKS_FILE", sched_file)
    monkeypatch.setattr(_wm._persistence, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._state, "INDEX_FILE", index_file)
    monkeypatch.setattr(_wm._scheduling, "SCHEDULED_TASKS_FILE", sched_file)
    return root


@pytest.fixture()
def manager(state_root: Path) -> WorkspaceManager:
    return WorkspaceManager()


def _make_workspace(manager: WorkspaceManager, tmp_path: Path, name: str = "WS") -> Any:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return manager.create_workspace(WorkspaceCreate(name=name, path=str(repo), session_prefix="q"))


def _make_task(manager: WorkspaceManager, workspace_id: str, title: str) -> Any:
    return manager._create_task(
        workspace_id,
        WorkspaceTaskCreate(title=title, prompt="do", task_mode=WorkspaceTaskMode.REVIEWED),
    )


def _quarantine_entries(root: Path) -> list[Path]:
    return sorted(root.glob("**/*.corrupt-*"))


def _quarantine_markers(root: Path) -> list[Path]:
    return sorted(root.glob("**/*.quarantined"))


def _read_marker(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


# ---------------------------------------------------------------------------
# Missing / valid files
# ---------------------------------------------------------------------------


def test_missing_files_load_empty_without_marker(
    manager: WorkspaceManager, state_root: Path
) -> None:
    assert manager.workspaces == {}
    assert manager.tasks == {}
    assert manager.scheduled_tasks == {}
    assert _quarantine_markers(state_root) == []
    assert _quarantine_entries(state_root) == []
    assert manager.state_recovery_warnings() == []


def test_valid_state_loads_unchanged(
    manager: WorkspaceManager, state_root: Path, tmp_path: Path
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    task = _make_task(manager, workspace.id, "Persisted task")
    manager._save_state()

    reloaded = WorkspaceManager()
    assert list(reloaded.workspaces) == [workspace.id]
    assert list(reloaded.tasks) == [task.id]
    assert reloaded.tasks[task.id].title == "Persisted task"
    assert _quarantine_markers(state_root) == []
    assert _quarantine_entries(state_root) == []
    assert reloaded.state_recovery_warnings() == []


# ---------------------------------------------------------------------------
# index.json corruption
# ---------------------------------------------------------------------------


def test_corrupt_index_is_quarantined_with_marker_and_error_log(
    state_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    index_file = state_root / "index.json"
    corrupt = b'{"workspaces": ['
    index_file.write_bytes(corrupt)

    with caplog.at_level(logging.ERROR, logger=_PERSISTENCE_LOGGER):
        manager = WorkspaceManager()

    assert manager.workspaces == {}
    quarantined = _quarantine_entries(state_root)
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == corrupt
    assert quarantined[0].name.startswith("index.json.corrupt-")

    markers = _quarantine_markers(state_root)
    assert len(markers) == 1
    doc = _read_marker(markers[0])
    assert doc["store_path"] == str(index_file)
    event = doc["events"][0]
    assert event["label"] == "workspace index"
    assert event["reason"].startswith("invalid JSON:")
    assert event["detected_at"]
    assert event["original_sha256"] == hashlib.sha256(corrupt).hexdigest()
    assert event["quarantine_file"] == quarantined[0].name
    assert event["quarantine_moved"] is True
    assert event["recovered_from_backup"] is False
    assert event["backup_file"] is None

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("Quarantined corrupt durable state store" in r.getMessage() for r in error_records)
    recovery_warnings = manager.state_recovery_warnings()
    assert recovery_warnings
    # The in-memory quarantine event and the on-disk marker summary describe
    # the same marker: exactly one warning line, not two.
    assert len(recovery_warnings) == 1
    assert str(markers[0]) in recovery_warnings[0]


def test_structural_index_failure_is_quarantined(state_root: Path) -> None:
    # Decodable JSON but not the documented object/list shape.
    (state_root / "index.json").write_text(json.dumps({"workspaces": {"id": "x"}}))

    manager = WorkspaceManager()

    assert manager.workspaces == {}
    markers = _quarantine_markers(state_root)
    assert len(markers) == 1
    assert "structural validation failed" in _read_marker(markers[0])["events"][0]["reason"]


def test_repeated_corrupt_loads_never_overwrite_quarantine(state_root: Path) -> None:
    index_file = state_root / "index.json"
    first = b"corrupt-one"
    index_file.write_bytes(first)
    WorkspaceManager()

    second = b"corrupt-two"
    index_file.write_bytes(second)
    WorkspaceManager()

    entries = _quarantine_entries(state_root)
    assert len(entries) == 2
    contents = {path.read_bytes() for path in entries}
    assert contents == {first, second}

    markers = _quarantine_markers(state_root)
    assert len(markers) == 1
    events = _read_marker(markers[0])["events"]
    assert len(events) == 2
    assert events[0]["original_sha256"] == hashlib.sha256(first).hexdigest()
    assert events[1]["original_sha256"] == hashlib.sha256(second).hexdigest()


def test_index_recovers_from_backup(state_root: Path, tmp_path: Path) -> None:
    manager = WorkspaceManager()
    workspace = _make_workspace(manager, tmp_path)
    manager._save_state()
    index_file = state_root / "index.json"
    good_payload = json.loads(index_file.read_text(encoding="utf-8"))

    # Last write is truncated; the rotating backup is still complete.
    index_file.write_text('{"workspaces": [{"id": ')
    reloaded = WorkspaceManager()

    assert list(reloaded.workspaces) == [workspace.id]
    # Primary was restored from backup.
    assert json.loads(index_file.read_text(encoding="utf-8")) == good_payload
    marker = _read_marker(state_root / "index.json.quarantined")["events"][0]
    assert marker["recovered_from_backup"] is True
    assert marker["backup_file"] == "index.json.bak"
    assert _quarantine_entries(state_root)[0].read_text() == '{"workspaces": [{"id": '


def test_save_after_corrupt_index_preserves_evidence(
    manager: WorkspaceManager, state_root: Path
) -> None:
    index_file = state_root / "index.json"
    corrupt = b"{broken"
    index_file.write_bytes(corrupt)
    quarantined = WorkspaceManager()

    # Manager started empty; a later save must not destroy evidence.
    quarantined._save_state()

    assert json.loads(index_file.read_text(encoding="utf-8")) == {"workspaces": []}
    surviving = _quarantine_entries(state_root)
    assert len(surviving) == 1 and surviving[0].read_bytes() == corrupt
    assert (state_root / "index.json.quarantined").exists()


# ---------------------------------------------------------------------------
# per-workspace state.json corruption
# ---------------------------------------------------------------------------


def _state_paths(workspace_id: str, state_root: Path) -> tuple[Path, Path]:
    state_file = state_root / workspace_id / "state.json"
    return state_file, state_file.with_name("state.json.bak")


def test_corrupt_workspace_state_restores_truncated_write_from_backup(
    manager: WorkspaceManager, state_root: Path, tmp_path: Path
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    first = _make_task(manager, workspace.id, "First")
    manager._save_state()
    state_file, backup_file = _state_paths(workspace.id, state_root)
    first_payload = json.loads(state_file.read_text(encoding="utf-8"))

    # Simulate a newer complete save followed by a torn primary write.
    _make_task(manager, workspace.id, "Second")
    manager._save_state()
    # Force the backup to the known-good previous complete state.
    backup_file.write_text(json.dumps(first_payload), encoding="utf-8")
    truncated = '{"tasks": [{"id": "'
    state_file.write_text(truncated)

    reloaded = WorkspaceManager()

    restored_ids = set(reloaded.tasks)
    assert restored_ids == {first.id}
    assert json.loads(state_file.read_text(encoding="utf-8")) == first_payload
    marker = _read_marker(state_file.with_name("state.json.quarantined"))["events"][0]
    assert marker["label"] == f"workspace state ({workspace.id})"
    assert marker["recovered_from_backup"] is True
    assert marker["backup_file"] == "state.json.bak"
    assert marker["original_sha256"] == hashlib.sha256(truncated.encode("utf-8")).hexdigest()


def test_corrupt_workspace_state_without_backup_starts_empty_and_preserves_evidence(
    manager: WorkspaceManager, state_root: Path, tmp_path: Path
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    _make_task(manager, workspace.id, "Doomed")
    manager._save_state()
    state_file, backup_file = _state_paths(workspace.id, state_root)
    # Simulate corruption on the only complete copy (no good backup), e.g. a
    # torn first durable write.
    backup_file.unlink()
    corrupt = b"not json at all"
    state_file.write_bytes(corrupt)

    reloaded = WorkspaceManager()

    # The index row survives; entity state starts empty.
    assert workspace.id in reloaded.workspaces
    assert reloaded.tasks == {}
    quarantined = _quarantine_entries(state_root / workspace.id)
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == corrupt

    # Any subsequent save must keep the quarantined payload and marker.
    reloaded._save_state()
    assert json.loads(state_file.read_text(encoding="utf-8"))["tasks"] == []
    assert quarantined[0].exists()
    assert state_file.with_name("state.json.quarantined").exists()


def test_structural_workspace_state_failure_is_quarantined(
    manager: WorkspaceManager, state_root: Path, tmp_path: Path
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    manager._save_state()
    state_file, _ = _state_paths(workspace.id, state_root)
    state_file.write_text(json.dumps({"tasks": "not-a-list"}))

    reloaded = WorkspaceManager()

    assert reloaded.tasks == {}
    marker = state_file.with_name("state.json.quarantined")
    assert marker.exists()
    assert "structural validation failed" in _read_marker(marker)["events"][0]["reason"]


def test_unknown_task_fields_quarantine_instead_of_crash(
    manager: WorkspaceManager, state_root: Path, tmp_path: Path
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    manager._save_state()
    state_file, _ = _state_paths(workspace.id, state_root)
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    payload["tasks"].append({"title": "x", "unknown_field_from_a_newer_build": True})
    state_file.write_text(json.dumps(payload))

    reloaded = WorkspaceManager()  # must not raise

    assert workspace.id in reloaded.workspaces
    assert reloaded.tasks == {}
    assert state_file.with_name("state.json.quarantined").exists()


def test_unreadable_workspace_state_os_error_is_quarantined(
    manager: WorkspaceManager, state_root: Path, tmp_path: Path
) -> None:
    """A directory at the state path raises OSError at read (covers root)."""

    workspace = _make_workspace(manager, tmp_path)
    manager._save_state()
    state_file, _ = _state_paths(workspace.id, state_root)
    state_file.unlink()
    state_file.mkdir()

    reloaded = WorkspaceManager()  # must not crash

    assert workspace.id in reloaded.workspaces
    assert reloaded.tasks == {}
    marker = state_file.with_name("state.json.quarantined")
    assert marker.exists()
    event = _read_marker(marker)["events"][0]
    assert event["reason"].startswith("unreadable state file:")
    # The directory itself was moved aside as evidence.
    assert any(path.is_dir() for path in _quarantine_entries(state_root / workspace.id))


def test_corrupt_workspace_does_not_affect_unrelated_workspaces(
    manager: WorkspaceManager, state_root: Path, tmp_path: Path
) -> None:
    """One quarantined workspace must not take healthy workspaces down."""

    good_base = tmp_path / "good-base"
    bad_base = tmp_path / "bad-base"
    good_base.mkdir()
    bad_base.mkdir()
    good_ws = _make_workspace(manager, good_base, "Good")
    bad_ws = _make_workspace(manager, bad_base, "Bad")
    good_task = _make_task(manager, good_ws.id, "Alive")
    _make_task(manager, bad_ws.id, "Doomed")
    manager._save_state()

    bad_state, bad_backup = _state_paths(bad_ws.id, state_root)
    corrupt = b"{torn write"
    bad_backup.unlink()
    bad_state.write_bytes(corrupt)

    reloaded = WorkspaceManager()

    assert good_ws.id in reloaded.workspaces and bad_ws.id in reloaded.workspaces
    assert set(reloaded.tasks) == {good_task.id}
    assert reloaded.tasks[good_task.id].title == "Alive"
    assert _quarantine_markers(state_root / bad_ws.id)
    assert _quarantine_markers(state_root / good_ws.id) == []
    assert _quarantine_entries(state_root / good_ws.id) == []

    # A later save keeps the bad workspace's evidence and marker, and never
    # writes the healthy workspace's state over the quarantined payload.
    good_state, _ = _state_paths(good_ws.id, state_root)
    good_before = good_state.read_bytes()
    reloaded._save_state()
    assert good_state.read_bytes() == good_before
    surviving = _quarantine_entries(state_root / bad_ws.id)
    assert len(surviving) == 1 and surviving[0].read_bytes() == corrupt
    assert bad_state.with_name("state.json.quarantined").exists()
    assert set(reloaded.tasks) == {good_task.id}


# ---------------------------------------------------------------------------
# rotating backup behaviour
# ---------------------------------------------------------------------------


def test_atomic_save_rotates_single_backup(manager: WorkspaceManager, state_root: Path) -> None:
    target = state_root / "rot.json"
    manager._atomic_save_state_text(target, "first")
    assert not target.with_name("rot.json.bak").exists()

    manager._atomic_save_state_text(target, "second")
    assert target.read_text() == "second"
    assert target.with_name("rot.json.bak").read_text() == "first"

    manager._atomic_save_state_text(target, "third")
    assert target.read_text() == "third"
    assert target.with_name("rot.json.bak").read_text() == "second"
    # Single rotating backup only.
    assert list(state_root.glob("rot.json.bak*")) == [target.with_name("rot.json.bak")]


def test_corrupt_backup_is_quarantined_and_store_starts_empty(state_root: Path) -> None:
    index_file = state_root / "index.json"
    index_file.write_text("broken-primary")
    index_file.with_name("index.json.bak").write_text("broken-backup")

    manager = WorkspaceManager()

    assert manager.workspaces == {}
    quarantined = {p.name for p in _quarantine_entries(state_root)}
    assert any(n.startswith("index.json.corrupt-") for n in quarantined)
    assert any(n.startswith("index.json.bak.corrupt-") for n in quarantined)
    event = _read_marker(state_root / "index.json.quarantined")["events"][0]
    assert event["recovered_from_backup"] is False
    assert event["backup_error"]


# ---------------------------------------------------------------------------
# missing primary with an orphan backup
# ---------------------------------------------------------------------------


def test_missing_index_primary_is_recovered_from_backup(state_root: Path) -> None:
    index_file = state_root / "index.json"
    backup = state_root / "index.json.bak"
    payload: dict[str, Any] = {"workspaces": []}
    backup.write_text(json.dumps(payload), encoding="utf-8")

    manager = WorkspaceManager()

    assert json.loads(index_file.read_text(encoding="utf-8")) == payload
    marker = _read_marker(state_root / "index.json.quarantined")["events"][0]
    assert marker["recovered_from_backup"] is True
    assert "missing" in marker["reason"]
    assert manager.state_recovery_warnings()


def test_missing_workspace_primary_is_recovered_from_backup(
    manager: WorkspaceManager, state_root: Path, tmp_path: Path
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    task = _make_task(manager, workspace.id, "Recovered")
    manager._save_state()
    state_file, backup_file = _state_paths(workspace.id, state_root)
    good_payload = state_file.read_text(encoding="utf-8")
    assert backup_file.exists()
    state_file.unlink()

    reloaded = WorkspaceManager()

    assert list(reloaded.tasks) == [task.id]
    assert state_file.read_text(encoding="utf-8") == good_payload
    marker = _read_marker(state_file.with_name("state.json.quarantined"))["events"][0]
    assert marker["recovered_from_backup"] is True
    assert "missing" in marker["reason"]


def test_missing_primary_with_unusable_backup_starts_empty_and_marks(
    state_root: Path,
) -> None:
    index_file = state_root / "index.json"
    (state_root / "index.json.bak").write_text("broken-backup")
    # No primary at all.

    manager = WorkspaceManager()

    assert manager.workspaces == {}
    assert not index_file.exists()
    quarantined = {p.name for p in _quarantine_entries(state_root)}
    assert any(n.startswith("index.json.bak.corrupt-") for n in quarantined)
    marker = _read_marker(state_root / "index.json.quarantined")["events"][0]
    assert marker["recovered_from_backup"] is False
    assert marker["backup_error"]
    assert manager.state_recovery_warnings()


# ---------------------------------------------------------------------------
# scheduled_tasks.json
# ---------------------------------------------------------------------------


def test_corrupt_scheduled_tasks_are_quarantined(
    manager: WorkspaceManager, state_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sched_file = state_root / "scheduled_tasks.json"
    corrupt = b"{oops"
    sched_file.write_bytes(corrupt)

    with caplog.at_level(logging.ERROR, logger=_PERSISTENCE_LOGGER):
        reloaded = WorkspaceManager()

    assert reloaded.scheduled_tasks == {}
    marker = state_root / "scheduled_tasks.json.quarantined"
    event = _read_marker(marker)["events"][0]
    assert event["label"] == "scheduled tasks"
    assert event["original_sha256"] == hashlib.sha256(corrupt).hexdigest()
    assert any("Quarantined corrupt durable state store" in r.getMessage() for r in caplog.records)

    # A later scheduled save must keep the evidence.
    reloaded._save_scheduled_tasks()
    assert json.loads(sched_file.read_text(encoding="utf-8"))["scheduled_tasks"] == []
    assert marker.exists()
    assert any(p.read_bytes() == corrupt for p in _quarantine_entries(state_root))


def test_scheduled_tasks_recover_from_backup(
    manager: WorkspaceManager, state_root: Path, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = _make_workspace(manager, tmp_path)
    monkeypatch.setattr(ttyd_manager, "get_tab", lambda tid: object() if tid == "tab-1" else None)
    scheduled = manager.create_scheduled_task(
        ScheduledTaskCreate(
            name="nudge",
            kind=ScheduledTaskKind.TAB_MESSAGE,
            cron="*/10 * * * *",
            workspace_id=workspace.id,
            tab_id="tab-1",
            message="ping",
        )
    )
    sched_file = state_root / "scheduled_tasks.json"
    assert sched_file.exists()
    good_payload = sched_file.read_text(encoding="utf-8")
    sched_file.with_name("scheduled_tasks.json.bak").write_text(good_payload)
    sched_file.write_text('{"scheduled_tasks": [')

    reloaded = WorkspaceManager()

    assert list(reloaded.scheduled_tasks) == [scheduled.id]
    marker = _read_marker(state_root / "scheduled_tasks.json.quarantined")["events"][0]
    assert marker["recovered_from_backup"] is True


# ---------------------------------------------------------------------------
# /health surfacing (existing endpoint gains a warnings detail only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_surfaces_quarantine_warnings(
    manager: WorkspaceManager, state_root: Path, monkeypatch: MonkeyPatch
) -> None:
    from httpx import ASGITransport, AsyncClient

    from claude_hub import main as main_module

    state_root.joinpath("index.json").write_text("{bad")
    quarantined = WorkspaceManager()
    assert quarantined.state_recovery_warnings()

    monkeypatch.setattr(
        main_module.workspace_manager,
        "state_recovery_warnings",
        quarantined.state_recovery_warnings,
    )
    async with AsyncClient(
        transport=ASGITransport(app=main_module.app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    body = response.json()
    assert body["status"] == "healthy"
    assert body["warnings"] == quarantined.state_recovery_warnings()

    monkeypatch.setattr(main_module.workspace_manager, "state_recovery_warnings", lambda: [])
    async with AsyncClient(
        transport=ASGITransport(app=main_module.app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.json() == {"status": "healthy", "instance_id": body["instance_id"]}
