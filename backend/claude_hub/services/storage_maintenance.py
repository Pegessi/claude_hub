"""Offline, verified legacy-source archival for the unified SQLite store.

Automatic imports retain original bytes for recovery. This explicit maintenance
step replaces those stale sources with owner-only gzip archives after SQLite has
committed them. It never operates while a backend owns the runtime directory.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable

from .backend_instance_lock import BackendInstanceLock
from .local_storage import connection, has_stored_document, storage_location
from .runtime_isolation import resolve_state_root

TOP_LEVEL_DOCUMENTS = (
    "tabs.json",
    "tab_order.json",
    "sessions.json",
    "chat_messages.json",
    "goal_runs.json",
    "subagents.json",
    "feishu_bot.json",
    "secrets/feishu_bot_pool.json",
    "secrets/feishu_bot.json",
)


def legacy_sources(root: Path, *, workspace_root: Path | None = None) -> list[Path]:
    """Bounded inventory of migrated stores, including old rotating backups."""
    workspace_root = workspace_root or root / "workspaces"
    documents = [root / name for name in TOP_LEVEL_DOCUMENTS]
    documents += [workspace_root / "index.json", workspace_root / "scheduled_tasks.json"]
    documents += sorted(workspace_root.glob("*/state.json"))
    documents += sorted(workspace_root.glob("*/agent_streams/*.cursor.json"))
    paths = documents + [path.with_name(path.name + ".bak") for path in documents]
    paths += sorted(workspace_root.glob("*/agent_streams/*.jsonl"))
    return [path for path in paths if path.is_file()]


def _digest(path: Path, *, compressed: bool = False) -> bytes:
    digest = hashlib.sha256()
    opener = gzip.open if compressed else open
    with opener(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def archive_sources(paths: Iterable[Path]) -> int:
    """Archive committed sources without silent overwrite or uncertain deletion.

    Caller holds the runtime's backend lock. Corrupt stream lines are included
    byte-for-byte in the archive as well as SQLite's stream_errors table.
    """
    total = 0
    for path in paths:
        primary = (
            path.with_name(path.name.removesuffix(".bak")) if path.name.endswith(".bak") else path
        )
        if primary.suffix == ".jsonl":
            with connection(primary) as db:
                _, name = storage_location(primary)
                if db.execute("SELECT 1 FROM streams WHERE name=?", (name,)).fetchone() is None:
                    raise ValueError(f"stream has not been imported: {primary}")
        elif not has_stored_document(primary):
            raise ValueError(f"document has not been imported: {primary}")
        target = path.with_name(path.name + ".gz")
        original = _digest(path)
        if not target.exists():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=target.name + ".", suffix=".tmp", dir=target.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as raw:
                    with (
                        gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as archive,
                        path.open("rb") as source,
                    ):
                        shutil.copyfileobj(source, archive)
                    raw.flush()
                    os.fsync(raw.fileno())
                if _digest(temporary, compressed=True) != original or _digest(path) != original:
                    raise ValueError(f"source changed during archive: {path}")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        if _digest(target, compressed=True) != original or _digest(path) != original:
            raise ValueError(f"archive does not match source: {path}")
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
            path.unlink()
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        total += target.stat().st_size
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    os.environ["CLAUDE_HUB_HOME"] = str(root)
    workspace_root = resolve_state_root()
    with BackendInstanceLock(root / "backend.lock"):
        print(
            f"Archived bytes: {archive_sources(legacy_sources(root, workspace_root=workspace_root))}"
        )


if __name__ == "__main__":
    main()
