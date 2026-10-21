import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from claude_hub.api import agent_stream as stream_api
from claude_hub.api import goal_runs as goal_api
from claude_hub.auth.dependencies import get_current_user
from claude_hub.main import app
from claude_hub.models import GoalDispatchState, GoalRunCreate, GoalRunStatus, SessionKind, User
from claude_hub.services.goal_run import GoalRunController, GoalRunStore


@pytest.fixture()
def goal_client(monkeypatch, tmp_path):
    async def dispatch(goal, prompt):
        return "initial-turn"

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    stream_session = SimpleNamespace(id="terminal-tab-tab-1", chat_mode="default")
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
    assert "token_budget" not in goal
    assert "max_turns" not in goal
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
    assert changed.status_code == 410
    assert "removed" in changed.json()["detail"]
    assert goal_client.get(f"/api/goals/{goal['id']}").json()["status"] == "paused"


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
    sent: list[tuple[object, stream_api.AgentStreamSendRequest, object, dict[str, object]]] = []
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
    goal.current_turn_id = "step-1"

    turn_id = await goal_api._dispatch_goal_turn(goal, "continue safely")
    assert turn_id == "step-1"
    assert sent[0][0] is session
    assert sent[0][1].client_turn_id == "step-1"
    assert sent[0][1].text == "continue safely"
    assert sent[0][2] is manager
    assert sent[0][1].delivery == "normal"
    assert sent[0][3] == {
        "visible_text": "Continue active Goal",
        "turn_metadata": {
            "origin": "goal",
            "protocol": "goal-continuation-v1",
        },
    }
    await goal_api._cancel_goal_turn(goal)
    assert cancelled == [(session, "step-1")]


@pytest.mark.asyncio
async def test_unknown_goal_turn_identity_never_cancels_an_unrelated_turn(monkeypatch, tmp_path):
    calls = []
    session = SimpleNamespace(id="terminal-tab-tab-1")
    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab_id: session)
    monkeypatch.setattr(
        stream_api,
        "_get_tab_tailer_manager",
        lambda: SimpleNamespace(
            turn_in_flight=lambda session: _return_true(),
            cancel_turn=lambda *args, **kwargs: _record_cancel(calls, args),
        ),
    )
    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"))
    goal = manager.create("tab-1", GoalRunCreate(objective="Finish it", client_request_id="create"))
    with pytest.raises(RuntimeError, match="identity"):
        await goal_api._cancel_goal_turn(goal)
    assert calls == []


async def _record_cancel(calls: list[object], session: object) -> bool:
    calls.append(session)
    return True


async def _return_false() -> bool:
    return False


async def _return_true() -> bool:
    return True


def test_active_goal_accepts_only_pending_question_answer(goal_client, monkeypatch):
    monkeypatch.setattr("claude_hub.services.goal_run.get_goal_manager", goal_api.get_goal_manager)
    goal_client.post(
        "/api/tabs/tab-1/goal",
        json={"objective": "Finish it", "client_request_id": "create"},
    )
    answered = []

    async def answer(session, text, expected_turn_id):
        answered.append(expected_turn_id)
        return len(answered) == 1

    monkeypatch.setattr(
        stream_api,
        "_get_tab_tailer_manager",
        lambda: SimpleNamespace(
            answer_pending_question=answer,
            accepts_question_followup=lambda *args: _return_false(),
        ),
    )
    payload = {
        "text": '{"type":"ask_question_response","answers":[{"questionId":"q1","selected":["A"]}]}',
        "client_turn_id": "answer",
    }
    url = "/api/workspaces/tabs/tab-1/stream/send"
    assert goal_client.post(url, json=payload).status_code == 200
    assert goal_client.post(url, json=payload).status_code == 409
    assert answered == ["initial-turn", "initial-turn"]


