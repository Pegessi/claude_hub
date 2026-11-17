"""Tests for Feishu personal ``/ai`` command workflow, user token store/client, poller, and API."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from claude_hub.api.routes.feishu_bot.ai_command import register_ai_command_routes
from claude_hub.models import AgentType
from claude_hub.services.feishu_ai_command import (
    DEFAULT_AI_COMMAND_FOOTER,
    FeishuAiCommandConfig,
    FeishuAiCommandService,
    build_ai_post_payload,
    build_interim_reply_text,
    extract_context_references,
    parse_ai_command,
    resolve_ai_command_agent_env,
    validate_ai_command_agent_config,
)
from claude_hub.services.feishu_ai_poller import FeishuAiPoller
from claude_hub.services.feishu_bot import FeishuMessageDedupStore
from claude_hub.services.feishu_bot_websocket import message_event_from_sdk
from claude_hub.services.feishu_user_client import (
    REQUIRED_SEND_AS_USER_SCOPE,
    FeishuUserClient,
    FeishuUserMessage,
    FeishuUserScopeError,
)
from claude_hub.services.feishu_user_token_store import (
    FeishuUserTokenStore,
    aes_256_gcm_encrypt,
)


def test_user_token_store_oauth_and_encrypted_lark_cli_import(tmp_path: Path) -> None:
    store = FeishuUserTokenStore(
        path=tmp_path / "secrets" / "feishu_user_tokens.json",
        key_path=tmp_path / "secrets" / "feishu_user_tokens.key",
        now=lambda: 1000.0,
    )
    saved = store.save_oauth_token(
        open_id="ou_owner_1",
        user_name="Test Owner",
        app_id="cli_app_1",
        access_token="u-oauth-token-1",
        refresh_token="ur-oauth-refresh-1",
        expires_in=7200,
        scope="im:message im:message.send_as_user",
    )
    assert saved.open_id == "ou_owner_1"
    assert saved.has_scope(REQUIRED_SEND_AS_USER_SCOPE)
    assert (tmp_path / "secrets" / "feishu_user_tokens.key").stat().st_mode & 0o777 == 0o600

    loaded = store.get_token("ou_owner_1", app_id="cli_app_1")
    assert loaded is not None
    assert loaded.access_token == "u-oauth-token-1"
    assert loaded.refresh_token == "ur-oauth-refresh-1"

    # Now simulate ~/.lark-cli/config.json + ~/.local/share/lark-cli/<appId>_<openId>.enc
    lark_dir = tmp_path / ".lark-cli"
    data_dir = tmp_path / "lark-cli-data"
    lark_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    (lark_dir / "config.json").write_text(
        json.dumps(
            {
                "apps": [
                    {
                        "appId": "cli_lark_app",
                        "appSecret": {"source": "keychain", "id": "appsecret:cli_lark_app"},
                        "brand": "feishu",
                        "users": [
                            {"userOpenId": "ou_lark_user", "userName": "[redacted]"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    master_key = secrets.token_bytes(32)
    (data_dir / "master.key").write_bytes(master_key)
    token_payload = {
        "userOpenId": "ou_lark_user",
        "appId": "cli_lark_app",
        "accessToken": "u-lark-cli-decrypted",
        "refreshToken": "ur-lark-cli-refresh",
        "expiresAt": 1794920401245,
        "refreshExpiresAt": 1795518001245,
        "scope": "im:message im:message.reactions:write_only im:chat:read search:message",
    }
    (data_dir / "cli_lark_app_ou_lark_user.enc").write_bytes(
        aes_256_gcm_encrypt(master_key, json.dumps(token_payload).encode("utf-8"))
    )
    (data_dir / "appsecret_cli_lark_app.enc").write_bytes(
        aes_256_gcm_encrypt(master_key, b"sec_from_keychain")
    )

    imported = store.import_from_lark_cli(
        lark_cli_dir=lark_dir,
        data_dir=data_dir,
        app_id="cli_lark_app",
    )
    assert imported is not None
    assert imported.open_id == "ou_lark_user"
    assert imported.user_name == "[redacted]"
    assert imported.access_token == "u-lark-cli-decrypted"
    assert imported.app_secret == "sec_from_keychain"
    assert imported.source == "lark_cli"
    assert not imported.has_scope(REQUIRED_SEND_AS_USER_SCOPE)


@pytest.mark.asyncio
async def test_feishu_user_client_preflight_and_operations(tmp_path: Path) -> None:
    store = FeishuUserTokenStore(
        path=tmp_path / "secrets" / "tokens.json",
        key_path=tmp_path / "secrets" / "tokens.key",
    )
    # Token without im:message.send_as_user fails require_send_as_user_scope loudly
    store.save_oauth_token(
        open_id="ou_me",
        app_id="cli_test",
        access_token="u-missing-scope",
        refresh_token="ur-1",
        scope="im:message im:message.reactions:write_only",
    )

    requests_seen: list[tuple[str, str, str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("Authorization", "")
        body = json.loads(req.content.decode("utf-8")) if req.content else None
        requests_seen.append((req.method, req.url.path, auth, body))
        if req.url.path.endswith("/reactions") and req.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"reaction_id": "rx_on_it"}})
        if "/reactions/" in req.url.path and req.method == "DELETE":
            return httpx.Response(200, json={"code": 0, "data": {}})
        if req.url.path.endswith("/reply") and req.method == "POST":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"message_id": "om_reply_1", "thread_id": "omt_1"}},
            )
        if req.url.path == "/open-apis/im/v1/chats":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"chat_id": "oc_chat_1", "name": "[redacted] review"}],
                        "has_more": False,
                    },
                },
            )
        if req.url.path == "/open-apis/im/v1/messages":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "message_id": "om_ctx_1",
                                "chat_id": "oc_chat_1",
                                "thread_id": "omt_1",
                                "sender": {"id": "ou_peer", "sender_type": "user"},
                                "msg_type": "text",
                                "create_time": "1700000001000",
                                "body": {
                                    "content": json.dumps(
                                        {"text": "请看 [redacted]!4045 最新 commit 2a0bab453f"}
                                    )
                                },
                            }
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_sent_1"}})

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        user_client = FeishuUserClient(
            store,
            open_id="ou_me",
            app_id="cli_test",
            client=http_client,
        )
        preflight = user_client.preflight_scopes()
        assert preflight.can_send_as_user is False
        assert REQUIRED_SEND_AS_USER_SCOPE in preflight.missing_required_scopes
        assert "lark-cli auth login" in preflight.auth_login_command

        with pytest.raises(FeishuUserScopeError) as exc_info:
            await user_client.reply_as_user("om_target", "post", {"zh_cn": {"content": []}})
        assert REQUIRED_SEND_AS_USER_SCOPE in str(exc_info.value)

        # Grant im:message.send_as_user and verify user-token calls succeed
        store.save_oauth_token(
            open_id="ou_me",
            app_id="cli_test",
            access_token="u-valid-user-token",
            refresh_token="ur-1",
            scope="im:message im:message.send_as_user im:message.reactions:write_only im:chat:read",
        )
        assert user_client.preflight_scopes().can_send_as_user is True

        rx_id = await user_client.add_reaction_as_user("om_target", "OnIt")
        assert rx_id == "rx_on_it"
        await user_client.delete_reaction_as_user("om_target", rx_id)

        receipt = await user_client.reply_as_user(
            "om_target",
            "post",
            build_ai_post_payload("审查通过"),
            reply_in_thread=True,
            uuid="ai:test:1",
        )
        assert receipt.message_id == "om_reply_1"
        assert receipt.thread_id == "omt_1"

        chats = await user_client.list_chats()
        assert len(chats) == 1
        assert chats[0].chat_id == "oc_chat_1"

        msgs = await user_client.list_messages(container_id_type="thread", container_id="omt_1")
        assert len(msgs) == 1
        assert "[redacted]!4045" in msgs[0].text

    assert all(auth == "Bearer u-valid-user-token" for _, _, auth, _ in requests_seen)


def test_parse_ai_command_and_reference_extraction() -> None:
    parsed = parse_ai_command(
        "/ai --provider codex --model gpt-5.4 --effort xhigh review一下 没问题approve",
        default_provider=AgentType.CLAUDE,
        default_model="[redacted]",
        default_reasoning_effort="high",
    )
    assert parsed is not None
    assert parsed.provider == AgentType.CODEX
    assert parsed.model == "gpt-5.4"
    assert parsed.reasoning_effort == "xhigh"
    assert parsed.instruction == "review一下 没问题approve"
    assert parsed.effective_env["CODEX_MODEL"] == "gpt-5.4"
    assert parsed.effective_env["CODEX_REASONING_EFFORT"] == "xhigh"

    # Preserves multi-line formatting and quotes in instruction body after leading flags
    multiline = parse_ai_command(
        '/ai --model=o3 --effort high 第一行 "保留引号"\n  第二行缩进',
        default_provider=AgentType.CODEX,
        default_model="gpt-5.4",
        default_reasoning_effort="medium",
    )
    assert multiline is not None
    assert multiline.model == "o3"
    assert multiline.reasoning_effort == "high"
    assert multiline.instruction == '第一行 "保留引号"\n  第二行缩进'

    # Rejects messages containing the AI reply footer to prevent self-trigger loops
    assert (
        parse_ai_command(
            f"/ai test\n{DEFAULT_AI_COMMAND_FOOTER}",
        )
        is None
    )

    # Validates provider and reasoning_effort
    with pytest.raises(ValueError, match="cannot run /ai Chat turns"):
        validate_ai_command_agent_config("terminal", "m", "high")
    with pytest.raises(ValueError, match="Unsupported /ai provider"):
        validate_ai_command_agent_config("unknown-provider", "m", "high")
    with pytest.raises(ValueError, match="Invalid reasoning_effort"):
        validate_ai_command_agent_config("claude", "m", "super-duper")

    cursor_env = resolve_ai_command_agent_env("cursor", "gpt-5.3-codex", "xhigh")
    assert cursor_env["CURSOR_MODEL"] == "gpt-5.3-codex-extra-high"

    refs = extract_context_references(
        [
            "[redacted]发起了 https://[redacted]/[redacted]/merge_requests/4045",
            "最新 commit 2a0bab453f9988776655，文档见 https://[redacted]/docx/doxcnAbc123",
            "顺便看下 !4046",
        ]
    )
    assert refs.mr_refs == ("[redacted]!4045", "[redacted]!4046")
    assert refs.commit_refs == ("2a0bab453f",)
    assert refs.doc_refs == ("https://[redacted]/docx/doxcnAbc123",)

    interim = build_interim_reply_text(parsed, refs)
    assert "正在派 Subagent 对 [redacted]!4045（最新 commit 2a0bab453f）" in interim
    assert interim.endswith(DEFAULT_AI_COMMAND_FOOTER)


@pytest.mark.asyncio
async def test_ai_command_pipeline_poller_and_ws_hook(tmp_path: Path) -> None:
    store = FeishuUserTokenStore(
        path=tmp_path / "secrets" / "tokens.json",
        key_path=tmp_path / "secrets" / "tokens.key",
    )
    store.save_oauth_token(
        open_id="ou_owner",
        user_name="任洪滨",
        app_id="cli_app_1",
        access_token="u-owner-token",
        refresh_token="ur-owner",
        scope=(
            "im:message im:message.send_as_user im:message.reactions:write_only "
            "im:chat:read im:message.group_msg:get_as_user"
        ),
    )

    api_events: list[tuple[str, str, Any]] = []

    def feishu_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode("utf-8")) if req.content else None
        api_events.append((req.method, req.url.path, body))
        if req.url.path.endswith("/reactions") and req.method == "POST":
            assert isinstance(body, dict)
            emoji = body["reaction_type"]["emoji_type"]
            return httpx.Response(200, json={"code": 0, "data": {"reaction_id": f"rx_{emoji}"}})
        if "/reactions/" in req.url.path and req.method == "DELETE":
            return httpx.Response(200, json={"code": 0, "data": {}})
        if req.url.path.endswith("/reply") and req.method == "POST":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"message_id": "om_reply", "thread_id": "omt_thread_1"}},
            )
        if req.url.path == "/open-apis/im/v1/chats":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"items": [{"chat_id": "oc_group_1"}], "has_more": False}},
            )
        if req.url.path == "/open-apis/im/v1/messages":
            container_type = req.url.params.get("container_id_type")
            if container_type == "thread":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "message_id": "om_root",
                                    "chat_id": "oc_group_1",
                                    "thread_id": "omt_thread_1",
                                    "sender": {"id": "ou_other", "sender_type": "user"},
                                    "msg_type": "text",
                                    "create_time": "1700000001000",
                                    "body": {
                                        "content": json.dumps(
                                            {
                                                "text": "请 review [redacted]!4045 commit 2a0bab453f"
                                            }
                                        )
                                    },
                                }
                            ]
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "message_id": "om_other_ai",
                                "chat_id": "oc_group_1",
                                "thread_id": "omt_thread_1",
                                "sender": {"id": "ou_colleague", "sender_type": "user"},
                                "msg_type": "text",
                                "create_time": "9999999991000",
                                "body": {"content": json.dumps({"text": "/ai 别人发的指令不触发"})},
                            },
                            {
                                "message_id": "om_owner_ai_1",
                                "chat_id": "oc_group_1",
                                "thread_id": "omt_thread_1",
                                "sender": {"id": "ou_owner", "sender_type": "user"},
                                "msg_type": "text",
                                "create_time": "9999999992000",
                                "body": {
                                    "content": json.dumps({"text": "/ai review一下 没问题approve"})
                                },
                            },
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {}})

    created_tabs: list[dict[str, Any]] = []
    tabs_by_id: dict[str, SimpleNamespace] = {}
    switched_envs: list[tuple[str, dict[str, str]]] = []
    dispatched_turns: list[tuple[str, str, str, dict[str, Any]]] = []

    async def fake_create_tab(**kwargs: Any) -> SimpleNamespace:
        created_tabs.append(kwargs)
        tab_id = f"tab-ai-{len(created_tabs)}"
        tab_obj = SimpleNamespace(id=tab_id, env=dict(kwargs.get("env") or {}), archived=False)
        tabs_by_id[tab_id] = tab_obj
        return tab_obj

    async def fake_switch_tab_env(tab_id: str, new_env: dict[str, str]) -> None:
        switched_envs.append((tab_id, dict(new_env)))
        if tab_id in tabs_by_id:
            tabs_by_id[tab_id].env = dict(new_env)

    async def fake_dispatch(
        tab_id: str,
        prompt: str,
        turn_id: str,
        visible_text: str,
        metadata: Any,
    ) -> str:
        dispatched_turns.append((tab_id, prompt, visible_text, dict(metadata)))
        return "v6（2a0bab453f）逐文件审查通过，未发现阻塞问题。"

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(feishu_handler),
    ) as http_client:
        dedup = FeishuMessageDedupStore(tmp_path / "dedup.json")
        service = FeishuAiCommandService(
            state_path=tmp_path / "ai_command.json",
            token_store=store,
            dedup_store=dedup,
            user_client_factory=lambda cfg: FeishuUserClient(
                store,
                open_id=cfg.owner_open_id,
                app_id=cfg.app_id,
                client=http_client,
            ),
            tab_creator=fake_create_tab,
            tab_getter=lambda tid: tabs_by_id.get(tid),
            tab_env_switcher=fake_switch_tab_env,
            turn_dispatcher=fake_dispatch,
        )
        service.update_config(
            {
                "enabled": True,
                "owner_open_id": "ou_owner",
                "app_id": "cli_app_1",
                "provider": "maso",
                "model": "[redacted]_reasoning_high",
                "reasoning_effort": "high",
            }
        )
        poller = FeishuAiPoller(service)
        processed = await poller.poll_once()
        assert processed == ["om_owner_ai_1"]
        assert poller.status_dict()["commands_succeeded"] == 1

        # Verify tab creation & turn dispatch metadata
        assert len(created_tabs) == 1
        assert created_tabs[0]["agent_type"] == AgentType.MASO
        assert created_tabs[0]["env"]["MASO_MODEL"] == "[redacted]_reasoning_high"
        assert len(dispatched_turns) == 1
        _, prompt_sent, _, meta = dispatched_turns[0]
        assert meta["origin"] == "feishu_ai_command"
        assert meta["mr_refs"] == ["[redacted]!4045"]
        assert meta["commit_refs"] == ["2a0bab453f"]
        assert "[redacted]!4045" in prompt_sent

        # Verify reaction lifecycle: OnIt added -> interim post -> final post -> OnIt deleted -> DONE added
        reaction_Adds = [
            b["reaction_type"]["emoji_type"]
            for m, p, b in api_events
            if m == "POST" and p.endswith("/reactions")
        ]
        reaction_deletes = [
            p for m, p, _ in api_events if m == "DELETE" and "/reactions/rx_OnIt" in p
        ]
        replies = [b for m, p, b in api_events if m == "POST" and p.endswith("/reply")]
        assert reaction_Adds == ["OnIt", "DONE"]
        assert len(reaction_deletes) == 1
        assert len(replies) == 2
        assert replies[0]["msg_type"] == "post"
        assert (
            "正在派 Subagent 对 [redacted]!4045（最新 commit 2a0bab453f）"
            in replies[0]["content"]
        )
        assert DEFAULT_AI_COMMAND_FOOTER in replies[0]["content"]
        assert replies[1]["msg_type"] == "post"
        assert "逐文件审查通过" in replies[1]["content"]
        assert DEFAULT_AI_COMMAND_FOOTER in replies[1]["content"]

        # Duplicate replay of the same message_id is rejected by FeishuMessageDedupStore
        dup = await service.handle_message(
            FeishuUserMessage(
                message_id="om_owner_ai_1",
                chat_id="oc_group_1",
                sender_open_id="ou_owner",
                sender_type="user",
                msg_type="text",
                text="/ai review一下 没问题approve",
                create_time_ms=9999999992000,
                thread_id="omt_thread_1",
            )
        )
        assert dup.handled is False
        assert dup.status == "duplicate"

        # Verify WebSocket event hook works for unmentioned group /ai message from owner
        sdk_data = SimpleNamespace(
            header=SimpleNamespace(event_id="ev_ws_1", app_id="cli_app_1"),
            event=SimpleNamespace(
                sender=SimpleNamespace(
                    sender_type="user",
                    sender_id=SimpleNamespace(open_id="ou_owner"),
                ),
                message=SimpleNamespace(
                    message_id="om_ws_ai_2",
                    chat_id="oc_group_1",
                    chat_type="group",
                    thread_id="omt_thread_1",
                    parent_id=None,
                    root_id=None,
                    message_type="text",
                    content=json.dumps(
                        {"text": "/ai --provider codex --model gpt-5.4 --effort xhigh 跟进合入"}
                    ),
                    create_time="1700000099000",
                    mentions=None,
                ),
            ),
        )
        ws_event = message_event_from_sdk(
            sdk_data,
            expected_app_id="cli_app_1",
            expected_bot_open_id=None,
            require_group_mention=False,
        )
        handled_ws = await service.handle_ws_event(ws_event)
        assert handled_ws is True
        assert len(dispatched_turns) == 2
        assert dispatched_turns[1][3]["provider"] == "codex"
        assert dispatched_turns[1][3]["model"] == "gpt-5.4"
        assert dispatched_turns[1][3]["reasoning_effort"] == "xhigh"

        # Follow-up in same thread/provider with inline --model/--effort hot-switches tab env
        followup_res = await service.handle_message(
            FeishuUserMessage(
                message_id="om_ws_ai_3",
                chat_id="oc_group_1",
                sender_open_id="ou_owner",
                sender_type="user",
                msg_type="text",
                text="/ai --provider codex --model o3 --effort medium 再跑一次单测",
                create_time_ms=1700000100000,
                thread_id="omt_thread_1",
            )
        )
        assert followup_res.handled is True
        assert len(switched_envs) == 1
        assert switched_envs[0][1]["CODEX_MODEL"] == "o3"
        assert switched_envs[0][1]["CODEX_REASONING_EFFORT"] == "medium"

        # Empty owner_open_id fails closed
        empty_token_store = FeishuUserTokenStore(
            path=tmp_path / "secrets" / "empty_tokens.json",
            key_path=tmp_path / "secrets" / "empty_tokens.key",
        )
        unowned_service = FeishuAiCommandService(
            state_path=tmp_path / "unowned_ai_command.json",
            token_store=empty_token_store,
            dedup_store=dedup,
        )
        unowned_service.update_config({"enabled": True, "owner_open_id": ""})
        unowned_res = await unowned_service.handle_message(
            FeishuUserMessage(
                message_id="om_unowned_1",
                chat_id="oc_group_1",
                sender_open_id="ou_someone",
                sender_type="user",
                msg_type="text",
                text="/ai should fail closed",
                create_time_ms=1700000200000,
            )
        )
        assert unowned_res.handled is False
        assert unowned_res.reason == "no_owner_open_id"


def test_ai_command_rest_api_routes(tmp_path: Path) -> None:
    store = FeishuUserTokenStore(
        path=tmp_path / "secrets" / "tokens.json",
        key_path=tmp_path / "secrets" / "tokens.key",
    )
    store.save_oauth_token(
        open_id="ou_api_user",
        user_name="API User",
        app_id="cli_api_app",
        access_token="u-api-token",
        scope="im:message im:message.send_as_user",
    )
    service = FeishuAiCommandService(
        state_path=tmp_path / "ai_command.json",
        token_store=store,
        dedup_store=FeishuMessageDedupStore(tmp_path / "dedup.json"),
    )
    poller = FeishuAiPoller(service)

    app = FastAPI()
    from fastapi import APIRouter

    router = APIRouter(prefix="/api/feishu/bot")
    register_ai_command_routes(
        router,
        require_hub_user=lambda: SimpleNamespace(open_id="ou_api_user"),
        service_getter=lambda: service,
        poller_getter=lambda: poller,
    )
    app.include_router(router)
    client = TestClient(app)

    res_get = client.get("/api/feishu/bot/ai-command")
    assert res_get.status_code == 200
    body = res_get.json()
    assert body["token_status"]["can_send_as_user"] is True
    assert "claude" in body["capabilities"]["providers"]

    res_patch = client.patch(
        "/api/feishu/bot/ai-command",
        json={
            "enabled": True,
            "provider": "codex",
            "model": "gpt-5.4",
            "reasoning_effort": "xhigh",
            "watch_chats": ["oc_watch_1"],
        },
    )
    assert res_patch.status_code == 200
    updated_cfg = res_patch.json()["config"]
    assert updated_cfg["enabled"] is True
    assert updated_cfg["provider"] == "codex"
    assert updated_cfg["model"] == "gpt-5.4"
    assert updated_cfg["reasoning_effort"] == "xhigh"
    assert updated_cfg["watch_chats"] == ["oc_watch_1"]

    res_bad = client.patch(
        "/api/feishu/bot/ai-command",
        json={"provider": "terminal"},
    )
    assert res_bad.status_code == 400

    res_dry = client.post(
        "/api/feishu/bot/ai-command/dry-run",
        json={
            "text": "/ai --model o3 --effort high review !4045",
            "context_messages": [
                "https://[redacted]/[redacted]/merge_requests/4045 2a0bab453f"
            ],
        },
    )
    assert res_dry.status_code == 200
    dry_data = res_dry.json()["dry_run"]
    assert dry_data["handled"] is True
    assert dry_data["model"] == "o3"
    assert dry_data["reasoning_effort"] == "high"
    assert dry_data["refs"]["mr_refs"] == ["[redacted]!4045"]

    res_guide = client.get("/api/feishu/bot/ai-command/auth-guide")
    assert res_guide.status_code == 200
    assert "lark-cli auth login" in res_guide.json()["auth_login_command"]
