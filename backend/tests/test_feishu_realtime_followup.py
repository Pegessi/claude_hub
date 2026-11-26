"""Shared-card diagnosis, replay and cleanup contracts (all identities synthetic)."""

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

# Import before any resolver monkeypatch: importing the API under a patched
# agent module would permanently capture the fake resolver in realtime routes.
from claude_hub.api import feishu_bot as bot_api
from claude_hub.models import AgentType
from claude_hub.services import feishu_realtime_agent as agent
from claude_hub.services import feishu_realtime_delivery as delivery
from claude_hub.services.demand_patrol import feishu_triage as lifecycle
from claude_hub.services.dot import DotService, DotStore
from claude_hub.services.feishu_bot import FeishuBotClient, FeishuBotConfig
from claude_hub.services.feishu_card_cache import card_text
from claude_hub.services.feishu_realtime_matching import FeishuRealtimeEventConfig
from tests.test_demand_patrol_triage import FakeWorkspaceManager
from tests.test_feishu_realtime_agent import event

INITIAL = '{"decision":"investigate","summary":"Initial assessment","reply_draft":"Checking"}'
FINAL = '{"decision":"notify","summary":"Verified conclusion","reply_draft":"Use this fix"}'


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_HUB_HOME", str(tmp_path / "hub"))
    dot = DotService(DotStore(tmp_path / "dot"))
    monkeypatch.setattr(dot, "tab_id", lambda: "dot-tab")
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)
    manager = FakeWorkspaceManager()
    monkeypatch.setattr(lifecycle, "workspace_manager", manager)
    monkeypatch.setattr(
        agent,
        "resolve_realtime_agent",
        AsyncMock(return_value=agent.RealtimeAgent(AgentType.MASO, "test-model", "high", {})),
    )
    send = AsyncMock(return_value=(True, "om_initial", "test-app"))
    monkeypatch.setattr(bot_api, "push_shared_card_to_tab", send)
    return dot, manager, send


@pytest.mark.parametrize(
    "payload,expected",
    [
        (
            {"chat_name": "Team", "chat_id": "chat", "sender_name": "Peer", "sender_id": "sender"},
            "Team (chat) · Peer (sender)",
        ),
        ({"chat_id": "chat", "sender_id": "sender"}, "chat · sender"),
        ({"chat_name": " Team  One ", "sender_name": " Peer "}, "Team One · Peer"),
        ({"sender_name": "Peer"}, "Peer"),
        ({"chat_name": " ", "sender_id": "id"}, "id"),
        ({"chat_name": "id", "chat_id": "id"}, "id"),
        ({}, ""),
    ],
)
def test_source_plaintext_fallbacks(payload, expected):
    source = event().model_copy(update={"payload": payload})
    assert delivery.source_context(source) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        FINAL,
        RuntimeError("private"),
        TimeoutError(),
        "not json",
        '{"decision":"skip","summary":"No"}',
    ],
)
async def test_owed_followup_backfills_even_after_owner_reply(monkeypatch, runtime, result):
    dot, manager, send = runtime

    async def turn(*args, **kwargs):
        if run.await_count == 1:
            return INITIAL
        saved = delivery._read_delivery(dot.store, event().dedupe_key)
        assert saved.followup_due and saved.message_id == "om_initial"
        assert saved.initial_summary == "Initial assessment"
        if isinstance(result, Exception):
            raise result
        return result

    run = AsyncMock(side_effect=turn)
    monkeypatch.setattr(lifecycle, "_run_workspace_triage_turn", run)
    assert await delivery.deliver_realtime_triage(
        event(), FeishuRealtimeEventConfig(), still_unanswered=lambda: send.await_count == 0
    )
    first, last = (call.args[1] for call in send.call_args_list)
    assert first["config"]["update_multi"] is True
    assert "正在核查…" in json.dumps(first, ensure_ascii=False)
    text = json.dumps(last, ensure_ascii=False)
    assert "Initial assessment" in text and "正在核查…" not in text
    assert ("Verified conclusion" in text) == (result == FINAL)
    if result != FINAL:
        assert "Investigation could not be completed" in text and "private" not in text
    assert send.call_args.kwargs == {"message_id": "om_initial", "app_id": "test-app"}
    assert all(s.archived for s in manager.sessions.values())
    assert not await delivery.has_pending_realtime_followup(event().dedupe_key)
    assert await delivery.deliver_realtime_triage(event(), FeishuRealtimeEventConfig())
    assert send.await_count == run.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("two_beats", [False, True])