def test_goal_resume_rejects_plan_and_defers_while_chat_turn_runs(goal_client, monkeypatch):
    session = SimpleNamespace(id="terminal-tab-tab-1", chat_mode="plan")
    monkeypatch.setattr(stream_api, "_terminal_tab_session_or_404", lambda tab_id: session)
    body = {"objective": "Finish it", "client_request_id": "create"}
    assert goal_client.post("/api/tabs/tab-1/goal", json=body).status_code == 409
    session.chat_mode = "default"
    goal = goal_client.post("/api/tabs/tab-1/goal", json=body).json()
    goal_client.post(f"/api/goals/{goal['id']}/pause", json={"client_request_id": "pause"})
    session.chat_mode = "plan"
    url = f"/api/goals/{goal['id']}/resume"
    assert goal_client.post(url, json={"client_request_id": "resume"}).status_code == 409
    assert goal_client.get(f"/api/goals/{goal['id']}").json()["status"] == "paused"
    session.chat_mode = "default"
    monkeypatch.setattr(
        stream_api,
        "_get_tab_tailer_manager",
        lambda: SimpleNamespace(turn_in_flight=lambda session: _return_true()),
    )
    deferred = goal_client.post(url, json={"client_request_id": "resume"})
    assert deferred.status_code == 200, deferred.text
    assert deferred.json()["status"] == "active"
    assert deferred.json()["dispatch_state"] == "idle"
    assert deferred.json()["resume_after_turn"] is True


def test_goal_resume_releases_when_chat_turn_ends_during_deferral(goal_client, monkeypatch):
    goal = goal_client.post(
        "/api/tabs/tab-1/goal", json={"objective": "Finish it", "client_request_id": "create"}
    ).json()
    goal_client.post(f"/api/goals/{goal['id']}/pause", json={"client_request_id": "pause"})
    in_flight = iter([True, False])

    async def turn_in_flight(session):
        return next(in_flight)

    monkeypatch.setattr(
        stream_api,
        "_get_tab_tailer_manager",
        lambda: SimpleNamespace(turn_in_flight=turn_in_flight),
    )
    resumed = goal_client.post(
        f"/api/goals/{goal['id']}/resume", json={"client_request_id": "resume"}
    ).json()
    assert resumed["resume_after_turn"] is False
    assert resumed["dispatch_state"] == "dispatched"


def test_second_resume_during_deferral_waits_for_the_turn_or_releases_it(goal_client, monkeypatch):
    goal = goal_client.post(
        "/api/tabs/tab-1/goal", json={"objective": "Finish it", "client_request_id": "create"}
    ).json()
    goal_client.post(f"/api/goals/{goal['id']}/pause", json={"client_request_id": "pause"})
    in_flight = {"value": True}

    async def turn_in_flight(session):
        return in_flight["value"]

    monkeypatch.setattr(
        stream_api,
        "_get_tab_tailer_manager",
        lambda: SimpleNamespace(turn_in_flight=turn_in_flight),
    )
    url = f"/api/goals/{goal['id']}/resume"
    assert goal_client.post(url, json={"client_request_id": "resume-1"}).json()["resume_after_turn"]

    stale = goal_client.post(url, json={"client_request_id": "resume-2"})
    assert stale.status_code == 200, stale.text
    assert stale.json()["status"] == "active"
    assert stale.json()["resume_after_turn"] is True
    assert stale.json()["dispatch_state"] == "idle"

    in_flight["value"] = False
    released = goal_client.post(url, json={"client_request_id": "resume-3"}).json()
    assert released["resume_after_turn"] is False
    assert released["dispatch_state"] == "dispatched"


def _steerable_goal(monkeypatch, tmp_path, **stream_methods):
    """Use the real Goal transport callback with a recorded native send."""
    manager = GoalRunController(
        GoalRunStore(tmp_path / "steer-goals.json"), goal_api._dispatch_goal_turn
    )
    monkeypatch.setattr(goal_api, "get_goal_manager", lambda: manager)
    monkeypatch.setattr("claude_hub.services.goal_run.get_goal_manager", lambda: manager)
    resolved: list[str] = []

    async def resolve(session, text):
        resolved.append(text)

    stream_manager = SimpleNamespace(
        turn_in_flight=lambda session: _return_false(),
        answer_pending_question=lambda *args: _return_false(),
        accepts_question_followup=lambda *args: _return_false(),
        resolve_answered_questions=resolve,
    )
    for name, value in stream_methods.items():
        setattr(stream_manager, name, value)
    monkeypatch.setattr(stream_api, "_get_tab_tailer_manager", lambda: stream_manager)
    sent: list[tuple[stream_api.AgentStreamSendRequest, dict[str, object]]] = []

    async def send(session, payload, manager_arg, **kwargs):
        sent.append((payload, kwargs))

    monkeypatch.setattr(stream_api, "_send_to_native", send)
    return manager, sent, resolved


