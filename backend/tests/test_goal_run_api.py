from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from claude_hub.api import goal_runs as goal_api
from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import GoalRunStatus, SessionKind, User
from claude_hub.services.goal_run import GoalRunController, GoalRunStore


@pytest.fixture()
def goal_client(monkeypatch, tmp_path):
    async def dispatch(goal, prompt):
        return "initial-turn"

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    monkeypatch.setattr(goal_api, "get_goal_manager", lambda: manager)
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


def test_goal_api_lifecycle_and_idempotency(goal_client: TestClient) -> None:
    create = goal_client.post(
        "/api/tabs/tab-1/goal",
        json={"objective": "Finish it", "client_request_id": "create-1"},
    )
    assert create.status_code == 201, create.text
    goal = create.json()
    assert goal["status"] == "active"
    assert goal["current_turn_id"] == "initial-turn"

    replay = goal_client.post(
        "/api/tabs/tab-1/goal",
        json={"objective": "Finish it", "client_request_id": "create-1"},
    )
    assert replay.json()["id"] == goal["id"]
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
