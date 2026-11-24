"""Personal images must reach native input as bytes, preserving source-message ownership."""

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from claude_hub.services.feishu_ai_images import message_from_event, prepare_images
from claude_hub.services.feishu_ai_poller import _message_from_dict
from claude_hub.services.feishu_bot import FeishuBotError
from claude_hub.services.feishu_user_client import FeishuUserClient, _parse_user_message
from claude_hub.services.feishu_user_media import MAX_IMAGE_BYTES, read_image_response
from claude_hub.services.feishu_user_message import FeishuUserMessage
from claude_hub.services.feishu_web_session.adapter import FeishuWebUserTransport
from claude_hub.services.feishu_web_session.client import FeishuWebClient
from claude_hub.services.feishu_web_session.codec import encode
from claude_hub.services.feishu_web_session.rich import (
    TYPE_IMAGE,
    TYPE_POST,
    T,
    WebMessage,
    rich_text,
)
from claude_hub.services.feishu_web_session.store import FeishuWebSession

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aH3sAAAAASUVORK5CYII="
)


def message(mid="10001", **kwargs):
    return FeishuUserMessage(mid, "20001", "30001", "user", "post", "/ai inspect", 1000, **kwargs)


@pytest.mark.parametrize("direct", [False, True])
def test_web_resource_refs_survive_normalization_and_durable_queue(direct):
    if direct:
        content = encode("lark_entities_ImageContent", {"imageV2": {"imageKey": "img_direct"}})
        expected = (("10001", "img_direct"),)
    else:
        rich, _ = rich_text([[T("/ai inspect")]], paragraphs=True)
        for key in ["img_first", "img_second", "img_first"]:
            nid = str(len(rich["elementIds"]))
            rich["elementIds"].append(nid)
            rich["elements"]["dictionary"][nid] = {
                "tag": 2,
                "property": base64.b64encode(
                    encode("lark_entities_RichTextElement_ImageProperty", {"imageKey": key})
                ).decode(),
            }
        rich["elements"]["dictionary"]["unreachable"] = {"tag": 2}
        content = encode("lark_entities_PostContent", {"richText": rich})
        expected = (("10001", "img_first"), ("10001", "img_second"))
    transport = FeishuWebUserTransport(MagicMock(spec=FeishuWebClient))
    normalized = transport.to_user_message(
        WebMessage(
            id="10001",
            chat_id="20001",
            parent_id="9999",
            type=TYPE_IMAGE if direct else TYPE_POST,
            content=content,
        ),
        session=FeishuWebSession(),
    )
    assert normalized.image_refs == expected
    assert normalized.parent_id == "9999"
    assert _message_from_dict(json.loads(json.dumps(normalized.to_dict()))) == normalized


def test_openapi_and_ws_preserve_original_resource_owner():
    parsed = _parse_user_message(
        {
            "message_id": "om_image",
            "chat_id": "oc_chat",
            "msg_type": "image",
            "body": {"content": json.dumps({"image_key": "img_a"})},
        }
    )
    assert parsed.image_refs == (("om_image", "img_a"),)
    event = SimpleNamespace(
        message_id="om_command",
        chat_id="oc_chat",
        sender_open_id="ou_owner",
        message_type="post",
        text="/ai inspect",
        message_created_at_ms=1,
        thread_id=None,
        parent_id="om_parent",
        root_id=None,
        image_refs=(("om_child", "img_child"),),
        image_keys=("img_a",),
    )
    normalized = message_from_event(event)
    assert normalized.image_refs == (("om_child", "img_child"), ("om_command", "img_a"))
    assert _message_from_dict(normalized.to_dict()) == normalized


