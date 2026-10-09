from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from claude_hub.services.feishu_bot import FeishuBotConfig
from claude_hub.services.feishu_bot_pool import FeishuBotPoolStore, OwnerIdentity
from claude_hub.services.feishu_bot_websocket import (
    FeishuBotWebSocketSupervisor,
    _BoundedRouteTasks,
    _discover_connection_url,
    _LarkConnection,
    _RouteCapacityExceeded,
    message_event_from_sdk,
)


def _config(app_id: str = "cli-a", secret: str = "secret-a") -> FeishuBotConfig:
    return FeishuBotConfig(app_id=app_id, app_secret=secret)


class _FakeConnection:
    def __init__(self, config: FeishuBotConfig, callback: Any) -> None:
        self.config = config
        self.callback = callback
        self.connected = False
        self.closed = False
        self._done = asyncio.Event()

    async def run(self) -> None:
        self.connected = True
        await self._done.wait()

    async def close(self) -> None:
        self.connected = False
        self.closed = True
        self._done.set()


class _HandshakeConnection(_FakeConnection):
    def __init__(self, config: FeishuBotConfig, callback: Any) -> None:
        super().__init__(config, callback)
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.cancelled.set()

    async def close(self) -> None:
        assert self.cancelled.is_set(), "handshake task must be cancelled before close"
        await super().close()


class _FailedConnection(_FakeConnection):
    async def run(self) -> None:
        raise RuntimeError("connection failed")


@pytest.mark.asyncio
async def test_supervisor_reconciles_one_connection_per_effective_bot(tmp_path) -> None:
    pool = FeishuBotPoolStore(
        path=tmp_path / "pool.json",
        legacy_path=tmp_path / "legacy.json",
        now=lambda: 1_700_000_000.0,
    )
    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})
    received: list[tuple[str, Any]] = []
    connections: list[_FakeConnection] = []

    def factory(config: FeishuBotConfig, callback: Any) -> _FakeConnection:
        connection = _FakeConnection(config, callback)
        connections.append(connection)
        return connection

    async def handle(bot: str, event: Any) -> None:
        received.append((bot, event))

    supervisor = FeishuBotWebSocketSupervisor(pool, handle, connection_factory=factory)
    await supervisor.start()
    await asyncio.sleep(0)

    assert len(connections) == 1
    assert connections[0].config == _config()
    assert supervisor.status(bot_id) == "connected"

    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    pool.rotate_secrets(
        bot_id,
        app_secret="secret-b",
        expected_revision=entry.revision,
        environ={},
    )
    await supervisor.reconcile()
    await asyncio.sleep(0)

    assert connections[0].closed is True
    assert len(connections) == 2
    assert connections[1].config.app_secret == "secret-b"

    await connections[1].callback(SimpleNamespace(marker="event"))
    assert received == [(bot_id, SimpleNamespace(marker="event"))]

    await supervisor.stop()
    assert connections[1].closed is True
    assert supervisor.status(bot_id) == "stopped"


@pytest.mark.asyncio
async def test_pairing_revision_keeps_the_connection_and_routes_immediately(tmp_path) -> None:
    pool = FeishuBotPoolStore(
        path=tmp_path / "pool.json",
        legacy_path=tmp_path / "legacy.json",
        now=lambda: 1_700_000_000.0,
    )
    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})
    received: list[Any] = []
    connections: list[_FakeConnection] = []

    def factory(config: FeishuBotConfig, callback: Any) -> _FakeConnection:
        connection = _FakeConnection(config, callback)
        connections.append(connection)
        return connection

    async def handle(_bot: str, event: Any) -> None:
        received.append(event)

    supervisor = FeishuBotWebSocketSupervisor(pool, handle, connection_factory=factory)
    await supervisor.start()
    await asyncio.sleep(0)
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    pool.issue_code(
        bot_id,
        owner=OwnerIdentity(open_id="ou-owner", email="owner@example.test", kind="oauth"),
        tab_id="tab-1",
        workspace_id=None,
        expected_revision=entry.revision,
        environ={},
    )

    event = SimpleNamespace(marker="pairing-code")
    await connections[0].callback(event)
    await supervisor.reconcile()

    assert received == [event]
    assert len(connections) == 1
    assert connections[0].closed is False
    await supervisor.stop()


@pytest.mark.asyncio
async def test_supervisor_stops_disabled_and_deleted_bots(tmp_path) -> None:
    pool = FeishuBotPoolStore(
        path=tmp_path / "pool.json",
        legacy_path=tmp_path / "legacy.json",
        now=lambda: 1_700_000_000.0,
    )
    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})
    connections: list[_FakeConnection] = []

    def factory(config: FeishuBotConfig, callback: Any) -> _FakeConnection:
        connection = _FakeConnection(config, callback)
        connections.append(connection)
        return connection

    async def handle(_bot: str, _event: Any) -> None:
        raise AssertionError("disabled Bot routed an event")

    supervisor = FeishuBotWebSocketSupervisor(pool, handle, connection_factory=factory)
    await supervisor.start()
    await asyncio.sleep(0)
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    pool.update_bot(bot_id, expected_revision=entry.revision, enabled=False, environ={})

    await supervisor.reconcile()

    assert connections[0].closed is True
    assert supervisor.status(bot_id) == "stopped"
    with pytest.raises(_RouteCapacityExceeded, match="no longer active"):
        connections[0].callback(SimpleNamespace(marker="late"))


