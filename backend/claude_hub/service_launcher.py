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
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

from .services.backend_instance_lock import BackendInstanceLock
from .services.runtime_isolation import resolve_runtime_home
from .services.service_restart import ACTIVE_STATUSES, RestartStore

FRONTEND_BUILD_TERMINATE_TIMEOUT = 1.0
FRONTEND_BUILD_KILL_TIMEOUT = 5.0


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
        frontend_build_command: list[str] | None = None,
        frontend_build_cwd: Path | None = None,
        frontend_dist_directory: Path | None = None,
        frontend_build_timeout: float = 120,
    ) -> None:
        self.store = store
        self.command = command
        self.health_url = health_url
        self.env = dict(os.environ if env is None else env)
        self.startup_timeout = startup_timeout
        self.dependency_sync_command = dependency_sync_command
        self.dependency_sync_cwd = dependency_sync_cwd
        self.dependency_sync_timeout = dependency_sync_timeout
        self.frontend_build_command = frontend_build_command
        self.frontend_build_cwd = frontend_build_cwd
        self.frontend_dist_directory = frontend_dist_directory
        self.frontend_build_timeout = frontend_build_timeout
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

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
            return True
        except ProcessLookupError:
            return False

    @classmethod
    def _stop_build_process_group(cls, process: subprocess.Popen[bytes]) -> None:
        process_group_id = process.pid
        for sig, timeout in (
            (signal.SIGTERM, FRONTEND_BUILD_TERMINATE_TIMEOUT),
            (signal.SIGKILL, FRONTEND_BUILD_KILL_TIMEOUT),
        ):
            try:
                os.killpg(process_group_id, sig)
            except ProcessLookupError:
                process.wait()
                return
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                process.poll()  # Reap the group leader as soon as it exits.
                if not cls._process_group_exists(process_group_id):
                    process.wait()
                    return
                time.sleep(0.05)

        if cls._process_group_exists(process_group_id):
            raise RuntimeError(
                "Frontend build timed out and its process group could not be stopped."
            )
        process.wait()

    def build_frontend(self) -> Path | None:
        if self.frontend_build_command is None:
            return None
        if self.frontend_build_cwd is None or self.frontend_dist_directory is None:
            raise RuntimeError("Frontend build paths are not configured.")

        build_directory = Path(
            tempfile.mkdtemp(prefix=".claude-hub-build-", dir=self.frontend_build_cwd)
        )
        build_env = {
            **self.env,
            # vite.config.ts reads this fixed launcher-owned destination. The
            # live dist remains intact until a complete build is available.
            "CLAUDE_HUB_FRONTEND_OUT_DIR": str(build_directory),
        }
        try:
            try:
                process = subprocess.Popen(
                    self.frontend_build_command,
                    cwd=self.frontend_build_cwd,
                    env=build_env,
                    start_new_session=True,
                )
                try:
                    returncode = process.wait(timeout=self.frontend_build_timeout)
                except subprocess.TimeoutExpired:
                    self._stop_build_process_group(process)
                    raise
                except BaseException:
                    # An external launcher stop or Ctrl+C must not orphan the
                    # pnpm/vue-tsc/Vite group while unwinding the supervisor.
                    if self._process_group_exists(process.pid):
                        self._stop_build_process_group(process)
                    raise
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "Frontend build timed out. Run `pnpm build` from frontend and try again."
                ) from exc
            except OSError as exc:
                raise RuntimeError(
                    f"Frontend build could not start: {exc}. "
                    "Run `pnpm build` from frontend and try again."
                ) from exc
            if returncode != 0:
                raise RuntimeError(
                    f"Frontend build failed (exit {returncode}). "
                    "Run `pnpm build` from frontend and try again."
                )
            if not (build_directory / "index.html").is_file():
                raise RuntimeError(
                    "Frontend build did not produce index.html. "
                    "Run `pnpm build` from frontend and try again."
                )
            return build_directory
        except BaseException:
            shutil.rmtree(build_directory, ignore_errors=True)
            raise

    def promote_frontend(self, build_directory: Path | None) -> None:
        if build_directory is None:
            return
        if self.frontend_build_cwd is None or self.frontend_dist_directory is None:
            shutil.rmtree(build_directory, ignore_errors=True)
            raise RuntimeError("Frontend build paths are not configured.")

        backup_directory = self.frontend_build_cwd / f".claude-hub-dist-{uuid4()}"
        promoted = False
        backup_created = False
        try:
            # Promotion happens only after the old backend stops, keeping the
            # old SPA/API pair intact throughout preparation. Adjacent renames
            # keep rollback on the same filesystem.
            if self.frontend_dist_directory.exists():
                self.frontend_dist_directory.replace(backup_directory)
                backup_created = True
            try:
                build_directory.replace(self.frontend_dist_directory)
                promoted = True
            except OSError as promote_error:
                if backup_created:
                    try:
                        backup_directory.replace(self.frontend_dist_directory)
                        backup_created = False
                    except OSError as restore_error:
                        raise RuntimeError(
                            "Frontend build promotion failed and the previous frontend "
                            f"could not be restored from {backup_directory}."
                        ) from restore_error
                    raise RuntimeError(
                        "Frontend build promotion failed. The previous frontend was restored."
                    ) from promote_error
                raise RuntimeError("Frontend build promotion failed.") from promote_error
        finally:
            if not promoted and not backup_created and build_directory.exists():
                shutil.rmtree(build_directory, ignore_errors=True)
            if promoted and backup_directory.exists():
                shutil.rmtree(backup_directory, ignore_errors=True)

    def restart(self) -> None:
        started = time.monotonic()
        build_directory: Path | None = None
        try:
            if self.dependency_sync_command is not None:
                self.store.update_operation(
                    "preparing",
                    "Synchronizing backend dependencies. The current service is still available…",
                )
            self.synchronize_dependencies()
            if self.frontend_build_command is not None:
                self.store.update_operation(
                    "preparing",
                    "Building the frontend. The current service is still available…",
                )
            build_directory = self.build_frontend()
            self.store.update_operation(
                "restarting",
                (
                    "Frontend built. Restarting the service and waiting for it to reconnect…"
                    if self.frontend_build_command is not None
                    else "Restarting the service. Waiting for it to reconnect…"
                ),
            )
            self.stop_backend()
            stopped = time.monotonic()
            print(f"Restart: backend stopped in {stopped - started:.2f}s", flush=True)
            candidate = build_directory
            build_directory = None
            try:
                self.promote_frontend(candidate)
            except RuntimeError as promotion_error:
                # Promotion restores the previous dist whenever possible. Bring
                # the API/status endpoint back before reporting the failure.
                try:
                    self.start_backend()
                    self.wait_healthy()
                except (OSError, RuntimeError) as recovery_error:
                    raise RuntimeError(
                        f"{promotion_error} Backend recovery also failed: {recovery_error}"
                    ) from recovery_error
                raise promotion_error
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
        finally:
            if build_directory is not None:
                shutil.rmtree(build_directory, ignore_errors=True)

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
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        print("Error: pnpm is required to build the frontend during restart.", file=sys.stderr)
        return 1
    frontend_directory = backend_directory.parent / "frontend"
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
        frontend_build_command=[pnpm, "build"],
        frontend_build_cwd=frontend_directory,
        frontend_dist_directory=frontend_directory / "dist",
        frontend_build_timeout=90,
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
