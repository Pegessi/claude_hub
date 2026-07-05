"""Read-only SQLite round-trip verification helper.

Given a state root (a directory holding ``index.json`` + per-workspace
``state.json`` files in the current JSON layout), ``verify_state_dir`` loads
the snapshot through :class:`JsonStorageBackend`, writes it into a SQLite DB
in a temporary directory, runs ``PRAGMA integrity_check`` on that DB,
reloads the snapshot back from SQLite, exports it to a fresh JSON root via
:func:`export_sqlite_to_json`, reloads that exported JSON, and compares
fingerprints against the original.

The helper treats ``state_root`` as read-only. It never mutates or creates
files inside ``state_root``: all SQLite and export artifacts live under a
:class:`tempfile.TemporaryDirectory` that is torn down on return.

If you want to verify a *live* state root against which a server may be
writing concurrently, copy the directory first (``shutil.copytree``) and
pass the copy — torn reads from a concurrent :func:`_save_state` would
otherwise surface as :class:`json.JSONDecodeError` and abort verification.
The ``claude-hub storage verify`` CLI does this copy by default.

This module does NOT wire SQLite into the running workspace manager and does
NOT flip any default. It exists so an operator (or CI) can pre-flight the
SQLite backend against real data *before* any phase-4 shadow-write rollout.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from . import StorageSnapshot
from .json_backend import JsonStorageBackend
from .migrate import RoundTripError, export_sqlite_to_json
from .sqlite_backend import SqliteStorageBackend, _run_integrity_check


class VerificationError(RuntimeError):
    """Raised when verification fails (integrity check, round-trip mismatch,
    missing source). The ``report`` attribute holds the partial
    :class:`VerificationReport` for diagnostics."""

    def __init__(self, message: str, report: "VerificationReport") -> None:
        super().__init__(message)
        self.report = report


@dataclass
class EntityDiff:
    """Per-entity-kind diff between source and reloaded snapshots."""

    kind: str
    missing: List[str] = field(default_factory=list)
    extra: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.changed)


@dataclass
class VerificationReport:
    """Structured result of a verification run."""

    state_root: Path
    source_counts: dict = field(default_factory=dict)
    sqlite_counts: dict = field(default_factory=dict)
    exported_json_counts: dict = field(default_factory=dict)
    integrity_ok: bool = False
    sqlite_roundtrip_ok: bool = False
    exported_json_roundtrip_ok: bool = False
    orphan_items: dict = field(default_factory=dict)
    diffs: List[EntityDiff] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    temp_dir: str | None = None  # populated for tests/debugging; None after cleanup
    ok: bool = False

    def to_dict(self) -> dict:
        return {
            "state_root": str(self.state_root),
            "ok": self.ok,
            "integrity_ok": self.integrity_ok,
            "sqlite_roundtrip_ok": self.sqlite_roundtrip_ok,
            "exported_json_roundtrip_ok": self.exported_json_roundtrip_ok,
            "source_counts": self.source_counts,
            "sqlite_counts": self.sqlite_counts,
            "exported_json_counts": self.exported_json_counts,
            "orphan_items": self.orphan_items,
            "warnings": self.warnings,
            "diffs": [
                {
                    "kind": d.kind,
                    "missing": d.missing,
                    "extra": d.extra,
                    "changed": d.changed,
                }
                for d in self.diffs
            ],
        }


def _fingerprint(snapshot: StorageSnapshot) -> dict[str, dict[str, str]]:
    def canon(items: list[dict]) -> dict[str, str]:
        return {
            i["id"]: json.dumps(i, sort_keys=True, ensure_ascii=False) for i in items if "id" in i
        }

    return {
        "workspaces": canon(snapshot.workspaces),
        "tasks": canon(snapshot.tasks),
        "sessions": canon(snapshot.sessions),
        "reports": canon(snapshot.reports),
    }


def _diff(label: str, src: dict[str, dict[str, str]], dst: dict[str, dict[str, str]]) -> EntityDiff:
    """Compare fingerprint dicts and collect missing/extra/changed ids for the
    single entity ``label``. ``src`` and ``dst`` are of the form
    ``{"workspaces": {id: fp, ...}, "tasks": {...}, ...}``; only the
    sub-dictionary for ``label`` is compared."""
    diff = EntityDiff(kind=label)
    s = src.get(label, {})
    d = dst.get(label, {})
    diff.missing = sorted(set(s) - set(d))
    diff.extra = sorted(set(d) - set(s))
    diff.changed = sorted(i for i in set(s) & set(d) if s[i] != d[i])
    return diff


def _count_orphans(snapshot: StorageSnapshot) -> dict:
    """Count tasks/sessions/reports that have no workspace_id (JSON backend
    drops these by design; SQLite phase-2 was taught to match that behavior, so
    round-trip should preserve the absence)."""
    counts = {}
    for kind, items in (
        ("tasks", snapshot.tasks),
        ("sessions", snapshot.sessions),
        ("reports", snapshot.reports),
    ):
        n = sum(1 for i in items if not i.get("workspace_id"))
        if n:
            counts[kind] = n
    return counts


def _snapshot_counts(snapshot: StorageSnapshot) -> dict:
    return {
        "workspaces": len(snapshot.workspaces),
        "tasks": len(snapshot.tasks),
        "sessions": len(snapshot.sessions),
        "reports": len(snapshot.reports),
    }


def verify_state_dir(state_root: Path) -> VerificationReport:
    """Load ``state_root`` JSON, round-trip through SQLite, export back to
    JSON, compare fingerprints, and return a :class:`VerificationReport`.

    Raises :class:`VerificationError` on any failure (missing root, integrity
    failure, round-trip mismatch). The return value always holds ``ok=True``
    on success.

    ``state_root`` is read but never written to; SQLite/export artifacts live
    in a TemporaryDirectory that is cleaned up before return.
    """
    state_root = Path(state_root)
    report = VerificationReport(state_root=state_root)
    index_file = state_root / "index.json"
    if not index_file.exists():
        raise VerificationError(
            f"state_root {state_root} does not contain index.json (not a JSON state directory?)",
            report,
        )

    try:
        source = JsonStorageBackend(state_root).load()
    except (json.JSONDecodeError, FileNotFoundError, OSError) as e:
        raise VerificationError(f"failed to load JSON state from {state_root}: {e}", report) from e

    report.source_counts = _snapshot_counts(source)
    report.orphan_items = _count_orphans(source)
    if report.orphan_items:
        report.warnings.append(
            "found items without workspace_id: "
            + ", ".join(f"{k}={v}" for k, v in report.orphan_items.items())
            + " (these are dropped by JSON backend and by SQLite phase-2+; verify they are expected)"
        )

    # Build an "effective" source fingerprint that strips orphans — both
    # backends silently skip items without workspace_id on save(), so we must
    # not treat their absence after round-trip as a mismatch. Orphans are
    # already surfaced as warnings above.
    def _drop_orphans(snap: StorageSnapshot) -> StorageSnapshot:
        return StorageSnapshot(
            workspaces=list(snap.workspaces),
            tasks=[t for t in snap.tasks if t.get("workspace_id")],
            sessions=[s for s in snap.sessions if s.get("workspace_id")],
            reports=[r for r in snap.reports if r.get("workspace_id")],
        )

    effective_source = _drop_orphans(source)
    src_fp = _fingerprint(effective_source)

    # All SQLite + export work happens in a temp dir so we never write next to
    # state_root.
    with tempfile.TemporaryDirectory(prefix="claude-hub-verify-") as tmp:
        tmp_root = Path(tmp)
        report.temp_dir = str(tmp_root)
        db_path = tmp_root / "state.sqlite3"
        exported_root = tmp_root / "exported-json"

        # 1. Write source snapshot into SQLite.
        sqlite_backend = SqliteStorageBackend(db_path)
        sqlite_backend.save(source)
        reloaded_pre_check = sqlite_backend.load()
        report.sqlite_counts = _snapshot_counts(reloaded_pre_check)

        # 2. Integrity check BEFORE promoting our trust in the DB.
        try:
            _run_integrity_check(db_path)
        except sqlite3.DatabaseError as e:
            raise VerificationError(f"integrity_check failed: {e}", report) from e
        report.integrity_ok = True

        # 3. SQLite -> SQLite round-trip (sanity: load() returns what save() wrote).
        reloaded_sqlite = sqlite_backend.load()
        sql_fp = _fingerprint(_drop_orphans(reloaded_sqlite))
        sqlite_diffs = [_diff(k, src_fp, sql_fp) for k in src_fp]
        report.diffs.extend(sqlite_diffs)
        if any(not d.ok for d in sqlite_diffs):
            raise VerificationError(
                "SQLite round-trip mismatch: " + _format_diffs(sqlite_diffs), report
            )
        report.sqlite_roundtrip_ok = True

        # 4. Export SQLite -> JSON directory via the production export path, then reload.
        try:
            export_sqlite_to_json(db_path, exported_root)
        except (RoundTripError, sqlite3.DatabaseError, OSError) as e:
            raise VerificationError(f"SQLite -> JSON export failed: {e}", report) from e
        reloaded_json = JsonStorageBackend(exported_root).load()
        report.exported_json_counts = _snapshot_counts(reloaded_json)
        json_fp = _fingerprint(_drop_orphans(reloaded_json))
        json_diffs = [_diff(k, src_fp, json_fp) for k in src_fp]
        report.diffs.extend(json_diffs)
        if any(not d.ok for d in json_diffs):
            raise VerificationError(
                "SQLite -> JSON export round-trip mismatch: " + _format_diffs(json_diffs), report
            )
        report.exported_json_roundtrip_ok = True

    report.temp_dir = None
    report.ok = True
    return report


def _format_diffs(diffs: List[EntityDiff]) -> str:
    parts = []
    for d in diffs:
        if d.ok:
            continue
        parts.append(f"{d.kind}: missing={d.missing} extra={d.extra} changed={d.changed}")
    return "; ".join(parts)
