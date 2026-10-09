import asyncio
import importlib
import json
import socket
import subprocess
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Callable

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pytest import MonkeyPatch
from starlette.types import Message, Scope

from claude_hub.api.tabs import router as tabs_router
from claude_hub.auth.dependencies import get_current_user
from claude_hub.models import AgentType, ExecutionTarget, SessionKind, TerminalTab, User
from claude_hub.services.ttyd_manager import TabStartupTimeoutError, TTYDManager

api_tabs_module = importlib.import_module("claude_hub.api.tabs")
ttyd_manager_module = importlib.import_module("claude_hub.services.ttyd_manager")


async def _post_tab_then_disconnect(
    create_started: asyncio.Event,
    *,
    on_disconnect: Callable[[], None] | None = None,
    wait_before_disconnect_delivery: asyncio.Event | None = None,
) -> list[Message]:
    """Invoke the tabs router with a real ASGI http.disconnect message."""
    app = FastAPI()
    app.include_router(tabs_router)
    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="local", name="Local User", email="local@localhost", avatar_url=None
    )
    payload = json.dumps(
        {
            "name": "proxy-ab-controlled",
            "agent_type": "codex",
            "session_kind": "chat",
            "cwd": "/tmp/proxy-ab-controlled",
            "env": {"CODEX_MODEL": "gpt-6.1-sol"},
        }
    ).encode()
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": payload, "more_body": False})
    sent: list[Message] = []

    async def receive() -> Message:
        message = await incoming.get()
        if message["type"] == "http.disconnect":
            if on_disconnect is not None:
                on_disconnect()
            if wait_before_disconnect_delivery is not None:
                await wait_before_disconnect_delivery.wait()
        return message

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/tabs",
        "raw_path": b"/api/tabs",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8173),
        "root_path": "",
        "state": {},
    }
    request_task = asyncio.create_task(app(scope, receive, send))
    await create_started.wait()
    await incoming.put({"type": "http.disconnect"})
    await request_task
    return sent


@pytest.mark.asyncio
async def test_list_tabs_empty(client: AsyncClient) -> None:
    """Test that listing tabs returns an empty list when no tabs exist."""
    response = await client.get("/api/tabs")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_create_tab_rejects_retired_agent_session_kind(client: AsyncClient) -> None:
    response = await client.post(
        "/api/tabs",
        json={
            "name": "Retired Agent Surface",
            "agent_type": "claude",
            "session_kind": "agent",
        },
    )

    assert response.status_code == 422
    assert "session_kind" in response.text


