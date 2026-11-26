"""Background realtime triage: provider resolution, lifecycle and passive delivery."""

import asyncio
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from claude_hub.api import feishu_bot as bot_api
from claude_hub.models import AgentType, SessionKind, StreamModelOption, StreamReasoningEffortOption
from claude_hub.services import feishu_realtime_agent as agent
from claude_hub.services import feishu_realtime_delivery as delivery
from claude_hub.services.demand_patrol import feishu_triage as lifecycle
from claude_hub.services.dot import DotService, DotStore
from claude_hub.services.dot.context import build_turn_pack
from claude_hub.services.dot.events_gateway import ExternalEvent, triggers_wake
from claude_hub.services.feishu_realtime_events import FeishuRealtimeEventRouter
from claude_hub.services.feishu_realtime_matching import FeishuRealtimeEventConfig
from tests.test_demand_patrol_triage import FakeWorkspaceManager


def event(kind: str = "mention", key: str = "message-1") -> ExternalEvent:
    return ExternalEvent(
        source="feishu",
        event_type=f"im.{kind}",
        dedupe_key=key,
        title="Feishu question",
        summary="Can you explain this?",
        payload={"chat_id": "chat-1", "sender_id": "sender-1", "text": "Can you explain this?"},
    )


def model_option(name: str) -> StreamModelOption:
    return StreamModelOption(
        id=name,
        label=name,
        provider_model_id=name,
        supported_reasoning_efforts=[
            StreamReasoningEffortOption(
                id="high", label="High", provider_model_id=f"{name}_reasoning_high"
            )
        ],
    )


@pytest.fixture
def live_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    import claude_hub.services.feishu_ai_command_models as models

    monkeypatch.setattr(models, "_DYNAMIC_MASO_MODEL_VARIANTS", {})
    monkeypatch.setattr(models, "_DYNAMIC_MASO_DEFAULT_MODEL", "")
    monkeypatch.setattr(
        agent,
        "discover_maso_service",
        AsyncMock(
            return_value=(
                "http://localhost:9000",
                [model_option("large-model"), model_option("flash-model")],
                "large-model",
            )
        ),
    )
    monkeypatch.setattr(agent, "local_setting", lambda _name, default="": default)


@pytest.mark.asyncio
async def test_live_default_prefers_flash_and_legacy_variant_maps_to_logical_id(
    monkeypatch: pytest.MonkeyPatch, live_catalog: None
) -> None:
    result = await agent.resolve_realtime_agent(FeishuRealtimeEventConfig())
    assert (result.provider, result.model, result.reasoning_effort) == (
        AgentType.MASO,
        "flash-model",
        "high",
    )
    assert result.env["MASO_MODEL"] == "flash-model_reasoning_high"
    monkeypatch.setattr(agent, "local_setting", lambda *_: "large-model_reasoning_high")
    result = await agent.resolve_realtime_agent(FeishuRealtimeEventConfig())
    assert result.model == "large-model" and result.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_unavailable_default_falls_back_but_explicit_model_fails(
    monkeypatch: pytest.MonkeyPatch, live_catalog: None
) -> None:
    monkeypatch.setattr(
        agent, "discover_maso_service", AsyncMock(side_effect=RuntimeError("offline"))
    )
    monkeypatch.setattr(agent.shutil, "which", lambda name: name if name == "codex" else None)
    result = await agent.resolve_realtime_agent(FeishuRealtimeEventConfig())
    assert result.provider == AgentType.CODEX
    assert result.env == {"CODEX_REASONING_EFFORT": "high"}
    with pytest.raises(RuntimeError, match="offline"):
        await agent.resolve_realtime_agent(FeishuRealtimeEventConfig(model="chosen-model"))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "malformed", "cancelled"])
async def test_ephemeral_session_archived_on_every_outcome(
    monkeypatch: pytest.MonkeyPatch, live_catalog: None, outcome: str
) -> None:
    manager = FakeWorkspaceManager()
    monkeypatch.setattr(lifecycle, "workspace_manager", manager)
    turn = AsyncMock(return_value='{"decision":"notify","summary":"Answer","reply_draft":"Draft"}')
    if outcome == "malformed":
        turn.return_value = "not json"
    elif outcome == "cancelled":
        turn.side_effect = asyncio.CancelledError()
    monkeypatch.setattr(lifecycle, "_run_workspace_triage_turn", turn)
    if outcome == "success":
        assert (
            await agent.diagnose_realtime_event(event(), FeishuRealtimeEventConfig())
        ).decision == "notify"
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancelled" else ValueError):
            await agent.diagnose_realtime_event(event(), FeishuRealtimeEventConfig())
    _, request, bootstrap = manager.ensure_calls[0]
    assert request.session_kind == SessionKind.CHAT
    assert request.ephemeral and request.caller_owned_ephemeral and not request.reuse_existing
    assert not request.solo_mode and not bootstrap
    assert request.env["MASO_MODEL"] == "flash-model_reasoning_high"
    assert all(s.archived for s in manager.sessions.values())
    prompt = turn.call_args.args[1]
    assert "Do not modify files" in prompt and "untrusted" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mention", "watch_chat_url", "p2p", "topic_reply"])