@pytest.mark.asyncio
async def test_supervisor_cancels_an_inflight_handshake_before_close(tmp_path) -> None:
    pool = FeishuBotPoolStore(
        path=tmp_path / "pool.json",
        legacy_path=tmp_path / "legacy.json",
        now=lambda: 1_700_000_000.0,
    )
    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})
    connections: list[_HandshakeConnection] = []

    def factory(config: FeishuBotConfig, callback: Any) -> _HandshakeConnection:
        connection = _HandshakeConnection(config, callback)
        connections.append(connection)
        return connection

    async def handle(_bot: str, _event: Any) -> None:
        raise AssertionError("no event expected")

    supervisor = FeishuBotWebSocketSupervisor(pool, handle, connection_factory=factory)
    await supervisor.start()
    await connections[0].started.wait()
    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    pool.update_bot(bot_id, expected_revision=entry.revision, enabled=False, environ={})

    await asyncio.wait_for(supervisor.reconcile(), timeout=1)

    assert connections[0].cancelled.is_set()
    assert connections[0].closed is True


@pytest.mark.asyncio
async def test_disabling_a_failed_bot_clears_its_failure_status(tmp_path) -> None:
    pool = FeishuBotPoolStore(
        path=tmp_path / "pool.json",
        legacy_path=tmp_path / "legacy.json",
        now=lambda: 1_700_000_000.0,
    )
    bot_id = pool.create_bot(name="Bot", config=_config(), environ={})

    async def handle(_bot: str, _event: Any) -> None:
        raise AssertionError("no event expected")

    supervisor = FeishuBotWebSocketSupervisor(pool, handle, connection_factory=_FailedConnection)
    await supervisor.start()
    await asyncio.sleep(0)
    assert supervisor.status(bot_id) == "failed"

    entry = pool.snapshot({}).get(bot_id)
    assert entry is not None
    pool.update_bot(bot_id, expected_revision=entry.revision, enabled=False, environ={})
    await supervisor.reconcile()

    assert supervisor.status(bot_id) == "stopped"


@pytest.mark.asyncio
async def test_transport_close_does_not_cancel_an_accepted_message() -> None:
    release = asyncio.Event()
    routed = asyncio.Event()

    async def callback(_event: Any) -> None:
        await release.wait()
        routed.set()

    route_tasks = _BoundedRouteTasks()
    connection = _LarkConnection(_config(), callback, route_tasks=route_tasks)
    route_tasks.submit(callback, SimpleNamespace(marker="accepted"))
    await asyncio.sleep(0)

    await connection.close()

    release.set()
    assert await route_tasks.drain(1) is True
    assert routed.is_set()


@pytest.mark.asyncio
async def test_supervisor_final_stop_drains_accepted_routes(tmp_path) -> None:
    pool = FeishuBotPoolStore(
        path=tmp_path / "pool.json",
        legacy_path=tmp_path / "legacy.json",
        now=lambda: 1_700_000_000.0,
    )
    pool.create_bot(name="Bot", config=_config(), environ={})
    connections: list[_FakeConnection] = []
    route_tasks = _BoundedRouteTasks()
    started = asyncio.Event()
    release = asyncio.Event()
    routed = asyncio.Event()

    def factory(config: FeishuBotConfig, callback: Any) -> _FakeConnection:
        connection = _FakeConnection(config, callback)
        connections.append(connection)
        return connection

    async def handle(_bot: str, _event: Any) -> None:
        started.set()
        await release.wait()
        routed.set()

    supervisor = FeishuBotWebSocketSupervisor(
        pool,
        handle,
        connection_factory=factory,
        route_tasks=route_tasks,
        shutdown_drain_seconds=1,
    )
    await supervisor.start()
    await asyncio.sleep(0)
    route_tasks.submit(connections[0].callback, SimpleNamespace(marker="accepted"))
    await started.wait()

    stopping = asyncio.create_task(supervisor.stop())
    for _ in range(10):
        if connections[0].closed:
            break
        await asyncio.sleep(0)

    assert connections[0].closed is True
    assert stopping.done() is False
    release.set()
    await stopping
    assert routed.is_set()


