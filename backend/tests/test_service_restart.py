"""Restart requests are scoped to the running instance and never execute shell input."""

import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from httpx import AsyncClient


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_health_server(tmp_path):
    server = tmp_path / "server.py"
    server.write_text("""
import http.server, json, os
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"status": "healthy", "instance_id": os.environ["CLAUDE_HUB_INSTANCE_ID"]}).encode())
    def log_message(self, *args): pass
http.server.HTTPServer(("127.0.0.1", int(os.environ["TEST_PORT"])), Handler).serve_forever()
""")
    return server


@pytest.mark.asyncio
async def test_unmanaged_server_reports_restart_unavailable(client: AsyncClient, monkeypatch):
    monkeypatch.delenv("CLAUDE_HUB_LAUNCHER_ID", raising=False)
    response = await client.get("/api/system/restart")
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["instance_id"]


@pytest.mark.asyncio
async def test_restart_requires_header_and_current_instance(
    client: AsyncClient, monkeypatch, tmp_path
):
    from claude_hub.services import service_restart as restart

    store = restart.RestartStore(tmp_path)
    launcher_id = str(uuid4())
    store.initialize(launcher_id)
    monkeypatch.setenv("CLAUDE_HUB_LAUNCHER_ID", launcher_id)
    monkeypatch.setattr(restart, "get_store", lambda: store)
    payload = {"instance_id": restart.INSTANCE_ID, "request_id": str(uuid4())}
    assert (await client.post("/api/system/restart", json=payload)).status_code == 403
    headers = {"X-Claude-Hub-Restart": "1"}
    stale = await client.post(
        "/api/system/restart", json={**payload, "instance_id": "old"}, headers=headers
    )
    assert stale.status_code == 409
    accepted = await client.post("/api/system/restart", json=payload, headers=headers)
    assert accepted.status_code == 202
    assert accepted.json()["operation"]["status"] == "preparing"
    # A lost response / concurrent click must not queue a second restart.
    again = await client.post(
        "/api/system/restart", json={**payload, "request_id": str(uuid4())}, headers=headers
    )
    assert again.json()["operation"]["id"] == payload["request_id"]
    assert store.read()["launcher_pid"] == os.getpid()


@pytest.mark.asyncio
async def test_restart_rejects_unmanaged_server(client: AsyncClient, monkeypatch):
    monkeypatch.delenv("CLAUDE_HUB_LAUNCHER_ID", raising=False)
    state = (await client.get("/api/system/restart")).json()
    response = await client.post(
        "/api/system/restart",
        json={"instance_id": state["instance_id"], "request_id": str(uuid4())},
        headers={"X-Claude-Hub-Restart": "1"},
    )
    assert response.status_code == 503


def test_launcher_replaces_only_its_child_and_checks_new_identity(tmp_path):
    from claude_hub.service_launcher import ServiceLauncher
    from claude_hub.services.service_restart import RestartStore

    port = _free_port()
    # A tiny real HTTP child makes shutdown, port release, and instance
    # identity observable without booting tmux or workspace orchestration.
    server = _write_health_server(tmp_path)
    store = RestartStore(tmp_path)
    launcher = ServiceLauncher(
        store,
        [sys.executable, str(server)],
        f"http://127.0.0.1:{port}/health",
        env={**os.environ, "TEST_PORT": str(port)},
        startup_timeout=5,
    )
    store.initialize(launcher.launcher_id)
    try:
        launcher.start_backend()
        launcher.wait_healthy()
        old_child = launcher.child
        old_id = launcher.instance_id
        with store.locked() as state:
            state["operation"] = {"id": "test", "instance_id": old_id, "status": "preparing"}
        launcher.restart()
        assert old_child.poll() is not None
        assert launcher.child.poll() is None
        assert launcher.instance_id != old_id
        assert store.read()["operation"]["status"] == "succeeded"
        # A response from another process must not count as recovery.
        launcher.instance_id = "wrong-instance"
        assert launcher.is_healthy() is False
    finally:
        launcher.stop_backend()


def test_concurrent_requests_share_one_operation_and_completed_retry_is_idempotent(
    tmp_path, monkeypatch
):
    from claude_hub.services import service_restart as restart

    store = restart.RestartStore(tmp_path)
    store.initialize("launcher")
    monkeypatch.setenv("CLAUDE_HUB_LAUNCHER_ID", "launcher")
    monkeypatch.setattr(restart, "get_store", lambda: store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: restart.request_restart(restart.INSTANCE_ID, str(uuid4())), range(20)
            )
        )
    ids = {result["operation"]["id"] for result in results}
    assert len(ids) == 1
    store.update_operation("succeeded", "Back online")
    # A response retry from the old page cannot restart the new process again.
    result = restart.request_restart("previous-instance", ids.pop())
    assert result["operation"]["status"] == "succeeded"


