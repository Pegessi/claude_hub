"""Process-wide ownership lock for Claude Hub's local mutable state."""

import fcntl
import os
from pathlib import Path
from typing import TextIO


class BackendInstanceLock:
    """Prevent two backend workers from mutating the same local state files.

    The API port is bound only after FastAPI startup completes, while startup
    itself restores tmux sessions and persists ``tabs.json``.  Port conflicts
    therefore cannot protect state from two concurrently-starting workers.
    This advisory lock is acquired before any startup recovery and is held for
    the full application lifespan.  A stale lock file is harmless: ``flock``
    ownership is released by the kernel when the owning process exits.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: TextIO | None = None

    def __enter__(self) -> "BackendInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.seek(0)
            owner = lock_file.read().strip() or "unknown process"
            lock_file.close()
            raise RuntimeError(f"Backend process {owner} already owns Claude Hub state") from exc

        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        self._file = lock_file
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._file is None:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