@pytest.mark.asyncio
async def test_route_capacity_rejects_before_ack_and_shutdown_timeout_cancels() -> None:
    route_tasks = _BoundedRouteTasks(max_tasks=1)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(_event: Any) -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    route_tasks.submit(blocked, SimpleNamespace(marker="first"))
    await started.wait()
    with pytest.raises(_RouteCapacityExceeded, match="at capacity"):
        route_tasks.submit(blocked, SimpleNamespace(marker="second"))

    assert await route_tasks.drain(0) is False
    assert cancelled.is_set()
    with pytest.raises(_RouteCapacityExceeded, match="at capacity"):
        route_tasks.submit(blocked, SimpleNamespace(marker="stopping"))

    route_tasks.start_accepting()


def test_lark_callback_rejects_capacity_synchronously_before_sdk_ack() -> None:
    route_tasks = _BoundedRouteTasks(max_tasks=1)

    async def callback(_event: Any) -> None:
        await asyncio.Future()

    connection = _LarkConnection(_config(), callback, route_tasks=route_tasks)
    route_tasks.stop_accepting()

    with pytest.raises(_RouteCapacityExceeded, match="at capacity"):
        connection._accept_message(SimpleNamespace(marker="overloaded"))


@pytest.mark.asyncio
async def test_connection_discovery_is_bounded_and_applies_sdk_config() -> None:
    sdk = importlib.import_module("lark_oapi.ws.client")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {
                    "URL": "wss://example.test/ws?device_id=device&service_id=7",
                    "ClientConfig": {
                        "ReconnectCount": 4,
                        "ReconnectInterval": 30,
                        "ReconnectNonce": 10,
                        "PingInterval": 45,
                    },
                },
            },
        )

    configured: list[Any] = []
    sdk_client = SimpleNamespace(_configure=configured.append)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        url = await _discover_connection_url(
            sdk_client, _config(), sdk, http_client=http_client, timeout_seconds=0.2
        )

    assert url == "wss://example.test/ws?device_id=device&service_id=7"
    assert len(configured) == 1
    assert configured[0].PingInterval == 45
    assert len(requests) == 1
    assert requests[0].url == "https://open.feishu.cn/callback/ws/endpoint"
    assert requests[0].headers["locale"] == "zh"
    assert json.loads(requests[0].content) == {
        "AppID": "cli-a",
        "AppSecret": "secret-a",
    }


@pytest.mark.asyncio
async def test_connection_discovery_times_out_a_stalled_request() -> None:
    sdk = importlib.import_module("lark_oapi.ws.client")

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.Future()
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(sdk.ServerException, match="endpoint discovery timed out"):
            await asyncio.wait_for(
                _discover_connection_url(
                    SimpleNamespace(_configure=lambda _config: None),
                    _config(),
                    sdk,
                    http_client=http_client,
                    timeout_seconds=0.01,
                ),
                timeout=0.2,
            )


@pytest.mark.asyncio
async def test_connection_discovery_rejects_a_missing_endpoint_url() -> None:
    sdk = importlib.import_module("lark_oapi.ws.client")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(sdk.ServerException, match="invalid endpoint response"):
            await _discover_connection_url(
                SimpleNamespace(_configure=lambda _config: None),
                _config(),
                sdk,
                http_client=http_client,
            )


def test_sdk_message_is_normalized_without_callback_secrets() -> None:
    data = SimpleNamespace(
        header=SimpleNamespace(event_id="evt-1", app_id="cli-a"),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type="user", sender_id=SimpleNamespace(open_id="ou-user")
            ),
            message=SimpleNamespace(
                message_id="om-1",
                chat_id="oc-1",
                chat_type="p2p",
                message_type="text",
                create_time="1700000000000",
                content=json.dumps({"text": " hello "}),
            ),
        ),
    )

    event = message_event_from_sdk(data, expected_app_id="cli-a")

    assert event.event_id == "evt-1"
    assert event.message_id == "om-1"
    assert event.sender_open_id == "ou-user"
    assert event.chat_id == "oc-1"
    assert event.text == "hello"
    assert event.message_created_at_ms == 1_700_000_000_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("header.app_id", "another-app"),
        ("event.sender.sender_type", "app"),
        ("event.message.chat_type", "group"),
        ("event.message.message_type", "image"),
        ("event.message.content", "{}"),
    ],
)
def test_sdk_message_rejects_untrusted_or_unsupported_payloads(field: str, value: str) -> None:
    data = SimpleNamespace(
        header=SimpleNamespace(event_id="evt-1", app_id="cli-a"),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type="user", sender_id=SimpleNamespace(open_id="ou-user")
            ),
            message=SimpleNamespace(
                message_id="om-1",
                chat_id="oc-1",
                chat_type="p2p",
                message_type="text",
                create_time="1700000000000",
                content=json.dumps({"text": "hello"}),
            ),
        ),
    )
    target: Any = data
    parts = field.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)

    with pytest.raises(ValueError):
        message_event_from_sdk(data, expected_app_id="cli-a")
