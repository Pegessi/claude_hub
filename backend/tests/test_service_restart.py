"""Restart requests are scoped to the running instance and never execute shell input."""

import os
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from httpx import AsyncClient


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

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # A tiny real HTTP child makes shutdown, port release, and instance
    # identity observable without booting tmux or workspace orchestration.
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
