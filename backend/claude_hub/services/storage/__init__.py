"""Pluggable persistence backends for workspace state (spike / additive).

This package introduces a small ``StorageBackend`` abstraction that captures the
serialization boundary the workspace manager already uses:
``model_dump(mode="json")`` on save, model reconstruction on load. Two backends
are provided:

* :class:`~claude_hub.services.storage.json_backend.JsonStorageBackend` — a
  faithful, atomic-write-capable extraction of the *current* nested-JSON
  behavior. This is the default.
* :class:`~claude_hub.services.storage.sqlite_backend.SqliteStorageBackend` — a
  stdlib ``sqlite3`` prototype storing one JSON-blob row per entity, keyed by
  ``id``/``workspace_id``, with a ``schema_meta`` version table.

**Nothing here is wired into the running workspace manager yet.** The manager's
``_save_state``/``_load_state`` are unchanged; enabling the ``sqlite`` backend via
``settings.workspace_storage_backend`` only affects :func:`get_storage_backend`,
which the hot path does not call. This keeps the spike strictly additive with
JSON as the default. See ``docs/working-logs/2026-07-03-sqlite-persistence-safety-spike.md``.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "StorageSnapshot",
    "StorageBackend",
    "atomic_write_text",
    "get_storage_backend",
    "SCHEMA_VERSION",
    "ShadowStorageBackend",
    "ShadowDrift",
    "ShadowDriftWarning",
    "ShadowError",
    "assert_path_outside_root",
]

# Bump when the on-disk layout (not the pydantic models) changes in a way that
# needs a forward migration. The JSON payloads are versioned by the pydantic
# models + the manager's ``_normalize_*`` helpers, not by this number.
SCHEMA_VERSION = 1


@dataclass
class StorageSnapshot:
    """A backend-agnostic snapshot of persisted workspace state.

    Each list holds ``model_dump(mode="json")`` dicts — exactly the payloads the
    workspace manager already produces in ``_save_state`` and consumes in
    ``_load_state``. Keeping the snapshot as plain JSON-able dicts (not pydantic
    models) keeps the storage layer decoupled from the schema module and means a
    backend never needs to know about model evolution.
    """

    workspaces: list[dict] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)
    sessions: list[dict] = field(default_factory=list)
    reports: list[dict] = field(default_factory=list)


@runtime_checkable
class StorageBackend(Protocol):
    """Minimal read/write contract for a workspace-state backend."""

    def load(self) -> StorageSnapshot:
        """Return the full persisted snapshot (empty snapshot if none yet)."""
        ...

    def save(self, snapshot: StorageSnapshot) -> None:
        """Persist the full snapshot durably."""
        ...


def atomic_write_text(path: Path, data: str, *, keep_backup: bool = True) -> None:
    """Write ``data`` to ``path`` atomically (temp file + ``os.replace``).

    A crash / disk-full mid-write can never truncate the live file: the partial
    write lands on a sibling temp file, and ``os.replace`` is atomic on POSIX.
    When ``keep_backup`` is set and ``path`` already exists, the prior contents
    are preserved at ``path`` + ``.bak`` (one-deep rolling backup) before the
    replace. This is the immediate, backend-independent data-loss fix for the
    current JSON path, which today calls ``Path.write_text`` directly on the
    live file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if keep_backup and path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            backup.write_bytes(path.read_bytes())
        except OSError:
            # A failed backup must not block the (safer) atomic write.
            pass
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # Clean up the temp file on any failure; never leave the live file
        # half-written.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def get_storage_backend(
    state_root: Path | None = None, *, backend: str | None = None
) -> StorageBackend:
    """Return the configured storage backend (default: JSON).

    ``backend`` overrides the configured value (useful for tests). Unknown
    values fall back to JSON — a misconfigured flag must never silently pick a
    non-default backend. **This function is intentionally not called by the
    running workspace manager yet; it exists so a follow-up wiring task can flip
    the manager onto it behind the flag.**
    """
    from ...config import settings  # local import: avoid import cycle at module load

    root = (
        Path(state_root) if state_root is not None else (Path.home() / ".claude_hub" / "workspaces")
    )
    choice = (backend or getattr(settings, "workspace_storage_backend", "json") or "json").lower()

    if choice == "sqlite":
        from .sqlite_backend import SqliteStorageBackend

        return SqliteStorageBackend(root / "state.sqlite3")

    from .json_backend import JsonStorageBackend

    return JsonStorageBackend(root)


# Lazy imports for shadow-write helpers: avoid loading shadow (which pulls in
# verify + sqlite_backend) at package import time unless a caller actually
# asks for it. The default server path does not touch these names.
def __getattr__(name: str):  # type: ignore[no-untyped-def]  # pragma: no cover - trivial lazy shim
    if name in {
        "ShadowStorageBackend",
        "ShadowDrift",
        "ShadowDriftWarning",
        "ShadowError",
        "assert_path_outside_root",
    }:
        from .shadow import (  # noqa: WPS433  (intentional lazy import)
            ShadowDrift,
            ShadowDriftWarning,
            ShadowError,
            ShadowStorageBackend,
            assert_path_outside_root,
        )

        globals()[name] = locals()[name]
        return locals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
