"""Small, durable mailbox shared by the API and its independent launcher."""

import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from .runtime_isolation import resolve_runtime_home

INSTANCE_ID = os.environ.get("CLAUDE_HUB_INSTANCE_ID") or str(uuid4())
ACTIVE_STATUSES = {"preparing", "restarting"}


class RestartStore:
    def __init__(self, runtime_home: Path):
        self.directory = runtime_home / "restart"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "state.json"

    def read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        with (self.directory / "request.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.read()
            yield state
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state))
            temporary.replace(self.path)

    def initialize(self, launcher_id: str) -> None:
        with self.locked() as state:
            # An externally replaced launcher must not claim the old request succeeded.
            operation = state.get("operation")
            if operation and operation["status"] in ACTIVE_STATUSES:
                operation.update(status="failed", message="The restart was interrupted.")
            state.update(launcher_id=launcher_id, launcher_pid=os.getpid(), operation=operation)

    def update_operation(self, status: str, message: str) -> None:
        with self.locked() as state:
            state["operation"].update(status=status, message=message)


def get_store() -> RestartStore:
    return RestartStore(resolve_runtime_home())


def is_available(state: dict[str, Any]) -> bool:
    launcher_id = os.environ.get("CLAUDE_HUB_LAUNCHER_ID")
    if not launcher_id or state.get("launcher_id") != launcher_id:
        return False
    try:
        pid = int(state["launcher_pid"])
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (KeyError, TypeError, ValueError, OSError):
        return False


def public_status(state: dict[str, Any]) -> dict[str, Any]:
    available = is_available(state)
    return {
        "instance_id": INSTANCE_ID,
        "available": available,
        "reason": "" if available else "Start the service with ./start.sh to enable restart.",
        "operation": state.get("operation"),
    }


def request_restart(instance_id: str, request_id: str) -> dict[str, Any]:
    with get_store().locked() as state:
        if not is_available(state):
            raise RuntimeError("Restart is unavailable. Start the service with ./start.sh.")
        operation = state.get("operation")
        # Retries of a completed request are also idempotent.
        if operation and operation["id"] == request_id:
            return public_status(state)
        if instance_id != INSTANCE_ID:
            raise ValueError("The service changed. Reopen the restart dialog before trying again.")
        if not operation or operation["status"] not in ACTIVE_STATUSES:
            state["operation"] = {
                "id": request_id,
                "instance_id": instance_id,
                "status": "preparing",
                "message": "Preparing the service. The current service is still available.",
                "requested_at": time.time(),
            }
        return public_status(state)
