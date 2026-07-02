"""Non-destructive JSON <-> SQLite migration with round-trip verification.

Both directions:

* Never mutate the source in place.
* Stage the result to a **temporary** target, **verify** the round-trip there
  (reconstructed snapshot must be set-equal to the source: same ids, same JSON
  payloads), and only **then** promote it. If verification fails, the staged
  target is discarded and a :class:`RoundTripError` is raised — a bad migration
  can never overwrite a good source of truth in either direction.
* ``import_json_to_sqlite`` writes a fresh DB and removes it on failure.
  ``export_sqlite_to_json`` builds the JSON tree in a sibling staging dir,
  verifies it, backs up any existing ``state_root`` to ``<state_root>.bak``, and
  atomically swaps the verified tree into place.

Because the live source is never written before verification, rollback is
simply "keep using the source you have".
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import StorageSnapshot
from .json_backend import JsonStorageBackend
from .sqlite_backend import SqliteStorageBackend


class RoundTripError(RuntimeError):
    """Raised when a migration's verification round-trip does not match."""


def _fingerprint(snapshot: StorageSnapshot) -> dict[str, dict[str, str]]:
    """Map each entity kind to {id: canonical-json} for order-independent compare."""
    import json

    def canon(items: list[dict]) -> dict[str, str]:
        return {item["id"]: json.dumps(item, sort_keys=True, ensure_ascii=False) for item in items}

    return {
        "workspaces": canon(snapshot.workspaces),
        "tasks": canon(snapshot.tasks),
        "sessions": canon(snapshot.sessions),
        "reports": canon(snapshot.reports),
    }


def _assert_round_trip(source: StorageSnapshot, reloaded: StorageSnapshot) -> None:
    src = _fingerprint(source)
    dst = _fingerprint(reloaded)
    if src != dst:
        diffs = []
        for kind in src:
            missing = set(src[kind]) - set(dst[kind])
            extra = set(dst[kind]) - set(src[kind])
            changed = {i for i in set(src[kind]) & set(dst[kind]) if src[kind][i] != dst[kind][i]}
            if missing or extra or changed:
                diffs.append(
                    f"{kind}: missing={sorted(missing)} extra={sorted(extra)} "
                    f"changed={sorted(changed)}"
                )
        raise RoundTripError("round-trip mismatch: " + "; ".join(diffs))


def import_json_to_sqlite(state_root: Path, db_path: Path | None = None) -> Path:
    """Import current JSON state into a new SQLite DB. JSON is never modified.

    Returns the path of the written DB. Raises :class:`RoundTripError` (and
    removes the DB) if the SQLite reload does not match the JSON source.
    """
    state_root = Path(state_root)
    target = Path(db_path) if db_path else state_root / "state.sqlite3"
    source = JsonStorageBackend(state_root).load()

    backend = SqliteStorageBackend(target)
    backend.save(source)
    try:
        _assert_round_trip(source, backend.load())
    except RoundTripError:
        for path in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return target


def export_sqlite_to_json(db_path: Path, state_root: Path) -> Path:
    """Export SQLite state back to the JSON layout (rollback direction).

    Non-destructive: the JSON is built in a sibling staging directory and the
    round-trip is verified **before** the live ``state_root`` is touched. Only
    after verification passes is any existing ``state_root`` moved aside to
    ``<state_root>.bak`` and the verified tree swapped into place. On mismatch a
    :class:`RoundTripError` is raised and the live ``state_root`` is left exactly
    as it was. Returns the JSON root.
    """
    db_path = Path(db_path)
    state_root = Path(state_root)
    source = SqliteStorageBackend(db_path).load()

    # Stage into a sibling directory (same parent => same filesystem => atomic
    # os.replace on promotion). Never write into the live state_root first.
    state_root.parent.mkdir(parents=True, exist_ok=True)
    staging = state_root.with_name(state_root.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    try:
        backend = JsonStorageBackend(staging)
        backend.save(source)
        # Verify in staging; on failure the live tree is still untouched.
        _assert_round_trip(source, backend.load())

        # Promote: back up any existing live tree, then swap staging in.
        backup = state_root.with_name(state_root.name + ".bak")
        if state_root.exists():
            if backup.exists():
                shutil.rmtree(backup)
            state_root.rename(backup)
        staging.rename(state_root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return state_root