@pytest.mark.parametrize("decision", ["skip", "notify"])
async def test_each_kind_silent_or_notifies_without_waking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, decision: Literal["skip", "notify"]
) -> None:
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path / "hub"))
    dot = DotService(DotStore(tmp_path / "dot"))
    notify = AsyncMock(return_value=(True, "om_notice", "app"))
    monkeypatch.setattr(bot_api, "push_shared_card_to_tab", notify)
    monkeypatch.setattr(dot, "tab_id", lambda: "dot-tab")
    dot.wake_for_external_event = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)
    diagnose = AsyncMock(
        return_value=agent.RealtimeDecision(
            decision=decision,
            summary="The evidence says wait.",
            reply_draft="Please wait for the result.",
        )
    )
    monkeypatch.setattr(delivery, "diagnose_realtime_event", diagnose)
    router = FeishuRealtimeEventRouter(config=FeishuRealtimeEventConfig())
    assert await router._deliver_event(event(kind), router.resolve_config())
    assert await router._deliver_event(event(kind), router.resolve_config())
    diagnose.assert_awaited_once()
    dot.wake_for_external_event.assert_not_awaited()
    if decision == "skip":
        notify.assert_not_awaited()
        assert not dot.store.list_inbox()
    else:
        notify.assert_awaited_once()
        inbox = dot.store.list_inbox()
        assert len(inbox) == 1 and not triggers_wake(inbox[0])
        pack = build_turn_pack(
            dot.store, None, "What happened?", first_turn=False, session_turn=1, wake=False
        ).text
        assert "Please wait for the result." in pack
        assert "The evidence says wait." in pack
        assert (
            "Please wait for the result."
            not in build_turn_pack(
                dot.store, None, "", first_turn=False, session_turn=1, wake=True
            ).text
        )


@pytest.mark.asyncio
async def test_concurrency_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()
    reached = asyncio.Event()
    active = peak = 0

    async def slow(
        _event: ExternalEvent, _config: FeishuRealtimeEventConfig, **_kw: object
    ) -> bool:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            reached.set()
        await release.wait()
        active -= 1
        return True

    monkeypatch.setattr(delivery, "deliver_realtime_triage", slow)
    router = FeishuRealtimeEventRouter()
    tasks = [
        asyncio.create_task(router._deliver_event(event(key=str(i)), FeishuRealtimeEventConfig()))
        for i in range(8)
    ]
    await asyncio.wait_for(reached.wait(), 2)
    assert peak == 2
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2


@pytest.mark.asyncio
async def test_failed_notify_can_retry_without_waking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path / "hub"))
    dot = DotService(DotStore(tmp_path / "dot"))
    notify = AsyncMock(side_effect=[(False, "", ""), (True, "om_notice", "app")])
    monkeypatch.setattr(bot_api, "push_shared_card_to_tab", notify)
    monkeypatch.setattr(dot, "tab_id", lambda: "dot-tab")
    dot.wake_for_external_event = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)
    monkeypatch.setattr(
        delivery,
        "diagnose_realtime_event",
        AsyncMock(return_value=agent.RealtimeDecision(decision="notify", summary="Answer")),
    )
    assert not await delivery.deliver_realtime_triage(event(), FeishuRealtimeEventConfig())
    assert await delivery.deliver_realtime_triage(event(), FeishuRealtimeEventConfig())
    dot.wake_for_external_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_card_is_not_resent_when_passive_store_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path / "hub"))
    dot = DotService(DotStore(tmp_path / "dot"))
    notify = AsyncMock(return_value=(True, "om_notice", "app"))
    monkeypatch.setattr(bot_api, "push_shared_card_to_tab", notify)
    monkeypatch.setattr(dot, "tab_id", lambda: "dot-tab")
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)
    diagnose = AsyncMock(return_value=agent.RealtimeDecision(decision="notify", summary="Answer"))
    monkeypatch.setattr(delivery, "diagnose_realtime_event", diagnose)
    original = delivery.submit_external_event
    monkeypatch.setattr(
        delivery,
        "submit_external_event",
        AsyncMock(side_effect=OSError("disk temporarily unavailable")),
    )
    with pytest.raises(OSError):
        await delivery.deliver_realtime_triage(event(), FeishuRealtimeEventConfig())
    monkeypatch.setattr(delivery, "submit_external_event", original)
    assert await delivery.deliver_realtime_triage(
        event(), FeishuRealtimeEventConfig(), still_unanswered=lambda: False
    )
    diagnose.assert_awaited_once()
    notify.assert_awaited_once()
    assert len(dot.store.list_inbox()) == 1


