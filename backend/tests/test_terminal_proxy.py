import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request, WebSocket, WebSocketDisconnect

from claude_hub.api import terminal as terminal_api
from claude_hub.api.terminal import (
    is_generated_terminal_probe_response,
    proxy_terminal_request,
    proxy_websocket,
)
from claude_hub.models import AgentType, ExecutionTarget, User


class FakeClientWebSocket:
    def __init__(self, messages: list[dict[str, str | bytes]]) -> None:
        self.messages = list(messages)

    async def receive(self) -> dict[str, str | bytes]:
        if self.messages:
            await asyncio.sleep(0)
            return self.messages.pop(0)
        raise WebSocketDisconnect()


class FakeServerWebSocket:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    def __aiter__(self) -> "FakeServerWebSocket":
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(3600)
        raise StopAsyncIteration


class FakeWebSocketConnection:
    def __init__(self, server: FakeServerWebSocket) -> None:
        self.server = server

    async def __aenter__(self) -> FakeServerWebSocket:
        return self.server

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeTerminalRequest:
    method = "GET"
    query_params: dict[str, str] = {}
    headers: dict[str, str] = {}

    async def body(self) -> bytes:
        return b""


class FakeTerminalHTMLResponse:
    status_code = 200
    headers = {"content-type": "text/html"}

    async def aread(self) -> bytes:
        return b"<html><head></head><body><main>ttyd</main></body></html>"


async def read_streaming_body(response: object) -> bytes:
    body = bytearray()
    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
        body.extend(chunk.encode() if isinstance(chunk, str) else chunk)
    return bytes(body)


def test_generated_terminal_probe_responses_are_detected() -> None:
    assert is_generated_terminal_probe_response("0\x1b[>0;276;0c")
    assert is_generated_terminal_probe_response(b"0\x1b[>0;276;0c")
    assert is_generated_terminal_probe_response("0\x1b[?1;2c")
    assert is_generated_terminal_probe_response("0\x1b[24;80R")
    assert is_generated_terminal_probe_response("\x1b[>0;276;0c")


def test_regular_terminal_input_is_not_filtered() -> None:
    assert not is_generated_terminal_probe_response("0")
    assert not is_generated_terminal_probe_response("00")
    assert not is_generated_terminal_probe_response("0hello")
    assert not is_generated_terminal_probe_response("0\x1b[A")
    assert not is_generated_terminal_probe_response("0;276;0c")
    assert not is_generated_terminal_probe_response('1{"columns":120,"rows":40}')


@pytest.mark.asyncio
async def test_terminal_html_proxy_injects_history_and_resize_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the f-string construction path, not just injected JS fragments."""

    async def fake_ensure_tab_running(tab_id: str) -> object:
        assert tab_id == "tab-injection"
        return SimpleNamespace(
            port=12345,
            agent_type=AgentType.CODEX,
            target=ExecutionTarget.LOCAL,
        )

    async def fake_request(**kwargs: object) -> FakeTerminalHTMLResponse:
        assert kwargs["url"] == "http://127.0.0.1:12345/"
        return FakeTerminalHTMLResponse()

    monkeypatch.setattr(terminal_api.ttyd_manager, "ensure_tab_running", fake_ensure_tab_running)
    monkeypatch.setattr(terminal_api.client, "request", fake_request)

    response = await proxy_terminal_request(
        "tab-injection",
        "",
        cast(Request, FakeTerminalRequest()),
        cast(User, object()),
    )
    body = (await read_streaming_body(response)).decode("utf-8")

    assert "<main>ttyd</main>" in body
    assert "__claudeHubHistoryHooked" in body
    assert "__claudeHubRequestFit" in body
    assert "terminal-history-refresh" in body
    assert "ResizeObserver" in body
    assert response.headers["content-length"] == str(len(body.encode("utf-8")))


@pytest.mark.asyncio
async def test_proxy_drops_generated_probe_responses_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServerWebSocket()

    def fake_connect(*args: object, **kwargs: object) -> FakeWebSocketConnection:
        return FakeWebSocketConnection(server)

    monkeypatch.setattr(terminal_api.websockets, "connect", fake_connect)
    client = FakeClientWebSocket(
        [
            {"type": "websocket.receive", "text": "0hello"},
            {"type": "websocket.receive", "text": "0\x1b[>0;276;0c"},
            {"type": "websocket.receive", "bytes": b"0\x1b[?1;2c"},
            {"type": "websocket.receive", "text": "0\x1b[A"},
        ]
    )

    await proxy_websocket(
        cast(WebSocket, client),
        "ws://example.invalid/ws",
        filter_terminal_probe_responses=True,
    )

    assert server.sent == ["0hello", "0\x1b[A"]


@pytest.mark.asyncio
async def test_proxy_forwards_probe_responses_when_filter_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeServerWebSocket()

    def fake_connect(*args: object, **kwargs: object) -> FakeWebSocketConnection:
        return FakeWebSocketConnection(server)

    monkeypatch.setattr(terminal_api.websockets, "connect", fake_connect)
    client = FakeClientWebSocket([{"type": "websocket.receive", "text": "0\x1b[>0;276;0c"}])

    await proxy_websocket(
        cast(WebSocket, client),
        "ws://example.invalid/ws",
        filter_terminal_probe_responses=False,
    )

    assert server.sent == ["0\x1b[>0;276;0c"]
