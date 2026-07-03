"""Non-destructive JSON <-> SQLite migration with round-trip verification.

Both directions:

* Never mutate the source in place.
* Stage the result to a **temporary** target (``<target>.staging`` for
  ``import_json_to_sqlite``; ``<state_root>.staging`` for
  ``export_sqlite_to_json``), **verify** the round-trip there (reconstructed
  snapshot must be set-equal to the source: same ids, same JSON payloads),
  and only **then** promote it. If verification fails, the staged target is
  discarded and a :class:`RoundTripError` is raised — a bad migration can
  never overwrite a good source of truth or a pre-existing destination file
  in either direction.
* ``import_json_to_sqlite`` builds its DB in a ``<target>.staging`` sibling,
  checkpoints WAL, backs up any existing ``<target>`` to ``<target>.bak``
  (one-deep rolling backup), and atomically ``os.replace``s the verified DB
  into place; on failure only the staging files are removed.
* ``export_sqlite_to_json`` builds the JSON tree in a sibling staging dir,
  verifies it, backs up any existing ``state_root`` to ``<state_root>.bak``,
  and atomically swaps the verified tree into place.

Because the live source and the pre-existing destination are never written
before verification, rollback on either failure mode is simply "keep using
the source / destination you already have".
"""

from __future__ import annotations

import os
import shutil
import sqlite3
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


def _delete_db_files(path: Path) -> None:
    """Delete a SQLite DB and its WAL/SHM sidecars (if present). Missing files are ignored."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix) if suffix else path
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _checkpoint_db(path: Path) -> None:
    """Force a WAL checkpoint (TRUNCATE) so the DB is a single self-contained file.

    After this returns, the ``-wal`` / ``-shm`` sidecars are removed and all
    committed data is in the main DB file — safe to ``os.replace`` alone.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def import_json_to_sqlite(state_root: Path, db_path: Path | None = None) -> Path:
    """Import current JSON state into a SQLite DB. The JSON source is never modified.

    Safety properties:

    * The DB is built in a **staging** sibling file (``<target>.staging``); the
      final ``target`` path is not touched until the staged DB passes round-trip
      verification. A pre-existing ``target`` (and its ``-wal`` / ``-shm``
      sidecars) is therefore never overwritten, truncated, or deleted on a
      failed import.
    * If the staged DB fails the round-trip check, only the staging files are
      deleted and :class:`RoundTripError` is raised — the original target (if
      any) remains byte-identical.
    * On success any existing ``target`` is moved aside to ``<target>.bak`` (a
      one-deep rolling backup, consistent with :func:`atomic_write_text` and
      :func:`export_sqlite_to_json`) before the verified staging DB is
      atomically ``os.replace``d into place.
    * Returns the path of the written DB.
    """
    state_root = Path(state_root)
    target = Path(db_path) if db_path else state_root / "state.sqlite3"
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".staging")

    # Never write directly to target; always start from a clean staging file.
    _delete_db_files(staging)

    source = JsonStorageBackend(state_root).load()
    try:
        backend = SqliteStorageBackend(staging)
        backend.save(source)
        _assert_round_trip(source, backend.load())
        # Ensure staging is a single self-contained file before promotion
        # (merge + remove WAL/SHM sidecars).
        _checkpoint_db(staging)

        # Promote: back up any existing DB (one-deep rolling .bak) then atomically
        # move the verified staging DB into place.
        backup = target.with_name(target.name + ".bak")
        if target.exists():
            _delete_db_files(backup)
            target.rename(backup)
            # Also tuck the existing sidecars away to the .bak location so a
            # future rollback by renaming .bak back has its WAL/SHM alongside.
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(target) + suffix)
                if sidecar.exists():
                    sidecar.rename(Path(str(backup) + suffix))
        os.replace(staging, target)
    except BaseException:
        # On any failure (round-trip error, OS error, etc.) only delete staging.
        # The final target — and any pre-existing DB at that path — is untouched.
        _delete_db_files(staging)
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