@pytest.mark.asyncio
async def test_command_multiple_and_quoted_images_are_real_bytes_with_source_provenance():
    command = message(
        image_refs=(("10001", "img_a"), ("10001", "img_b")), has_images=True, parent_id="9999"
    )
    parent = message("9999", image_refs=(("9999", "img_c"),), has_images=True)
    client = SimpleNamespace(download_message_image=AsyncMock(return_value=PNG))
    attachments, metadata = await prepare_images(client, command, [parent, command])
    assert [call.args[0].message_id for call in client.download_message_image.await_args_list] == [
        "10001",
        "10001",
        "9999",
    ]
    from claude_hub.api.agent_stream import _decode_attachments

    images, _ = _decode_attachments(attachments)
    assert images == [PNG, PNG, PNG]
    assert [m["resource_key"] for m in metadata] == ["img_a", "img_b", "img_c"]
    assert all(
        m["sha256"] == hashlib.sha256(PNG).hexdigest() and m["width"] == m["height"] == 1
        for m in metadata
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["permission", "expired", "download", "invalid", "oversized", "many"]
)
async def test_acquisition_failures_never_silently_drop_images(kind):
    command = message(has_images=True, image_refs=(("10001", "img_a"),))
    client = SimpleNamespace(download_message_image=AsyncMock(return_value=PNG))
    if kind in {"permission", "expired", "download"}:
        client.download_message_image.side_effect = FeishuBotError(kind)
    elif kind == "invalid":
        client.download_message_image.return_value = b"<html>login required</html>"
    elif kind == "oversized":
        client.download_message_image.return_value = PNG + b"0" * MAX_IMAGE_BYTES
    else:
        command = replace(command, image_refs=tuple(("10001", f"img_{i}") for i in range(11)))
    with pytest.raises(Exception):
        await prepare_images(client, command, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 401, 403, 404, 500])
async def test_http_errors_are_bounded_and_do_not_follow_redirects(status):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(
            status, headers={"location": "https://other.invalid/private"}, content=b"error"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises((FeishuBotError, httpx.HTTPStatusError)):
            await read_image_response(http, "https://files.invalid/image", {"cookie": "test-only"})
    assert calls == ["https://files.invalid/image"]


@pytest.mark.asyncio
async def test_web_personal_download_uses_only_bound_cookie_and_correct_source():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=PNG)

    session = FeishuWebSession(
        user_id="30001", cookies={"session": "test-only"}, csrf_token="test-only"
    )
    store = MagicMock()
    store.load.return_value = session
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        web = FeishuWebClient(store, http_client=http)
        adapter = FeishuWebUserTransport(web, owner_open_id="30001")
        parent = message("9999", image_refs=(("9999", "img_parent"),), has_images=True)
        assert await adapter.download_message_image(parent, "img_parent") == PNG
        with pytest.raises(FeishuBotError):
            await adapter.download_message_image(parent, "img_other")
    assert len(requests) == 1
    assert requests[0].url.path == "/download/messages/9999/keys/img_parent"
    assert requests[0].url.params["chat_id"] == "20001"
    assert requests[0].headers["cookie"] == "session=test-only"
    assert "authorization" not in requests[0].headers


@pytest.mark.asyncio
async def test_web_owner_mismatch_and_missing_session_never_use_bot():
    client = MagicMock(spec=FeishuUserClient)
    client._get_web_transport.return_value = None
    from claude_hub.services.feishu_user_media import download_user_image

    with pytest.raises(FeishuBotError, match="not authorized"):
        await download_user_image(client, message(image_refs=(("10001", "img_a"),)), "img_a")
    client._with_user_token_retry.assert_not_called()


@pytest.mark.asyncio
async def test_managed_dispatch_reuses_native_attachment_validation(monkeypatch):
    from claude_hub.api import agent_stream
    from claude_hub.models import AgentStreamEventType
    from claude_hub.services import feishu_ai_workspace

    command = message(image_refs=(("10001", "img_a"),), has_images=True)
    attachments, provenance = await prepare_images(
        SimpleNamespace(download_message_image=AsyncMock(return_value=PNG)), command, []
    )
    queue = asyncio.Queue()
    await queue.put(
        SimpleNamespace(
            turn_id="turn-test",
            type=AgentStreamEventType.TURN_COMPLETED,
            payload={"status": "completed", "assistant_text": "The picture contains a test mark."},
        )
    )
    manager = SimpleNamespace(
        subscribe_completion=AsyncMock(return_value=queue),
        send_message=AsyncMock(),
        unsubscribe_completion=MagicMock(),
    )
    monkeypatch.setattr(agent_stream, "get_managed_tailer_manager", lambda: manager)
    monkeypatch.setattr(
        agent_stream,
        "_capabilities_for",
        AsyncMock(return_value=SimpleNamespace(supports_images=True)),
    )
    session = SimpleNamespace(
        id="managed-test",
        tab_id="tab-test",
        agent_type=agent_stream.AgentType.CODEX,
        session_kind=agent_stream.SessionKind.CHAT,
    )
    answer = await feishu_ai_workspace.dispatch_managed_turn(
        session,
        "/ai inspect",
        "turn-test",
        "inspect",
        {"images": provenance},
        attachments=attachments,
    )
    assert "test mark" in answer
    assert manager.send_message.await_args.args[2] == [PNG]
    assert manager.send_message.await_args.args[3] == "turn-test"
    assert manager.send_message.await_args.kwargs["turn_metadata"]["images"] == provenance


