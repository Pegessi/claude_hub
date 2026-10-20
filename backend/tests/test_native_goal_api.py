"""Authenticated native Goal routes and Chat Stop behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from claude_hub.api import agent_stream as stream_api
from claude_hub.api import goal_runs as goal_api
from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import AgentType, ChatMode, SessionKind, User
from claude_hub.services import goal_run
from claude_hub.services.goal_run import GoalRunController, GoalRunStore
from tests.test_native_goal_coordinator import FakeGoalProvider


@pytest.fixture
def native_api(monkeypatch, tmp_path):
    provider = FakeGoalProvider()
    dispatch = AsyncMock(side_effect=AssertionError("Hub must not schedule native Goals"))
    controller = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    controller.native.resolver = provider.connect
    session = SimpleNamespace(
        id="terminal-tab-tab",
        chat_mode=ChatMode.DEFAULT,
        session_kind=SessionKind.CHAT,
        agent_type=AgentType.CODEX,
    )

    async def in_flight(session):
        return provider.turn is not None

    async def active_turn(session):
        return provider.turn

    async def cancel_turn(session, expected_turn_id=None):
        if expected_turn_id is not None and provider.turn != expected_turn_id:
            return False
        provider.calls.append("manual-cancel")
        provider.turn = None
        return True

    stream_manager = SimpleNamespace(
        turn_in_flight=in_flight,
        active_turn_id=active_turn,
        cancel_turn=cancel_turn,
        answer_pending_question=AsyncMock(return_value=False),
        accepts_question_followup=AsyncMock(return_value=False),
        resolve_answered_questions=AsyncMock(),
    )
    monkeypatch.setattr(goal_api, "get_goal_manager", lambda: controller)
    monkeypatch.setattr(goal_run, "get_goal_manager", lambda: controller)
    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab: session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: stream_manager)
    monkeypatch.setattr(
        goal_api.ttyd_manager,
        "get_tab",
        lambda tab: SimpleNamespace(
            session_kind=SessionKind.CHAT,
            workspace_role=None,
        ),
    )
    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="local",
        name="Local",
        email="local@example.test",
    )
    try:
        with TestClient(app) as client:
            yield client, provider, controller, dispatch
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def create(client):
    response = client.post(
        "/api/tabs/tab/goal",
        json={
            "objective": "Do native work",
            "client_request_id": "create",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_native_route_lifecycle_reads_provider_state_and_clear(native_api):
    client, provider, _, dispatch = native_api
    assert client.get("/api/tabs/tab/goal/current").json() is None
    goal = create(client)
    assert goal["execution_owner"] == "provider_native"
    provider.goal["status"] = "usageLimited"
    assert client.get(f"/api/goals/{goal['id']}").json()["status"] == "usageLimited"
    changed = client.patch(
        f"/api/goals/{goal['id']}/objective",
        json={
            "objective": "Updated native work",
            "client_request_id": "edit",
        },
    )
    assert changed.status_code == 200, changed.text
    assert provider.goal["objective"] == "Updated native work"
    provider.goal["status"] = "active"
    provider.turn = "native-turn"
    paused = client.post(f"/api/goals/{goal['id']}/pause", json={"client_request_id": "pause"})
    assert paused.json()["status"] == "paused"
    assert provider.turn is None
    resumed = client.post(f"/api/goals/{goal['id']}/resume", json={"client_request_id": "resume"})
    assert resumed.json()["status"] == "active"
    done = client.post(f"/api/goals/{goal['id']}/complete", json={"client_request_id": "done"})
    assert done.json()["status"] == "complete"
    calls = len(provider.calls)
    cleared = client.post(f"/api/goals/{goal['id']}/clear", json={"client_request_id": "clear"})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["status"] == "cancelled"
    assert all(not isinstance(call, tuple) or call[0] != "set" for call in provider.calls[calls:])
    assert client.get("/api/tabs/tab/goal/current").json() is None
    dispatch.assert_not_called()


def test_native_stop_pauses_goal_before_interrupt_and_ignores_stale_stop(native_api):
    client, provider, _, _ = native_api
    create(client)
    provider.turn = "native-turn"
    before = len(provider.calls)
    response = client.post(
        "/api/workspaces/tabs/tab/stream/cancel", json={"expected_turn_id": "old-turn"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["cancelled"] is False
    assert len(provider.calls) == before
    response = client.post(
        "/api/workspaces/tabs/tab/stream/cancel", json={"expected_turn_id": "native-turn"}
    )
    assert response.status_code == 200, response.text
    assert provider.calls[-2:] == [("set", {"status": "paused"}), "stop"]
    assert provider.goal["status"] == "paused"
    assert provider.turn is None


def test_native_goal_read_failure_is_503_not_absent_or_hub_fallback(native_api):
    client, provider, controller, dispatch = native_api
    goal = create(client)
    provider.read_error = True
    response = client.get("/api/tabs/tab/goal/current")
    assert response.status_code == 503, response.text
    assert controller.get(goal["id"]).status.value == "active"
    dispatch.assert_not_called()


def test_native_goal_stop_failure_is_not_reported_as_success(native_api):
    client, provider, _, _ = native_api
    create(client)
    provider.turn = "native-turn"
    provider.stop_error = True
    response = client.post(
        "/api/workspaces/tabs/tab/stream/cancel", json={"expected_turn_id": "native-turn"}
    )
    assert response.status_code == 503, response.text
    current = client.get("/api/tabs/tab/goal/current").json()
    assert current["status"] == "paused"
    assert current["dispatch_state"] == "uncertain"


def test_native_steer_routes_plain_input_without_hub_continuation(native_api):
    client, provider, _, dispatch = native_api
    create(client)
    provider.turn = "native-turn"
    response = client.post(
        "/api/workspaces/tabs/tab/stream/send",
        json={
            "text": "Check the other case",
            "client_turn_id": "steer-turn",
            "delivery": "steer",
        },
    )
    assert response.status_code == 200, response.text
    assert ("input", "Check the other case", "steer-turn") in provider.calls
    assert provider.goal["status"] == "active"
    dispatch.assert_not_called()