def test_launcher_start_failure_is_recorded_without_retry(tmp_path):
    from claude_hub.service_launcher import ServiceLauncher
    from claude_hub.services.service_restart import RestartStore

    store = RestartStore(tmp_path)
    launcher = ServiceLauncher(
        store, [str(tmp_path / "missing-program")], "http://127.0.0.1:1/health"
    )
    store.initialize(launcher.launcher_id)
    with store.locked() as state:
        state["operation"] = {"id": "failure", "status": "preparing"}
    launcher.restart()
    assert store.read()["operation"]["status"] == "failed"
    assert launcher.child is None


def test_launcher_synchronizes_dependencies_before_replacing_child(tmp_path):
    from claude_hub.service_launcher import ServiceLauncher
    from claude_hub.services.service_restart import RestartStore

    port = _free_port()
    server = _write_health_server(tmp_path)
    marker = tmp_path / "dependencies-synchronized"
    synchronize = tmp_path / "synchronize.py"
    synchronize.write_text("""
import json, os, urllib.request
from pathlib import Path
with urllib.request.urlopen(os.environ["TEST_HEALTH_URL"], timeout=1) as response:
    health = json.load(response)
assert health["instance_id"] == os.environ["EXPECTED_INSTANCE_ID"]
Path("dependencies-synchronized").write_text("ready")
""")
    store = RestartStore(tmp_path)
    launcher = ServiceLauncher(
        store,
        [sys.executable, str(server)],
        f"http://127.0.0.1:{port}/health",
        env={
            **os.environ,
            "TEST_PORT": str(port),
            "TEST_HEALTH_URL": f"http://127.0.0.1:{port}/health",
        },
        startup_timeout=5,
        dependency_sync_command=[sys.executable, str(synchronize)],
        dependency_sync_cwd=tmp_path,
    )
    store.initialize(launcher.launcher_id)
    try:
        launcher.start_backend()
        launcher.wait_healthy()
        old_child = launcher.child
        old_id = launcher.instance_id
        launcher.env["EXPECTED_INSTANCE_ID"] = old_id
        with store.locked() as state:
            state["operation"] = {"id": "sync", "instance_id": old_id, "status": "preparing"}

        launcher.restart()

        assert marker.read_text() == "ready"
        assert old_child.poll() is not None
        assert launcher.child.poll() is None
        assert launcher.instance_id != old_id
        assert store.read()["operation"]["status"] == "succeeded"
    finally:
        launcher.stop_backend()


def test_dependency_sync_failure_keeps_current_backend_running(tmp_path):
    from claude_hub.service_launcher import ServiceLauncher
    from claude_hub.services.service_restart import RestartStore

    port = _free_port()
    server = _write_health_server(tmp_path)
    fail_sync = tmp_path / "fail_sync.py"
    fail_sync.write_text("import sys; print('lock mismatch', file=sys.stderr); raise SystemExit(7)")
    store = RestartStore(tmp_path)
    launcher = ServiceLauncher(
        store,
        [sys.executable, str(server)],
        f"http://127.0.0.1:{port}/health",
        env={**os.environ, "TEST_PORT": str(port)},
        startup_timeout=5,
        dependency_sync_command=[sys.executable, str(fail_sync)],
        dependency_sync_cwd=tmp_path,
    )
    store.initialize(launcher.launcher_id)
    try:
        launcher.start_backend()
        launcher.wait_healthy()
        old_child = launcher.child
        old_id = launcher.instance_id
        with store.locked() as state:
            state["operation"] = {
                "id": "sync-failure",
                "instance_id": old_id,
                "status": "preparing",
            }

        launcher.restart()

        operation = store.read()["operation"]
        assert operation["status"] == "failed"
        assert operation["message"] == (
            "Backend dependency synchronization failed (exit 7). "
            "Run `uv sync --locked --inexact` from backend and try again."
        )
        assert launcher.child is old_child
        assert launcher.child.poll() is None
        assert launcher.instance_id == old_id
        assert launcher.is_healthy() is True
    finally:
        launcher.stop_backend()


def test_dependency_sync_timeout_keeps_current_backend_running(tmp_path):
    from claude_hub.service_launcher import ServiceLauncher
    from claude_hub.services.service_restart import RestartStore

    port = _free_port()
    server = _write_health_server(tmp_path)
    store = RestartStore(tmp_path)
    launcher = ServiceLauncher(
        store,
        [sys.executable, str(server)],
        f"http://127.0.0.1:{port}/health",
        env={**os.environ, "TEST_PORT": str(port)},
        startup_timeout=5,
        dependency_sync_command=[sys.executable, "-c", "import time; time.sleep(60)"],
        dependency_sync_cwd=tmp_path,
        dependency_sync_timeout=0.01,
    )
    store.initialize(launcher.launcher_id)
    try:
        launcher.start_backend()
        launcher.wait_healthy()
        old_child = launcher.child
        old_id = launcher.instance_id
        with store.locked() as state:
            state["operation"] = {"id": "sync-timeout", "status": "preparing"}

        launcher.restart()

        operation = store.read()["operation"]
        assert operation["status"] == "failed"
        assert operation["message"] == (
            "Backend dependency synchronization timed out. "
            "Run `uv sync --locked --inexact` from backend and try again."
        )
        assert launcher.child is old_child
        assert launcher.child.poll() is None
        assert launcher.instance_id == old_id
        assert launcher.is_healthy() is True
    finally:
        launcher.stop_backend()