@pytest.mark.asyncio
async def test_answer_arriving_during_diagnosis_suppresses_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path / "hub"))
    dot = DotService(DotStore(tmp_path / "dot"))
    notify = AsyncMock(return_value=(True, "om_notice", "app"))
    monkeypatch.setattr(bot_api, "push_shared_card_to_tab", notify)
    monkeypatch.setattr(dot, "tab_id", lambda: "dot-tab")
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)
    monkeypatch.setattr(
        delivery,
        "diagnose_realtime_event",
        AsyncMock(return_value=agent.RealtimeDecision(decision="notify", summary="Answer")),
    )
    assert await delivery.deliver_realtime_triage(
        event(), FeishuRealtimeEventConfig(), still_unanswered=lambda: False
    )
    notify.assert_not_awaited()
    assert not dot.store.list_inbox()


@pytest.mark.asyncio
async def test_saved_claude_empty_effort_survives_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.feishu_realtime_settings import write_realtime_settings

    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path))
    write_realtime_settings({"provider": "claude", "model": "", "reasoning_effort": ""})
    config = FeishuRealtimeEventConfig.from_runtime()
    assert config.reasoning_effort == ""
    assert (await agent.resolve_realtime_agent(config)).provider == AgentType.CLAUDE


@pytest.mark.asyncio
async def test_settings_api_validates_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from types import SimpleNamespace

    import httpx
    from fastapi import APIRouter, FastAPI

    from claude_hub.api.routes.feishu_bot import realtime

    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path))
    monkeypatch.setattr(
        realtime,
        "get_feishu_ai_poller",
        lambda: SimpleNamespace(realtime_status=lambda: {"effective_enabled": True}),
    )
    app, router = FastAPI(), APIRouter()
    realtime.register_realtime_routes(router, require_hub_user=lambda: None)
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.patch(
            "/realtime-events",
            json={
                "provider": "claude",
                "reasoning_effort": "",
                "model": "",
                "workspace_id": "",
                "p2p_debounce_seconds": 45,
                "p2p_active_seconds": 300,
            },
        )
        assert response.status_code == 200
        assert response.json()["config"]["reasoning_effort"] == ""
        saved = json.loads((tmp_path / "feishu_realtime_events.json").read_text())
        assert saved["provider"] == "claude"
        assert saved["p2p_debounce_seconds"] == 45 and saved["p2p_active_seconds"] == 300
        before = (tmp_path / "feishu_realtime_events.json").read_text()
        for bad in (
            {"provider": "invalid"},
            {"reasoning_effort": None},
            {"unknown": True},
            {"p2p_debounce_seconds": -1},
            {"p2p_active_seconds": "NaN"},
            {"p2p_active_seconds": 86401},
        ):
            assert (await client.patch("/realtime-events", json=bad)).status_code == 422
        assert (tmp_path / "feishu_realtime_events.json").read_text() == before


@pytest.mark.asyncio
async def test_settings_keeps_automatic_separate_from_effective_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_catalog: None
) -> None:
    from types import SimpleNamespace

    from claude_hub.api.routes.feishu_bot import realtime

    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path))
    monkeypatch.setattr(
        realtime, "get_feishu_ai_poller", lambda: SimpleNamespace(realtime_status=lambda: {})
    )
    result = await realtime.realtime_settings_payload()
    assert result["config"]["model"] == ""
    assert result["effective_agent"]["model"] == "flash-model"


@pytest.mark.asyncio
async def test_automatic_selection_honors_effort(
    monkeypatch: pytest.MonkeyPatch, live_catalog: None
) -> None:
    option = model_option("flash-model")
    option.supported_reasoning_efforts.append(
        StreamReasoningEffortOption(
            id="low", label="Low", provider_model_id="flash-model_reasoning_low"
        )
    )
    monkeypatch.setattr(
        agent,
        "discover_maso_service",
        AsyncMock(return_value=("http://localhost:9000", [option], "flash-model")),
    )
    result = await agent.resolve_realtime_agent(FeishuRealtimeEventConfig(reasoning_effort="low"))
    assert result.env["MASO_MODEL"] == "flash-model_reasoning_low"


