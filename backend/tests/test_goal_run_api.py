from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from claude_hub.api import agent_stream as stream_api
from claude_hub.api import goal_runs as goal_api
from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import GoalRunCreate, GoalRunStatus, SessionKind, User
from claude_hub.services.goal_run import GoalRunController, GoalRunStore


@pytest.fixture()
def goal_client(monkeypatch, tmp_path):
    async def dispatch(goal, prompt):
        return "initial-turn"

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    stream_session = SimpleNamespace(id="terminal-tab-tab-1")
    stream_manager = SimpleNamespace(turn_in_flight=lambda session: _return_false())
    monkeypatch.setattr(goal_api, "get_goal_manager", lambda: manager)
    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab_id: stream_session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: stream_manager)
    monkeypatch.setattr(
        goal_api.ttyd_manager,
        "get_tab",
        lambda tab_id: SimpleNamespace(session_kind=SessionKind.CHAT, workspace_role=None),
    )
    app.dependency_overrides[get_current_user] = lambda: User(
        open_id="local", name="Local", email="local@example.test"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_goal_api_lifecycle_and_idempotency(goal_client: TestClient, monkeypatch) -> None:
    create = goal_client.post(
        "/api/tabs/tab-1/goal",
        json={"objective": "Finish it", "client_request_id": "create-1"},
    )
    assert create.status_code == 201, create.text
    goal = create.json()
    assert goal["status"] == "active"
    assert goal["current_turn_id"] == "initial-turn"

    # A network retry must replay the successful create even though that
    # Goal's own first turn is now in flight.
    stream_manager = SimpleNamespace(turn_in_flight=lambda session: _return_true())
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: stream_manager)
    replay = goal_client.post(
        "/api/tabs/tab-1/goal",
        json={"objective": "Finish it", "client_request_id": "create-1"},
    )
    assert replay.json()["id"] == goal["id"]
    conflicting_replay = goal_client.post(
        "/api/tabs/tab-1/goal",
        json={"objective": "Different", "client_request_id": "create-1"},
    )
    assert conflicting_replay.status_code == 409
    assert goal_client.get("/api/tabs/tab-1/goal/current").json()["id"] == goal["id"]
    assert goal_client.get(f"/api/goals/{goal['id']}").status_code == 200

    paused = goal_client.post(
        f"/api/goals/{goal['id']}/pause", json={"client_request_id": "pause-1"}
    )
    assert paused.json()["status"] == GoalRunStatus.PAUSED.value
    changed = goal_client.patch(
        f"/api/goals/{goal['id']}/budget",
        json={"client_request_id": "budget-1", "token_budget": 500},
    )
    assert changed.json()["token_budget"] == 500


def test_goal_api_rejects_non_direct_chat(goal_client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        goal_api.ttyd_manager,
        "get_tab",
        lambda tab_id: SimpleNamespace(session_kind=SessionKind.TERMINAL, workspace_role=None),
    )
    response = goal_client.post(
        "/api/tabs/tab-1/goal",
        json={"objective": "Nope", "client_request_id": "create-1"},
    )
    assert response.status_code == 400


def test_goal_api_rejects_create_while_chat_turn_is_running(
    goal_client: TestClient, monkeypatch
) -> None:
    stream_manager = SimpleNamespace(turn_in_flight=lambda session: _return_true())
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: stream_manager)

    response = goal_client.post(
        "/api/tabs/tab-1/goal",
        json={"objective": "Wait first", "client_request_id": "create-busy"},
    )

    assert response.status_code == 409
    assert "current Chat turn" in response.json()["detail"]


@pytest.mark.asyncio
async def test_goal_runtime_callbacks_use_direct_chat_transport(monkeypatch, tmp_path) -> None:
    sent: list[tuple[object, object, object]] = []
    cancelled: list[object] = []
    session = SimpleNamespace(id="terminal-tab-tab-1")
    manager = SimpleNamespace(
        cancel_turn=lambda value, expected_turn_id=None: _record_cancel(
            cancelled, (value, expected_turn_id)
        )
    )

    async def send(session_arg, payload, manager_arg, **kwargs):
        sent.append((session_arg, payload, manager_arg, kwargs))

    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab_id: session)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: manager)
    monkeypatch.setattr(stream_api, "_send_to_native", send)
    controller = GoalRunController(GoalRunStore(tmp_path / "goals.json"))
    goal = controller.create(
        "tab-1", GoalRunCreate(objective="Finish it", client_request_id="create-1")
    )
    goal.pending_step_id = "step-1"

    turn_id = await goal_api._dispatch_goal_turn(goal, "continue safely")
    assert turn_id == "step-1"
    assert sent[0][0] is session
    assert sent[0][1].client_turn_id == "step-1"
    assert sent[0][1].text == "continue safely"
    assert sent[0][2] is manager
    assert sent[0][3] == {
        "visible_text": "Continue active Goal",
        "turn_metadata": {
            "origin": "goal",
            "protocol": "goal-continuation-v1",
        },
    }
    await goal_api._cancel_goal_turn(goal)
    assert cancelled == [(session, None)]


async def _record_cancel(calls: list[object], session: object) -> bool:
    calls.append(session)
    return True


async def _return_false() -> bool:
    return False


async def _return_true() -> bool:
    return True