def test_launcher_main_uses_locked_inexact_dependency_sync(monkeypatch):
    from claude_hub import service_launcher as launcher_module

    captured = {}

    class FakeLauncher:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def run(self):
            return 0

    monkeypatch.setattr(launcher_module, "ServiceLauncher", FakeLauncher)
    monkeypatch.setattr(launcher_module.shutil, "which", lambda command: "/opt/bin/uv")
    monkeypatch.setattr(launcher_module.signal, "signal", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["service-launcher"])

    assert launcher_module.main() == 0
    assert captured["dependency_sync_command"] == [
        "/opt/bin/uv",
        "sync",
        "--locked",
        "--inexact",
    ]
    assert captured["dependency_sync_timeout"] == 20
    assert captured["dependency_sync_cwd"] == (
        tmp_path := launcher_module.Path(launcher_module.__file__).resolve().parents[1]
    )
    assert tmp_path.name == "backend"


def test_new_launcher_marks_unfinished_operation_failed(tmp_path):
    from claude_hub.services.service_restart import RestartStore

    store = RestartStore(tmp_path)
    store.initialize("old")
    with store.locked() as state:
        state["operation"] = {"id": "unfinished", "status": "restarting"}
    store.initialize("new")
    assert store.read()["operation"]["status"] == "failed"


def test_health_probe_uses_matching_loopback_family():
    from claude_hub.service_launcher import health_url

    assert health_url("0.0.0.0", 18174) == "http://127.0.0.1:18174/health"
    assert health_url("::", 18174) == "http://[::1]:18174/health"
    assert health_url("::1", 18174) == "http://[::1]:18174/health"


def test_restart_drains_live_sse_and_runs_lifespan_cleanup(tmp_path):
    """A real open stream must not consume the launcher's 30s kill budget."""
    from claude_hub.service_launcher import ServiceLauncher
    from claude_hub.services.service_restart import RestartStore

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    (tmp_path / "stream_fixture.py").write_text("""
import asyncio, os
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
@asynccontextmanager
async def lifespan(app):
    yield
    Path(os.environ["CLEANUP_MARKER"]).write_text("cleaned")
app = FastAPI(lifespan=lifespan)
@app.get("/health")
async def health():
    return {"status":"healthy", "instance_id":os.environ["CLAUDE_HUB_INSTANCE_ID"]}
@app.get("/events")
async def events():
    async def stream():
        while True:
            yield "data: alive\\n\\n"
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream")
""")
    marker = tmp_path / "cleanup.txt"
    store = RestartStore(tmp_path)
    launcher = ServiceLauncher(
        store,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "stream_fixture:app",
            "--app-dir",
            str(tmp_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        f"http://127.0.0.1:{port}/health",
        env={**os.environ, "CLEANUP_MARKER": str(marker)},
        startup_timeout=10,
    )
    store.initialize(launcher.launcher_id)
    stream = None
    try:
        launcher.start_backend()
        launcher.wait_healthy()
        old_id = launcher.instance_id
        stream = launcher.http.open(f"http://127.0.0.1:{port}/events", timeout=5)
        assert stream.readline().startswith(b"data:")
        with store.locked() as state:
            state["operation"] = {"id": "stream", "instance_id": old_id, "status": "preparing"}
        started = time.monotonic()
        launcher.restart()
        elapsed = time.monotonic() - started
        assert marker.exists(), "SIGKILL skipped application shutdown"
        assert elapsed < 10, f"stream restart took {elapsed:.2f}s"
        assert launcher.instance_id != old_id
        assert store.read()["operation"]["status"] == "succeeded"
    finally:
        if stream is not None:
            stream.close()
        launcher.stop_backend()


@pytest.mark.asyncio
async def test_stream_shutdown_flushes_sessions_concurrently_before_returning():
    import asyncio
    from types import SimpleNamespace

    from claude_hub.services.agent_stream.tailer import TailerManager

    manager = TailerManager.__new__(TailerManager)
    manager._lock = asyncio.Lock()
    entered = set()
    flushed = set()
    all_entered = asyncio.Event()

    async def stop(session_id):
        entered.add(session_id)
        if len(entered) == 8:
            all_entered.set()
        await asyncio.wait_for(all_entered.wait(), timeout=1)
        flushed.add(session_id)

    from functools import partial

    manager._tailers = {
        str(i): SimpleNamespace(session_id=str(i), stop=partial(stop, str(i))) for i in range(8)
    }
    await manager.stop_all()
    assert flushed == {str(i) for i in range(8)}
    assert manager._tailers == {}
