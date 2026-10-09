"""Process-level Feishu long-connection runtime for the configured Bot pool."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Awaitable, Callable, Protocol

import httpx

from claude_hub.services.feishu_bot import FeishuBotConfig, FeishuMessageEvent
from claude_hub.services.feishu_bot_pool import (
    EffectiveBot,
    FeishuBotPoolError,
    FeishuBotPoolStore,
)

logger = logging.getLogger(__name__)

_RECONCILE_SECONDS = 5.0
_CLOSE_TIMEOUT_SECONDS = 3.0
_DISCOVERY_TIMEOUT_SECONDS = 15.0
_MAX_ROUTE_TASKS = 32
_SHUTDOWN_DRAIN_SECONDS = 30.0
_MAX_INPUT_CHARS = 4_000
_MAX_MESSAGE_TIME_MS = 4_102_444_800_000


class _Connection(Protocol):
    @property
    def connected(self) -> bool: ...

    async def run(self) -> None: ...

    async def close(self) -> None: ...


ConnectionFactory = Callable[[FeishuBotConfig, Callable[[Any], Awaitable[None]]], _Connection]
EventHandler = Callable[[str, Any], Awaitable[None]]
SdkModules = tuple[Any, Any]


class _SdkLoader(Protocol):
    async def load(self) -> SdkModules: ...


class _RouteCapacityExceeded(RuntimeError):
    """Raised synchronously so the SDK returns a retryable failure response."""


class _LarkSdkLoader:
    """Import lark-oapi once in a process-wide, event-loop-safe flight."""

    def __init__(self, *, import_module: Callable[[str], Any] = importlib.import_module) -> None:
        self._import_module = import_module
        self._lock = threading.Lock()
        self._future: Future[SdkModules] | None = None

    def prewarm(self) -> None:
        self._start()

    async def load(self) -> SdkModules:
        # The underlying future cannot be cancelled by an individual Bot or
        # during shutdown. Import runs on one daemon thread and all connections
        # await the same result without occupying the Hub event loop.
        return await asyncio.shield(asyncio.wrap_future(self._start()))

    def _start(self) -> Future[SdkModules]:
        with self._lock:
            if self._future is not None:
                return self._future
            future: Future[SdkModules] = Future()
            self._future = future
            thread = threading.Thread(
                target=self._import_and_publish,
                args=(future,),
                name="feishu-lark-sdk-import",
                daemon=True,
            )
            thread.start()
            return future

    def _import_and_publish(self, future: Future[SdkModules]) -> None:
        started = time.perf_counter()
        try:
            modules = (
                self._import_module("lark_oapi"),
                self._import_module("lark_oapi.ws.client"),
            )
        except BaseException as exc:
            logger.warning(
                "Feishu WebSocket stage=sdk_import outcome=failed duration_ms=%.1f error_type=%s",
                (time.perf_counter() - started) * 1000,
                type(exc).__name__,
            )
            future.set_exception(exc)
        else:
            logger.info(
                "Feishu WebSocket stage=sdk_import outcome=completed duration_ms=%.1f",
                (time.perf_counter() - started) * 1000,
            )
            future.set_result(modules)


_LARK_SDK_LOADER = _LarkSdkLoader()


def prewarm_lark_sdk() -> None:
    """Start the process-wide SDK import before the first Bot needs it."""

    _LARK_SDK_LOADER.prewarm()


async def _timed_connection_stage(stage: str, operation: Awaitable[Any]) -> Any:
    started = time.perf_counter()
    try:
        result = await operation
    except asyncio.CancelledError:
        logger.info(
            "Feishu WebSocket stage=%s outcome=cancelled duration_ms=%.1f",
            stage,
            (time.perf_counter() - started) * 1000,
        )
        raise
    except Exception as exc:
        logger.warning(
            "Feishu WebSocket stage=%s outcome=failed duration_ms=%.1f error_type=%s",
            stage,
            (time.perf_counter() - started) * 1000,
            type(exc).__name__,
        )
        raise
    logger.info(
        "Feishu WebSocket stage=%s outcome=completed duration_ms=%.1f",
        stage,
        (time.perf_counter() - started) * 1000,
    )
    return result


class _BoundedRouteTasks:
    """Own accepted message tasks across individual transport lifetimes."""

    def __init__(self, max_tasks: int = _MAX_ROUTE_TASKS) -> None:
        if max_tasks < 1:
            raise ValueError("max_tasks must be positive")
        self._max_tasks = max_tasks
        self._tasks: set[asyncio.Future[Any]] = set()
        self._accepting = True

    def start_accepting(self) -> None:
        if self._accepting:
            return
        if self._tasks:
            raise RuntimeError("cannot reopen Feishu routing while tasks are still active")
        self._accepting = True

    def stop_accepting(self) -> None:
        self._accepting = False

    def submit(self, callback: Callable[[Any], Awaitable[None]], data: Any) -> None:
        if not self._accepting or len(self._tasks) >= self._max_tasks:
            raise _RouteCapacityExceeded("Feishu message routing is at capacity")
        task: asyncio.Future[None] = asyncio.ensure_future(callback(data))
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Future[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("Feishu WebSocket message routing failed")

    async def drain(self, timeout_seconds: float) -> bool:
        """Stop intake and wait for accepted work, cancelling only at the deadline."""

        self.stop_accepting()
        tasks = set(self._tasks)
        if not tasks:
            return True
        _done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout_seconds))
        if not pending:
            return True
        logger.warning(
            "Cancelling %d Feishu message route(s) after %.1fs shutdown drain",
            len(pending),
            timeout_seconds,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.difference_update(tasks)
        return False


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Feishu message {name} is missing")
    return value.strip()


def message_event_from_sdk(data: Any, *, expected_app_id: str) -> FeishuMessageEvent:
    """Normalize the SDK model after the long connection authenticated it."""

    header = getattr(data, "header", None)
    envelope = getattr(data, "event", None)
    sender = getattr(envelope, "sender", None)
    message = getattr(envelope, "message", None)
    sender_id = getattr(sender, "sender_id", None)
    app_id = _required_text(getattr(header, "app_id", None), "app_id")
    if app_id != expected_app_id:
        raise ValueError("Feishu message app_id does not match this Bot")
    if getattr(sender, "sender_type", None) != "user":
        raise ValueError("Only Feishu user messages are supported")
    if getattr(message, "chat_type", None) != "p2p":
        raise ValueError("Only Feishu p2p messages are supported")
    if getattr(message, "message_type", None) != "text":
        raise ValueError("Only Feishu text messages are supported")
    try:
        content = json.loads(getattr(message, "content", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Feishu text message content is invalid") from exc
    text = content.get("text") if isinstance(content, dict) else None
    text = _required_text(text, "text")
    if len(text) > _MAX_INPUT_CHARS:
        raise ValueError("Feishu text message is too long")
    raw_created_at = getattr(message, "create_time", None)
    if (
        not isinstance(raw_created_at, str)
        or not raw_created_at.isascii()
        or not raw_created_at.isdecimal()
        or not 1 <= len(raw_created_at) <= 13
    ):
        raise ValueError("Feishu message create_time is invalid")
    created_at = int(raw_created_at)
    if created_at > _MAX_MESSAGE_TIME_MS:
        raise ValueError("Feishu message create_time is invalid")
    return FeishuMessageEvent(
        event_id=_required_text(getattr(header, "event_id", None), "event_id"),
        message_id=_required_text(getattr(message, "message_id", None), "message_id"),
        app_id=app_id,
        sender_open_id=_required_text(getattr(sender_id, "open_id", None), "sender open_id"),
        chat_id=_required_text(getattr(message, "chat_id", None), "chat_id"),
        text=text,
        message_created_at_ms=created_at,
    )


async def _discover_connection_url(
    client: Any,
    config: FeishuBotConfig,
    sdk: Any,
    *,
    http_client: httpx.AsyncClient | None = None,
    timeout_seconds: float = _DISCOVERY_TIMEOUT_SECONDS,
) -> str:
    """Fetch one endpoint without the SDK's unbounded synchronous request."""

    async def request(requester: httpx.AsyncClient) -> httpx.Response:
        return await requester.post(
            config.api_base_url + sdk.GEN_ENDPOINT_URI,
            headers={"locale": "zh"},
            json={"AppID": config.app_id, "AppSecret": config.app_secret},
        )

    try:
        async with asyncio.timeout(timeout_seconds):
            if http_client is None:
                async with httpx.AsyncClient(timeout=timeout_seconds) as requester:
                    response = await request(requester)
            else:
                response = await request(http_client)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise sdk.ServerException(
            HTTPStatus.GATEWAY_TIMEOUT, "endpoint discovery timed out"
        ) from exc

    if response.status_code != HTTPStatus.OK:
        raise sdk.ServerException(response.status_code, "system busy")

    try:
        result = sdk.JSON.unmarshal(response.text, sdk.EndpointResp)
    except (TypeError, ValueError) as exc:
        raise sdk.ServerException(HTTPStatus.BAD_GATEWAY, "invalid endpoint response") from exc
    if result.code == sdk.OK:
        pass
    elif result.code == sdk.SYSTEM_BUSY:
        raise sdk.ServerException(result.code, "system busy")
    elif result.code == sdk.INTERNAL_ERROR:
        raise sdk.ServerException(result.code, result.msg)
    else:
        raise sdk.ClientException(result.code, result.msg)

    endpoint = result.data
    url = getattr(endpoint, "URL", None)
    if not isinstance(url, str) or not url.strip():
        raise sdk.ServerException(HTTPStatus.BAD_GATEWAY, "invalid endpoint response")
    client_config = getattr(endpoint, "ClientConfig", None)
    if client_config is not None:
        client._configure(client_config)
    return url.strip()