@pytest.mark.asyncio
async def test_raw_advertised_reasoning_variant_is_a_valid_default(
    monkeypatch: pytest.MonkeyPatch, live_catalog: None
) -> None:
    option = StreamModelOption(id="fast-model_reasoning_high", label="Fast reasoning")
    monkeypatch.setattr(
        agent,
        "discover_maso_service",
        AsyncMock(return_value=("http://localhost:9000", [option], option.id)),
    )
    result = await agent.resolve_realtime_agent(FeishuRealtimeEventConfig())
    assert result.env["MASO_MODEL"] == option.id


@pytest.mark.asyncio
async def test_two_beat_same_session_and_retry_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_catalog: None
) -> None:
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path / "hub"))
    dot = DotService(DotStore(tmp_path / "dot"))
    notify = AsyncMock(return_value=(True, "om_notice", "app"))
    monkeypatch.setattr(bot_api, "push_shared_card_to_tab", notify)
    monkeypatch.setattr(dot, "tab_id", lambda: "dot-tab")
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)
    manager = FakeWorkspaceManager()
    monkeypatch.setattr(lifecycle, "workspace_manager", manager)
    turn = AsyncMock(
        side_effect=[
            '{"decision":"investigate","summary":"Looking into it","reply_draft":""}',
            asyncio.CancelledError(),
            '{"decision":"notify","summary":"Found the cause","reply_draft":"Try this"}',
        ]
    )
    monkeypatch.setattr(lifecycle, "_run_workspace_triage_turn", turn)
    with pytest.raises(asyncio.CancelledError):
        await delivery.deliver_realtime_triage(event(), FeishuRealtimeEventConfig())
    assert await delivery.deliver_realtime_triage(event(), FeishuRealtimeEventConfig())
    assert notify.await_count == 2
    assert turn.call_args_list[0].args[0] is turn.call_args_list[1].args[0]
    assert "heads-up was already sent" in turn.call_args_list[2].args[1]
    assert "screenshots" in turn.call_args_list[1].args[1]
    assert all(session.archived for session in manager.sessions.values())


@pytest.mark.asyncio
async def test_owner_reply_does_not_suppress_second_beat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_catalog: None
) -> None:
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path / "hub"))
    dot = DotService(DotStore(tmp_path / "dot"))
    notify = AsyncMock(return_value=(True, "om_notice", "app"))
    monkeypatch.setattr(bot_api, "push_shared_card_to_tab", notify)
    monkeypatch.setattr(dot, "tab_id", lambda: "dot-tab")
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)
    manager = FakeWorkspaceManager()
    monkeypatch.setattr(lifecycle, "workspace_manager", manager)
    unanswered = True

    async def turn(*args: object, **kwargs: object) -> str:
        nonlocal unanswered
        if unanswered:
            return '{"decision":"investigate","summary":"Investigating"}'
        return '{"decision":"notify","summary":"Found it"}'

    async def send(*args: object, **kwargs: object) -> tuple[bool, str, str]:
        nonlocal unanswered
        unanswered = False
        return True, "om_notice", "app"

    notify.side_effect = send
    monkeypatch.setattr(lifecycle, "_run_workspace_triage_turn", turn)
    assert await delivery.deliver_realtime_triage(
        event(), FeishuRealtimeEventConfig(), still_unanswered=lambda: unanswered
    )
    assert notify.await_count == 2
    assert all(session.archived for session in manager.sessions.values())


@pytest.mark.asyncio
async def test_image_attachment_reaches_the_investigation_session(
    monkeypatch: pytest.MonkeyPatch, live_catalog: None
) -> None:
    from claude_hub.models import WorkspaceAttachmentCreate

    manager = FakeWorkspaceManager()
    monkeypatch.setattr(lifecycle, "workspace_manager", manager)
    turn = AsyncMock(
        side_effect=[
            '{"decision":"investigate","summary":"Looking"}',
            '{"decision":"notify","summary":"Found"}',
        ]
    )
    monkeypatch.setattr(lifecycle, "_run_workspace_triage_turn", turn)
    image = WorkspaceAttachmentCreate(
        filename="image.png", mime_type="image/png", data_url="data:image/png;base64,AA=="
    )
    await agent.diagnose_realtime_event(
        event(),
        FeishuRealtimeEventConfig(),
        on_initial=AsyncMock(return_value=True),
        attachments=[image],
    )
    assert turn.call_args_list[0].kwargs["attachments"] == [image]
    assert turn.call_args_list[0].args[0] is turn.call_args_list[1].args[0]