@pytest.mark.asyncio
async def test_create_tab_timeout_returns_gateway_timeout(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        raise TabStartupTimeoutError("tab startup timed out after 15s")

    monkeypatch.setattr("claude_hub.api.tabs.ttyd_manager.create_tab", fake_create_tab)

    response = await client.post("/api/tabs", json={"name": "Timed out", "agent_type": "terminal"})

    assert response.status_code == 504
    assert response.json() == {"detail": "tab startup timed out after 15s"}


@pytest.mark.asyncio
async def test_create_tab_asgi_disconnect_cancels_and_waits_for_rollback(
    monkeypatch: MonkeyPatch,
) -> None:
    create_started = asyncio.Event()
    rollback_completed = asyncio.Event()
    captured: dict[str, object] = {}

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        captured.update(kwargs)
        create_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            rollback_completed.set()
        raise AssertionError("unreachable after cancellation")

    monkeypatch.setattr("claude_hub.api.tabs.ttyd_manager.create_tab", fake_create_tab)

    sent = await _post_tab_then_disconnect(create_started)

    assert rollback_completed.is_set()
    assert captured["agent_type"] == AgentType.CODEX
    session_kind = captured["session_kind"]
    assert isinstance(session_kind, SessionKind)
    assert session_kind.value == "chat"
    assert captured["cwd"] == "/tmp/proxy-ab-controlled"
    assert captured["env"] == {"CODEX_MODEL": "gpt-6.1-sol"}
    response_start = next(message for message in sent if message["type"] == "http.response.start")
    assert response_start["status"] == 499


@pytest.mark.asyncio
async def test_create_tab_disconnect_rolls_back_create_completed_before_response(
    monkeypatch: MonkeyPatch,
) -> None:
    create_started = asyncio.Event()
    allow_create = asyncio.Event()
    create_completed = asyncio.Event()
    deleted: list[str] = []
    created = TerminalTab(
        id="completed-before-disconnect-response",
        name="proxy-ab-controlled",
        shell="codex",
        cwd="/tmp/proxy-ab-controlled",
        solo_mode=False,
        agent_type=AgentType.CODEX,
        target=ExecutionTarget.LOCAL,
        remote_profile_id=None,
        remote_cwd=None,
        remote_reconnect=True,
        port=12017,
        created_at=datetime.now(),
        is_active=True,
        workspace_id=None,
    )

    async def fake_create_tab(**kwargs: object) -> TerminalTab:
        create_started.set()
        await allow_create.wait()
        create_completed.set()
        return created

    async def fake_delete_tab(tab_id: str) -> bool:
        deleted.append(tab_id)
        return True

    monkeypatch.setattr("claude_hub.api.tabs.ttyd_manager.create_tab", fake_create_tab)
    monkeypatch.setattr("claude_hub.api.tabs.ttyd_manager.delete_tab", fake_delete_tab)

    sent = await _post_tab_then_disconnect(
        create_started,
        on_disconnect=allow_create.set,
        wait_before_disconnect_delivery=create_completed,
    )

    assert deleted == [created.id]
    response_start = next(message for message in sent if message["type"] == "http.response.start")
    assert response_start["status"] == 499


@pytest.mark.asyncio
@pytest.mark.skipif(
    ttyd_manager_module.shutil.which("tmux") is None
    or ttyd_manager_module.shutil.which("ttyd") is None,
    reason="tmux and ttyd are required",
)
async def test_create_tab_handler_cancellation_rolls_back_real_owned_resources(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """Server-side handler cancellation cannot leave its child create running."""
    socket_name = f"ch-asgi-cancel-{uuid.uuid4().hex[:8]}"
    tmux_tmp = Path("/tmp") / f"ch-tmux-{uuid.uuid4().hex[:8]}"
    tmux_tmp.mkdir(mode=0o700)
    home = tmp_path / "home"
    xdg_config = tmp_path / "xdg-config"
    runtime = tmp_path / "runtime"
    for directory in (home, xdg_config, runtime):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("CLAUDE_HUB_TMUX_SOCKET", socket_name)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_tmp))
    monkeypatch.setattr(ttyd_manager_module, "STATE_FILE", runtime / "tabs.json")
    monkeypatch.setattr(ttyd_manager_module, "ORDER_FILE", runtime / "tab_order.json")
    monkeypatch.setattr(ttyd_manager_module, "LAUNCH_ENV_DIR", runtime / "launch_env")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(ttyd_manager_module.settings, "ttyd_base_port", port)
    manager = TTYDManager()
    monkeypatch.setattr(api_tabs_module, "ttyd_manager", manager)

    app = FastAPI()
    app.include_router(tabs_router)
    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="local", name="Local User", email="local@localhost", avatar_url=None
    )
    payload = json.dumps(
        {"name": "cancelled-handler", "agent_type": "terminal", "cwd": str(tmp_path)}
    ).encode()
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": payload, "more_body": False})

    async def receive() -> Message:
        return await incoming.get()

    async def send(message: Message) -> None:
        pytest.fail(f"cancelled handler must not commit a response: {message}")

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/tabs",
        "raw_path": b"/api/tabs",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8173),
        "root_path": "",
        "state": {},
    }
    handler_task = asyncio.create_task(app(scope, receive, send))
    owned_session = ""
    try:
        for _ in range(200):
            listed = subprocess.run(
                ttyd_manager_module.tmux_command("list-sessions", "-F", "#{session_name}"),
                capture_output=True,
                text=True,
                check=False,
            )
            owned = [
                name
                for name in listed.stdout.splitlines()
                if name.startswith(ttyd_manager_module.TMUX_SESSION_PREFIX)
                and name != "__tmux_server_keepalive__"
            ]
            if owned:
                owned_session = owned[0]
                break
            await asyncio.sleep(0.01)
        assert owned_session

        handler_task.cancel()
        await asyncio.sleep(0)
        handler_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handler_task

        assert manager.processes == {}
        assert manager._tab_order == []
        assert (
            subprocess.run(
                ttyd_manager_module.tmux_command("has-session", "-t", owned_session),
                capture_output=True,
                check=False,
            ).returncode
            != 0
        )
        assert ttyd_manager_module._is_local_port_available(port) is True
    finally:
        if not handler_task.done():
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task
        for tab_id in list(manager.processes):
            await manager.delete_tab(tab_id)
        subprocess.run(
            ttyd_manager_module.tmux_command("kill-server"), capture_output=True, check=False
        )
        ttyd_manager_module.shutil.rmtree(tmux_tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_update_tab_order_route_not_shadowed(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    """Test that the static order route is not handled as a tab ID update."""
    captured_order: list[str] = []

    def fake_set_tab_order(tab_ids: list[str]) -> None:
        captured_order.extend(tab_ids)

    monkeypatch.setattr("claude_hub.api.tabs.ttyd_manager.set_tab_order", fake_set_tab_order)

    response = await client.put("/api/tabs/order", json={"tab_ids": []})

    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert captured_order == []


@pytest.mark.asyncio
async def test_duplicate_tab_route_preserves_solo_mode(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    captured_tab_ids: list[str] = []

    async def fake_duplicate_tab(tab_id: str) -> TerminalTab:
        captured_tab_ids.append(tab_id)
        return TerminalTab(
            id="copy-id",
            name="Codex (copy)",
            shell="codex",
            cwd="/tmp",
            solo_mode=True,
            agent_type=AgentType.CODEX,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12345,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
        )

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.duplicate_tab",
        fake_duplicate_tab,
    )

    response = await client.post("/api/tabs/source-id/duplicate")

    assert response.status_code == 201
    assert captured_tab_ids == ["source-id"]
    data = response.json()
    assert data["name"] == "Codex (copy)"
    assert data["solo_mode"] is True
    assert data["agent_type"] == "codex"


@pytest.mark.asyncio
async def test_duplicate_tab_route_returns_404_for_missing_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_duplicate_tab(tab_id: str) -> None:
        return None

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.duplicate_tab",
        fake_duplicate_tab,
    )

    response = await client.post("/api/tabs/missing-id/duplicate")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_switch_env_route_returns_404_for_missing_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_switch_env(tab_id: str, env, solo_mode=None):
        raise KeyError(tab_id)

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.switch_env",
        fake_switch_env,
    )

    response = await client.post(
        "/api/tabs/missing-id/switch-env",
        json={"env": {"ANTHROPIC_MODEL": "x"}},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_switch_env_route_returns_400_for_invalid_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_switch_env(tab_id: str, env, solo_mode=None):
        raise ValueError("switch_env is only supported for Claude tabs")

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.switch_env",
        fake_switch_env,
    )

    response = await client.post(
        "/api/tabs/codex-tab/switch-env",
        json={"env": {"ANTHROPIC_MODEL": "x"}},
    )
    assert response.status_code == 400
    assert "Claude tabs" in response.json()["detail"]


