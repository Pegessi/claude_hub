"""Production backend owner. The UI can request a restart without owning it.

Invoked by start.sh, never by an HTTP request. Only this launcher's direct
child is stopped; an occupied port or a different instance is not a target.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

from .services.backend_instance_lock import BackendInstanceLock
from .services.runtime_isolation import resolve_runtime_home
from .services.service_restart import ACTIVE_STATUSES, RestartStore


class ServiceLauncher:
    def __init__(
        self,
        store: RestartStore,
        command: list[str],
        health_url: str,
        *,
        env: dict[str, str] | None = None,
        startup_timeout: float = 120,
        dependency_sync_command: list[str] | None = None,
        dependency_sync_cwd: Path | None = None,
        dependency_sync_timeout: float = 120,
    ) -> None:
        self.store = store
        self.command = command
        self.health_url = health_url
        self.env = dict(os.environ if env is None else env)
        self.startup_timeout = startup_timeout
        self.dependency_sync_command = dependency_sync_command
        self.dependency_sync_cwd = dependency_sync_cwd
        self.dependency_sync_timeout = dependency_sync_timeout
        self.launcher_id = str(uuid4())
        self.instance_id = ""
        self.child: subprocess.Popen[bytes] | None = None
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start_backend(self) -> None:
        self.instance_id = str(uuid4())
        env = {
            **self.env,
            "CLAUDE_HUB_LAUNCHER_ID": self.launcher_id,
            "CLAUDE_HUB_INSTANCE_ID": self.instance_id,
            # Uvicorn otherwise waits forever for SSE responses before it
            # enters lifespan shutdown. Leave the rest of the 30s outer
            # budget for flushing streams and releasing owned ttyd children.
            "UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN": "3",
        }
        self.child = subprocess.Popen(self.command, env=env, start_new_session=True)

    def is_healthy(self) -> bool:
        if self.child is None or self.child.poll() is not None:
            return False
        try:
            with self.http.open(self.health_url, timeout=1) as response:
                health = json.load(response)
            return bool(
                isinstance(health, dict)
                and health.get("status") == "healthy"
                and health.get("instance_id") == self.instance_id
                and self.child.poll() is None
            )
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def wait_healthy(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.child is None or self.child.poll() is not None:
                raise RuntimeError(
                    "The backend exited before becoming ready. Check the service logs."
                )
            if self.is_healthy():
                return
            time.sleep(0.25)
        raise RuntimeError("The backend did not become ready in time. Check the service logs.")

    def stop_backend(self) -> None:
        if self.child is None or self.child.poll() is not None:
            return
        self.child.terminate()
        try:
            self.child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()

    def synchronize_dependencies(self) -> None:
        if self.dependency_sync_command is None:
            return
        try:
            result = subprocess.run(
                self.dependency_sync_command,
                cwd=self.dependency_sync_cwd,
                env=self.env,
                timeout=self.dependency_sync_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "Backend dependency synchronization timed out. "
                "Run `uv sync --locked --inexact` from backend and try again."
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"Backend dependency synchronization failed (exit {result.returncode}). "
                "Run `uv sync --locked --inexact` from backend and try again."
            )

    def restart(self) -> None:
        started = time.monotonic()
        try:
            self.synchronize_dependencies()
            self.store.update_operation(
                "restarting", "Restarting the service. Waiting for it to reconnect…"
            )
            self.stop_backend()
            stopped = time.monotonic()
            print(f"Restart: backend stopped in {stopped - started:.2f}s", flush=True)
            self.start_backend()
            self.wait_healthy()
        except (OSError, RuntimeError) as exc:
            self.store.update_operation("failed", str(exc))
            # Keep the launcher and a slow-starting child observable. Never
            # silently retry an operation that might interrupt fresh work.
            print(f"Restart failed: {exc}", flush=True)
        else:
            print(
                f"Restart: new backend ready in {time.monotonic() - stopped:.2f}s; "
                f"total {time.monotonic() - started:.2f}s",
                flush=True,
            )
            self.store.update_operation("succeeded", "The service is back online.")

    def run(self) -> int:
        with BackendInstanceLock(self.store.directory / "launcher.lock"):
            self.store.initialize(self.launcher_id)
            try:
                self.start_backend()
                self.wait_healthy()
                while self.child is not None and self.child.poll() is None:
                    operation = self.store.read().get("operation")
                    if operation and operation["status"] == "preparing":
                        self.restart()
                    time.sleep(0.25)
                return self.child.returncode if self.child is not None else 1
            finally:
                self.stop_backend()
                operation = self.store.read().get("operation")
                if operation and operation["status"] in ACTIVE_STATUSES:
                    self.store.update_operation("failed", "The launcher stopped during restart.")


def health_url(host: str, port: int) -> str:
    probe_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if ":" in probe_host:
        probe_host = f"[{probe_host}]"
    return f"http://{probe_host}:{port}/health"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8173)
    args = parser.parse_args()
    backend_directory = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    if uv is None:
        print("Error: uv is required to run the supervised backend.", file=sys.stderr)
        return 1
    launcher = ServiceLauncher(
        RestartStore(resolve_runtime_home()),
        [
            sys.executable,
            "-m",
            "uvicorn",
            "claude_hub.main:app",
            "--host",
            args.host,
            "--port",
            str(args.port),
        ],
        health_url(args.host, args.port),
        dependency_sync_command=[uv, "sync", "--locked", "--inexact"],
        dependency_sync_cwd=backend_directory,
        dependency_sync_timeout=20,
    )

    def stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        return launcher.run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
