"""Native Goal route retries and provider binding boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from claude_hub.api import agent_stream as stream_api
from claude_hub.api.routes.goal_provider import resolve_goal_provider
from claude_hub.models import AgentType, SessionKind
from claude_hub.services.goal_run.native import NativeGoalError
from tests import test_native_goal_api as api_fixtures

native_api = api_fixtures.native_api
create = api_fixtures.create


def test_rejected_native_create_replay_never_returns_201(native_api):
    client, provider, _, dispatch = native_api
    body = {"objective": "Do native work", "client_request_id": "create"}
    provider.set_error = True
    assert client.post("/api/tabs/tab/goal", json=body).status_code == 503
    provider.set_error = False
    assert client.get("/api/tabs/tab/goal/current").json() is None
    replay = client.post("/api/tabs/tab/goal", json=body)
    assert replay.status_code == 503, replay.text
    assert provider.goal is None
    assert len(provider.calls) == 1
    dispatch.assert_not_called()


def test_native_create_lost_response_api_replay_confirms_without_second_start(native_api):
    client, provider, _, _ = native_api
    body = {"objective": "Do native work", "client_request_id": "create"}
    provider.accept_then_error = True
    assert client.post("/api/tabs/tab/goal", json=body).status_code == 503
    provider.accept_then_error = False
    replay = client.post("/api/tabs/tab/goal", json=body)
    assert replay.status_code == 201, replay.text
    assert replay.json()["status"] == "active"
    assert len(provider.calls) == 1


@pytest.mark.parametrize("state", ["paused", "usageLimited", "budgetLimited"])
def test_manual_stop_preserves_inactive_native_goal_status(native_api, state):
    client, provider, _, _ = native_api
    create(client)
    provider.goal["status"] = state
    client.get("/api/tabs/tab/goal/current")
    provider.turn = "manual-turn"
    calls = len(provider.calls)
    result = client.post(
        "/api/workspaces/tabs/tab/stream/cancel", json={"expected_turn_id": "manual-turn"}
    )
    assert result.status_code == 200, result.text
    assert result.json()["cancelled"] is True
    assert provider.calls[calls:] == ["manual-cancel"]
    assert provider.goal["status"] == state
    assert provider.turn is None


def test_repeat_stop_can_retry_failed_interrupt(native_api):
    client, provider, _, _ = native_api
    create(client)
    provider.turn = "native-turn"
    provider.stop_error = True
    endpoint = "/api/workspaces/tabs/tab/stream/cancel"
    body = {"expected_turn_id": "native-turn"}
    assert client.post(endpoint, json=body).status_code == 503
    provider.stop_error = False
    retried = client.post(endpoint, json=body)
    assert retried.status_code == 200, retried.text
    assert retried.json()["cancelled"] is True
    assert provider.goal["status"] == "paused"
    assert provider.turn is None


def test_complete_retry_stop_preserves_completed_goal(native_api):
    client, provider, _, _ = native_api
    goal = create(client)
    provider.turn = "native-turn"
    provider.stop_error = True
    failed = client.post(
        f"/api/goals/{goal['id']}/complete", json={"client_request_id": "complete"}
    )
    assert failed.status_code == 503, failed.text
    current = client.get("/api/tabs/tab/goal/current").json()
    assert current["status"] == "complete"
    assert current["provider_stop_pending"]
    provider.stop_error = False
    calls = len(provider.calls)
    retry = client.post(f"/api/goals/{goal['id']}/pause", json={"client_request_id": "retry-stop"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "complete"
    assert provider.goal["status"] == "complete"
    assert provider.calls[calls:] == ["stop"]


@pytest.mark.parametrize("expected_id", [None, "native-turn"])
@pytest.mark.parametrize("transition", ["complete", "ended", "new-turn", "cleared", "new-goal"])
def test_stop_reconciles_native_state_change_without_cancelling_new_work(
    native_api, monkeypatch, expected_id, transition
):
    client, provider, controller, _ = native_api
    original = create(client)
    provider.turn = "native-turn"
    read = provider.read
    reads = 0

    async def change_on_pause_read():
        nonlocal reads
        reads += 1
        if reads == 2:
            if transition == "cleared":
                provider.goal = None
            elif transition == "new-goal":
                provider.goal = {
                    **provider.goal,
                    "objective": "New provider Goal",
                    "createdAt": provider.goal["createdAt"] + 1,
                }
            else:
                provider.goal["status"] = "complete"
                if transition == "ended":
                    provider.turn = None
                elif transition == "new-turn":
                    provider.turn = "new-turn"
        return await read()

    monkeypatch.setattr(provider, "read", change_on_pause_read)
    client._transport.raise_server_exceptions = False
    calls = len(provider.calls)
    response = client.post(
        "/api/workspaces/tabs/tab/stream/cancel", json={"expected_turn_id": expected_id}
    )
    if transition == "new-goal":
        assert response.status_code == 409, response.text
        assert provider.turn == "native-turn"
        assert provider.goal["objective"] == "New provider Goal"
        assert provider.calls[calls:] == []
    else:
        assert response.status_code == 200, response.text
        cancelled = transition in {"complete", "cleared"}
        assert response.json()["cancelled"] is cancelled
        if cancelled:
            assert provider.turn is None
            assert provider.calls[calls:] == ["manual-cancel"]
        else:
            assert provider.calls[calls:] == []
            assert provider.turn == ("new-turn" if transition == "new-turn" else None)
        if transition != "cleared":
            assert provider.goal["status"] == "complete"
            assert controller.get(original["id"]).status.value == "complete"


@pytest.mark.parametrize("expected_id", [None, "old-hub-turn"])
@pytest.mark.parametrize("claimed", [False, True], ids=["before-start", "claimed-start"])
def test_stop_pauses_native_goal_before_hub_id_exists(
    native_api, monkeypatch, expected_id, claimed
):
    client, provider, _, _ = native_api
    create(client)
    initial_turn = "claimed-native-turn" if claimed else None
    provider.turn = initial_turn
    monkeypatch.setattr(provider, "active_turn", AsyncMock(return_value=None))
    manager = stream_api._get_tab_tailer_manager()
    monkeypatch.setattr(manager, "active_turn_id", AsyncMock(return_value=None))
    calls = len(provider.calls)
    response = client.post(
        "/api/workspaces/tabs/tab/stream/cancel", json={"expected_turn_id": expected_id}
    )
    assert response.status_code == 200, response.text
    assert response.json()["cancelled"] is (expected_id is None)
    if expected_id is None:
        assert provider.goal["status"] == "paused"
        assert provider.turn is None
        assert provider.calls[calls:] == [("set", {"status": "paused"}), "stop"]
    else:
        assert provider.goal["status"] == "active"
        assert provider.turn == initial_turn
        assert provider.calls[calls:] == []


def test_unconfirmed_claimed_native_stop_can_be_retried_without_hub_id(native_api, monkeypatch):
    client, provider, _, _ = native_api
    create(client)
    provider.turn = "claimed-native-turn"
    monkeypatch.setattr(provider, "active_turn", AsyncMock(return_value=None))
    manager = stream_api._get_tab_tailer_manager()
    monkeypatch.setattr(manager, "active_turn_id", AsyncMock(return_value=None))
    provider.stop_error = True
    endpoint = "/api/workspaces/tabs/tab/stream/cancel"
    assert client.post(endpoint).status_code == 503
    pending = client.get("/api/tabs/tab/goal/current").json()
    assert pending["provider_stop_pending"]
    assert pending["current_turn_id"] is None
    provider.stop_error = False
    retried = client.post(endpoint)
    assert retried.status_code == 200, retried.text
    assert retried.json()["cancelled"] is True
    assert provider.goal["status"] == "paused"
    assert provider.turn is None


@pytest.mark.parametrize("transition", ["complete", "cleared", "replaced"])
def test_changed_goal_with_unknown_hub_turn_requires_fresh_stop(
    native_api, monkeypatch, transition
):
    client, provider, _, _ = native_api
    create(client)
    provider.turn = "claimed-native-turn"
    monkeypatch.setattr(provider, "active_turn", AsyncMock(return_value=None))
    manager = stream_api._get_tab_tailer_manager()
    monkeypatch.setattr(manager, "active_turn_id", AsyncMock(return_value=None))
    read = provider.read
    reads = 0

    async def change_on_pause_read():
        nonlocal reads
        reads += 1
        if reads == 2:
            if transition == "cleared":
                provider.goal = None
            elif transition == "replaced":
                provider.goal = {**provider.goal, "createdAt": provider.goal["createdAt"] + 1}
            else:
                provider.goal["status"] = "complete"
        return await read()

    monkeypatch.setattr(provider, "read", change_on_pause_read)
    calls = len(provider.calls)
    response = client.post("/api/workspaces/tabs/tab/stream/cancel")
    assert response.status_code == 409, response.text
    assert provider.calls[calls:] == []
    assert provider.turn == "claimed-native-turn"


@pytest.mark.asyncio
async def test_provider_binding_waits_for_handshake_and_stops_cold_queued_turn(monkeypatch):
    session = SimpleNamespace(
        id="session", session_kind=SessionKind.CHAT, agent_type=AgentType.CODEX
    )
    transport = SimpleNamespace(
        active_thread_id=None,
        turn_in_flight=False,
        cancel_active_turn=AsyncMock(),
        goal_set=AsyncMock(return_value={"objective": "Work"}),
        goal_clear=AsyncMock(return_value=True),
    )

    async def start():
        transport.active_thread_id = "thread"

    async def snapshot():
        assert transport.active_thread_id == "thread"
        return {"thread_id": "thread", "goal": None}

    transport.start = AsyncMock(side_effect=start)
    transport.goal_snapshot = AsyncMock(side_effect=snapshot)
    tailer = SimpleNamespace(
        native_transport=transport, native_error=None, matches_session=lambda value: True
    )
    manager = SimpleNamespace(
        ensure_started=AsyncMock(return_value=tailer),
        session_is_current=lambda value: True,
        active_turn_id=AsyncMock(return_value=None),
        cancel_turn=AsyncMock(),
    )
    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab: session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: manager)
    connection = await resolve_goal_provider("tab")
    assert connection is not None
    transport.start.assert_awaited_once()
    await connection.set_goal({"objective": "Work"})
    transport.goal_set.assert_awaited_once_with({"objective": "Work"}, origin="user")
    await connection.clear_goal()
    transport.goal_clear.assert_awaited_once_with(origin="user")
    await connection.stop_turn()
    manager.cancel_turn.assert_not_awaited()
    transport.cancel_active_turn.assert_awaited_once()
    transport.active_thread_id = "replacement"
    with pytest.raises(NativeGoalError, match="thread changed"):
        await connection.set_goal({"status": "paused"})
    assert transport.goal_set.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "agent"),
    [(SessionKind.TERMINAL, AgentType.CODEX), (SessionKind.CHAT, AgentType.CURSOR)],
)
async def test_non_native_modes_do_not_resolve_a_goal_transport(monkeypatch, kind, agent):
    session = SimpleNamespace(
        id="session", session_kind=kind, agent_type=agent, cursor_transport="terminal_transcript"
    )
    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab: session)

    def unexpected_manager():
        raise AssertionError("terminal modes must not create a native Goal transport")

    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", unexpected_manager)
    assert await resolve_goal_provider("tab") is None