@pytest.mark.asyncio
async def test_switch_env_route_returns_200_on_success(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_switch_env(tab_id: str, env, solo_mode=None):
        return TerminalTab(
            id=tab_id,
            name="Switched",
            shell=None,
            cwd=None,
            solo_mode=bool(solo_mode),
            agent_type=AgentType.CLAUDE,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12345,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
            env=env,
        )

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.switch_env",
        fake_switch_env,
    )

    response = await client.post(
        "/api/tabs/live-tab/switch-env",
        json={"env": {"ANTHROPIC_MODEL": "claude-opus"}, "solo_mode": True},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "live-tab"
    assert data["solo_mode"] is True
    assert data["env"]["ANTHROPIC_MODEL"] == "claude-opus"


@pytest.mark.asyncio
async def test_archive_tab_route_returns_404_for_missing_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_archive_tab(tab_id: str) -> None:
        return None

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.archive_tab",
        fake_archive_tab,
    )

    response = await client.post("/api/tabs/missing-id/archive")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unarchive_tab_route_returns_404_for_missing_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_unarchive_tab(tab_id: str) -> None:
        return None

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.unarchive_tab",
        fake_unarchive_tab,
    )

    response = await client.post("/api/tabs/missing-id/unarchive")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_archived_route_not_shadowed(client: AsyncClient, monkeypatch: MonkeyPatch) -> None:
    """GET /api/tabs/archived must list archived tabs, not treat "archived" as a tab id."""
    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.list_archived_tabs",
        lambda: [],
    )

    response = await client.get("/api/tabs/archived")

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_archive_tab_route_returns_archived_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    archived_at = datetime.now()

    async def fake_archive_tab(tab_id: str) -> TerminalTab:
        return TerminalTab(
            id=tab_id,
            name="Archived",
            shell=None,
            cwd=None,
            solo_mode=False,
            agent_type=AgentType.CLAUDE,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12345,
            created_at=datetime.now(),
            is_active=False,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
            archived=True,
            archived_at=archived_at,
        )

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.archive_tab",
        fake_archive_tab,
    )

    response = await client.post("/api/tabs/live-tab/archive")

    assert response.status_code == 200
    data = response.json()
    assert data["archived"] is True
    assert data["archived_at"] is not None


async def test_mark_tab_viewed_route_clears_unread(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    def fake_mark_tab_viewed(tab_id: str) -> bool:
        return True

    def fake_get_tab(tab_id: str) -> TerminalTab:
        return TerminalTab(
            id=tab_id,
            name="Viewed",
            shell=None,
            cwd=None,
            solo_mode=False,
            agent_type=AgentType.CLAUDE,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12345,
            created_at=datetime.now(),
            is_active=False,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
            archived=False,
            archived_at=None,
            last_viewed_at=datetime.now(),
            is_unread=False,
        )

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.mark_tab_viewed",
        fake_mark_tab_viewed,
    )
    monkeypatch.setattr("claude_hub.api.tabs.ttyd_manager.get_tab", fake_get_tab)

    response = await client.post("/api/tabs/live-tab/view")

    assert response.status_code == 200
    data = response.json()
    assert data["is_unread"] is False
    assert data["last_viewed_at"] is not None


async def test_mark_tab_viewed_route_returns_404_for_missing_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    def fake_mark_tab_viewed(tab_id: str) -> bool:
        return False

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.mark_tab_viewed",
        fake_mark_tab_viewed,
    )

    response = await client.post("/api/tabs/missing-id/view")

    assert response.status_code == 404
