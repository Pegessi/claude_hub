"""Tests for the WebSocket ConnectionManager.

The ConnectionManager tracks live terminal WebSocket fan-out per tab and is the
only place broadcast failures are reaped, so its connect/disconnect/broadcast
bookkeeping is exercised directly here with fake sockets.
"""

import pytest

from claude_hub.services.session_manager import ConnectionManager


class FakeWebSocket:
    """Minimal WebSocket stand-in recording sends and accept calls."""

    def __init__(self, *, fail_on_send: bool = False) -> None:
        self.accepted = False
        self.sent: list[str] = []
        self.fail_on_send = fail_on_send

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, message: str) -> None:
        if self.fail_on_send:
            raise RuntimeError("connection broken")
        self.sent.append(message)


@pytest.mark.asyncio
async def test_connect_accepts_and_registers_connection() -> None:
    manager = ConnectionManager()
    ws = FakeWebSocket()

    await manager.connect(ws, "tab-1")  # type: ignore[arg-type]

    assert ws.accepted is True
    assert ws in manager.active_connections["tab-1"]


@pytest.mark.asyncio
async def test_disconnect_removes_connection_and_prunes_empty_tab() -> None:
    manager = ConnectionManager()
    ws = FakeWebSocket()
    await manager.connect(ws, "tab-1")  # type: ignore[arg-type]

    manager.disconnect(ws, "tab-1")  # type: ignore[arg-type]

    # Tab entry is pruned entirely once the last socket leaves.
    assert "tab-1" not in manager.active_connections


@pytest.mark.asyncio
async def test_disconnect_keeps_tab_with_remaining_connections() -> None:
    manager = ConnectionManager()
    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()
    await manager.connect(ws1, "tab-1")  # type: ignore[arg-type]
    await manager.connect(ws2, "tab-1")  # type: ignore[arg-type]

    manager.disconnect(ws1, "tab-1")  # type: ignore[arg-type]

    assert manager.active_connections["tab-1"] == {ws2}


def test_disconnect_unknown_tab_is_noop() -> None:
    manager = ConnectionManager()
    # Should not raise even though the tab was never registered.
    manager.disconnect(FakeWebSocket(), "missing")  # type: ignore[arg-type]
    assert manager.active_connections == {}


@pytest.mark.asyncio
async def test_broadcast_sends_to_all_connections_in_tab() -> None:
    manager = ConnectionManager()
    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()
    await manager.connect(ws1, "tab-1")  # type: ignore[arg-type]
    await manager.connect(ws2, "tab-1")  # type: ignore[arg-type]

    await manager.broadcast_to_tab("tab-1", "hello")

    assert ws1.sent == ["hello"]
    assert ws2.sent == ["hello"]


@pytest.mark.asyncio
async def test_broadcast_to_unknown_tab_is_noop() -> None:
    manager = ConnectionManager()
    # No registered tab -> returns silently without raising.
    await manager.broadcast_to_tab("missing", "hello")


@pytest.mark.asyncio
async def test_broadcast_prunes_failed_connections() -> None:
    manager = ConnectionManager()
    healthy = FakeWebSocket()
    broken = FakeWebSocket(fail_on_send=True)
    await manager.connect(healthy, "tab-1")  # type: ignore[arg-type]
    await manager.connect(broken, "tab-1")  # type: ignore[arg-type]

    await manager.broadcast_to_tab("tab-1", "ping")

    # Healthy socket received the message; the broken one was disconnected.
    assert healthy.sent == ["ping"]
    assert broken not in manager.active_connections["tab-1"]
    assert healthy in manager.active_connections["tab-1"]


@pytest.mark.asyncio
async def test_broadcast_prunes_tab_when_all_connections_fail() -> None:
    manager = ConnectionManager()
    broken = FakeWebSocket(fail_on_send=True)
    await manager.connect(broken, "tab-1")  # type: ignore[arg-type]

    await manager.broadcast_to_tab("tab-1", "ping")

    assert "tab-1" not in manager.active_connections


@pytest.mark.asyncio
async def test_send_personal_message_targets_single_socket() -> None:
    manager = ConnectionManager()
    ws = FakeWebSocket()

    await manager.send_personal_message("just you", ws)  # type: ignore[arg-type]

    assert ws.sent == ["just you"]