async def test_completed_decision_survives_archive_failure(monkeypatch, runtime, two_beats):
    dot, manager, send = runtime
    run = AsyncMock(side_effect=[INITIAL, FINAL] if two_beats else [FINAL])
    monkeypatch.setattr(lifecycle, "_run_workspace_triage_turn", run)
    archive = lifecycle._archive_patrol_session
    failure = lifecycle.PatrolTriageDiagnosisError("cleanup", retained_session_id="retained")
    monkeypatch.setattr(lifecycle, "_archive_patrol_session", AsyncMock(side_effect=failure))
    with pytest.raises(lifecycle.PatrolTriageDiagnosisError):
        await delivery.deliver_realtime_triage(event(), FeishuRealtimeEventConfig())
    saved = delivery._read_delivery(dot.store, event().dedupe_key)
    assert saved.decision == agent.RealtimeDecision.model_validate_json(FINAL)
    assert saved.notified
    assert "Verified conclusion" in json.dumps(send.call_args.args[1])
    monkeypatch.setattr(lifecycle, "_archive_patrol_session", archive)
    assert await delivery.deliver_realtime_triage(
        event(), FeishuRealtimeEventConfig(), still_unanswered=lambda: False
    )
    assert run.await_count == send.await_count == (2 if two_beats else 1)
    assert all(s.archived for s in manager.sessions.values())


