import asyncio

import pytest
from fastapi import WebSocketDisconnect

from claude_hub.api import terminal as terminal_api
from claude_hub.api.terminal import is_generated_terminal_probe_response, proxy_websocket


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
        client,
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
        client,
        "ws://example.invalid/ws",
        filter_terminal_probe_responses=False,
    )

    assert server.sent == ["0\x1b[>0;276;0c"]