@pytest.mark.asyncio
@pytest.mark.parametrize("plain_401", [False, True])
async def test_expired_oauth_refreshes_user_token_without_bot_fallback(monkeypatch, plain_401):
    from claude_hub.services.feishu_user_token_store import FeishuUserTokenRecord

    tokens = []

    def handle(request):
        tokens.append(request.headers["authorization"])
        if len(tokens) == 1:
            return (
                httpx.Response(401, content=b"Expired")
                if plain_401
                else httpx.Response(400, json={"code": 99991677, "msg": "expired"})
            )
        return httpx.Response(200, content=PNG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = FeishuUserClient(token_store=SimpleNamespace(), client=http)
        record = FeishuUserTokenRecord(
            open_id="ou_owner",
            app_id="cli_test",
            access_token="old-test",
            refresh_token="refresh-test",
        )
        get_token = AsyncMock(side_effect=[("old-test", record), ("new-test", record)])
        monkeypatch.setattr(client, "get_user_token", get_token)
        source = message("om_image", image_refs=(("om_image", "img_a"),))
        assert await client.download_message_image(source, "img_a") == PNG
        assert get_token.await_args_list[1].kwargs == {"force_refresh": True}
        assert tokens == ["Bearer old-test", "Bearer new-test"]
        assert not client._fallback_bot_clients


@pytest.mark.asyncio
async def test_image_metadata_read_failure_and_download_timeout_are_explicit(monkeypatch):
    client = SimpleNamespace(get_message=AsyncMock(return_value=None))
    with pytest.raises(FeishuBotError, match="metadata"):
        await prepare_images(client, message(has_images=True), [])
    original_timeout = asyncio.timeout
    monkeypatch.setattr(
        "claude_hub.services.feishu_ai_images.asyncio.timeout", lambda _: original_timeout(0.001)
    )

    async def never(*args):
        await asyncio.sleep(10)

    client = SimpleNamespace(download_message_image=never)
    with pytest.raises(TimeoutError):
        await prepare_images(client, message(image_refs=(("10001", "img_a"),)), [])


@pytest.mark.asyncio
async def test_standalone_dispatch_preserves_attachments(monkeypatch):
    from claude_hub.api import agent_stream
    from claude_hub.services import feishu_ai_workspace

    attachments, metadata = await prepare_images(
        SimpleNamespace(download_message_image=AsyncMock(return_value=PNG)),
        message(image_refs=(("10001", "img_a"),)),
        [],
    )
    dispatch = AsyncMock(return_value="image answer")
    monkeypatch.setattr(agent_stream, "dispatch_tab_chat_and_wait", dispatch)
    service = SimpleNamespace(_turn_dispatcher=None, _channel_record_for_tab=lambda _: {})
    assert (
        await feishu_ai_workspace.dispatch_turn(
            service,
            "tab-test",
            "inspect",
            "turn-test",
            "inspect",
            {"images": metadata},
            attachments=attachments,
        )
        == "image answer"
    )
    images, previews = agent_stream._decode_attachments(dispatch.await_args.kwargs["attachments"])
    assert images == [PNG] and previews == [PNG]


def test_real_queue_checkpoint_preserves_image_ownership_without_bytes(tmp_path):
    from claude_hub.services.feishu_ai_command import FeishuAiCommandService

    source = message(has_images=True, image_refs=(("9999", "img_parent"),), parent_id="9999")
    service = FeishuAiCommandService(state_path=tmp_path / "queue.json")
    assert service.enqueue_pending_command(source)
    fresh = FeishuAiCommandService(state_path=tmp_path / "queue.json")
    raw = fresh.list_pending_commands()[0]["message"]
    assert _message_from_dict(raw) == source
    assert "base64" not in json.dumps(raw)
