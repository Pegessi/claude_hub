"""JSON storage backend — faithful extraction of the current nested layout.

The running workspace manager persists state as:

* ``<root>/index.json`` — ``{"workspaces": [<workspace dict>, ...]}``
* ``<root>/<workspace_id>/state.json`` — ``{"tasks": [...], "sessions": [...],
  "reports": [...]}`` filtered to that workspace.

This backend reproduces exactly that layout so it is drop-in compatible with the
existing on-disk state, with one safety upgrade: writes go through
:func:`~claude_hub.services.storage.atomic_write_text` (temp file + ``os.replace``
+ one-deep ``.bak``) instead of a raw ``Path.write_text``. Reading tolerates a
missing/empty root by returning an empty snapshot. It does **not** write the
``snapshot.md`` human summary — that stays owned by the workspace manager.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import StorageSnapshot, atomic_write_text


class JsonStorageBackend:
    """Nested-JSON backend matching the manager's current on-disk format."""

    def __init__(self, root: Path, *, atomic: bool = True) -> None:
        self.root = Path(root)
        self.atomic = atomic

    @property
    def index_file(self) -> Path:
        return self.root / "index.json"

    def _workspace_state_file(self, workspace_id: str) -> Path:
        return self.root / workspace_id / "state.json"

    def _write(self, path: Path, data: str) -> None:
        if self.atomic:
            atomic_write_text(path, data)
        else:  # pragma: no cover - parity escape hatch, not used by default
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data, encoding="utf-8")

    def load(self) -> StorageSnapshot:
        snapshot = StorageSnapshot()
        if not self.index_file.exists():
            return snapshot
        index = json.loads(self.index_file.read_text(encoding="utf-8"))
        snapshot.workspaces = list(index.get("workspaces", []))
        for workspace in snapshot.workspaces:
            workspace_id = workspace.get("id")
            if not workspace_id:
                continue
            state_file = self._workspace_state_file(workspace_id)
            if not state_file.exists():
                continue
            data = json.loads(state_file.read_text(encoding="utf-8"))
            snapshot.tasks.extend(data.get("tasks", []))
            snapshot.sessions.extend(data.get("sessions", []))
            snapshot.reports.extend(data.get("reports", []))
        return snapshot

    def save(self, snapshot: StorageSnapshot) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._write(
            self.index_file,
            json.dumps({"workspaces": snapshot.workspaces}, indent=2),
        )
        # Group per workspace, matching the manager's per-workspace state.json.
        by_workspace: dict[str, dict[str, list[dict]]] = {}
        for workspace in snapshot.workspaces:
            workspace_id = workspace.get("id")
            if workspace_id:
                by_workspace.setdefault(workspace_id, {"tasks": [], "sessions": [], "reports": []})
        for key, items in (
            ("tasks", snapshot.tasks),
            ("sessions", snapshot.sessions),
            ("reports", snapshot.reports),
        ):
            for item in items:
                workspace_id = item.get("workspace_id")
                if not workspace_id:
                    continue
                bucket = by_workspace.setdefault(
                    workspace_id, {"tasks": [], "sessions": [], "reports": []}
                )
                bucket[key].append(item)
        for workspace_id, payload in by_workspace.items():
            self._write(
                self._workspace_state_file(workspace_id),
                json.dumps(payload, indent=2),
            )