class _LarkConnection:
    """A bounded, stoppable adapter around lark-oapi 1.5.3's private async core."""

    def __init__(
        self,
        config: FeishuBotConfig,
        callback: Callable[[Any], Awaitable[None]],
        *,
        route_tasks: _BoundedRouteTasks,
        sdk_loader: _SdkLoader = _LARK_SDK_LOADER,
    ) -> None:
        self._config = config
        self._callback = callback
        self._route_tasks = route_tasks
        self._sdk_loader = sdk_loader
        self._client: Any = None
        self._receive_task: asyncio.Task[Any] | None = None
        self._ping_task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._client is not None and getattr(self._client, "_conn", None) is not None

    async def run(self) -> None:
        lark, lark_ws_client = await self._sdk_loader.load()

        loop = asyncio.get_running_loop()
        setattr(lark_ws_client, "loop", loop)

        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._accept_message)
            .build()
        )
        client = lark.ws.Client(
            self._config.app_id,
            self._config.app_secret,
            event_handler=dispatcher,
            log_level=lark.LogLevel.WARNING,
            domain=self._config.api_base_url,
            auto_reconnect=False,
        )
        self._client = client
        original_receive = client._receive_message_loop

        async def receive() -> None:
            self._receive_task = asyncio.current_task()
            await original_receive()

        client._receive_message_loop = receive
        try:
            initial_url = await _timed_connection_stage(
                "endpoint_discovery",
                _discover_connection_url(client, self._config, lark_ws_client),
            )
            client._get_conn_url = lambda: initial_url
            await _timed_connection_stage("websocket_handshake", client._connect())
            # ``_connect`` schedules the receive loop. Let its wrapper record
            # the task before building the liveness wait set.
            await asyncio.sleep(0)
            self._ping_task = loop.create_task(client._ping_loop())
            stop_waiter = loop.create_task(self._stop.wait())
            try:
                waiters = {stop_waiter}
                if self._receive_task is not None:
                    waiters.add(self._receive_task)
                done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if self._receive_task in done and not self._stop.is_set():
                    assert self._receive_task is not None
                    self._receive_task.result()
            finally:
                stop_waiter.cancel()
                await asyncio.gather(stop_waiter, return_exceptions=True)
        finally:
            await self.close()

    def _accept_message(self, data: Any) -> None:
        if self._stop.is_set():
            raise _RouteCapacityExceeded("Feishu message routing is stopping")
        # This callback runs synchronously inside the SDK's data-frame handler.
        # Raising makes lark-oapi answer with a non-2xx code, allowing Feishu to
        # retry instead of ACKing work we cannot retain.
        self._route_tasks.submit(self._callback, data)

    async def close(self) -> None:
        self._stop.set()
        tasks = [task for task in (self._ping_task, self._receive_task) if task is not None]
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
        client = self._client
        if client is not None:
            closer = getattr(client, "_disconnect", None)
            if callable(closer):
                try:
                    result = closer()
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=_CLOSE_TIMEOUT_SECONDS)
                except Exception as exc:
                    logger.warning("Feishu WebSocket close failed: %s", type(exc).__name__)
        pending = [task for task in tasks if task is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


@dataclass
class _ManagedConnection:
    transport: tuple[str, str, FeishuBotConfig]
    connection: _Connection
    task: asyncio.Task[None]


class FeishuBotWebSocketSupervisor:
    """Reconcile the effective Bot pool into one long connection per Bot."""

    def __init__(
        self,
        pool: FeishuBotPoolStore,
        handler: EventHandler,
        *,
        connection_factory: ConnectionFactory | None = None,
        reconcile_seconds: float = _RECONCILE_SECONDS,
        route_tasks: _BoundedRouteTasks | None = None,
        shutdown_drain_seconds: float = _SHUTDOWN_DRAIN_SECONDS,
    ) -> None:
        self._pool = pool
        self._handler = handler
        self._route_tasks = route_tasks or _BoundedRouteTasks()
        if connection_factory is None:
            self._connection_factory: ConnectionFactory = lambda config, callback: _LarkConnection(
                config, callback, route_tasks=self._route_tasks
            )
        else:
            self._connection_factory = connection_factory
        self._reconcile_seconds = reconcile_seconds
        self._shutdown_drain_seconds = shutdown_drain_seconds
        self._connections: dict[str, _ManagedConnection] = {}
        self._failures: set[str] = set()
        self._lock = asyncio.Lock()
        self._monitor: asyncio.Task[None] | None = None
        self._started = False
        self._stopping = False

    async def start(self) -> None:
        self._route_tasks.start_accepting()
        self._stopping = False
        self._started = True
        await self.reconcile()
        if self._monitor is None or self._monitor.done():
            self._monitor = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        self._stopping = True
        self._started = False
        self._route_tasks.stop_accepting()
        if self._monitor is not None:
            self._monitor.cancel()
            await asyncio.gather(self._monitor, return_exceptions=True)
            self._monitor = None
        async with self._lock:
            for bot_id in list(self._connections):
                await self._stop_one(bot_id)
            self._failures.clear()
        await self._route_tasks.drain(self._shutdown_drain_seconds)

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reconcile_seconds)
            try:
                await self.reconcile()
            except Exception:
                logger.exception("Feishu WebSocket reconciliation failed")

    async def reconcile(self) -> None:
        if not self._started:
            return
        async with self._lock:
            desired: dict[str, EffectiveBot] = {}
            for entry in self._pool.snapshot().bots:
                if entry.enabled and entry.configured:
                    desired[entry.bot_id] = self._pool.effective(entry.bot_id)
            for bot_id, managed in list(self._connections.items()):
                effective = desired.get(bot_id)
                if (
                    effective is None
                    or self._transport(effective) != managed.transport
                    or managed.task.done()
                ):
                    await self._stop_one(bot_id)
            self._failures.intersection_update(desired)
            if self._stopping:
                return
            for bot_id, effective in desired.items():
                if bot_id not in self._connections:
                    self._start_one(bot_id, effective)

    def _start_one(self, bot_id: str, effective: EffectiveBot) -> None:
        transport = self._transport(effective)

        def callback(data: Any) -> Awaitable[None]:
            # A connection remains authoritative across pairing/name/revision
            # mutations. Only transport-affecting changes invalidate accepted
            # work; the handler re-reads current business state before routing.
            try:
                current = self._pool.effective(bot_id)
            except FeishuBotPoolError as exc:
                raise _RouteCapacityExceeded("Feishu Bot transport is no longer active") from exc
            if self._transport(current) != transport:
                raise _RouteCapacityExceeded("Feishu Bot transport has been replaced")
            return self._handler(bot_id, data)

        connection = self._connection_factory(effective.config, callback)
        task = asyncio.create_task(connection.run())
        self._connections[bot_id] = _ManagedConnection(transport, connection, task)
        self._failures.discard(bot_id)

        def finished(done: asyncio.Task[None]) -> None:
            if not done.cancelled() and done.exception() is not None and not self._stopping:
                self._failures.add(bot_id)
                logger.warning(
                    "Feishu WebSocket stopped for Bot %s: %s",
                    bot_id,
                    type(done.exception()).__name__,
                )

        task.add_done_callback(finished)

    @staticmethod
    def _transport(effective: EffectiveBot) -> tuple[str, str, FeishuBotConfig]:
        return (effective.bot_id, effective.app_id, effective.config)

    async def _stop_one(self, bot_id: str) -> None:
        managed = self._connections.pop(bot_id, None)
        if managed is None:
            return
        # Cancel first so a connection still blocked in its handshake releases
        # the SDK lock before ``close`` tries to acquire it. ``run`` performs its
        # own close in ``finally``; the explicit close keeps custom adapters
        # idempotently covered as well.
        managed.task.cancel()
        await asyncio.gather(managed.task, return_exceptions=True)
        await managed.connection.close()

    def status(self, bot_id: str) -> str:
        managed = self._connections.get(bot_id)
        if managed is None:
            return "failed" if bot_id in self._failures else "stopped"
        if managed.task.done():
            return "failed"
        return "connected" if managed.connection.connected else "connecting"


__all__ = [
    "FeishuBotWebSocketSupervisor",
    "message_event_from_sdk",
    "prewarm_lark_sdk",
]