@pytest.mark.asyncio
async def test_cancelled_with_failed_cleanup_preserves_followup_for_replay(monkeypatch, runtime):
    dot, _, send = runtime
    cancelled = asyncio.CancelledError()
    run = AsyncMock(side_effect=[INITIAL, cancelled, FINAL])
    monkeypatch.setattr(lifecycle, "_run_workspace_triage_turn", run)
    archive = lifecycle._archive_patrol_session
    monkeypatch.setattr(
        lifecycle,
        "_archive_patrol_session",
        AsyncMock(
            side_effect=lifecycle.PatrolTriageDiagnosisError(
                "cleanup", retained_session_id="retained"
            )
        ),
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        await delivery.deliver_realtime_triage(event(), FeishuRealtimeEventConfig())
    assert caught.value is cancelled and "archive_failed" in cancelled.__notes__[0]
    saved = delivery._read_delivery(dot.store, event().dedupe_key)
    assert saved.followup_due and saved.message_id == "om_initial"
    assert send.await_count == 1
    monkeypatch.setattr(lifecycle, "_archive_patrol_session", archive)
    assert await delivery.deliver_realtime_triage(
        event(), FeishuRealtimeEventConfig(), still_unanswered=lambda: False
    )
    assert run.await_count == 3 and send.await_count == 2
    assert "heads-up was already sent" in run.call_args.args[1]


@pytest.mark.asyncio
async def test_failed_followup_retry_keeps_original_summary(monkeypatch, runtime):
    dot, _, send = runtime
    run = AsyncMock(side_effect=[INITIAL, FINAL])
    monkeypatch.setattr(lifecycle, "_run_workspace_triage_turn", run)
    send.side_effect = [(True, "om_initial", "test-app"), (False, "", ""), (True, "", "")]
    assert not await delivery.deliver_realtime_triage(event(), FeishuRealtimeEventConfig())
    assert await delivery.has_pending_realtime_followup(event().dedupe_key)
    assert await delivery.deliver_realtime_triage(
        event(), FeishuRealtimeEventConfig(), still_unanswered=lambda: False
    )
    assert send.call_args_list[1] == send.call_args_list[2]
    assert run.await_count == 2


@pytest.mark.asyncio
async def test_legacy_schema_migrates_announced_investigation(monkeypatch, runtime):
    dot, _, send = runtime
    dot.store.ensure()
    with closing(sqlite3.connect(dot.store.home / "index.sqlite.events")) as db, db:
        db.execute(
            "CREATE TABLE realtime_triage (key TEXT PRIMARY KEY, decision TEXT, notified INTEGER)"
        )
        db.execute("INSERT INTO realtime_triage VALUES (?, ?, 1)", (event().dedupe_key, INITIAL))
    monkeypatch.setattr(lifecycle, "_run_workspace_triage_turn", AsyncMock(return_value=FINAL))
    assert await delivery.deliver_realtime_triage(
        event(), FeishuRealtimeEventConfig(), still_unanswered=lambda: False
    )
    assert send.await_count == 1
    assert send.call_args.kwargs["message_id"] == ""
    assert "Initial assessment" in json.dumps(send.call_args.args[1])


@pytest.mark.asyncio
async def test_outer_deadline_produces_terminal_failure(monkeypatch, runtime):
    calls = 0

    async def turn(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return INITIAL
        await asyncio.Event().wait()

    monkeypatch.setattr(agent, "_TURN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(lifecycle, "_run_workspace_triage_turn", turn)
    assert await delivery.deliver_realtime_triage(event(), FeishuRealtimeEventConfig())
    assert "Investigation could not be completed" in json.dumps(runtime[2].call_args.args[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["patch", "no-id", "patch-failed", "ping-failed", "other-bot"])
async def test_real_shared_card_transport_and_cache(tmp_path, monkeypatch, mode):
    """Exercise the real Bot client and cache, replacing only HTTP and binding lookup."""
    dot = DotService(DotStore(tmp_path / "dot"))
    monkeypatch.setattr(dot, "tab_id", lambda: "dot-tab")
    monkeypatch.setattr("claude_hub.services.dot.get_dot_service", lambda: dot)
    monkeypatch.setattr(delivery, "archive_realtime_event_sessions", AsyncMock())
    unanswered = True
    sent = []
    bot_config = FeishuBotConfig("synthetic-app", "test-secret")
    effective = SimpleNamespace(config=bot_config, bot_id="test-bot")
    binding = SimpleNamespace(tab_id="dot-tab", chat_id="oc_destination")
    monkeypatch.setattr(bot_api, "_bot_bound_to_tab", lambda _: SimpleNamespace(bot_id="test-bot"))
    monkeypatch.setattr(bot_api, "_entry_bindings", lambda _: (binding,))
    monkeypatch.setattr(bot_api, "_binding_still_routes", lambda *_: True)
    monkeypatch.setattr(
        bot_api,
        "_pool",
        SimpleNamespace(effective=lambda _: effective, is_current_for_delivery=lambda _: True),
    )
    ping_failed = False

    def http(request):
        nonlocal unanswered, ping_failed
        payload = json.loads(request.content)
        sent.append((request.method, request.url.path, payload))
        if request.method == "PATCH" and mode == "patch-failed":
            return httpx.Response(200, json={"code": 1})
        if request.url.path.endswith("/reply") and mode == "ping-failed" and not ping_failed:
            ping_failed = True
            return httpx.Response(200, json={"code": 1})
        unanswered = False
        mid = None if mode == "no-id" else "om_initial"
        return httpx.Response(200, json={"code": 0, "data": {"message_id": mid}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(http), base_url="https://example.com"
    ) as http_client:
        client = FeishuBotClient(bot_config, client=http_client)
        monkeypatch.setattr(client, "get_tenant_token", AsyncMock(return_value="synthetic-token"))
        monkeypatch.setattr(bot_api, "FeishuBotClient", lambda _: client)

        async def diagnose(*args, on_initial, **kwargs):
            assert await on_initial(agent.RealtimeDecision.model_validate_json(INITIAL))
            saved = delivery._read_delivery(dot.store, event().dedupe_key)
            assert saved.followup_due and saved.app_id == "synthetic-app"
            if mode == "other-bot":
                saved.app_id = "previous-app"
                delivery._save_delivery(dot.store, event().dedupe_key, saved)
                raise asyncio.CancelledError()
            return agent.RealtimeDecision.model_validate_json(FINAL)

        diagnosis = AsyncMock(side_effect=diagnose)
        monkeypatch.setattr(delivery, "diagnose_realtime_event", diagnosis)
        if mode == "other-bot":
            with pytest.raises(asyncio.CancelledError):
                await delivery.deliver_realtime_triage(event(), FeishuRealtimeEventConfig())
            diagnosis.side_effect = None
            diagnosis.return_value = agent.RealtimeDecision.model_validate_json(FINAL)
        accepted = await delivery.deliver_realtime_triage(
            event(), FeishuRealtimeEventConfig(), still_unanswered=lambda: unanswered
        )
        if mode == "ping-failed":
            assert not accepted
            assert await delivery.deliver_realtime_triage(event(), FeishuRealtimeEventConfig())
            assert diagnosis.await_count == 1
        else:
            assert accepted
    cards = [
        json.loads(payload["content"])
        for method, path, payload in sent
        if method == "POST" and not path.endswith("/reply")
    ]
    assert cards[0]["config"]["update_multi"] is True
    assert cards[0]["body"]["elements"][0]["text"]["tag"] == "plain_text"
    assert len(cards) == (2 if mode in {"no-id", "patch-failed", "other-bot"} else 1)
    if mode in {"patch", "ping-failed"}:
        patches = [
            json.loads(payload["content"]) for method, _, payload in sent if method == "PATCH"
        ]
        assert "Initial assessment" in json.dumps(patches[-1])
        assert "Verified conclusion" in json.dumps(patches[-1])
        assert "Use this fix" in json.dumps(patches[-1])
        cached = card_text("synthetic-app", "om_initial")
        assert cached and "Verified conclusion" in cached and "正在核查…" not in cached
        replies = [
            (path, json.loads(payload["content"])["text"])
            for _, path, payload in sent
            if path.endswith("/reply")
        ]
        assert replies[-1][0].endswith("/om_initial/reply") and "\n" not in replies[-1][1]


def test_prompt_requires_open_request():
    prompt = agent.triage_prompt(event())
    for term in ("venting", "complaints", "agreement", "commentary", "settled", "open question"):
        assert term in prompt
    assert "Return skip" in prompt and "without tools" in prompt


@pytest.mark.asyncio
async def test_resumed_member_restores_frozen_text_images_and_receipts_all_members(
    monkeypatch, runtime
):
    from claude_hub.models import WorkspaceAttachmentCreate
    from claude_hub.services.dot.events_gateway import has_external_event_receipt

    dot, _, send = runtime
    source = event(key="member-b")
    source.payload.update(text="A and B evidence", coalesced_message_ids=["member-a", "member-b"])
    image = WorkspaceAttachmentCreate(
        filename="a.png", mime_type="image/png", data_url="data:image/png;base64,AA=="
    )

    async def interrupted(ev, cfg, *, on_initial, **kwargs):
        assert await on_initial(agent.RealtimeDecision.model_validate_json(INITIAL))
        raise asyncio.CancelledError()

    monkeypatch.setattr(delivery, "diagnose_realtime_event", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await delivery.deliver_realtime_triage(
            source, FeishuRealtimeEventConfig(), attachments=[image]
        )
    run = AsyncMock(return_value=agent.RealtimeDecision.model_validate_json(FINAL))
    monkeypatch.setattr(delivery, "diagnose_realtime_event", run)
    assert await delivery.deliver_realtime_triage(
        event(key="member-a"), FeishuRealtimeEventConfig()
    )
    assert run.call_args.args[0] == source
    assert run.call_args.kwargs["attachments"] == [image]
    assert run.call_args.kwargs["resume_investigation"]
    assert await has_external_event_receipt("feishu", "member-a", dot)
    assert await has_external_event_receipt("feishu", "member-b", dot)
    assert delivery._read_delivery(dot.store, "member-a").event is None
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_failed_member_receipt_keeps_batch_without_resending(monkeypatch, runtime):
    dot, _, send = runtime
    source = event(key="member-b")
    source.payload["coalesced_message_ids"] = ["member-a", "member-b"]
    run = AsyncMock(return_value=agent.RealtimeDecision.model_validate_json(FINAL))
    monkeypatch.setattr(delivery, "diagnose_realtime_event", run)
    receipt = delivery.record_suppressed_external_event
    monkeypatch.setattr(
        delivery, "record_suppressed_external_event", AsyncMock(side_effect=OSError("disk"))
    )
    with pytest.raises(OSError):
        await delivery.deliver_realtime_triage(source, FeishuRealtimeEventConfig())
    assert delivery._read_delivery(dot.store, "member-a").notified
    monkeypatch.setattr(delivery, "record_suppressed_external_event", receipt)
    assert await delivery.deliver_realtime_triage(
        event(key="member-a"), FeishuRealtimeEventConfig()
    )
    assert run.await_count == send.await_count == 1
