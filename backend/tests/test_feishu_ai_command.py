"""Tests for Feishu personal ``/ai`` command workflow, user token store/client, poller, and API."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from claude_hub.api.routes.feishu_bot.ai_command import register_ai_command_routes
from claude_hub.models import AgentType
from claude_hub.services.feishu_ai_command import (
    DEFAULT_AI_COMMAND_FOOTER,
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
    FeishuMessagePage,
    FeishuUserClient,
    FeishuUserMessage,
    FeishuUserScopeError,
)
from claude_hub.services.feishu_user_token_store import (
    FeishuUserTokenStore,
    aes_256_gcm_decrypt,
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
                        "appSecret": {
                            "source": "keychain",
                            "id": "appsecret:cli_lark_app",
                        },
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
                json={
                    "code": 0,
                    "data": {"message_id": "om_reply_1", "thread_id": "omt_1"},
                },
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
        validate_ai_command_agent_config("codex", "gpt-5.4", "super-duper")
    with pytest.raises(ValueError, match="does not support a separate reasoning_effort"):
        validate_ai_command_agent_config("claude", "[redacted]", "high")

    cursor_env = resolve_ai_command_agent_env("cursor", "gpt-5.3-codex", "xhigh")
    assert cursor_env["CURSOR_MODEL"] == "gpt-5.3-codex-xhigh"
    cursor_max_env = resolve_ai_command_agent_env("cursor", "gpt-5.1-codex-max", "xhigh")
    assert cursor_max_env["CURSOR_MODEL"] == "gpt-5.1-codex-max-extra-high"
    # Replaces existing suffix on Cursor models instead of appending a second suffix
    cursor_replace_env = resolve_ai_command_agent_env("cursor", "gpt-5.3-codex-high", "low")
    assert cursor_replace_env["CURSOR_MODEL"] == "gpt-5.3-codex-low"
    cursor_fast_env = resolve_ai_command_agent_env("cursor", "gpt-5.3-codex-high-fast", "xhigh")
    assert cursor_fast_env["CURSOR_MODEL"] == "gpt-5.3-codex-xhigh-fast"

    # MASO maps reasoning_effort onto real model variants consumed by MasoHttpSession
    maso_low_env = resolve_ai_command_agent_env("maso", "[redacted]_reasoning_high", "low")
    assert maso_low_env["MASO_MODEL"] == "[redacted]"
    maso_med_env = resolve_ai_command_agent_env("maso", "[redacted]_reasoning_high", "medium")
    assert maso_med_env["MASO_MODEL"] == "[redacted]_reasoning_medium"
    maso_seed_env = resolve_ai_command_agent_env("maso", "[redacted]-reasoning-high", "medium")
    assert maso_seed_env["MASO_MODEL"] == "[redacted]-reasoning"

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
                json={
                    "code": 0,
                    "data": {"message_id": "om_reply", "thread_id": "omt_thread_1"},
                },
            )
        if req.url.path == "/open-apis/im/v1/chats":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": [{"chat_id": "oc_group_1"}], "has_more": False},
                },
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
        await poller.wait_for_inflight()
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


@pytest.mark.asyncio
async def test_f1_hub_oauth_login_scope_and_refresh_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.auth.feishu import (
        DEFAULT_FEISHU_OAUTH_SCOPES,
        FEISHU_USER_REFRESH_URL,
        FEISHU_USER_TOKEN_URL,
        get_feishu_auth_url,
        get_user_access_token_bundle,
        refresh_user_access_token_bundle,
    )

    monkeypatch.setattr("claude_hub.auth.feishu.settings.feishu_app_id", "cli_oauth_app")
    monkeypatch.setattr("claude_hub.auth.feishu.settings.feishu_app_secret", "sec_oauth_app")
    monkeypatch.setattr(
        "claude_hub.auth.feishu.settings.feishu_redirect_uri",
        "https://hub.example.com/api/auth/callback",
    )

    login_url = get_feishu_auth_url(state="state_123")
    assert "im%3Amessage.send_as_user" in login_url or "im:message.send_as_user" in login_url
    assert "im:message.send_as_user" in DEFAULT_FEISHU_OAUTH_SCOPES
    assert FEISHU_USER_REFRESH_URL != FEISHU_USER_TOKEN_URL
    assert FEISHU_USER_REFRESH_URL.endswith("/open-apis/authen/v1/refresh_access_token")

    # Verify OIDC exchange + scope preservation and legacy v1 refresh endpoint
    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        if req.url.path == "/open-apis/auth/v3/app_access_token/internal":
            return httpx.Response(200, json={"code": 0, "app_access_token": "aat-1"})
        if req.url.path == "/open-apis/authen/v1/oidc/access_token":
            body = json.loads(req.content.decode("utf-8"))
            if body.get("grant_type") == "authorization_code":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "access_token": "u-oidc-token",
                            "refresh_token": "ur-oidc-token",
                            "expires_in": 7200,
                            "refresh_expires_in": 2592000,
                            "scope": "im:message im:message.send_as_user im:chat:read",
                        },
                    },
                )
        if req.url.path == "/open-apis/authen/v2/oauth/token":
            # Force fallback to legacy v1 refresh endpoint
            return httpx.Response(404, json={"code": 99991663, "msg": "not found"})
        if req.url.path == "/open-apis/authen/v1/oidc/refresh_access_token":
            return httpx.Response(404, json={"code": 0})
        if req.url.path == "/open-apis/authen/v1/refresh_access_token":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "access_token": "u-v1-refreshed",
                        "refresh_token": "ur-v1-refreshed",
                        "expires_in": 7200,
                        "refresh_expires_in": 2592000,
                        "scope": "im:message im:message.send_as_user",
                    },
                },
            )
        return httpx.Response(404)

    def mock_create_client(base_url: str, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("claude_hub.auth.feishu.create_async_client", mock_create_client)

    bundle = await get_user_access_token_bundle("code_abc")
    assert bundle is not None
    assert "im:message.send_as_user" in str(bundle.get("scope", ""))

    store = FeishuUserTokenStore(
        path=tmp_path / "secrets" / "tokens.json",
        key_path=tmp_path / "secrets" / "tokens.key",
    )
    store.save_oauth_token(
        open_id="ou_oauth_1",
        user_name="OAuth User",
        app_id="cli_oauth_app",
        access_token=str(bundle["access_token"]),
        refresh_token=str(bundle["refresh_token"]),
        expires_in=int(bundle["expires_in"]),
        refresh_expires_in=int(bundle["refresh_expires_in"]),
        scope=str(bundle["scope"]),
    )
    client = FeishuUserClient(store, open_id="ou_oauth_1", app_id="cli_oauth_app")
    checked = client.require_send_as_user_scope()
    assert checked.has_scope(REQUIRED_SEND_AS_USER_SCOPE) is True
    assert client.preflight_scopes().can_send_as_user is True

    refreshed = await refresh_user_access_token_bundle(
        "ur-legacy-1",
        app_id="cli_oauth_app",
        app_secret="sec_oauth_app",
    )
    assert refreshed is not None
    assert refreshed["access_token"] == "u-v1-refreshed"
    assert "/open-apis/authen/v1/refresh_access_token" in seen_paths


@pytest.mark.asyncio
async def test_f2_f5_lark_cli_profile_selection_and_single_use_refresh_rotation(
    tmp_path: Path,
) -> None:
    lark_config_dir = tmp_path / ".lark-cli"
    lark_data_dir = tmp_path / ".local" / "share" / "lark-cli"
    lark_config_dir.mkdir(parents=True)
    lark_data_dir.mkdir(parents=True)

    # Two encrypted profiles in config.json with currentApp selecting the second profile
    (lark_config_dir / "config.json").write_text(
        json.dumps(
            {
                "currentApp": "work-bot",
                "apps": [
                    {
                        "name": "personal-bot",
                        "appId": "cli_app_1",
                        "brand": "feishu",
                        "users": [{"userOpenId": "ou_user_1", "userName": "User One"}],
                    },
                    {
                        "name": "work-bot",
                        "appId": "cli_app_2",
                        "brand": "feishu",
                        "defaultUserOpenId": "ou_user_2",
                        "users": [{"userOpenId": "ou_user_2", "userName": "User Two"}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    master_key = secrets.token_bytes(32)
    (lark_data_dir / "master.key").write_bytes(master_key)
    for app_id, open_id, secret, at, rt in [
        ("cli_app_1", "ou_user_1", "sec_1", "at-app1", "rt-app1"),
        ("cli_app_2", "ou_user_2", "sec_2", "at-0", "rt-0"),
    ]:
        (lark_data_dir / f"appsecret_{app_id}.enc").write_bytes(
            aes_256_gcm_encrypt(master_key, secret.encode("utf-8"))
        )
        token_payload = {
            "userOpenId": open_id,
            "appId": app_id,
            "accessToken": at,
            "refreshToken": rt,
            "expiresAt": 1000.0,
            "refreshExpiresAt": 9999999999.0,
            "scope": "im:message im:message.send_as_user",
        }
        (lark_data_dir / f"{app_id}_{open_id}.enc").write_bytes(
            aes_256_gcm_encrypt(master_key, json.dumps(token_payload).encode("utf-8"))
        )

    store = FeishuUserTokenStore(
        path=tmp_path / "secrets" / "feishu_user_tokens.json",
        key_path=tmp_path / "secrets" / "feishu_user_tokens.key",
        lark_cli_dir=lark_config_dir,
        lark_cli_data_dir=lark_data_dir,
    )

    # F5: Default import honors currentApp ("work-bot" -> cli_app_2 / ou_user_2)
    imported = store.import_from_lark_cli()
    assert imported is not None
    assert imported.app_id == "cli_app_2"
    assert imported.open_id == "ou_user_2"
    assert imported.access_token == "at-0"
    assert imported.refresh_token == "rt-0"
    assert imported.generation == 1

    # F5: Dangling currentApp fails closed instead of falling back to apps[0]
    (lark_config_dir / "config.json").write_text(
        json.dumps(
            {
                "currentApp": "nonexistent-profile",
                "apps": [{"name": "personal-bot", "appId": "cli_app_1"}],
            }
        ),
        encoding="utf-8",
    )
    dangling_store = FeishuUserTokenStore(
        path=tmp_path / "secrets" / "dangling.json",
        key_path=tmp_path / "secrets" / "dangling.key",
        lark_cli_dir=lark_config_dir,
        lark_cli_data_dir=lark_data_dir,
    )
    assert dangling_store.import_from_lark_cli() is None

    # Restore valid config.json for F2 single-use refresh rotation test
    (lark_config_dir / "config.json").write_text(
        json.dumps(
            {
                "currentApp": "work-bot",
                "apps": [
                    {
                        "name": "work-bot",
                        "appId": "cli_app_2",
                        "appSecret": {
                            "source": "keychain",
                            "id": "appsecret:cli_app_2",
                        },
                        "brand": "feishu",
                        "defaultUserOpenId": "ou_user_2",
                        "users": [{"userOpenId": "ou_user_2", "userName": "User Two"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    used_refresh_tokens: list[str] = []
    rotation_step = 0

    def oauth_handler(req: httpx.Request) -> httpx.Response:
        nonlocal rotation_step
        body = json.loads(req.content.decode("utf-8"))
        rt = body.get("refresh_token", "")
        if rt in used_refresh_tokens:
            return httpx.Response(
                400,
                json={"code": 20026, "error_description": "refresh_token already used"},
            )
        used_refresh_tokens.append(rt)
        rotation_step += 1
        return httpx.Response(
            200,
            json={
                "code": 0,
                "access_token": f"at-{rotation_step}",
                "refresh_token": f"rt-{rotation_step}",
                "expires_in": 1000,
                "refresh_token_expires_in": 86400,
                "scope": "im:message im:message.send_as_user",
            },
        )

    clock = [1100.0]
    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(oauth_handler),
    ) as http_client:
        # Re-import so app_secret from keychain is attached
        store = FeishuUserTokenStore(
            path=tmp_path / "secrets" / "feishu_user_tokens_rot.json",
            key_path=tmp_path / "secrets" / "feishu_user_tokens_rot.key",
            lark_cli_dir=lark_config_dir,
            lark_cli_data_dir=lark_data_dir,
            now=lambda: clock[0],
        )
        store.import_from_lark_cli()

        async def fake_refresher(
            refresh_token: str,
            *,
            app_id: str | None = None,
            app_secret: str | None = None,
        ) -> dict[str, Any]:
            resp = await http_client.post(
                "/open-apis/authen/v2/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "refresh_token": refresh_token,
                },
            )
            resp.raise_for_status()
            return dict(resp.json())

        client1 = FeishuUserClient(
            store,
            open_id="ou_user_2",
            app_id="cli_app_2",
            client=http_client,
            token_refresher=fake_refresher,
        )
        # First refresh at t=1100 rotates at-0/rt-0 -> at-1/rt-1 (expires_at=2100, gen=2)
        tok1, _ = await client1.get_user_token()
        assert tok1 == "at-1"
        rec1 = store.get_token(open_id="ou_user_2", app_id="cli_app_2")
        assert rec1 is not None
        assert rec1.refresh_token == "rt-1"
        assert rec1.generation == 2

        # Advance past first rotated expiry (t=2200 > 2100) and create a new store instance
        # (simulating process restart); resolve_token + get_user_token must use rt-1, NEVER rt-0!
        clock[0] = 2200.0
        restarted_store = FeishuUserTokenStore(
            path=tmp_path / "secrets" / "feishu_user_tokens_rot.json",
            key_path=tmp_path / "secrets" / "feishu_user_tokens_rot.key",
            lark_cli_dir=lark_config_dir,
            lark_cli_data_dir=lark_data_dir,
            now=lambda: clock[0],
        )
        client2 = FeishuUserClient(
            restarted_store,
            open_id="ou_user_2",
            app_id="cli_app_2",
            client=http_client,
            token_refresher=fake_refresher,
        )
        tok2, _ = await client2.get_user_token()
        assert tok2 == "at-2"
        rec2 = restarted_store.get_token(open_id="ou_user_2", app_id="cli_app_2")
        assert rec2 is not None
        assert rec2.refresh_token == "rt-2"
        assert rec2.generation == 3
        assert used_refresh_tokens == ["rt-0", "rt-1"]


@pytest.mark.asyncio
async def test_f3_poller_pagination_under_burst_and_nonblocking_dispatch(
    tmp_path: Path,
) -> None:
    store = FeishuUserTokenStore(
        path=tmp_path / "secrets" / "tokens.json",
        key_path=tmp_path / "secrets" / "tokens.key",
    )
    store.save_oauth_token(
        open_id="ou_owner",
        user_name="Owner",
        app_id="cli_app_1",
        access_token="u-owner-token",
        scope="im:message im:message.send_as_user im:message.reactions:write_only im:chat:read",
    )

    # Burst scenario in oc_burst:
    # Watermark is at 1000ms.
    # Owner sent `/ai burst command` at 1005ms (on page 2), followed by 25 peer messages
    # at 1010..1034ms (on page 1).
    page_1_items = [
        {
            "message_id": f"om_noise_{i}",
            "chat_id": "oc_burst",
            "msg_type": "text",
            "create_time": str(1034 - i),
            "sender": {"id": "ou_peer", "sender_type": "user"},
            "body": {"content": json.dumps({"text": f"noise {i}"})},
        }
        for i in range(25)
    ]
    page_2_items = [
        {
            "message_id": "om_burst_ai",
            "chat_id": "oc_burst",
            "thread_id": "omt_burst",
            "msg_type": "text",
            "create_time": "1005",
            "sender": {"id": "ou_owner", "sender_type": "user"},
            "body": {"content": json.dumps({"text": "/ai handle burst command"})},
        },
        {
            "message_id": "om_before_watermark",
            "chat_id": "oc_burst",
            "msg_type": "text",
            "create_time": "995",
            "sender": {"id": "ou_peer", "sender_type": "user"},
            "body": {"content": json.dumps({"text": "old message"})},
        },
    ]

    slow_turn_started = asyncio.Event()
    release_slow_turn = asyncio.Event()
    dispatched_messages: list[str] = []

    def feishu_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/reactions") and req.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"reaction_id": "rx_1"}})
        if "/reactions/" in req.url.path and req.method == "DELETE":
            return httpx.Response(200, json={"code": 0, "data": {}})
        if req.url.path.endswith("/reply") and req.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"message_id": "om_reply", "thread_id": "omt_1"},
                },
            )
        if req.url.path == "/open-apis/im/v1/messages":
            container_type = req.url.params.get("container_id_type")
            container_id = req.url.params.get("container_id")
            page_token = req.url.params.get("page_token")
            if container_type == "thread":
                return httpx.Response(
                    200, json={"code": 0, "data": {"items": [], "has_more": False}}
                )
            if container_id == "oc_burst":
                if not page_token:
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "data": {
                                "items": page_1_items,
                                "has_more": True,
                                "page_token": "cursor_page_2",
                            },
                        },
                    )
                if page_token == "cursor_page_2":
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "data": {"items": page_2_items, "has_more": False},
                        },
                    )
            if container_id == "oc_slow":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "message_id": "om_slow_ai",
                                    "chat_id": "oc_slow",
                                    "msg_type": "text",
                                    "create_time": "2005",
                                    "sender": {"id": "ou_owner", "sender_type": "user"},
                                    "body": {"content": json.dumps({"text": "/ai slow task"})},
                                }
                            ],
                            "has_more": False,
                        },
                    },
                )
        return httpx.Response(404)

    async def fake_dispatch(
        tab_id: str,
        prompt: str,
        turn_id: str,
        visible_text: str,
        metadata: Any,
    ) -> str:
        msg_id = metadata.get("message_id") or metadata.get("feishu_message_id") or ""
        dispatched_messages.append(msg_id)
        if msg_id == "om_slow_ai":
            slow_turn_started.set()
            await release_slow_turn.wait()
        return f"done:{msg_id}"

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(feishu_handler),
    ) as http_client:
        service = FeishuAiCommandService(
            state_path=tmp_path / "ai_command.json",
            token_store=store,
            dedup_store=FeishuMessageDedupStore(tmp_path / "dedup.json"),
            user_client_factory=lambda cfg: FeishuUserClient(
                store,
                open_id=cfg.owner_open_id,
                app_id=cfg.app_id,
                client=http_client,
            ),
            tab_creator=lambda **kw: SimpleNamespace(
                id="tab-shared", env=dict(kw.get("env") or {}), archived=False
            ),
            tab_getter=lambda tid: SimpleNamespace(id=tid, env={}, archived=False),
            turn_dispatcher=fake_dispatch,
        )
        service.update_config(
            {
                "enabled": True,
                "owner_open_id": "ou_owner",
                "app_id": "cli_app_1",
                "watch_chats": ["oc_slow", "oc_burst"],
                "max_context_messages": 20,
            }
        )
        service.set_watermark("oc_slow", 2000)
        service.set_watermark("oc_burst", 1000)

        poller = FeishuAiPoller(service)
        # poll_once must NOT block on oc_slow's long-running turn and must still page
        # through oc_burst's page 1 + page 2 to discover om_burst_ai!
        processed = await asyncio.wait_for(poller.poll_once(), timeout=2.0)
        assert set(processed) == {"om_slow_ai", "om_burst_ai"}
        await asyncio.wait_for(slow_turn_started.wait(), timeout=1.0)
        # Wait briefly for om_burst_ai to finish while om_slow_ai is still blocked
        for _ in range(40):
            if "om_burst_ai" in dispatched_messages:
                break
            await asyncio.sleep(0.02)
        assert "om_burst_ai" in dispatched_messages
        assert service.get_watermarks()["oc_burst"] == 1034000

        release_slow_turn.set()
        await poller.wait_for_inflight()
        assert poller.status_dict()["commands_succeeded"] == 2
        assert service.list_pending_commands() == []


def test_f4_maso_transport_boundary_consumes_resolved_reasoning_effort(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone

    from claude_hub.models import (
        ManagedSession,
        ManagedSessionStatus,
        SessionKind,
        WorkspaceSessionRole,
    )
    from claude_hub.services.agent_stream.maso_http import MasoHttpSession

    seen_models: list[str | None] = []
    for effort in ("low", "medium", "high"):
        env = resolve_ai_command_agent_env("maso", "[redacted]_reasoning_high", effort)
        env["MASO_BASE_URL"] = "http://127.0.0.1:9796"
        ms = ManagedSession(
            id=f"test-maso-{effort}",
            workspace_id="ws-test",
            tab_id=f"tab-maso-{effort}",
            role=WorkspaceSessionRole.WORKER,
            agent_type=AgentType.MASO,
            session_kind=SessionKind.CHAT,
            status=ManagedSessionStatus.IDLE,
            title=f"test-maso-{effort}",
            workspace_path=str(tmp_path),
            tmux_session="inert",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            env=env,
        )
        transport = MasoHttpSession(ms, lambda _ev: None)
        seen_models.append(transport.current_model())

    assert seen_models == [
        "[redacted]",
        "[redacted]_reasoning_medium",
        "[redacted]_reasoning_high",
    ]
    assert len(set(seen_models)) == 3


@pytest.mark.asyncio
async def test_r2_oauth_offline_access_no_rt_login_and_legacy_v1_20026_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.parse import parse_qs, urlparse

    from starlette.requests import Request

    from claude_hub.api import auth as api_auth_mod
    from claude_hub.auth import feishu as feishu_auth_mod

    fake_settings = SimpleNamespace(
        auth_enabled=True,
        feishu_app_id="cli_r2_app",
        feishu_app_secret="sec_r2_app",
        feishu_redirect_uri="https://hub.example.test/api/auth/callback",
        frontend_url="https://hub.example.test",
        public_url="",
        session_cookie_name="hub_session",
        session_expire_days=1,
        allowed_open_ids_list=[],
        allowed_emails_list=[],
    )
    monkeypatch.setattr(feishu_auth_mod, "settings", fake_settings)
    monkeypatch.setattr(api_auth_mod, "settings", fake_settings)

    login_url = feishu_auth_mod.get_feishu_auth_url(state="state-r2")
    scopes = set(parse_qs(urlparse(login_url).query)["scope"][0].split())
    assert "offline_access" in scopes
    assert "im:message.send_as_user" in scopes

    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        if req.url.path.endswith("/app_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "app_access_token": "t-app"})
        if req.url.path.endswith("/v2/oauth/token"):
            body = json.loads(req.content.decode("utf-8"))
            if body.get("grant_type") == "authorization_code":
                # Valid OAuth v2 response without refresh_token
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "access_token": "u-no-rt-token",
                        "expires_in": 7200,
                        "scope": "im:message im:message.send_as_user",
                    },
                )
            # Legacy v1 refresh token rejected by v2 endpoint with code 20026
            return httpx.Response(
                400,
                json={"code": 20026, "error": "invalid_refresh_token"},
            )
        if req.url.path.endswith("/authen/v1/user_info"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "open_id": "ou_no_rt_user",
                        "name": "No RT User",
                        "email": "nort@example.com",
                    },
                },
            )
        if req.url.path.endswith("/refresh_access_token"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "access_token": "u-legacy-refreshed",
                        "refresh_token": "ur-legacy-rt2",
                        "expires_in": 7200,
                    },
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        feishu_auth_mod,
        "create_async_client",
        lambda *_a, **_kw: httpx.AsyncClient(
            base_url="https://open.feishu.cn",
            transport=httpx.MockTransport(handler),
        ),
    )
    store = FeishuUserTokenStore(
        path=tmp_path / "tokens.json",
        key_path=tmp_path / "tokens.key",
        lark_cli_dir=tmp_path / "empty_lark",
        lark_cli_data_dir=tmp_path / "empty_lark_data",
    )
    monkeypatch.setattr(
        "claude_hub.services.feishu_user_token_store.get_feishu_user_token_store",
        lambda: store,
    )

    request = Request(
        {
            "type": "http",
            "scheme": "https",
            "method": "GET",
            "path": "/api/auth/callback",
            "query_string": b"",
            "headers": [(b"cookie", b"claude_hub_oauth_state=state-r2")],
        }
    )
    resp = await api_auth_mod.callback(request, code="code-r2", state="state-r2")
    assert resp.status_code == 307
    saved = store.get_token(open_id="ou_no_rt_user", app_id="cli_r2_app")
    assert saved is not None
    assert saved.access_token == "u-no-rt-token"
    assert saved.refresh_token == ""

    # Legacy v1 refresh token falls through on v2 20026 and refreshes via v1/OIDC endpoint
    refreshed = await feishu_auth_mod.refresh_user_access_token_bundle(
        "ur-legacy-rt1",
        app_id="cli_r2_app",
        app_secret="sec_r2_app",
    )
    assert refreshed is not None
    assert refreshed["access_token"] == "u-legacy-refreshed"
    assert any(p.endswith("/refresh_access_token") for p in seen_paths)


@pytest.mark.asyncio
async def test_r2_async_refresh_no_deadlock_cli_ms_units_and_shared_cas(
    tmp_path: Path,
) -> None:
    now_val = 1_800_000_000.0
    cfg_dir = tmp_path / "lark_cfg"
    data_dir = tmp_path / "lark_data"
    cfg_dir.mkdir()
    data_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {
                "currentApp": "cli_r2",
                "apps": [{"appId": "cli_r2", "users": [{"userOpenId": "ou_r2"}]}],
            }
        ),
        encoding="utf-8",
    )
    master_key = b"\x33" * 32
    (data_dir / "master.key").write_bytes(master_key)
    enc_path = data_dir / "cli_r2_ou_r2.enc"
    enc_path.write_bytes(
        aes_256_gcm_encrypt(
            master_key,
            json.dumps(
                {
                    "appId": "cli_r2",
                    "userOpenId": "ou_r2",
                    "accessToken": "u-at0",
                    "refreshToken": "rt-0",
                    "expiresAt": int((now_val - 10) * 1000),
                    "refreshExpiresAt": int((now_val + 86400) * 1000),
                    "scope": "im:message im:message.send_as_user",
                }
            ).encode("utf-8"),
        )
    )

    store = FeishuUserTokenStore(
        path=tmp_path / "tokens.json",
        key_path=tmp_path / "tokens.key",
        lark_cli_dir=cfg_dir,
        lark_cli_data_dir=data_dir,
        now=lambda: now_val,
    )
    imported = store.import_from_lark_cli()
    assert imported is not None

    # 1. Concurrent async refresh callers with a yielding network refresher must not deadlock
    refresh_calls = 0

    async def yielding_refresher(rt: str, **_kw: Any) -> dict[str, Any]:
        nonlocal refresh_calls
        refresh_calls += 1
        await asyncio.sleep(0.05)
        return {
            "access_token": "u-at1",
            "refresh_token": "rt-1",
            "expires_in": 7200,
            "refresh_expires_in": 604800,
        }

    c1 = FeishuUserClient(
        store, open_id="ou_r2", app_id="cli_r2", token_refresher=yielding_refresher
    )
    c2 = FeishuUserClient(
        store, open_id="ou_r2", app_id="cli_r2", token_refresher=yielding_refresher
    )
    res1, res2 = await asyncio.wait_for(
        asyncio.gather(c1.get_user_token(), c2.get_user_token()),
        timeout=0.5,
    )
    assert res1[0] == "u-at1"
    assert res2[0] == "u-at1"
    assert refresh_calls == 1

    # 2. Verify CLI writeback uses Unix milliseconds (not seconds)
    decrypted = json.loads(aes_256_gcm_decrypt(master_key, enc_path.read_bytes()).decode("utf-8"))
    assert decrypted["expiresAt"] == int((now_val + 7200) * 1000)
    assert decrypted["refreshExpiresAt"] == int((now_val + 604800) * 1000)
    assert now_val * 1000 < decrypted["expiresAt"] - 300_000

    # 3. Simulate concurrent external lark-cli re-login during in-flight Hub refresh (CAS)
    now_val += 8000.0

    async def cas_refresher(rt: str, **_kw: Any) -> dict[str, Any]:
        # External CLI writer updates keychain with a fresh login + extra scope while Hub awaits network
        fresh_cli = {
            "appId": "cli_r2",
            "userOpenId": "ou_r2",
            "accessToken": "u-cli-fresh-login",
            "refreshToken": "rt-cli-fresh-login",
            "expiresAt": int((now_val + 14400) * 1000),
            "refreshExpiresAt": int((now_val + 900000) * 1000),
            "scope": "im:message im:message.send_as_user im:message.reactions:write_only",
        }
        enc_path.write_bytes(aes_256_gcm_encrypt(master_key, json.dumps(fresh_cli).encode("utf-8")))
        return {
            "access_token": "u-stale-hub-refresh",
            "refresh_token": "rt-stale-hub-refresh",
            "expires_in": 7200,
            "refresh_expires_in": 604800,
        }

    c3 = FeishuUserClient(store, open_id="ou_r2", app_id="cli_r2", token_refresher=cas_refresher)
    tok, rec = await c3.get_user_token()
    assert tok == "u-cli-fresh-login"
    assert rec.refresh_token == "rt-cli-fresh-login"
    assert "im:message.reactions:write_only" in rec.scopes_set()
    after_cli = json.loads(aes_256_gcm_decrypt(master_key, enc_path.read_bytes()).decode("utf-8"))
    assert after_cli["refreshToken"] == "rt-cli-fresh-login"
    assert "im:message.reactions:write_only" in after_cli["scope"]


@pytest.mark.asyncio
async def test_r2_poller_quiesce_preserves_unstarted_commands_and_resumes_capped_pagination(
    tmp_path: Path,
) -> None:
    def make_msg(mid: str, ts: int, *, owner: bool = True) -> FeishuUserMessage:
        return FeishuUserMessage(
            message_id=mid,
            chat_id="oc_chat",
            sender_open_id="ou_owner" if owner else "ou_peer",
            sender_type="user",
            msg_type="text",
            text="/ai run check" if owner else "chat noise",
            create_time_ms=ts,
        )

    class PagedClient:
        def __init__(self, msgs: list[FeishuUserMessage]) -> None:
            self.messages = sorted(msgs, key=lambda m: m.create_time_ms, reverse=True)
            self.page_offsets: list[int] = []

        def resolve_record(self) -> Any:
            return SimpleNamespace(open_id="ou_owner")

        async def list_messages_page(self, **kwargs: Any) -> FeishuMessagePage:
            offset = int(kwargs.get("page_token") or 0)
            size = int(kwargs["page_size"])
            self.page_offsets.append(offset)
            items = self.messages[offset : offset + size]
            more = offset + size < len(self.messages)
            return FeishuMessagePage(tuple(items), more, str(offset + size) if more else "")

    # Part 1: Graceful quiesce preserves waiting command and replays it on restart
    q_client = PagedClient([make_msg("om_active", 2000), make_msg("om_waiting", 2001)])
    q_store = FeishuUserTokenStore(path=tmp_path / "q_tok.json", key_path=tmp_path / "q_tok.key")
    q_svc = FeishuAiCommandService(
        state_path=tmp_path / "q_state.json",
        token_store=q_store,
        dedup_store=FeishuMessageDedupStore(tmp_path / "q_dedup.json"),
        user_client_factory=lambda _cfg: q_client,  # type: ignore[arg-type,return-value]
    )
    q_svc.update_config(
        {
            "enabled": True,
            "owner_open_id": "ou_owner",
            "app_id": "cli_app",
            "watch_chats": ["oc_chat"],
        }
    )
    q_svc.set_watermark("oc_chat", 1000)

    entered: list[str] = []
    blocker = asyncio.Event()

    async def blocking_handle(msg: FeishuUserMessage, **_kwargs: Any) -> Any:
        entered.append(msg.message_id)
        if msg.message_id == "om_active" and not blocker.is_set():
            await blocker.wait()
        return SimpleNamespace(handled=True, status="completed")

    q_svc.handle_message = blocking_handle  # type: ignore[method-assign,assignment]
    poller1 = FeishuAiPoller(q_svc, max_concurrent_dispatches=1)
    await poller1.poll_once()
    assert entered == ["om_active"]
    await poller1.quiesce()
    pending_after_quiesce = [row["message"]["message_id"] for row in q_svc.list_pending_commands()]
    assert "om_waiting" in pending_after_quiesce

    blocker.set()
    poller2 = FeishuAiPoller(q_svc, max_concurrent_dispatches=1)
    replay = await poller2.poll_once()
    await poller2.wait_for_inflight()
    assert "om_waiting" in replay
    assert "om_waiting" in entered
    assert q_svc.list_pending_commands() == []
    await poller2.stop()

    # Part 2: Backlog exceeding 25-page per-tick cap resumes on subsequent poll
    burst_msgs = [make_msg("om_oldest_cmd", 2000, owner=True)] + [
        make_msg(f"om_peer_{i}", 2001 + i, owner=False) for i in range(500)
    ]
    b_client = PagedClient(burst_msgs)
    b_store = FeishuUserTokenStore(path=tmp_path / "b_tok.json", key_path=tmp_path / "b_tok.key")
    b_svc = FeishuAiCommandService(
        state_path=tmp_path / "b_state.json",
        token_store=b_store,
        dedup_store=FeishuMessageDedupStore(tmp_path / "b_dedup.json"),
        user_client_factory=lambda _cfg: b_client,  # type: ignore[arg-type,return-value]
    )
    b_svc.update_config(
        {
            "enabled": True,
            "owner_open_id": "ou_owner",
            "app_id": "cli_app",
            "watch_chats": ["oc_chat"],
            "max_context_messages": 20,
        }
    )
    b_svc.set_watermark("oc_chat", 1000)
    b_handled: list[str] = []

    async def fast_handle(msg: FeishuUserMessage, **_kwargs: Any) -> Any:
        b_handled.append(msg.message_id)
        return SimpleNamespace(handled=True, status="completed")

    b_svc.handle_message = fast_handle  # type: ignore[method-assign,assignment]
    b_poller = FeishuAiPoller(b_svc)
    first_pass = await b_poller.poll_once()
    assert first_pass == []
    assert b_svc.get_watermarks()["oc_chat"] == 1000
    assert b_svc.get_scan_cursor("oc_chat") is not None

    second_pass = await b_poller.poll_once()
    await b_poller.wait_for_inflight()
    assert second_pass == ["om_oldest_cmd"]
    assert b_handled == ["om_oldest_cmd"]
    assert b_svc.get_watermarks()["oc_chat"] == 2500
    assert b_svc.get_scan_cursor("oc_chat") is None
    assert b_client.page_offsets[25:] == [500]
    await b_poller.stop()


@pytest.mark.asyncio
async def test_cycle3_token_cas_async_lock_scope_authority_and_turn_recovery(
    tmp_path: Path,
) -> None:
    """Regression tests for R3-01, R3-02, R3-03, and R3-04."""
    import fcntl
    import subprocess
    import sys
    import time
    from collections.abc import Mapping
    from datetime import datetime, timezone

    from claude_hub.api.routes.agent_stream.dispatch import ExternalDispatchRetired
    from claude_hub.models import AgentStreamEvent, AgentStreamEventType
    from claude_hub.services.agent_stream.store import AgentStreamStore
    from claude_hub.services.feishu_bot import FeishuReplyReceipt
    from claude_hub.services.feishu_user_client import FeishuMessagePage
    from claude_hub.services.feishu_user_token_store import aes_256_gcm_decrypt

    # 1. R3-01a: Newer Hub OAuth save during in-flight refresh is not overwritten
    t0 = 1_800_000_000.0
    oauth_store = FeishuUserTokenStore(
        path=tmp_path / "oauth_cas.json",
        key_path=tmp_path / "oauth_cas.key",
        now=lambda: t0,
    )
    oauth_store.save_oauth_token(
        open_id="ou_cas",
        app_id="cli_cas",
        access_token="old-at",
        refresh_token="old-rt",
        expires_in=1,
        scope="im:message im:message.send_as_user",
    )

    async def stale_oauth_refresher(_rt: str, **_kw: Any) -> dict[str, Any]:
        oauth_store.save_oauth_token(
            open_id="ou_cas",
            app_id="cli_cas",
            access_token="fresh-oauth-at",
            refresh_token="fresh-oauth-rt",
            expires_in=14_000,
            scope="im:message im:message.send_as_user im:message.reactions:write_only",
        )
        return {
            "access_token": "stale-refresh-at",
            "refresh_token": "stale-refresh-rt",
            "expires_in": 7200,
            "scope": "im:message im:message.send_as_user",
        }

    oauth_client = FeishuUserClient(
        oauth_store,
        open_id="ou_cas",
        app_id="cli_cas",
        token_refresher=stale_oauth_refresher,
    )
    _, after_oauth = await oauth_client.get_user_token()
    assert after_oauth.access_token == "fresh-oauth-at"
    assert after_oauth.has_scope("im:message.reactions:write_only")

    # 2. R3-01b: Already-imported fresh CLI login during in-flight refresh is preserved in Hub & CLI
    cli_cfg = tmp_path / "cli_cas_cfg"
    cli_data = tmp_path / "cli_cas_data"
    cli_cfg.mkdir(parents=True)
    cli_data.mkdir(parents=True)
    master_key = secrets.token_bytes(32)
    (cli_data / "master.key").write_bytes(master_key)
    (cli_cfg / "config.json").write_text(
        json.dumps(
            {
                "currentApp": "cli_cas",
                "apps": [{"appId": "cli_cas", "users": [{"userOpenId": "ou_cas"}]}],
            }
        ),
        encoding="utf-8",
    )
    enc_path = cli_data / "cli_cas_ou_cas.enc"
    enc_path.write_bytes(
        aes_256_gcm_encrypt(
            master_key,
            json.dumps(
                {
                    "appId": "cli_cas",
                    "userOpenId": "ou_cas",
                    "accessToken": "init-cli-at",
                    "refreshToken": "init-cli-rt",
                    "expiresAt": int((t0 + 1) * 1000),
                    "refreshExpiresAt": int((t0 + 600_000) * 1000),
                    "scope": "im:message im:message.send_as_user",
                }
            ).encode("utf-8"),
        )
    )
    imp_store = FeishuUserTokenStore(
        path=tmp_path / "imp_cas.json",
        key_path=tmp_path / "imp_cas.key",
        lark_cli_dir=cli_cfg,
        lark_cli_data_dir=cli_data,
        now=lambda: t0,
    )
    imp_store.import_from_lark_cli()

    async def stale_cli_refresher(_rt: str, **_kw: Any) -> dict[str, Any]:
        enc_path.write_bytes(
            aes_256_gcm_encrypt(
                master_key,
                json.dumps(
                    {
                        "appId": "cli_cas",
                        "userOpenId": "ou_cas",
                        "accessToken": "fresh-cli-at",
                        "refreshToken": "fresh-cli-rt",
                        "expiresAt": int((t0 + 14_000) * 1000),
                        "refreshExpiresAt": int((t0 + 900_000) * 1000),
                        "scope": "im:message im:message.send_as_user im:message.reactions:write_only",
                    }
                ).encode("utf-8"),
            )
        )
        imported_mid = imp_store.resolve_token(open_id="ou_cas", app_id="cli_cas")
        assert imported_mid is not None and imported_mid.access_token == "fresh-cli-at"
        return {
            "access_token": "stale-refresh-at",
            "refresh_token": "stale-refresh-rt",
            "expires_in": 7200,
            "scope": "im:message im:message.send_as_user",
        }

    imp_client = FeishuUserClient(
        imp_store,
        open_id="ou_cas",
        app_id="cli_cas",
        token_refresher=stale_cli_refresher,
    )
    _, after_imp = await imp_client.get_user_token()
    disk_cli = json.loads(aes_256_gcm_decrypt(master_key, enc_path.read_bytes()).decode("utf-8"))
    assert after_imp.access_token == "fresh-cli-at"
    assert disk_cli["refreshToken"] == "fresh-cli-rt"

    # 3. R3-02: External CLI lock holder does not block asyncio heartbeat
    lock_file = cli_cfg / "locks" / "refresh_cli_cas_ou_cas.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    holder_code = (
        "import fcntl, sys, time\n"
        "with open(sys.argv[1], 'w') as f:\n"
        "    fcntl.flock(f, fcntl.LOCK_EX)\n"
        "    print('LOCK_HELD', flush=True)\n"
        "    time.sleep(0.25)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", holder_code, str(lock_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "LOCK_HELD"
    t_start = time.monotonic()

    async def heartbeat() -> float:
        await asyncio.sleep(0.02)
        return time.monotonic() - t_start

    hb_task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    _, current_imp = await imp_client.get_user_token(force_refresh=True)
    hb_delay = await hb_task
    proc.communicate(timeout=3)
    assert hb_delay < 0.15

    # 4. R3-03: Authoritative reduced scope on refresh does not union prior CLI scopes
    reduced = imp_store.save_refreshed_token(
        current_imp,
        {
            "access_token": "reduced-at",
            "refresh_token": "reduced-rt",
            "expires_in": 7200,
            "refresh_expires_in": 604_800,
            "scope": "im:message:readonly",
        },
        now=t0,
    )
    assert not reduced.has_scope("im:message.send_as_user")
    reduced_cli = json.loads(aes_256_gcm_decrypt(master_key, enc_path.read_bytes()).decode("utf-8"))
    assert "im:message.send_as_user" not in reduced_cli["scope"].split()

    # 5. R3-04: Completed turn interrupted during final reply resumes only final reply (no re-dispatch);
    # uncertain turn retirement preserves dedup claim without re-executing
    cmd_msg = FeishuUserMessage(
        "om_done_turn", "oc_chat", "ou_owner", "user", "text", "/ai run once", 2000
    )
    final_gate = asyncio.Event()
    dispatched_turns: list[str] = []
    final_attempts = 0

    class RecoveryClient:
        def resolve_record(self) -> Any:
            return SimpleNamespace(open_id="ou_owner")

        def require_send_as_user_scope(self) -> None:
            return None

        async def list_messages_page(self, **_kw: Any) -> FeishuMessagePage:
            return FeishuMessagePage((cmd_msg,), False, "")

        async def list_messages(self, **_kw: Any) -> list[FeishuUserMessage]:
            return [cmd_msg]

        async def add_reaction_as_user(self, *_a: Any) -> str:
            return "rx_1"

        async def delete_reaction_as_user(self, *_a: Any) -> None:
            return None

        async def reply_as_user(self, *_a: Any, **kw: Any) -> FeishuReplyReceipt:
            nonlocal final_attempts
            if str(kw.get("uuid") or "").startswith("ai:final:"):
                final_attempts += 1
                if final_attempts == 1:
                    final_gate.set()
                    await asyncio.Event().wait()
            return FeishuReplyReceipt(message_id="om_reply", thread_id=None)

    async def once_dispatcher(
        _tab_id: str, _prompt: str, turn_id: str, _vis: str, _meta: Mapping[str, Any]
    ) -> str:
        dispatched_turns.append(turn_id)
        return "Completed once."

    tabs: dict[str, Any] = {}

    def make_tab(**kw: Any) -> Any:
        t = SimpleNamespace(id="tab_rec", env=kw["env"], archived=False)
        tabs["tab_rec"] = t
        return t

    rec_svc = FeishuAiCommandService(
        state_path=tmp_path / "rec_state.json",
        token_store=oauth_store,
        dedup_store=FeishuMessageDedupStore(tmp_path / "rec_dedup.json"),
        user_client_factory=lambda _cfg: RecoveryClient(),  # type: ignore[arg-type,return-value]
        tab_creator=make_tab,
        tab_getter=lambda ident: tabs.get(ident),
        turn_dispatcher=once_dispatcher,
    )
    rec_svc.update_config(
        {
            "enabled": True,
            "owner_open_id": "ou_owner",
            "app_id": "cli_cas",
            "watch_chats": ["oc_chat"],
        }
    )
    rec_svc.set_watermark("oc_chat", 1000)
    p1 = FeishuAiPoller(rec_svc)
    await p1.poll_once()
    await asyncio.wait_for(final_gate.wait(), timeout=1.0)
    await p1.quiesce()
    assert dispatched_turns == ["feishu-ai-om_done_turn"]
    assert rec_svc.get_pending_command("om_done_turn")["dispatch_state"] == "turn_completed"  # type: ignore[index]

    p2 = FeishuAiPoller(rec_svc)
    await p2.poll_once()
    await p2.wait_for_inflight()
    assert dispatched_turns == ["feishu-ai-om_done_turn"]
    assert final_attempts == 2
    assert rec_svc.list_pending_commands() == []
    assert rec_svc.get_recent_runs()[0]["status"] == "completed"
    await p2.stop()

    # Uncertain in-flight interruption preserves claim and does not replay
    unc_msg = FeishuUserMessage(
        "om_uncertain", "oc_chat", "ou_owner", "user", "text", "/ai uncertain", 3000
    )
    rec_svc.enqueue_pending_command(unc_msg)
    assert rec_svc._dedup.claim("ai_cmd:ou_owner:om_uncertain")
    rec_svc.mark_pending_command_state(
        "om_uncertain",
        "interrupted",
        dispatch_state="uncertain",
        tab_id="tab_rec",
        turn_id="feishu-ai-om_uncertain",
    )
    assert rec_svc.reconcile_interrupted_command("om_uncertain") is False
    assert rec_svc.get_recent_runs()[0]["status"] == "interrupted_uncertain"
    assert rec_svc._dedup.claim("ai_cmd:ou_owner:om_uncertain") is False

    # Retry-safe retired turn (`ExternalDispatchRetired`) releases claim and replays
    ret_msg = FeishuUserMessage(
        "om_retired", "oc_chat", "ou_owner", "user", "text", "/ai retired", 4000
    )
    rec_svc.enqueue_pending_command(ret_msg)

    async def retired_dispatcher(*_a: Any) -> str:
        raise ExternalDispatchRetired("feishu-ai-om_retired")

    rec_svc._turn_dispatcher = retired_dispatcher
    with pytest.raises(asyncio.CancelledError):
        await rec_svc.handle_message(ret_msg)
    assert rec_svc.get_pending_command("om_retired")["dispatch_state"] == "retired_retry_safe"  # type: ignore[index]
    assert rec_svc.reconcile_interrupted_command("om_retired") is True

    # Durable AgentStreamStore TURN_COMPLETED evidence promotes dispatched item to turn_completed
    stream_msg = FeishuUserMessage(
        "om_stream_done", "oc_chat", "ou_owner", "user", "text", "/ai stream done", 5000
    )
    rec_svc.enqueue_pending_command(stream_msg)
    assert rec_svc._dedup.claim("ai_cmd:ou_owner:om_stream_done")
    stream_store = AgentStreamStore("terminal-tabs", "terminal-tab-tab_rec")
    await stream_store.append(
        AgentStreamEvent(
            stream_sequence=0,
            session_id="terminal-tab-tab_rec",
            tab_id="tab_rec",
            agent_type=AgentType.CODEX,
            type=AgentStreamEventType.TURN_COMPLETED,
            turn_id="feishu-ai-om_stream_done",
            created_at=datetime.now(timezone.utc),
            payload={
                "status": "completed",
                "final_text": "Recovered from durable stream store.",
            },
        )
    )
    rec_svc.mark_pending_command_state(
        "om_stream_done",
        "interrupted",
        dispatch_state="dispatched",
        tab_id="tab_rec",
        turn_id="feishu-ai-om_stream_done",
    )
    assert rec_svc.reconcile_interrupted_command("om_stream_done") is True
    assert rec_svc.get_pending_command("om_stream_done")["dispatch_state"] == "turn_completed"  # type: ignore[index]


@pytest.mark.asyncio
async def test_cycle4_async_entrypoints_and_lock_cancellation_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression tests for R4-01 (all asyncio entrypoints non-blocking) and R4-02 (fd cleanup)."""
    import os
    import subprocess
    import sys
    import time

    from starlette.requests import Request

    from claude_hub.api import auth as api_auth
    from claude_hub.auth import feishu as feishu_auth

    cli_cfg = tmp_path / "c4_cli_cfg"
    cli_data = tmp_path / "c4_cli_data"
    cli_cfg.mkdir(parents=True)
    cli_data.mkdir(parents=True)
    master_key = secrets.token_bytes(32)
    (cli_data / "master.key").write_bytes(master_key)
    (cli_cfg / "config.json").write_text(
        json.dumps(
            {
                "currentApp": "cli_c4",
                "apps": [{"appId": "cli_c4", "users": [{"userOpenId": "ou_c4"}]}],
            }
        ),
        encoding="utf-8",
    )
    (cli_data / "cli_c4_ou_c4.enc").write_bytes(
        aes_256_gcm_encrypt(
            master_key,
            json.dumps(
                {
                    "appId": "cli_c4",
                    "userOpenId": "ou_c4",
                    "accessToken": "c4-cli-at",
                    "refreshToken": "c4-cli-rt",
                    "expiresAt": int((time.time() + 7200) * 1000),
                    "refreshExpiresAt": int((time.time() + 600_000) * 1000),
                    "scope": "im:message im:message.send_as_user",
                }
            ).encode("utf-8"),
        )
    )
    cli_lock_path = cli_cfg / "locks" / "refresh_cli_c4_ou_c4.lock"
    cli_lock_path.parent.mkdir(parents=True, exist_ok=True)

    def spawn_lock_holder(lock_target: Path, duration: float = 0.25) -> subprocess.Popen[str]:
        code = (
            "import fcntl, sys, time\n"
            "with open(sys.argv[1], 'w') as f:\n"
            "    fcntl.flock(f, fcntl.LOCK_EX)\n"
            "    print('LOCK_HELD', flush=True)\n"
            "    time.sleep(float(sys.argv[2]))\n"
        )
        p = subprocess.Popen(
            [sys.executable, "-u", "-c", code, str(lock_target), str(duration)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert p.stdout is not None and p.stdout.readline().strip() == "LOCK_HELD"
        return p

    async def measure_heartbeat(coro: Any, lock_target: Path = cli_lock_path) -> float:
        holder = spawn_lock_holder(lock_target, 0.25)
        t0 = time.monotonic()

        async def hb() -> float:
            await asyncio.sleep(0.02)
            return time.monotonic() - t0

        hb_task = asyncio.create_task(hb())
        await asyncio.sleep(0)
        try:
            await coro
            return await hb_task
        finally:
            holder.communicate(timeout=3)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reply"):
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_c4_reply"}})
        if request.url.path == "/open-apis/im/v1/messages":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "message_id": "om_c4_send",
                        "items": [],
                        "has_more": False,
                    },
                },
            )
        if request.url.path.endswith("/app_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "app_access_token": "app-tok"})
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "access_token": "oauth-at",
                    "refresh_token": "oauth-rt",
                    "expires_in": 7200,
                    "scope": "im:message im:message.send_as_user",
                },
            )
        if request.url.path.endswith("/user_info"):
            return httpx.Response(200, json={"code": 0, "data": {"open_id": "ou_c4", "name": "C4"}})
        raise AssertionError(f"Unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn", transport=httpx.MockTransport(handler)
    ) as http_client:
        # 1. reply_as_user and send_as_user do not block the event loop
        store_1 = FeishuUserTokenStore(
            path=tmp_path / "s1.json",
            key_path=tmp_path / "s1.key",
            lark_cli_dir=cli_cfg,
            lark_cli_data_dir=cli_data,
        )
        u_client = FeishuUserClient(store_1, open_id="ou_c4", app_id="cli_c4", client=http_client)
        d_reply = await measure_heartbeat(u_client.reply_as_user("om_1", "text", {"text": "hi"}))
        assert d_reply < 0.12

        store_2 = FeishuUserTokenStore(
            path=tmp_path / "s2.json",
            key_path=tmp_path / "s2.key",
            lark_cli_dir=cli_cfg,
            lark_cli_data_dir=cli_data,
        )
        u_client_2 = FeishuUserClient(store_2, open_id="ou_c4", app_id="cli_c4", client=http_client)
        d_send = await measure_heartbeat(u_client_2.send_as_user("oc_1", "text", {"text": "hi"}))
        assert d_send < 0.12

        # 2. poll_once() and handle_message(dry_run=True) / apreflight_status() do not block
        store_3 = FeishuUserTokenStore(
            path=tmp_path / "s3.json",
            key_path=tmp_path / "s3.key",
            lark_cli_dir=cli_cfg,
            lark_cli_data_dir=cli_data,
        )
        u_client_3 = FeishuUserClient(store_3, open_id="ou_c4", app_id="cli_c4", client=http_client)
        svc_3 = FeishuAiCommandService(
            state_path=tmp_path / "state3.json",
            token_store=store_3,
            user_client_factory=lambda _cfg: u_client_3,
        )
        svc_3.update_config(
            {
                "enabled": True,
                "app_id": "cli_c4",
                "owner_open_id": "ou_c4",
                "watch_chats": ["oc_1"],
            }
        )
        poller_3 = FeishuAiPoller(svc_3)
        d_poll = await measure_heartbeat(poller_3.poll_once())
        assert d_poll < 0.12
        await poller_3.stop()

        store_4 = FeishuUserTokenStore(
            path=tmp_path / "s4.json",
            key_path=tmp_path / "s4.key",
            lark_cli_dir=cli_cfg,
            lark_cli_data_dir=cli_data,
        )
        svc_4 = FeishuAiCommandService(
            state_path=tmp_path / "state4.json",
            token_store=store_4,
        )
        svc_4.update_config({"enabled": True})
        d_preflight = await measure_heartbeat(svc_4.apreflight_status())
        assert d_preflight < 0.12

        # 3. OAuth callback does not block the event loop
        store_5 = FeishuUserTokenStore(
            path=tmp_path / "s5.json",
            key_path=tmp_path / "s5.key",
            lark_cli_dir=cli_cfg,
            lark_cli_data_dir=cli_data,
        )
        fake_cfg = SimpleNamespace(
            auth_enabled=True,
            feishu_app_id="cli_c4",
            feishu_app_secret="sec",
            feishu_redirect_uri="https://hub.example.test/api/auth/callback",
            frontend_url="https://hub.example.test",
            public_url="",
            session_cookie_name="sid",
            session_expire_days=1,
            allowed_open_ids_list=[],
            allowed_emails_list=[],
        )
        monkeypatch.setattr(feishu_auth, "settings", fake_cfg)
        monkeypatch.setattr(api_auth, "settings", fake_cfg)
        monkeypatch.setattr(
            feishu_auth,
            "create_async_client",
            lambda *_a, **_kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        monkeypatch.setattr(
            api_auth, "create_session", lambda *_a: SimpleNamespace(session_id="sid-1")
        )
        monkeypatch.setattr(
            "claude_hub.services.feishu_user_token_store.get_feishu_user_token_store",
            lambda: store_5,
        )
        req = Request(
            {
                "type": "http",
                "scheme": "https",
                "method": "GET",
                "path": "/api/auth/callback",
                "query_string": b"",
                "headers": [(b"cookie", b"claude_hub_oauth_state=st-1")],
            }
        )
        d_oauth = await measure_heartbeat(api_auth.callback(req, code="code-1", state="st-1"))
        assert d_oauth < 0.12
        assert store_5.get_token(open_id="ou_c4", app_id="cli_c4") is not None

    # 4. R4-02: Cancelled async_file_lock waiters on first lock and second lock leak 0 fds
    def open_fds_for(targets: set[str]) -> list[int]:
        if not Path("/proc/self/fd").is_dir():
            return []
        found: list[int] = []
        for entry in os.listdir("/proc/self/fd"):
            try:
                if os.readlink(f"/proc/self/fd/{entry}") in targets:
                    found.append(int(entry))
            except OSError:
                pass
        return found

    cancel_store = FeishuUserTokenStore(
        path=tmp_path / "cancel.json",
        key_path=tmp_path / "cancel.key",
        lark_cli_dir=cli_cfg,
        lark_cli_data_dir=cli_data,
    )
    hub_lock_path = tmp_path / "feishu_user_tokens.refresh.lock"
    target_paths = {str(cli_lock_path.resolve()), str(hub_lock_path.resolve())}

    # Case A: Cancel while waiting on the first lock (cli_lock_path)
    before_a = open_fds_for(target_paths)
    holder_a = spawn_lock_holder(cli_lock_path, 0.35)
    try:
        for _ in range(5):
            with pytest.raises(asyncio.TimeoutError):
                async with asyncio.timeout(0.015):
                    async with cancel_store.async_file_lock(app_id="cli_c4", open_id="ou_c4"):
                        pass
        assert open_fds_for(target_paths) == before_a
        assert cancel_store._held_lock_fds == {}
    finally:
        holder_a.communicate(timeout=3)

    # Case B: Cancel while waiting on the second lock (hub_lock_path) after acquiring first lock
    before_b = open_fds_for(target_paths)
    holder_b = spawn_lock_holder(hub_lock_path, 0.35)
    try:
        for _ in range(5):
            with pytest.raises(asyncio.TimeoutError):
                async with asyncio.timeout(0.015):
                    async with cancel_store.async_file_lock(app_id="cli_c4", open_id="ou_c4"):
                        pass
        assert open_fds_for(target_paths) == before_b
        assert cancel_store._held_lock_fds == {}
    finally:
        holder_b.communicate(timeout=3)

    # 5. R5-02: Unrelated CLI profile lock does not delay or fail target account reads/sends
    (cli_cfg / "config.json").write_text(
        json.dumps(
            {
                "currentApp": "cli_c4",
                "apps": [
                    {"appId": "cli_c4", "users": [{"userOpenId": "ou_c4"}]},
                    {"appId": "cli_other", "users": [{"userOpenId": "ou_other"}]},
                ],
            }
        ),
        encoding="utf-8",
    )
    unrelated_lock = cli_cfg / "locks" / "refresh_cli_other_ou_other.lock"
    holder_other = spawn_lock_holder(unrelated_lock, 0.35)
    try:
        t_start = time.monotonic()
        rec_no_import = await store_1.aresolve_token(
            open_id="ou_c4", app_id="cli_c4", auto_import_lark_cli=False
        )
        assert rec_no_import is not None and rec_no_import.access_token == "c4-cli-at"
        assert (time.monotonic() - t_start) < 0.08

        # Normal auto-import client path for cli_c4 also skips cli_other's lock
        store_auto = FeishuUserTokenStore(
            path=tmp_path / "s_auto.json",
            key_path=tmp_path / "s_auto.key",
            lark_cli_dir=cli_cfg,
            lark_cli_data_dir=cli_data,
        )
        t_auto = time.monotonic()
        rec_auto = await store_auto.aresolve_token(open_id="ou_c4", app_id="cli_c4")
        assert rec_auto is not None and rec_auto.access_token == "c4-cli-at"
        assert (time.monotonic() - t_auto) < 0.08
    finally:
        holder_other.communicate(timeout=3)

    # 6. R5-01: Poller start() / _run_loop() survives transient config/token lock timeout and recovers
    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn", transport=httpx.MockTransport(handler)
    ) as poll_http:
        r5_store = FeishuUserTokenStore(
            path=tmp_path / "r5_tok.json",
            key_path=tmp_path / "r5_tok.key",
            lark_cli_dir=cli_cfg,
            lark_cli_data_dir=cli_data,
        )
        r5_client = FeishuUserClient(r5_store, open_id="ou_c4", app_id="cli_c4", client=poll_http)
        r5_svc = FeishuAiCommandService(
            state_path=tmp_path / "r5_state.json",
            token_store=r5_store,
            user_client_factory=lambda _cfg: r5_client,
        )
        r5_svc.update_config(
            {
                "enabled": True,
                "app_id": "cli_c4",
                "owner_open_id": "",
                "watch_chats": ["oc_1"],
                "poll_interval_seconds": 2.0,
            }
        )
        orig_aget = r5_svc.aget_config
        calls = 0

        async def flaky_aget_config(*a: Any, **kw: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError(
                    "Timed out waiting for Feishu token lock refresh_cli_c4_ou_c4.lock"
                )
            return await orig_aget(*a, **kw)

        monkeypatch.setattr(r5_svc, "aget_config", flaky_aget_config)
        r5_poller = FeishuAiPoller(r5_svc)
        await r5_poller.start()
        await asyncio.sleep(0.05)
        st_failed = r5_poller.status_dict()
        assert r5_poller.running is True
        assert "Timed out waiting for Feishu token lock" in str(st_failed["last_error"])
        assert st_failed["poll_count"] == 0
        # Wake the poller for the next tick without manual restart/save; it recovers automatically
        r5_poller._wake_event.set()
        await asyncio.sleep(0.05)
        st_recovered = r5_poller.status_dict()
        assert r5_poller.running is True
        assert st_recovered["poll_count"] >= 1
        assert st_recovered["last_error"] is None
        await r5_poller.stop()

    # 7. R6-01: Default import after CLI currentApp switch locks and imports currentApp B non-blockingly
    from fastapi import APIRouter, FastAPI

    from claude_hub.api.routes.feishu_bot.ai_command import register_ai_command_routes

    (cli_cfg / "config.json").write_text(
        json.dumps(
            {
                "currentApp": "secondary",
                "apps": [
                    {
                        "appId": "cli_c4",
                        "name": "primary",
                        "users": [{"userOpenId": "ou_c4"}],
                    },
                    {
                        "appId": "cli_other",
                        "name": "secondary",
                        "users": [{"userOpenId": "ou_other"}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (cli_data / "cli_other_ou_other.enc").write_bytes(
        aes_256_gcm_encrypt(
            master_key,
            json.dumps(
                {
                    "appId": "cli_other",
                    "userOpenId": "ou_other",
                    "accessToken": "c6-active-b-at",
                    "refreshToken": "c6-active-b-rt",
                    "expiresAt": int((time.time() + 14_000) * 1000),
                    "refreshExpiresAt": int((time.time() + 600_000) * 1000),
                    "scope": "im:message im:message.send_as_user",
                }
            ).encode("utf-8"),
        )
    )
    r6_store = FeishuUserTokenStore(
        path=tmp_path / "r6_tok.json",
        key_path=tmp_path / "r6_tok.key",
        lark_cli_dir=cli_cfg,
        lark_cli_data_dir=cli_data,
    )
    r6_store.save_oauth_token(
        open_id="ou_c4",
        app_id="cli_c4",
        access_token="cached-a-at",
        refresh_token="cached-a-rt",
        expires_in=7200,
        scope="im:message:readonly",
    )
    r6_svc = FeishuAiCommandService(
        state_path=tmp_path / "r6_state.json",
        token_store=r6_store,
    )
    r6_svc.update_config({"enabled": False, "app_id": "cli_c4", "owner_open_id": "ou_c4"})
    r6_poller = FeishuAiPoller(r6_svc)

    async def allow_user() -> Any:
        return SimpleNamespace(open_id="ou_c4")

    r6_app = FastAPI()
    r6_router = APIRouter(prefix="/api/feishu/bot")
    register_ai_command_routes(
        r6_router,
        require_hub_user=allow_user,
        service_getter=lambda: r6_svc,
        poller_getter=lambda: r6_poller,
    )
    r6_app.include_router(r6_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=r6_app), base_url="http://test.local"
    ) as asgi_client:
        d_imp = await measure_heartbeat(
            asgi_client.post("/api/feishu/bot/ai-command/import-token"),
            lock_target=unrelated_lock,
        )
        assert d_imp < 0.12
        assert r6_svc.get_config(auto_import_lark_cli=False).app_id == "cli_other"
        assert r6_svc.get_config(auto_import_lark_cli=False).owner_open_id == "ou_other"

        # 8. R6-02: Concurrent GET and PATCH return a single coherent account snapshot
        r6_store.save_oauth_token(
            open_id="ou_c4",
            app_id="cli_c4",
            access_token="expired-a-at",
            expires_in=1,
            scope="im:message:readonly",
        )
        r6_svc.update_config({"enabled": False, "app_id": "cli_c4", "owner_open_id": ""})
        holder_a_race = spawn_lock_holder(cli_lock_path, 0.25)
        try:
            get_task = asyncio.create_task(asgi_client.get("/api/feishu/bot/ai-command"))
            await asyncio.sleep(0.025)
            assert not get_task.done()
            patch_resp = await asgi_client.patch(
                "/api/feishu/bot/ai-command",
                json={"owner_open_id": "ou_other", "app_id": "cli_other"},
            )
            assert patch_resp.status_code == 200
            get_resp = await asyncio.wait_for(get_task, timeout=2.0)
            assert get_resp.status_code == 200
            body = get_resp.json()
            assert body["config"]["app_id"] == body["token_status"]["app_id"]
            assert body["config"]["owner_open_id"] == body["token_status"]["open_id"]
        finally:
            holder_a_race.communicate(timeout=3)

        # 9. R7-01: Unknown or conflicting explicit profile selector returns imported=False without rebinding
        r6_svc.update_config({"enabled": False, "app_id": "cli_other", "owner_open_id": "ou_other"})
        unk_resp = await asgi_client.post(
            "/api/feishu/bot/ai-command/import-token",
            json={"profile": "does-not-exist"},
        )
        assert unk_resp.status_code == 200
        unk_body = unk_resp.json()
        assert unk_body["imported"] is False
        assert unk_body["config"]["app_id"] == "cli_other"
        assert unk_body["config"]["owner_open_id"] == "ou_other"

        conflict_resp = await asgi_client.post(
            "/api/feishu/bot/ai-command/import-token",
            json={"profile": "secondary", "app_id": "cli_c4"},
        )
        assert conflict_resp.status_code == 200
        assert conflict_resp.json()["imported"] is False

        # 10. R7-02: Failed import / empty token store preflight with auto_import_lark_cli=False
        # does not synchronously auto-import or block on CLI locks
        r6_store.delete_token("ou_c4", app_id="cli_c4")
        r6_store.delete_token("ou_other", app_id="cli_other")
        r6_svc.update_config({"enabled": False, "app_id": "cli_c4", "owner_open_id": "ou_c4"})
        d_miss = await measure_heartbeat(
            asgi_client.post(
                "/api/feishu/bot/ai-command/import-token",
                json={"app_id": "cli_missing", "open_id": "ou_missing"},
            ),
            lock_target=cli_lock_path,
        )
        assert d_miss < 0.12
        assert r6_store.get_token(open_id="ou_c4", app_id="cli_c4") is None

        # 11. R8-01: open_id-only and partial selectors obey identical CLI app-selection
        # contract across cold/warm Hub cache and sync/async import paths
        r6_svc.update_config({"enabled": False, "app_id": "cli_other", "owner_open_id": "ou_other"})
        # Cold Hub cache: open_id='ou_c4' does not belong to currentApp='secondary' ('cli_other')
        assert r6_store.import_from_lark_cli(open_id="ou_c4") is None
        cold_open_resp = await asgi_client.post(
            "/api/feishu/bot/ai-command/import-token",
            json={"open_id": "ou_c4"},
        )
        assert cold_open_resp.status_code == 200
        assert cold_open_resp.json()["imported"] is False
        assert cold_open_resp.json()["config"]["app_id"] == "cli_other"

        # Warm Hub cache with cached (cli_c4, ou_c4): open_id='ou_c4' still fails closed against currentApp='secondary'
        r6_store.save_oauth_token(
            open_id="ou_c4",
            app_id="cli_c4",
            access_token="warm-cached-a-at",
            refresh_token="warm-cached-a-rt",
            expires_in=7200,
            scope="im:message im:message.send_as_user",
        )
        assert r6_store.import_from_lark_cli(open_id="ou_c4") is None
        warm_open_resp = await asgi_client.post(
            "/api/feishu/bot/ai-command/import-token",
            json={"open_id": "ou_c4"},
        )
        assert warm_open_resp.status_code == 200
        assert warm_open_resp.json()["imported"] is False
        assert warm_open_resp.json()["config"]["app_id"] == "cli_other"

        # open_id='ou_other' matches currentApp='secondary' in both sync and async paths
        sync_match = r6_store.import_from_lark_cli(open_id="ou_other")
        assert sync_match is not None and sync_match.app_id == "cli_other"
        match_open_resp = await asgi_client.post(
            "/api/feishu/bot/ai-command/import-token",
            json={"open_id": "ou_other"},
        )
        assert match_open_resp.status_code == 200
        assert match_open_resp.json()["imported"] is True
        assert match_open_resp.json()["config"]["app_id"] == "cli_other"

        # 12. R9-01 + R10-01 + R10-02: Matching config.json + plaintext credentials.json imports in
        # both sync/async, preserves snake_case app_secret/user_name over config keychain-ref objects,
        # selects the second user in multi-user plaintext layouts across sync/async, and rejects
        # unrelated plaintext credentials when config.json selects another app
        pt_cfg = tmp_path / "pt_cfg"
        pt_data = tmp_path / "pt_data"
        pt_cfg.mkdir()
        pt_data.mkdir()
        (pt_cfg / "config.json").write_text(
            json.dumps(
                {
                    "currentApp": "primary",
                    "apps": [
                        {
                            "name": "primary",
                            "appId": "cli_pt",
                            "appSecret": {
                                "source": "keychain",
                                "id": "appsecret:cli_pt",
                            },
                            "scope": "im:message",
                            "users": [
                                {"userOpenId": "ou_pt_first", "userName": "First User"},
                                {"userOpenId": "ou_pt", "userName": "Config Name"},
                            ],
                        },
                        {
                            "name": "secondary",
                            "appId": "cli_pt2",
                            "users": [{"userOpenId": "ou_pt2"}],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        (pt_cfg / "credentials.json").write_text(
            json.dumps(
                {
                    "appId": "cli_pt",
                    "userOpenId": "ou_pt",
                    "user_name": "Credential Name",
                    "app_secret": "pt-exported-secret",
                    "accessToken": "pt-access-token",
                    "refreshToken": "pt-refresh-token",
                    "expiresAt": int((time.time() + 7200) * 1000),
                    "refreshExpiresAt": int((time.time() + 600_000) * 1000),
                    "scope": "im:message im:message.send_as_user",
                }
            ),
            encoding="utf-8",
        )
        pt_store = FeishuUserTokenStore(
            path=tmp_path / "pt_tokens.json",
            key_path=tmp_path / "pt_tokens.key",
            lark_cli_dir=pt_cfg,
            lark_cli_data_dir=pt_data,
        )
        # Multi-user app-only import selects ou_pt (second user) in both sync and async modes
        pt_sync = pt_store.import_from_lark_cli(app_id="cli_pt")
        assert pt_sync is not None and pt_sync.open_id == "ou_pt"
        assert pt_sync.app_secret == "pt-exported-secret"
        assert pt_sync.user_name == "Credential Name"
        pt_async = await pt_store.aimport_from_lark_cli(app_id="cli_pt")
        assert pt_async is not None and pt_async.open_id == "ou_pt"
        assert pt_async.app_secret == "pt-exported-secret"
        assert pt_async.user_name == "Credential Name"
        # Explicit tokenless first user is rejected; selecting secondary is rejected without fallback
        assert (
            await pt_store.aimport_from_lark_cli(app_id="cli_pt", open_id="ou_pt_first")
        ) is None
        assert pt_store.import_from_lark_cli(profile="secondary") is None
        assert (await pt_store.aimport_from_lark_cli(profile="secondary")) is None
        # Deleting config.json preserves no-config plaintext credentials.json import
        (pt_cfg / "config.json").unlink()
        assert pt_store.import_from_lark_cli(app_id="cli_pt", open_id="ou_pt") is not None
        assert (await pt_store.aimport_from_lark_cli(app_id="cli_pt", open_id="ou_pt")) is not None

        # 13. R11-01: Explicit empty scope in plaintext or encrypted CLI tokens never inherits
        # prior CLI scope-cache grants or older warm Hub token grants
        pt_scope_cache = pt_cfg / "cache" / "auth_login_scopes"
        pt_scope_cache.mkdir(parents=True)
        (pt_scope_cache / "prior.json").write_text(
            json.dumps({"granted_scope": "im:message im:message.send_as_user"}),
            encoding="utf-8",
        )
        (pt_cfg / "credentials.json").write_text(
            json.dumps(
                {
                    "appId": "cli_pt",
                    "userOpenId": "ou_pt",
                    "accessToken": "pt-empty-scope-at",
                    "refreshToken": "pt-empty-scope-rt",
                    "expiresAt": int((time.time() + 14_000) * 1000),
                    "refreshExpiresAt": int((time.time() + 600_000) * 1000),
                    "scope": "",
                }
            ),
            encoding="utf-8",
        )
        # Warm store had send_as_user on older token; importing new token with scope="" must be empty
        rec_empty_pt = await pt_store.aimport_from_lark_cli(app_id="cli_pt", open_id="ou_pt")
        assert rec_empty_pt is not None and rec_empty_pt.access_token == "pt-empty-scope-at"
        assert rec_empty_pt.scope == ""
        assert (
            FeishuUserClient(pt_store, open_id="ou_pt", app_id="cli_pt")
            .preflight_scopes(rec_empty_pt)
            .can_send_as_user
            is False
        )
        # 14. R12-01a: Same-active re-import when file omits scope and old CLI cache exists
        # preserves the active known-empty scope verbatim
        (pt_cfg / "credentials.json").write_text(
            json.dumps(
                {
                    "appId": "cli_pt",
                    "userOpenId": "ou_pt",
                    "accessToken": "pt-empty-scope-at",
                    "refreshToken": "pt-empty-scope-rt",
                    "expiresAt": int((time.time() + 14_000) * 1000),
                    "refreshExpiresAt": int((time.time() + 600_000) * 1000),
                }
            ),
            encoding="utf-8",
        )
        rec_absent_same = await pt_store.aimport_from_lark_cli(app_id="cli_pt", open_id="ou_pt")
        assert rec_absent_same is not None and rec_absent_same.scope == ""

        # 14. R12-01b: Rotating a plaintext-imported token to scope="" and re-importing the
        # unchanged pre-rotation plaintext source preserves the rotated credentials AND empty scope
        (pt_cfg / "credentials.json").write_text(
            json.dumps(
                {
                    "appId": "cli_pt",
                    "userOpenId": "ou_pt",
                    "accessToken": "pt-pre-rot-at",
                    "refreshToken": "pt-pre-rot-rt",
                    "expiresAt": int((time.time() + 18_000) * 1000),
                    "refreshExpiresAt": int((time.time() + 600_000) * 1000),
                    "scope": "im:message im:message.send_as_user",
                }
            ),
            encoding="utf-8",
        )
        pre_rot = await pt_store.aimport_from_lark_cli(app_id="cli_pt", open_id="ou_pt")
        assert pre_rot is not None and pre_rot.has_scope("im:message.send_as_user")

        async def empty_scope_refresher(_rt: str, **_kw: Any) -> dict[str, Any]:
            return {
                "access_token": "pt-post-rot-at",
                "refresh_token": "pt-post-rot-rt",
                "expires_in": 24_000,
                "refresh_expires_in": 600_000,
                "scope": "",
            }

        rot_client = FeishuUserClient(
            pt_store,
            open_id="ou_pt",
            app_id="cli_pt",
            token_refresher=empty_scope_refresher,
        )
        post_rot = await rot_client.refresh_token(pre_rot)
        assert post_rot.access_token == "pt-post-rot-at" and post_rot.scope == ""
        reimp_rot = await pt_store.aimport_from_lark_cli(app_id="cli_pt", open_id="ou_pt")
        assert reimp_rot is not None
        assert reimp_rot.access_token == "pt-post-rot-at"
        assert reimp_rot.scope == ""
        assert rot_client.preflight_scopes(reimp_rot).can_send_as_user is False
        # Encrypted layout with explicit scope="" and prior CLI scope-cache also preserves empty scope
        (pt_cfg / "credentials.json").unlink()
        (pt_data / "master.key").write_bytes(master_key)
        (pt_data / "cli_pt_ou_pt.enc").write_bytes(
            aes_256_gcm_encrypt(
                master_key,
                json.dumps(
                    {
                        "appId": "cli_pt",
                        "userOpenId": "ou_pt",
                        "accessToken": "enc-empty-scope-at",
                        "refreshToken": "enc-empty-scope-rt",
                        "expiresAt": int((time.time() + 30_000) * 1000),
                        "refreshExpiresAt": int((time.time() + 600_000) * 1000),
                        "scope": "",
                    }
                ).encode("utf-8"),
            )
        )
        rec_empty_enc = await pt_store.aimport_from_lark_cli(app_id="cli_pt", open_id="ou_pt")
        assert rec_empty_enc is not None and rec_empty_enc.access_token == "enc-empty-scope-at"
        assert rec_empty_enc.scope == ""
        assert (
            FeishuUserClient(pt_store, open_id="ou_pt", app_id="cli_pt")
            .preflight_scopes(rec_empty_enc)
            .can_send_as_user
            is False
        )


@pytest.mark.asyncio
async def test_bot_fallback_reply_send_and_auto_invite_without_send_as_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User token with im:message (no im:message.send_as_user) sends/replies via single authorized bot fallback."""
    import time

    from claude_hub.services.feishu_bot import (
        FeishuBotConfig,
        FeishuBotError,
        FeishuBotRemoteError,
    )
    from claude_hub.services.feishu_bot_pool import FeishuBotPoolStore

    cli_cfg = tmp_path / "fb_cli_cfg"
    cli_data = tmp_path / "fb_cli_data"
    cli_cfg.mkdir()
    cli_data.mkdir()
    master_key = secrets.token_bytes(32)
    (cli_data / "master.key").write_bytes(master_key)
    (cli_data / "appsecret_cli_fb.enc").write_bytes(
        aes_256_gcm_encrypt(master_key, b"fb-bot-secret")
    )
    (cli_cfg / "config.json").write_text(
        json.dumps(
            {
                "currentApp": "cli_fb",
                "apps": [
                    {
                        "appId": "cli_fb",
                        "users": [{"userOpenId": "ou_fb", "userName": "[redacted]"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (cli_data / "cli_fb_ou_fb.enc").write_bytes(
        aes_256_gcm_encrypt(
            master_key,
            json.dumps(
                {
                    "appId": "cli_fb",
                    "userOpenId": "ou_fb",
                    "userName": "[redacted]",
                    "accessToken": "fb-user-at",
                    "refreshToken": "fb-user-rt",
                    "expiresAt": int((time.time() + 7200) * 1000),
                    "refreshExpiresAt": int((time.time() + 600_000) * 1000),
                    "scope": (
                        "im:message im:chat:read im:chat.members:write_only "
                        "im:message.reactions:write_only im:message.group_msg:get_as_user"
                    ),
                }
            ).encode("utf-8"),
        )
    )
    store = FeishuUserTokenStore(
        path=tmp_path / "fb_tokens.json",
        key_path=tmp_path / "fb_tokens.key",
        lark_cli_dir=cli_cfg,
        lark_cli_data_dir=cli_data,
    )
    # Also store an unrelated user token with a secret in the same store; fallback must NEVER select it.
    store.save_oauth_token(
        open_id="ou_unrelated",
        app_id="cli_unrelated",
        access_token="unrelated-user-at",
        refresh_token="unrelated-user-rt",
        app_secret="unrelated-bot-secret",
        expires_in=7200,
        scope="im:message",
    )

    rec = await store.aresolve_token(open_id="ou_fb", app_id="cli_fb", auto_import_lark_cli=True)
    assert rec is not None
    assert rec.app_secret == "fb-bot-secret"
    assert not rec.has_scope("im:message.send_as_user")
    assert store.resolve_fallback_bot_configs(preferred_app_id="cli_fb", record=rec) == [
        ("cli_fb", "fb-bot-secret")
    ]
    # Mismatched preferred_app_id vs record.app_id fails closed
    assert store.resolve_fallback_bot_configs(preferred_app_id="cli_unrelated", record=rec) == []

    calls: list[tuple[str, str, str, dict[str, Any]]] = []
    bot_in_chat = False
    simulate_timeout = False
    simulate_missing_id = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal bot_in_chat
        path = request.url.path
        auth_hdr = request.headers.get("Authorization", "")
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        calls.append((request.method, path, auth_hdr, body))

        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            assert body.get("app_id") == "cli_fb"
            assert body.get("app_secret") == "fb-bot-secret"
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-bot-token", "expire": 7200},
            )
        if path.endswith("/reactions"):
            assert auth_hdr == "Bearer fb-user-at"
            return httpx.Response(
                200,
                json={"code": 0, "data": {"reaction_id": "rx_fb_1"}},
            )
        if path == "/open-apis/im/v1/chats/oc_group_fb/members":
            assert auth_hdr == "Bearer fb-user-at"
            assert request.url.params.get("member_id_type") == "app_id"
            assert body == {"id_list": ["cli_fb"]}
            bot_in_chat = True
            return httpx.Response(200, json={"code": 0, "data": {}})
        if path.endswith("/reply"):
            assert auth_hdr == "Bearer t-bot-token"
            if simulate_timeout:
                raise httpx.ReadTimeout("simulated post-accept read timeout")
            if simulate_missing_id:
                return httpx.Response(200, json={"code": 0, "data": {}})
            if not bot_in_chat:
                return httpx.Response(
                    400,
                    json={
                        "code": 230002,
                        "msg": "Bot/User can NOT be out of the chat.",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"message_id": "om_bot_reply_1", "thread_id": "omt_fb_1"},
                },
            )
        if path == "/open-apis/im/v1/messages" and request.method == "POST":
            assert auth_hdr == "Bearer t-bot-token"
            if simulate_timeout:
                raise httpx.ReadTimeout("simulated post-accept read timeout")
            if simulate_missing_id:
                return httpx.Response(200, json={"code": 0, "data": {}})
            if not bot_in_chat:
                return httpx.Response(
                    400,
                    json={
                        "code": 230002,
                        "msg": "Bot/User can NOT be out of the chat.",
                    },
                )
            return httpx.Response(
                200,
                json={"code": 0, "data": {"message_id": "om_bot_send_1"}},
            )
        if path == "/open-apis/im/v1/messages" and request.method == "GET":
            assert auth_hdr == "Bearer fb-user-at"
            return httpx.Response(200, json={"code": 0, "data": {"items": []}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    mock_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://open.feishu.cn",
    )
    try:
        user_client = FeishuUserClient(store, open_id="ou_fb", app_id="cli_fb", client=mock_http)
        preflight = await user_client.apreflight_scopes()
        # F3: can_send_as_user truthfully reports im:message.send_as_user (False), while can_send is True via bot_fallback
        assert preflight.can_send_as_user is False
        assert preflight.has_send_as_user_scope is False
        assert preflight.can_send is True
        assert preflight.can_send_via_bot is True
        assert preflight.send_mode == "bot_fallback"
        assert preflight.fallback_bot_app_id == "cli_fb"
        assert preflight.missing_required_scopes == ("im:message.send_as_user",)

        # Strict require_send_as_user_scope(allow_bot_fallback=False) still raises
        with pytest.raises(FeishuUserScopeError):
            await user_client.arequire_send_as_user_scope(allow_bot_fallback=False)
        # With allow_bot_fallback=True it succeeds
        verified_rec = await user_client.arequire_send_as_user_scope(allow_bot_fallback=True)
        assert verified_rec.open_id == "ou_fb"

        # Default auto_invite_bot_to_chat=False does NOT mutate chat membership on 230002
        with pytest.raises(FeishuBotRemoteError) as exc_230002:
            await user_client.reply_as_user(
                "om_trigger_fb",
                "post",
                {"zh_cn": {"content": [[{"tag": "text", "text": "hi"}]]}},
                chat_id="oc_group_fb",
            )
        assert exc_230002.value.code == 230002
        assert bot_in_chat is False
        assert not any(p.endswith("/members") for _, p, _, _ in calls)

        # Explicit auto_invite_bot_to_chat=True invites only cli_fb and succeeds
        explicit_invite_reply = await user_client.reply_as_user(
            "om_trigger_fb",
            "post",
            {"zh_cn": {"content": [[{"tag": "text", "text": "hi"}]]}},
            chat_id="oc_group_fb",
            auto_invite_bot_to_chat=True,
        )
        assert explicit_invite_reply.message_id == "om_bot_reply_1"
        assert bot_in_chat is True

        # Full /ai command flow works end-to-end when bot is in chat
        svc = FeishuAiCommandService(
            state_path=tmp_path / "fb_ai_cfg.json",
            token_store=store,
            user_client_factory=lambda cfg: user_client,
            tab_creator=lambda **_kw: "tab-fb-1",
            tab_getter=lambda _tid: None,
            tab_env_switcher=lambda *_a, **_kw: None,
            turn_dispatcher=lambda *_a, **_kw: asyncio.sleep(0, result="Bot fallback reply ok"),
        )
        svc.update_config(
            {
                "enabled": True,
                "owner_open_id": "ou_fb",
                "app_id": "cli_fb",
                "cwd": str(tmp_path),
            }
        )
        msg = FeishuUserMessage(
            message_id="om_trigger_fb",
            chat_id="oc_group_fb",
            sender_open_id="ou_fb",
            sender_type="user",
            msg_type="text",
            text="/ai test fallback without send_as_user",
            create_time_ms=int(time.time() * 1000),
        )
        res = await svc.handle_message(msg, source="ws")
        assert res.handled is True
        assert res.status == "completed"
        assert res.final_message_id == "om_bot_reply_1"

        # Direct send_as_user also uses bot fallback
        send_res = await user_client.send_as_user("oc_group_fb", "text", {"text": "hello"})
        assert send_res["message_id"] == "om_bot_send_1"
        assert send_res["send_mode"] == "bot_fallback"

        # F2 / R2: Post-accept timeout or missing receipt raises immediately without switching/retrying another bot
        simulate_timeout = True
        before_calls = len(calls)
        with pytest.raises(httpx.ReadTimeout):
            await user_client.send_as_user("oc_group_fb", "text", {"text": "timeout-msg"})
        assert len(calls) == before_calls + 1
        with pytest.raises(httpx.ReadTimeout):
            await user_client.reply_as_user(
                "om_trigger_fb",
                "post",
                {"zh_cn": {"content": [[{"tag": "text", "text": "timeout-reply"}]]}},
                chat_id="oc_group_fb",
            )
        simulate_timeout = False

        simulate_missing_id = True
        before_calls = len(calls)
        with pytest.raises(FeishuBotError, match="delivery may have occurred"):
            await user_client.send_as_user("oc_group_fb", "text", {"text": "missing-id-msg"})
        assert len(calls) == before_calls + 1
        with pytest.raises(FeishuBotError, match="delivery may have occurred"):
            await user_client.reply_as_user(
                "om_trigger_fb",
                "post",
                {"zh_cn": {"content": [[{"tag": "text", "text": "missing-id-reply"}]]}},
                chat_id="oc_group_fb",
            )
        # R2: handle_message with missing reply receipt must NEVER report completed or send a duplicate error reply
        msg_missing_receipt = FeishuUserMessage(
            message_id="om_trigger_missing_receipt",
            chat_id="oc_group_fb",
            sender_open_id="ou_fb",
            sender_type="user",
            msg_type="text",
            text="/ai test missing reply receipt",
            create_time_ms=int(time.time() * 1000),
        )
        res_missing = await svc.handle_message(msg_missing_receipt, source="ws")
        assert res_missing.status == "failed"
        assert "delivery may have occurred" in (res_missing.reason or "")
        assert res_missing.final_message_id is None
        simulate_missing_id = False

        # R2: Also verify final reply missing receipt (when interim reply succeeds but final reply returns empty data={})
        reply_attempt = 0
        orig_handler = handler

        def final_missing_handler(request: httpx.Request) -> httpx.Response:
            nonlocal reply_attempt
            if request.url.path.endswith("/reply"):
                reply_attempt += 1
                if reply_attempt >= 2:
                    calls.append(
                        (
                            request.method,
                            request.url.path,
                            request.headers.get("Authorization", ""),
                            (
                                json.loads(request.content.decode("utf-8"))
                                if request.content
                                else {}
                            ),
                        )
                    )
                    return httpx.Response(200, json={"code": 0, "data": {"message_id": "   "}})
            return orig_handler(request)

        mock_http._transport = httpx.MockTransport(final_missing_handler)
        msg_final_missing = FeishuUserMessage(
            message_id="om_trigger_final_missing",
            chat_id="oc_group_fb",
            sender_open_id="ou_fb",
            sender_type="user",
            msg_type="text",
            text="/ai test final reply missing receipt",
            create_time_ms=int(time.time() * 1000),
        )
        before_reply_calls = sum(1 for _, p, _, _ in calls if p.endswith("/reply"))
        res_final_missing = await svc.handle_message(msg_final_missing, source="ws")
        after_reply_calls = sum(1 for _, p, _, _ in calls if p.endswith("/reply"))
        assert res_final_missing.status == "failed"
        assert "delivery may have occurred" in (res_final_missing.reason or "")
        assert res_final_missing.final_message_id is None
        # Only 2 reply calls (1 interim + 1 final), no 3rd blind error-fallback reply after uncertain final delivery
        assert after_reply_calls - before_reply_calls == 2

        # R2-remnant: Verify ReadError, RemoteProtocolError, and HTTP 200 malformed JSON on final reply
        # are all classified as FeishuDeliveryUncertainError and suppress any 3rd error-fallback reply
        for case_label, failure_mode in (
            ("read_error", "read_error"),
            ("remote_protocol_error", "remote_protocol_error"),
            ("malformed_200_json", "malformed_200_json"),
            ("unrecognizable_200_json", "unrecognizable_200_json"),
        ):
            case_reply_attempt = 0

            def uncertain_final_handler(
                request: httpx.Request,
                *,
                _mode: str = failure_mode,
            ) -> httpx.Response:
                nonlocal case_reply_attempt
                if request.url.path.endswith("/reply"):
                    case_reply_attempt += 1
                    if case_reply_attempt >= 2:
                        calls.append(
                            (
                                request.method,
                                request.url.path,
                                request.headers.get("Authorization", ""),
                                (
                                    json.loads(request.content.decode("utf-8"))
                                    if request.content
                                    else {}
                                ),
                            )
                        )
                        if _mode == "read_error":
                            raise httpx.ReadError("connection reset after submit", request=request)
                        if _mode == "remote_protocol_error":
                            raise httpx.RemoteProtocolError(
                                "server closed connection without response", request=request
                            )
                        if _mode == "malformed_200_json":
                            return httpx.Response(200, text="<html>gateway cut off</html>")
                        return httpx.Response(200, json={"unexpected": "shape"})
                return orig_handler(request)

            mock_http._transport = httpx.MockTransport(uncertain_final_handler)
            msg_uncertain = FeishuUserMessage(
                message_id=f"om_trigger_{case_label}",
                chat_id="oc_group_fb",
                sender_open_id="ou_fb",
                sender_type="user",
                msg_type="text",
                text=f"/ai test {case_label}",
                create_time_ms=int(time.time() * 1000),
            )
            before_case_replies = sum(1 for _, p, _, _ in calls if p.endswith("/reply"))
            res_uncertain = await svc.handle_message(msg_uncertain, source="ws")
            after_case_replies = sum(1 for _, p, _, _ in calls if p.endswith("/reply"))
            assert res_uncertain.status == "failed", case_label
            assert "delivery may have occurred" in (res_uncertain.reason or ""), case_label
            assert res_uncertain.final_message_id is None, case_label
            assert after_case_replies - before_case_replies == 2, case_label

        mock_http._transport = httpx.MockTransport(orig_handler)

        # F1 / R1: If cli_fb is registered in FeishuBotPoolStore and disabled, fallback is rejected even when keychain has secret
        pool = FeishuBotPoolStore(
            path=tmp_path / "fb_pool.json", legacy_path=tmp_path / "fb_legacy.json"
        )
        bot_id = pool.create_bot(
            name="FB Bot",
            config=FeishuBotConfig(app_id="cli_fb", app_secret="fb-bot-secret"),
        )
        cur_rev = next(b.revision for b in pool.snapshot().bots if b.bot_id == bot_id)
        pool.update_bot(bot_id, enabled=False, expected_revision=cur_rev)
        monkeypatch.setattr(
            "claude_hub.services.feishu_bot_pool.FeishuBotPoolStore",
            lambda: pool,
        )
        assert store.resolve_fallback_bot_configs(preferred_app_id="cli_fb", record=rec) == []
        disabled_pf = await user_client.apreflight_scopes(rec)
        assert disabled_pf.can_send_via_bot is False
        assert disabled_pf.can_send is False
        assert disabled_pf.send_mode == "none"
        with pytest.raises(FeishuUserScopeError):
            await user_client.send_as_user("oc_group_fb", "text", {"text": "blocked-when-disabled"})

        # R1: Deleting the bot from FeishuBotPoolStore preserves a revoked tombstone and does NOT re-enable cached credentials
        del_rev = next(b.revision for b in pool.snapshot().bots if b.bot_id == bot_id)
        pool.delete_bot(bot_id, expected_revision=del_rev)
        assert store.resolve_fallback_bot_configs(preferred_app_id="cli_fb", record=rec) == []
        deleted_pf = await user_client.apreflight_scopes(rec)
        assert deleted_pf.can_send_via_bot is False
        assert deleted_pf.can_send is False
        assert deleted_pf.send_mode == "none"
        with pytest.raises(FeishuUserScopeError):
            await user_client.send_as_user("oc_group_fb", "text", {"text": "blocked-when-deleted"})

        # R1: If FeishuBotPoolStore state cannot be read (raises FeishuBotPoolStateError), fail closed
        from claude_hub.services.feishu_bot_pool import FeishuBotPoolStateError

        class _UnreadablePool:
            def snapshot(self) -> Any:
                raise FeishuBotPoolStateError("simulated corrupt pool state")

        monkeypatch.setattr(
            "claude_hub.services.feishu_bot_pool.FeishuBotPoolStore",
            lambda: _UnreadablePool(),
        )
        assert store.resolve_fallback_bot_configs(preferred_app_id="cli_fb", record=rec) == []
        unreadable_pf = await user_client.apreflight_scopes(rec)
        assert unreadable_pf.can_send_via_bot is False
        assert unreadable_pf.can_send is False
        assert unreadable_pf.send_mode == "none"
        with pytest.raises(FeishuUserScopeError):
            await user_client.send_as_user(
                "oc_group_fb", "text", {"text": "blocked-when-pool-unreadable"}
            )
    finally:
        await mock_http.aclose()


@pytest.mark.asyncio
async def test_ai_capabilities_provider_model_options_and_new_model_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Capabilities expose logical StreamModelOption rows per provider and validate new MASO/Codex variants."""
    import claude_hub.services.feishu_ai_command as ai_cmd_mod
    from claude_hub.models import StreamModelOption, StreamReasoningEffortOption

    # Validate MASO [redacted] and [redacted] variants + Codex max/ultra efforts
    prov_carbon, model_carbon, eff_carbon = validate_ai_command_agent_config(
        "maso", "[redacted]", "high"
    )
    assert prov_carbon == AgentType.MASO
    assert model_carbon == "[redacted]_reasoning_high"
    assert eff_carbon == "high"

    prov_carbon_med, model_carbon_med, _ = validate_ai_command_agent_config(
        "maso", "[redacted]_reasoning_high", "medium"
    )
    assert prov_carbon_med == AgentType.MASO
    assert model_carbon_med == "[redacted]_reasoning_medium"

    with pytest.raises(ValueError, match="does not support reasoning_effort='xhigh'"):
        validate_ai_command_agent_config("maso", "[redacted]", "xhigh")

    _, model_sol61_xhigh, _ = validate_ai_command_agent_config("maso", "[redacted]", "xhigh")
    assert model_sol61_xhigh == "[redacted]_reasoning_xhigh"

    _, model_codex_max, eff_codex_max = validate_ai_command_agent_config(
        "codex", "[redacted]", "max"
    )
    assert model_codex_max == "[redacted]"
    assert eff_codex_max == "max"
    _, _, eff_codex_ultra = validate_ai_command_agent_config("codex", "[redacted]", "ultra")
    assert eff_codex_ultra == "ultra"

    monkeypatch.setattr(ai_cmd_mod, "_AI_MASO_CATALOG_CACHE", None)

    async def fake_check_maso_service(
        _env: Mapping[str, str],
    ) -> tuple[list[StreamModelOption], str]:
        return (
            [
                StreamModelOption(
                    id="[redacted]",
                    label="[redacted]",
                    provider_model_id="[redacted]",
                    default_reasoning_effort="high",
                    supported_reasoning_efforts=[
                        StreamReasoningEffortOption(
                            id="low", label="Low", provider_model_id="[redacted]"
                        ),
                        StreamReasoningEffortOption(
                            id="high",
                            label="High",
                            provider_model_id="[redacted]_reasoning_high",
                        ),
                        StreamReasoningEffortOption(
                            id="xhigh",
                            label="Extra High",
                            provider_model_id="[redacted]_reasoning_xhigh",
                        ),
                    ],
                ),
                StreamModelOption(
                    id="[redacted]",
                    label="[redacted]",
                    provider_model_id="[redacted]",
                    default_reasoning_effort="high",
                    supported_reasoning_efforts=[
                        StreamReasoningEffortOption(
                            id="low", label="Low", provider_model_id="[redacted]"
                        ),
                        StreamReasoningEffortOption(
                            id="medium",
                            label="Medium",
                            provider_model_id="[redacted]_reasoning_medium",
                        ),
                        StreamReasoningEffortOption(
                            id="high",
                            label="High",
                            provider_model_id="[redacted]_reasoning_high",
                        ),
                    ],
                ),
            ],
            "[redacted]_reasoning_high",
        )

    monkeypatch.setattr(
        "claude_hub.services.agent_stream.maso_http.check_maso_service",
        fake_check_maso_service,
    )
    service = FeishuAiCommandService(
        state_path=tmp_path / "caps_ai.json",
        token_store=FeishuUserTokenStore(
            path=tmp_path / "caps_tokens.json",
            key_path=tmp_path / "caps_tokens.key",
        ),
        dedup_store=FeishuMessageDedupStore(tmp_path / "caps_dedup.json"),
    )
    caps = await service.acapabilities_dict()
    opts_by_prov = caps["provider_model_options"]
    for prov in ("claude", "codex", "traex", "maso", "cursor", "antigravity"):
        assert prov in opts_by_prov
        assert len(opts_by_prov[prov]) >= 1

    maso_ids = [opt["id"] for opt in opts_by_prov["maso"]]
    assert maso_ids == ["[redacted]", "[redacted]"]
    # Logical options must NOT duplicate _reasoning_* suffix variants as separate top-level rows
    assert not any("_reasoning_" in mid for mid in maso_ids)
    cursor_ids = [opt["id"] for opt in opts_by_prov["cursor"]]
    assert len(cursor_ids) == len(set(cursor_ids))
    assert caps["model_reasoning_efforts"]["[redacted]"] == ["none", "low", "medium", "high"]
    assert caps["model_reasoning_efforts"]["[redacted]_reasoning_high"] == [
        "none",
        "low",
        "medium",
        "high",
    ]


@pytest.mark.asyncio
async def test_live_feishu_p2p_discovery_search_cross_app_ws_owner_and_230002_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end regression for live '/ai 你好' P2P discovery, bounded search, cross-app WS owner check, and 230002 fallback."""
    import time

    from claude_hub.services.bot_pool.common import OwnerIdentity
    from claude_hub.services.feishu_bot import (
        FeishuBotConfig,
        FeishuBotRemoteError,
        FeishuMessageEvent,
    )
    from claude_hub.services.feishu_bot_pool import FeishuBotPoolStore

    store = FeishuUserTokenStore(
        path=tmp_path / "live_tokens.json",
        key_path=tmp_path / "live_tokens.key",
    )
    # Primary /ai owner token on [redacted] app (cli_[redacted]) WITHOUT im:message.send_as_user
    store.save_oauth_token(
        open_id="ou_owner_[redacted]",
        app_id="cli_[redacted]",
        access_token="u-owner-[redacted]",
        refresh_token="ur-owner-[redacted]",
        app_secret="sec-[redacted]",
        user_name="[redacted]",
        expires_in=7200,
        scope="im:message im:chat:read im:message.reactions:write_only",
    )
    # Unrelated user who happens to share the exact same display_name ("[redacted]") on another app:
    # must NEVER be trusted without authoritative get_message() verification under cli_[redacted]!
    store.save_oauth_token(
        open_id="ou_impersonator_same_name",
        app_id="cli_other",
        access_token="u-other",
        refresh_token="ur-other",
        user_name="[redacted]",
        expires_in=7200,
        scope="im:message",
    )

    pool = FeishuBotPoolStore(
        path=tmp_path / "live_pool.json",
        legacy_path=tmp_path / "live_legacy.json",
    )
    # Enabled pool bot [redacted] (cli_[redacted]) bound to group/chat oc_bound_chat
    merlin_bot_id = pool.create_bot(
        name="[redacted]",
        config=FeishuBotConfig(app_id="cli_[redacted]", app_secret="sec-[redacted]"),
    )
    merlin_rev = next(b.revision for b in pool.snapshot().bots if b.bot_id == merlin_bot_id)
    pool.bind_group(
        merlin_bot_id,
        owner=OwnerIdentity(open_id="local", email="", kind="local"),
        tab_id="tab-merlin-bound",
        workspace_id=None,
        chat_id="oc_bound_chat",
        chat_name="Merlin Bound Chat",
        expected_revision=merlin_rev,
    )
    # Disabled/revoked pool bot also bound to oc_p2p_merlinbot: must NEVER be used as fallback!
    revoked_bot_id = pool.create_bot(
        name="RevokedBot",
        config=FeishuBotConfig(app_id="cli_revoked", app_secret="sec-revoked"),
    )
    rev_bot_rev = next(b.revision for b in pool.snapshot().bots if b.bot_id == revoked_bot_id)
    pool.bind_group(
        revoked_bot_id,
        owner=OwnerIdentity(open_id="local", email="", kind="local"),
        tab_id="tab-revoked",
        workspace_id=None,
        chat_id="oc_p2p_merlinbot",
        chat_name="Revoked Chat",
        expected_revision=rev_bot_rev,
    )
    rev_bot_rev2 = next(b.revision for b in pool.snapshot().bots if b.bot_id == revoked_bot_id)
    pool.update_bot(revoked_bot_id, enabled=False, expected_revision=rev_bot_rev2)

    monkeypatch.setattr(
        "claude_hub.services.feishu_bot_pool.FeishuBotPoolStore",
        lambda: pool,
    )

    now_ms = int(time.time() * 1000)
    chats_calls: list[dict[str, str]] = []
    search_bodies: list[dict[str, Any]] = []
    dm_sends: list[tuple[str, dict[str, str], dict[str, Any]]] = []
    bound_replies: list[tuple[str, str]] = []
    member_add_calls: list[str] = []

    messages_by_id: dict[str, dict[str, Any]] = {
        "om_p2p_hello": {
            "message_id": "om_p2p_hello",
            "chat_id": "oc_p2p_merlinbot",
            "sender": {"id": "ou_owner_[redacted]", "sender_type": "user"},
            "msg_type": "text",
            "create_time": str(now_ms - 10_000),
            "body": {"content": json.dumps({"text": "/ai 你好"})},
        },
        "om_search_outside_top": {
            "message_id": "om_search_outside_top",
            "chat_id": "oc_older_active_chat",
            "sender": {"id": "ou_owner_[redacted]", "sender_type": "user"},
            "msg_type": "text",
            "create_time": str(now_ms - 8_000),
            "body": {"content": json.dumps({"text": "/ai 帮我看下搜索发现"})},
        },
        "om_ws_cross_app_owner": {
            "message_id": "om_ws_cross_app_owner",
            "chat_id": "oc_bound_chat",
            # When looked up via cli_[redacted] user token, Feishu returns ou_owner_[redacted]!
            "sender": {"id": "ou_owner_[redacted]", "sender_type": "user"},
            "msg_type": "text",
            "create_time": str(now_ms - 5_000),
            "body": {"content": json.dumps({"text": "/ai 跨应用触发"})},
        },
        "om_ws_spoof_non_owner": {
            "message_id": "om_ws_spoof_non_owner",
            "chat_id": "oc_bound_chat",
            # Authoritative lookup reveals a different open_id under cli_[redacted]
            "sender": {"id": "ou_someone_else_on_[redacted]", "sender_type": "user"},
            "msg_type": "text",
            "create_time": str(now_ms - 4_000),
            "body": {"content": json.dumps({"text": "/ai 冒充指令"})},
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        auth = req.headers.get("Authorization", "")
        body = json.loads(req.content.decode("utf-8")) if req.content else {}

        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            app_id = body.get("app_id")
            if app_id == "cli_[redacted]" and body.get("app_secret") == "sec-[redacted]":
                return httpx.Response(
                    200, json={"code": 0, "tenant_access_token": "t-[redacted]", "expire": 7200}
                )
            if app_id == "cli_[redacted]" and body.get("app_secret") == "sec-[redacted]":
                return httpx.Response(
                    200, json={"code": 0, "tenant_access_token": "t-[redacted]", "expire": 7200}
                )
            if app_id == "cli_revoked":
                raise AssertionError("Revoked/disabled pool bot must never be used")
            return httpx.Response(400, json={"code": 10003, "msg": "invalid app"})

        if path == "/open-apis/im/v1/chats":
            params = dict(req.url.params)
            chats_calls.append(params)
            # Simulate real Feishu OpenAPI: P2P chats are returned ONLY when types includes p2p
            items = [{"chat_id": "oc_group_1", "name": "Group 1", "chat_mode": "group"}]
            if "p2p" in params.get("types", ""):
                items.insert(0, {"chat_id": "oc_p2p_merlinbot", "name": "", "chat_mode": "p2p"})
            return httpx.Response(
                200, json={"code": 0, "data": {"items": items, "has_more": False}}
            )

        if path == "/open-apis/search/v2/message":
            search_bodies.append(body)
            return httpx.Response(
                200,
                json={"code": 0, "data": {"items": ["om_search_outside_top"], "has_more": False}},
            )

        if path.startswith("/open-apis/im/v1/messages/om_") and req.method == "GET":
            mid = path.rsplit("/", 1)[-1]
            assert auth == "Bearer u-owner-[redacted]"
            raw_msg = messages_by_id.get(mid)
            if raw_msg is None:
                return httpx.Response(404, json={"code": 230001, "msg": "not found"})
            return httpx.Response(200, json={"code": 0, "data": {"items": [raw_msg]}})

        if path == "/open-apis/im/v1/messages" and req.method == "GET":
            cid = req.url.params.get("container_id", "")
            items = [
                m
                for m in messages_by_id.values()
                if m["chat_id"] == cid and "ws_" not in m["message_id"]
            ]
            return httpx.Response(
                200, json={"code": 0, "data": {"items": items, "has_more": False}}
            )

        if path.endswith("/members"):
            member_add_calls.append(path)
            return httpx.Response(400, json={"code": 232006, "msg": "p2p cannot add members"})

        if path.endswith("/reactions"):
            return httpx.Response(200, json={"code": 0, "data": {"reaction_id": "rx_live_1"}})

        if "/reactions/" in path and req.method == "DELETE":
            return httpx.Response(200, json={"code": 0, "data": {}})

        if path.endswith("/reply") and req.method == "POST":
            target_mid = path.split("/messages/", 1)[1].split("/reply", 1)[0]
            if auth == "Bearer t-[redacted]":
                # Primary fallback bot cli_[redacted] is NOT in oc_p2p_merlinbot, oc_older_active_chat, or oc_bound_chat
                return httpx.Response(400, json={"code": 230002, "msg": "bot not in chat"})
            if auth == "Bearer t-[redacted]":
                # [redacted] IS in oc_bound_chat
                bound_replies.append((auth, target_mid))
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {"message_id": f"om_bound_reply_{len(bound_replies)}"},
                    },
                )
            return httpx.Response(400, json={"code": 230002, "msg": "bot not in chat"})

        if path == "/open-apis/im/v1/messages" and req.method == "POST":
            params = dict(req.url.params)
            if params.get("receive_id_type") == "open_id":
                dm_sends.append((auth, params, body))
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"message_id": f"om_owner_dm_{len(dm_sends)}"}},
                )
            return httpx.Response(400, json={"code": 230002, "msg": "bot not in chat"})

        return httpx.Response(200, json={"code": 0, "data": {}})

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as mock_http:
        user_client = FeishuUserClient(
            store,
            open_id="ou_owner_[redacted]",
            app_id="cli_[redacted]",
            client=mock_http,
        )
        dispatched_prompts: list[str] = []

        async def fake_turn_dispatcher(
            _tab_id: str, prompt: str, _turn_id: str, _vis: str, _meta: Mapping[str, Any]
        ) -> str:
            dispatched_prompts.append(prompt)
            return "你好！已收到并处理。"

        service = FeishuAiCommandService(
            state_path=tmp_path / "live_ai.json",
            token_store=store,
            dedup_store=FeishuMessageDedupStore(tmp_path / "live_dedup.json"),
            user_client_factory=lambda _cfg: user_client,
            tab_creator=lambda **_kw: "tab-live-ai",
            tab_getter=lambda _tid: None,
            tab_env_switcher=lambda *_a, **_kw: None,
            turn_dispatcher=fake_turn_dispatcher,
        )
        service.update_config(
            {
                "enabled": True,
                "owner_open_id": "ou_owner_[redacted]",
                "app_id": "cli_[redacted]",
                "provider": "maso",
                "model": "[redacted]_reasoning_high",
                "reasoning_effort": "high",
                "cwd": str(tmp_path),
                "watch_chats": [],
                "max_active_chats": 2,
            }
        )

        # Low-level reply_as_user default MUST still raise 230002 (no implicit DM or membership mutation)
        with pytest.raises(FeishuBotRemoteError) as low_exc:
            await user_client.reply_as_user(
                "om_p2p_hello",
                "post",
                build_ai_post_payload("low-level check"),
                chat_id="oc_p2p_merlinbot",
            )
        assert low_exc.value.code == 230002
        assert member_add_calls == []
        assert dm_sends == []

        # 1. Poller discovers both P2P chat (via types='p2p,group') and outside-top chat (via search_messages)
        poller = FeishuAiPoller(service)
        detected = await poller.poll_once()
        await poller.wait_for_inflight(timeout=5.0)

        assert set(detected) == {"om_p2p_hello", "om_search_outside_top"}
        assert chats_calls and chats_calls[0].get("types") == "p2p,group"
        assert search_bodies and search_bodies[0].get("from_ids") == ["ou_owner_[redacted]"]
        # Both non-bound chats fell back cleanly to owner DM (interim + final = 2 DMs per command = 4 total)
        assert len(dm_sends) == 4
        assert all(auth == "Bearer t-[redacted]" for auth, _, _ in dm_sends)
        assert all(body["receive_id"] == "ou_owner_[redacted]" for _, _, body in dm_sends)
        assert member_add_calls == []

        # 2. Cross-app WebSocket event from [redacted] (ou_[redacted]_owner != ou_owner_[redacted]):
        #    verified authoritatively via get_message() under cli_[redacted] and replied in-chat via bound [redacted]!
        ws_owner_event = FeishuMessageEvent(
            event_id="ev_ws_cross_owner_1",
            app_id="cli_[redacted]",
            chat_id="oc_bound_chat",
            chat_type="p2p",
            sender_open_id="ou_[redacted]_owner",
            message_id="om_ws_cross_app_owner",
            message_type="text",
            text="/ai 跨应用触发",
            message_created_at_ms=now_ms - 5_000,
        )
        assert await service.handle_ws_event(ws_owner_event) is True
        assert len(bound_replies) == 2
        assert all(auth == "Bearer t-[redacted]" for auth, _ in bound_replies)

        # 3. Spoofed cross-app WebSocket event (even if display_name matches elsewhere) is rejected!
        ws_spoof_event = FeishuMessageEvent(
            event_id="ev_ws_spoof_1",
            app_id="cli_[redacted]",
            chat_id="oc_bound_chat",
            chat_type="p2p",
            sender_open_id="ou_impersonator_same_name",
            message_id="om_ws_spoof_non_owner",
            message_type="text",
            text="/ai 冒充指令",
            message_created_at_ms=now_ms - 4_000,
        )
        assert await service.handle_ws_event(ws_spoof_event) is False
        assert len(dispatched_prompts) == 3
