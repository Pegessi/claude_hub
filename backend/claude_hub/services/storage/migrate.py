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
        # Match the busy_timeout used by the backend so checkpoint waits out
        # any concurrent writer instead of failing immediately.
        from .sqlite_backend import _BUSY_TIMEOUT_MS  # local import avoids cycle at module load

        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _rename_db_with_sidecars(src: Path, dst: Path) -> None:
    """Rename a SQLite DB and its -wal/-shm sidecars from ``src`` to ``dst``.

    All-or-nothing with best-effort rollback:

    * Collect the set of files that actually exist (main DB plus any
      ``-wal`` / ``-shm`` sidecars present).
    * Rename them one at a time.
    * If any rename raises :class:`OSError`, **every file that was already
      moved is renamed back** to its original ``src`` location (in reverse
      order) before the exception propagates. This ensures callers either
      see the full DB set moved to ``dst`` or see the full DB set still at
      ``src`` — never a split state where the main file landed at ``dst``
      but a sidecar (or vice versa) was left stranded.
    * If a rollback rename itself fails, the original exception is still
      raised and the affected files are left where they landed (so an
      operator can manually recover; we never delete data).
    * Missing sidecars are silently skipped (a cleanly-checkpointed DB
      has none).
    """
    pairs: list[tuple[Path, Path]] = [(src, dst)]
    for suffix in ("-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.exists():
            pairs.append((s, Path(str(dst) + suffix)))

    moved: list[tuple[Path, Path]] = []
    try:
        for s, d in pairs:
            s.rename(d)
            moved.append((s, d))
    except OSError:
        # Best-effort rollback: move files back to their src locations in
        # reverse order so the main DB is the last to move back.
        for s, d in reversed(moved):
            try:
                d.rename(s)
            except OSError:
                # Leave what exists; never delete to "fix" the state — an
                # operator can manually recover from a partial layout but
                # not from deleted data.
                pass
        raise


def import_json_to_sqlite(state_root: Path, db_path: Path | None = None) -> Path:
    """Import current JSON state into a SQLite DB. The JSON source is never modified.

    Safety properties:

    * The DB is built in a **staging** sibling file (``<target>.staging``); the
      final ``target`` path is not touched until the staged DB passes round-trip
      verification AND ``PRAGMA integrity_check``. A pre-existing ``target``
      (and its ``-wal`` / ``-shm`` sidecars) is therefore never overwritten,
      truncated, or deleted during build or on a round-trip failure.
    * If the staged DB fails the round-trip or integrity check, only the
      staging files are deleted and :class:`RoundTripError` (or a
      ``sqlite3.DatabaseError``) is raised — the original target (if any)
      remains byte-identical.
    * On success any existing ``target`` is moved aside to ``<target>.bak`` (a
      one-deep rolling backup, consistent with :func:`atomic_write_text` and
      :func:`export_sqlite_to_json`) before the verified staging DB is
      atomically ``os.replace``d into place. If that final ``os.replace`` fails
      for any reason, the ``.bak`` is restored back to ``target`` before the
      exception propagates, so the pre-existing DB is never left missing.
    * Returns the path of the written DB.
    """
    from .sqlite_backend import _run_integrity_check  # local import: keep module-light

    state_root = Path(state_root)
    target = Path(db_path) if db_path else state_root / "state.sqlite3"
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".staging")

    # Never write directly to target; always start from a clean staging file.
    _delete_db_files(staging)

    source = JsonStorageBackend(state_root).load()
    backup = target.with_name(target.name + ".bak")
    backed_up = False
    try:
        backend = SqliteStorageBackend(staging)
        backend.save(source)
        _assert_round_trip(source, backend.load())
        # Collapse WAL so staging is single-file AND then verify integrity
        # before we touch the live target.
        _checkpoint_db(staging)
        _run_integrity_check(staging)

        # Promote: back up any existing DB (one-deep rolling .bak) then atomically
        # move the verified staging DB into place.
        if target.exists():
            _delete_db_files(backup)
            _rename_db_with_sidecars(target, backup)
            backed_up = True
        os.replace(staging, target)
    except BaseException:
        # On any failure, clean up staging and — if we already moved the
        # original target aside to .bak but the final os.replace did not
        # succeed — restore the backup so the pre-existing DB is back at the
        # expected path and not left missing.
        _delete_db_files(staging)
        if backed_up and backup.exists() and not target.exists():
            try:
                _rename_db_with_sidecars(backup, target)
            except OSError:
                # If even the restore fails (e.g. permissions, cross-fs),
                # leave the .bak in place rather than silently dropping it.
                # The caller can manually rename it back; we do not delete it.
                pass
            else:
                backed_up = False
        raise
    return target


def export_sqlite_to_json(db_path: Path, state_root: Path) -> Path:
    """Export SQLite state back to the JSON layout (rollback direction).

    Non-destructive: the JSON is built in a sibling staging directory and the
    round-trip is verified **before** the live ``state_root`` is touched. The
    source DB is integrity-checked before staging is promoted. Only after
    verification passes is any existing ``state_root`` moved aside to
    ``<state_root>.bak`` and the verified tree swapped into place. If the final
    rename fails, the prior ``state_root`` is restored from ``.bak`` (symmetric
    with :func:`import_json_to_sqlite`). On mismatch or integrity failure a
    :class:`RoundTripError` / ``sqlite3.DatabaseError`` is raised and the live
    ``state_root`` is left exactly as it was. Returns the JSON root.
    """
    from .sqlite_backend import _run_integrity_check  # local import: keep module-light

    db_path = Path(db_path)
    state_root = Path(state_root)

    # Refuse to migrate a corrupt DB. This runs BEFORE we touch state_root.
    if db_path.exists():
        _run_integrity_check(db_path)

    source = SqliteStorageBackend(db_path).load()

    # Stage into a sibling directory (same parent => same filesystem => atomic
    # os.replace on promotion). Never write into the live state_root first.
    state_root.parent.mkdir(parents=True, exist_ok=True)
    staging = state_root.with_name(state_root.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)

    backup = state_root.with_name(state_root.name + ".bak")
    backed_up = False
    try:
        backend = JsonStorageBackend(staging)
        backend.save(source)
        # Verify in staging; on failure the live tree is still untouched.
        _assert_round_trip(source, backend.load())

        # Promote: back up any existing live tree, then swap staging in.
        if state_root.exists():
            if backup.exists():
                shutil.rmtree(backup)
            state_root.rename(backup)
            backed_up = True
        staging.rename(state_root)
    except BaseException:
        # Clean up staging on any failure.
        if staging.exists():
            shutil.rmtree(staging)
        # If we moved the live tree aside to .bak but the final rename did
        # not succeed, restore the backup so state_root is not missing.
        if backed_up and backup.exists() and not state_root.exists():
            try:
                backup.rename(state_root)
            except OSError:
                # Leave .bak in place for manual recovery; never delete.
                pass
            else:
                backed_up = False
        raise
    return state_root