IMAGE = {
    "filename": "reference.png",
    "mime_type": "image/png",
    "data_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=",
    "preview_is_original": True,
}


def test_goal_create_and_steer_send_original_images_without_snapshot_blobs(
    goal_client, monkeypatch, tmp_path
):
    manager, sent, resolved = _steerable_goal(monkeypatch, tmp_path)

    async def capabilities(*args):
        return SimpleNamespace(supports_images=True)

    monkeypatch.setattr(stream_api, "_capabilities_for", capabilities)
    body = {"objective": "Use the reference", "client_request_id": "images", "attachments": [IMAGE]}
    created = goal_client.post("/api/tabs/tab-1/goal", json=body)
    assert created.status_code == 201, created.text
    goal = created.json()
    assert sent[-1][0].attachments[0].data_url == IMAGE["data_url"]
    assert goal["pending_image_count"] == 0
    assert goal_client.post("/api/tabs/tab-1/goal", json=body).json()["id"] == goal["id"]
    assert len(sent) == 1

    result = goal_client.post(
        "/api/workspaces/tabs/tab-1/stream/send",
        json={
            "text": "",
            "client_turn_id": "image-steer",
            "delivery": "steer",
            "attachments": [IMAGE],
        },
    )
    assert result.status_code == 200, result.text
    assert sent[-1][0].attachments[0].data_url == IMAGE["data_url"]
    assert sent[-1][0].delivery == "steer"
    assert resolved == []  # Images never accidentally answer a pending provider question.
    current = manager.get(goal["id"])
    assert current.status == GoalRunStatus.ACTIVE
    assert current.current_turn_id == "image-steer"
    assert "data:image" not in (tmp_path / "steer-goals.json").read_text()

    edit = goal_client.patch(
        f'/api/goals/{goal["id"]}/objective',
        json={
            "objective": "Updated outcome",
            "client_request_id": "edit-objective",
        },
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["objective"] == "Updated outcome"
    assert len(sent) == 2


def test_interrupted_goal_images_require_reattachment_after_restart(
    goal_client, monkeypatch, tmp_path
):
    manager, sent, _ = _steerable_goal(monkeypatch, tmp_path)

    async def capabilities(*args):
        return SimpleNamespace(supports_images=True)

    async def fail(*args, **kwargs):
        raise RuntimeError("acceptance unknown")

    monkeypatch.setattr(stream_api, "_capabilities_for", capabilities)
    monkeypatch.setattr(stream_api, "_send_to_native", fail)
    result = goal_client.post(
        "/api/tabs/tab-1/goal",
        json={
            "objective": "Use image",
            "client_request_id": "interrupted",
            "attachments": [IMAGE],
        },
    )
    assert result.status_code == 503
    current = manager.current("tab-1")
    assert current.pending_image_count == 1
    assert "data:image" not in (tmp_path / "steer-goals.json").read_text()
    cold = GoalRunController(GoalRunStore(tmp_path / "steer-goals.json"))
    from claude_hub.services.goal_run.images import pending_goal_images

    with pytest.raises(ValueError, match="Reattach"):
        pending_goal_images(cold.get(current.id))
    resume = goal_client.post(
        f"/api/goals/{current.id}/resume", json={"client_request_id": "resume"}
    )
    assert resume.status_code == 409
    assert "Reattach" in resume.text
    assert not sent

    async def recovered_send(session, payload, manager_arg, **kwargs):
        sent.append((payload, kwargs))

    monkeypatch.setattr(stream_api, "_send_to_native", recovered_send)
    recovered = goal_client.post(
        "/api/workspaces/tabs/tab-1/stream/send",
        json={
            "text": "Continue using this image",
            "client_turn_id": "explicit-recovery",
            "delivery": "steer",
            "attachments": [IMAGE],
        },
    )
    assert recovered.status_code == 200, recovered.text
    assert manager.current("tab-1").id == current.id
    assert manager.current("tab-1").status == GoalRunStatus.ACTIVE
    assert manager.current("tab-1").pending_image_count == 0
    assert len(sent) == 1
    assert sent[0][0].attachments[0].data_url == IMAGE["data_url"]


def test_goal_question_followup_becomes_next_goal_turn(goal_client, monkeypatch, tmp_path):
    manager, sent, resolved = _steerable_goal(
        monkeypatch, tmp_path, accepts_question_followup=lambda *args: _return_true()
    )
    goal = goal_client.post(
        "/api/tabs/tab-1/goal",
        json={"objective": "Finish it", "client_request_id": "create"},
    ).json()
    answer = '{"type":"ask_question_response","answers":[{"questionId":"q1","selected":["A"]}]}'
    url = "/api/workspaces/tabs/tab-1/stream/send"
    body = {"text": answer, "client_turn_id": "answer"}

    result = goal_client.post(url, json=body)

    assert result.status_code == 200, result.text
    payload, kwargs = sent[-1]
    assert payload.client_turn_id == "answer"
    assert payload.delivery == "steer"
    assert answer in payload.text
    assert kwargs["visible_text"] == answer
    assert kwargs["turn_metadata"] == {"origin": "goal", "protocol": "goal-continuation-v1"}
    assert resolved == [answer]
    current = manager.get(goal["id"])
    assert current.status == GoalRunStatus.ACTIVE
    assert current.current_turn_id == "answer"
    assert current.pending_input is None
    # A retried POST replays the steer without delivering it twice.
    assert goal_client.post(url, json=body).status_code == 200
    assert len(sent) == 2


def test_active_goal_steer_and_blocked_reply_route_into_goal(goal_client, monkeypatch, tmp_path):
    manager, sent, resolved = _steerable_goal(monkeypatch, tmp_path)
    goal = goal_client.post(
        "/api/tabs/tab-1/goal",
        json={"objective": "Finish it", "client_request_id": "create"},
    ).json()
    url = "/api/workspaces/tabs/tab-1/stream/send"
    # Plain Enter while a Goal is active is still refused by the backend.
    plain = goal_client.post(url, json={"text": "later", "client_turn_id": "plain"})
    assert plain.status_code == 409
    image = goal_client.post(
        url,
        json={
            "text": "look",
            "client_turn_id": "image",
            "delivery": "steer",
            "attachments": [
                {
                    "filename": "a.png",
                    "mime_type": "image/png",
                    "data_url": "data:image/png;base64,AA==",
                }
            ],
        },
    )
    assert image.status_code == 400
    assert manager.get(goal["id"]).current_turn_id == goal["current_turn_id"]
    oversized = goal_client.post(
        url, json={"text": "x" * 32_001, "client_turn_id": "huge", "delivery": "steer"}
    )
    assert oversized.status_code == 422
    assert resolved == []
    assert manager.get(goal["id"]).pending_input is None

    steer = goal_client.post(
        url, json={"text": "Prefer option B", "client_turn_id": "steer-1", "delivery": "steer"}
    )
    assert steer.status_code == 200, steer.text
    assert sent[-1][0].delivery == "steer"
    assert "Prefer option B" in sent[-1][0].text
    assert manager.get(goal["id"]).current_turn_id == "steer-1"

    blocked = manager.get(goal["id"])
    blocked.status = GoalRunStatus.BLOCKED
    blocked.current_turn_id = None
    blocked.dispatch_state = GoalDispatchState.IDLE
    manager.store.put(blocked)
    reply = goal_client.post(url, json={"text": "Use staging", "client_turn_id": "reply"})
    assert reply.status_code == 200, reply.text
    assert sent[-1][1]["visible_text"] == "Use staging"
    current = manager.get(goal["id"])
    assert current.status == GoalRunStatus.ACTIVE
    assert current.current_turn_id == "reply"


def test_blocked_goal_reply_waits_for_running_chat_turn(goal_client, monkeypatch, tmp_path):
    manager, sent, _ = _steerable_goal(
        monkeypatch, tmp_path, turn_in_flight=lambda session: _return_true()
    )
    goal = manager.create("tab-1", GoalRunCreate(objective="Finish it", client_request_id="c"))
    goal.status = GoalRunStatus.BLOCKED
    manager.store.put(goal)
    url = "/api/workspaces/tabs/tab-1/stream/send"
    busy = goal_client.post(url, json={"text": "Use staging", "client_turn_id": "reply"})
    assert busy.status_code == 409
    assert busy.headers["X-Claude-Hub-Error-Reason"] == "chat_busy"
    assert sent == []


def test_failed_goal_steer_reports_error_and_keeps_input(goal_client, monkeypatch, tmp_path):
    manager, sent, _ = _steerable_goal(monkeypatch, tmp_path)
    goal = goal_client.post(
        "/api/tabs/tab-1/goal",
        json={"objective": "Finish it", "client_request_id": "create"},
    ).json()

    async def broken_send(session, payload, manager_arg, **kwargs):
        raise RuntimeError("transport offline")

    monkeypatch.setattr(stream_api, "_send_to_native", broken_send)
    result = goal_client.post(
        "/api/workspaces/tabs/tab-1/stream/send",
        json={"text": "Keep me", "client_turn_id": "steer-1", "delivery": "steer"},
    )
    assert result.status_code == 503
    assert "transport offline" in result.json()["detail"]
    current = manager.get(goal["id"])
    assert current.status == GoalRunStatus.PAUSED
    assert current.pending_input == "Keep me"


def test_only_hub_goal_turns_enable_goal_protocol_handling(goal_client, monkeypatch, tmp_path):
    manager, sent, _ = _steerable_goal(monkeypatch, tmp_path)
    url = "/api/workspaces/tabs/tab-1/stream/send"
    assert goal_client.post(url, json={"text": "hi", "client_turn_id": "before"}).status_code == 200
    assert sent[-1][1]["turn_metadata"] == {"origin": "web"}
    goal = goal_client.post(
        "/api/tabs/tab-1/goal",
        json={"objective": "Finish it", "client_request_id": "create"},
    ).json()
    assert sent[-1][1]["turn_metadata"] == {"origin": "goal", "protocol": "goal-continuation-v1"}
    goal_client.post(f"/api/goals/{goal['id']}/pause", json={"client_request_id": "pause"})
    assert (
        goal_client.post(url, json={"text": "while paused", "client_turn_id": "paused"}).status_code
        == 200
    )
    assert sent[-1][1]["turn_metadata"] == {"origin": "web"}
    goal_client.post(f"/api/goals/{goal['id']}/clear", json={"client_request_id": "clear"})
    assert (
        goal_client.post(url, json={"text": "next", "client_turn_id": "after"}).status_code == 200
    )
    assert sent[-1][0].delivery == "normal"
    assert sent[-1][1]["turn_metadata"] == {"origin": "web"}


@pytest.mark.asyncio
async def test_goal_read_waits_for_in_progress_admission(goal_client, monkeypatch, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(goal, prompt):
        entered.set()
        await release.wait()
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "consistent-read.json"), dispatch)
    monkeypatch.setattr(goal_api, "get_goal_manager", lambda: manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        create = asyncio.create_task(
            client.post(
                "/api/tabs/read-serialization/goal",
                json={
                    "objective": "Consistent state",
                    "client_request_id": "create-read",
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        read = asyncio.create_task(client.get("/api/tabs/read-serialization/goal/current"))
        await asyncio.sleep(0)
        assert not read.done(), "GET must not expose a transient pending dispatch"
        release.set()
        created, observed = await asyncio.gather(create, read)
        assert created.status_code == 201
        assert observed.status_code == 200
        assert observed.json()["dispatch_state"] == "dispatched"
        assert observed.json()["current_turn_id"] == created.json()["current_turn_id"]
