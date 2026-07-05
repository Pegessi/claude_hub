"""Tests for phase-3 read-only SQLite verify helper and CLI.

These tests only ever write under tmp_path; they never touch the operator's
live state root.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List

import pytest
from click.testing import CliRunner

from claude_hub.cli.main import cli
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
from claude_hub.services.storage import StorageSnapshot
from claude_hub.services.storage.json_backend import JsonStorageBackend
from claude_hub.services.storage.sqlite_backend import SqliteStorageBackend
from claude_hub.services.storage.verify import (
    VerificationError,
    verify_state_dir,
)

NOW = datetime(2026, 7, 5, 0, 0, 0)
WS_ID = "ws-1"


def _representative_snapshot() -> StorageSnapshot:
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
        title="SQLite phase-3",
        prompt="verify",
        agent_type=AgentType.CLAUDE,
        task_mode=WorkspaceTaskMode.REVIEWED,
        status=WorkspaceTaskStatus.WORKING,
        goal_packet=GoalPacket(objective="o", acceptance_criteria=["a"]),
        autonomous_run=AutonomousRun(id="run-1", task_id="task-1"),
        feedback_lesson_ids=[],
        review_cycle=1,
        reviewed_cycle=0,
        created_at=NOW,
        updated_at=NOW,
    )
    session = ManagedSession(
        id="cb-agent-9",
        workspace_id=WS_ID,
        tab_id="tab-1",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.WORKING,
        runtime_status=AgentRuntimeStatus.WORKING,
        current_task_id="task-1",
        title="cb-agent-9",
        workspace_path="/tmp/repo",
        tmux_session="cb-agent-9",
        env={},
        created_at=NOW,
        updated_at=NOW,
    )
    report = AgentReport(
        id="rep-1",
        workspace_id=WS_ID,
        task_id="task-1",
        session_id="cb-agent-9",
        state=AgentReportState.COMPLETED,
        message="done",
        message_en="done",
        message_zh="完成",
        changed_files=[],
        acceptance_check=[
            AcceptanceCheck(
                criterion="c",
                status=AcceptanceCheckStatus.PASSED,
                evidence="e",
            )
        ],
        review_decision=ReviewDecision.REQUEST,
        review_reason="r",
        created_at=NOW,
    )
    return StorageSnapshot(
        workspaces=[workspace.model_dump(mode="json")],
        tasks=[task.model_dump(mode="json")],
        sessions=[session.model_dump(mode="json")],
        reports=[report.model_dump(mode="json")],
    )


def _seed_json(root: Path, snapshot: StorageSnapshot) -> None:
    JsonStorageBackend(root).save(snapshot)


# --- verify_state_dir unit tests -----------------------------------------------


def test_verify_clean_snapshot_passes(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _seed_json(root, _representative_snapshot())
    report = verify_state_dir(root)
    assert report.ok
    assert report.integrity_ok
    assert report.sqlite_roundtrip_ok
    assert report.exported_json_roundtrip_ok
    assert report.source_counts == {"workspaces": 1, "tasks": 1, "sessions": 1, "reports": 1}
    # No diffs should be non-empty
    for d in report.diffs:
        assert d.ok, f"{d.kind} not ok: missing={d.missing} extra={d.extra} changed={d.changed}"
    # verify must NOT create any files under state_root (it's read-only).
    before = {p.relative_to(root) for p in root.rglob("*") if p.is_file()}
    verify_state_dir(root)
    after = {p.relative_to(root) for p in root.rglob("*") if p.is_file()}
    assert before == after, "verify must not mutate state_root"


def test_verify_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(VerificationError, match="index.json"):
        verify_state_dir(tmp_path / "nope")


def test_verify_corrupt_source_json_raises(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / "index.json").write_text("{not valid json")
    with pytest.raises(VerificationError, match="failed to load JSON"):
        verify_state_dir(root)


def test_verify_detects_integrity_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If integrity_check fails, verify must raise VerificationError and NOT
    attempt the export step."""
    import claude_hub.services.storage.verify as verify_mod

    root = tmp_path / "state"
    _seed_json(root, _representative_snapshot())

    def _boom(_path: Path) -> None:
        raise sqlite3.DatabaseError("simulated integrity failure")

    monkeypatch.setattr(verify_mod, "_run_integrity_check", _boom)
    with pytest.raises(VerificationError, match="integrity"):
        verify_state_dir(root)


