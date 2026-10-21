"""Native Goal ownership at the merged inbox and completion-observer boundaries."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from claude_hub.api import agent_stream as stream
from claude_hub.api.routes.agent_stream import messages
from claude_hub.models import AgentStreamEvent, AgentStreamEventType, AgentType, GoalRunCreate
from claude_hub.models.chat_message import ChatMessageCreate
from claude_hub.services import goal_run, workspace_manager
from claude_hub.services.chat_message_inbox import InboxError, InboxTarget
from tests import test_chat_message_routes as inbox_fixtures
from tests.test_native_goal_coordinator import FakeGoalProvider

setup = inbox_fixtures.setup


@pytest.fixture
async def native_inbox(setup, monkeypatch, tmp_path):
    _, inbox = setup
    provider = FakeGoalProvider()
    controller = goal_run.GoalRunController(
        goal_run.GoalRunStore(tmp_path / "goals.json"),
        AsyncMock(side_effect=AssertionError("Hub must not schedule native Goals")),
    )
    controller.native.resolver = provider.connect
    monkeypatch.setattr(goal_run, "get_goal_manager", lambda: controller)
    monkeypatch.setattr(messages, "_target", lambda tab: InboxTarget("inc"))
    monkeypatch.setattr(messages, "ensure_chat_runtime", AsyncMock(return_value=True))
    monkeypatch.setattr(stream, "_terminal_tab_session_or_404", lambda tab: SimpleNamespace())

    async def send(session, text, images, turn_id, **kwargs):
        await kwargs["before_send"]()
        kwargs["before_delivery"]()

    manager = SimpleNamespace(
        turn_in_flight=AsyncMock(return_value=False),
        send_message=AsyncMock(side_effect=send),
    )
    monkeypatch.setattr(stream, "_get_tab_tailer_manager", lambda: manager)
    inbox.submit = messages._submit
    goal = await controller.create_and_start(
        "t", GoalRunCreate(objective="Native work", client_request_id="create")
    )
    return inbox, provider, controller, manager, goal


@pytest.mark.parametrize("native_status", ["active", "paused"])
async def test_inbox_admission_refreshes_native_state_instead_of_using_cached_goal(
    native_inbox, native_status
):
    inbox, provider, controller, manager, goal = native_inbox
    if native_status == "active":
        await controller.pause(goal.id, "pause")
    assert controller.current("t").status.value != native_status
    provider.goal["status"] = native_status
    row = await inbox.enqueue(
        "t", "local", "CLI", ChatMessageCreate(message="hello", call_id="input")
    )
    item = inbox.messages[row.message_id]
    if native_status == "active":
        with pytest.raises(InboxError) as exc:
            await messages._submit(item, lambda: None)
        assert exc.value.status == 409
        manager.send_message.assert_not_awaited()
    else:
        await messages._submit(item, lambda: None)
        manager.send_message.assert_awaited_once()
        kwargs = manager.send_message.await_args.kwargs
        assert "strip_goal_protocol" not in kwargs
        assert kwargs["turn_metadata"]["origin"] == "hub_message"
    assert controller.current("t").status.value == native_status


async def test_unavailable_native_goal_parks_inbox_until_state_can_be_confirmed(native_inbox):
    inbox, provider, _, manager, _ = native_inbox
    row = await inbox.enqueue(
        "t", "local", "CLI", ChatMessageCreate(message="hello", call_id="input")
    )
    provider.read_error = True
    await inbox.pump("t")
    receipt = await inbox.get("t", row.message_id, "local")
    assert receipt.state == "waiting"
    manager.send_message.assert_not_awaited()
    provider.read_error = False
    provider.goal["status"] = "paused"
    await inbox.pump("t")
    manager.send_message.assert_awaited_once()
    assert (await inbox.get("t", row.message_id, "local")).state == "accepted"


async def test_native_observation_and_inbox_completion_both_survive_merge(monkeypatch):
    goals = SimpleNamespace(refresh_current=AsyncMock(), on_turn_completed=AsyncMock())
    inbox = AsyncMock()
    scheduled = AsyncMock()
    monkeypatch.setattr(goal_run, "get_goal_manager", lambda: goals)
    monkeypatch.setattr(messages, "observe_completion", inbox)
    monkeypatch.setattr(workspace_manager, "on_scheduled_chat_turn_completed", scheduled)
    event = AgentStreamEvent(
        stream_sequence=0,
        created_at=datetime.now(timezone.utc),
        session_id="terminal-tab-t",
        tab_id="t",
        agent_type=AgentType.CODEX,
        type=AgentStreamEventType.STATUS,
        turn_id="native-turn",
        run_epoch=1,
        payload={"provider_notification": "thread/goal/updated"},
    )
    await stream._notify_goal_turn_completed(event)
    goals.refresh_current.assert_awaited_once_with("t")
    inbox.assert_not_awaited()
    goals.on_turn_completed.assert_not_awaited()
    completed = event.model_copy(
        update={"type": AgentStreamEventType.TURN_COMPLETED, "payload": {"status": "completed"}}
    )
    await stream._notify_goal_turn_completed(completed)
    inbox.assert_awaited_once_with(completed)
    goals.on_turn_completed.assert_awaited_once()
    scheduled.assert_awaited_once()