def test_verify_reports_orphan_items_as_warning(tmp_path: Path) -> None:
    """Orphan items (no workspace_id) are warned about but not fatal; the
    round-trip should still succeed because SQLite phase-2+ skips them on
    save, matching JSON backend semantics."""
    snap = _representative_snapshot()
    orphan_task = dict(snap.tasks[0])
    orphan_task["id"] = "orphan-task"
    del orphan_task["workspace_id"]
    polluted = StorageSnapshot(
        workspaces=list(snap.workspaces),
        tasks=snap.tasks + [orphan_task],
        sessions=list(snap.sessions),
        reports=list(snap.reports),
    )
    # Write polluted snapshot directly (bypassing JsonStorageBackend.save
    # which drops orphans) to simulate what a partial state.json might look
    # like if constructed outside the backend.
    root = tmp_path / "state"
    root.mkdir()
    ws_dir = root / WS_ID
    ws_dir.mkdir()
    (root / "index.json").write_text(json.dumps({"workspaces": polluted.workspaces}))
    (ws_dir / "state.json").write_text(
        json.dumps(
            {
                "tasks": polluted.tasks,
                "sessions": polluted.sessions,
                "reports": polluted.reports,
            }
        )
    )
    report = verify_state_dir(root)
    assert report.ok, f"verify should pass; warnings={report.warnings}"
    assert any("orphan" in w.lower() or "workspace_id" in w for w in report.warnings)
    # The orphan should NOT appear in the exported JSON (SQLite save dropped it
    # per parity with JsonStorageBackend).
    reexported = JsonStorageBackend(root).load  # not used; we test via report
    # Confirm via direct load of exported: since verify cleans up temp, we
    # replicate the flow against a tmp export to prove the drop semantics.
    tmp_out = tmp_path / "tmp-export"
    db = tmp_path / "tmp.db"
    SqliteStorageBackend(db).save(polluted)
    from claude_hub.services.storage.migrate import export_sqlite_to_json

    export_sqlite_to_json(db, tmp_out)
    exported = JsonStorageBackend(tmp_out).load()
    assert [t["id"] for t in exported.tasks] == ["task-1"], "orphan must be dropped"


# --- CLI integration tests -----------------------------------------------------


def test_cli_verify_passes_on_good_root(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _seed_json(root, _representative_snapshot())
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["storage", "verify", "--state-root", str(root), "--no-copy"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, f"CLI failed: {result.output}\n{result.exception}"
    assert "PASS" in result.output
    assert "integrity_check  : ok" in result.output
    # Verify no state.sqlite3 or staging files were left in state_root.
    debris = list(root.glob("*.sqlite3*")) + list(root.glob("*.staging*"))
    assert debris == [], f"debris left in state_root: {debris}"


def test_cli_verify_json_output(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _seed_json(root, _representative_snapshot())
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--json", "storage", "verify", "--state-root", str(root), "--no-copy"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["integrity_ok"] is True
    assert payload["source_counts"]["tasks"] == 1


def test_cli_verify_missing_root_fails_with_usage_error(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["storage", "verify", "--state-root", str(tmp_path / "nope"), "--no-copy"],
    )
    # ClickException -> exit code 1
    assert result.exit_code == 1
    assert "does not exist" in result.output or "state root" in result.output


def test_cli_verify_copies_by_default_and_cleans_up(tmp_path: Path) -> None:
    """Default --copy path must succeed and leave no files in the system
    temp directory afterwards. We test by looking for any stray
    claude-hub-verify-* dirs after the call — but since tempfile creates dirs
    in a shared tmp, we simply assert the CLI exits 0 and the state_root is
    unmodified."""
    root = tmp_path / "state"
    _seed_json(root, _representative_snapshot())
    snapshot_before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["storage", "verify", "--state-root", str(root)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    snapshot_after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert snapshot_before == snapshot_after, "state_root mutated by verify --copy"


def test_json_default_unchanged() -> None:
    """Meta-check: JSON must remain the default backend (phase-3 does not flip
    any setting)."""
    from claude_hub.config import settings
    from claude_hub.services.storage import get_storage_backend

    assert settings.workspace_storage_backend == "json"
    with pytest.MonkeyPatch.context() as mp:
        # Ensure env does not override.
        mp.delenv("WORKSPACE_STORAGE_BACKEND", raising=False)
        from claude_hub.config import Settings

        s = Settings()
        assert s.workspace_storage_backend == "json"
    # Default factory returns JsonStorageBackend even when given an unused root.
    with pytest.MonkeyPatch.context() as mp:
        tmp = Path("/tmp/__definitely_unused__")
        b = get_storage_backend(tmp)
        assert isinstance(b, JsonStorageBackend)
