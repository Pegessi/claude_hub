"""Comprehensive unit and protocol tests for the Feishu web-session (`larkweb` / `LarkAgentX`) transport."""

from __future__ import annotations

import base64
import gzip
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Mapping

import httpx
import pytest
from click.testing import CliRunner
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from claude_hub.api.routes.feishu_bot.ai_command import register_ai_command_routes
from claude_hub.cli.main import cli
from claude_hub.services.feishu_ai_command import FeishuAiCommandService
from claude_hub.services.feishu_ai_poller import FeishuAiPoller
from claude_hub.services.feishu_bot import FeishuDeliveryUncertainError, FeishuMessageDedupStore
from claude_hub.services.feishu_user_client import FeishuUserClient
from claude_hub.services.feishu_user_token_store import FeishuUserTokenStore
from claude_hub.services.feishu_web_session import (
    BLOCK_BULLETS,
    BLOCK_NUMBERS,
    BLOCK_PARAGRAPH,
    BLOCK_QUOTE,
    CARD_CHAT,
    CHAT_GROUP,
    CHAT_P2P,
    CMD_ACK,
    CMD_HEARTBEAT,
    CMD_PUSH_MESSAGES,
    FEED_INBOX,
    REACT_DONE,
    REACT_ON_IT,
    REDACTED_SECRET,
    TAG_A,
    TAG_AT,
    TAG_LI,
    TAG_OL,
    TAG_P,
    TAG_QUOTE,
    TAG_TEXT,
    TAG_UL,
    TYPE_CARD,
    TYPE_FILE,
    TYPE_IMAGE,
    TYPE_POST,
    TYPE_SYSTEM,
    TYPE_TEXT,
    TYPE_VIDEO_CHAT,
    FeishuWebClient,
    FeishuWebFrontierListener,
    FeishuWebLoginManager,
    FeishuWebQRLoginExpiredError,
    FeishuWebSession,
    FeishuWebSessionStore,
    FeishuWebSignedOutError,
    FeishuWebUserTransport,
    L,
    Node,
    QRCodeMatrix,
    T,
    WebMessage,
    WebOut,
    build_frontier_frame,
    card_text,
    carry_cookies,
    command_of,
    compute_frontier_access_key,
    decode,
    describe,
    describe_with,
    encode,
    encode_qr_matrix,
    feishu_host,
    formatted_out,
    has_markdown,
    markdown_blocks,
    parse_frontier_frame,
    render_qr_svg_data_url,
    render_qr_unicode,
    rich_blocks,
    rich_text,
    text_of,
    text_without,
)


def test_protobuf_codec_roundtrip_and_lark_desk_reference_vectors() -> None:
    """Verify protobuf wire encoding/decoding against lark-desk reference vectors (`web_test.go`)."""
    # 1. Command table lookups
    cmd_put = command_of("PutMessageRequest")
    assert cmd_put.cmd == 5
    assert cmd_put.request == "lark_messages_PutMessageRequest"
    assert cmd_put.response == "lark_messages_PutMessageResponse"

    cmd_feed = command_of("PullFeedCardsRequest")
    assert cmd_feed.cmd == 1000
    assert cmd_feed.request == "lark_feed_PullFeedCardsRequest"

    cmd_react = command_of("PutReactionRequest")
    assert cmd_react.cmd == 25

    cmd_unreact = command_of("DeleteReactionRequest")
    assert cmd_unreact.cmd == 26

    # 2. Signed/unsigned varint and negative int32/int64 encoding & decoding
    raw_positions = encode(
        "lark_messages_PullMessagesByPositionsRequest",
        {"chatId": "7300000000000000001", "positions": [1, 2, 3]},
    )
    dec_positions = decode("lark_messages_PullMessagesByPositionsRequest", raw_positions)
    assert dec_positions == {"chatId": "7300000000000000001", "positions": [1, 2, 3]}

    # 3. RichText / TextContent / PostContent roundtrip (matching TestRichTextRoundTrip)
    rows = [
        [T("Hello "), L("Feishu Docs", "https://feishu.cn/docs"), Node(text="Alice", at="7001")],
        [T("Second paragraph · 🤖 AI 代答，仅供参考")],
    ]
    rich_dict, mentions = rich_text(rows, paragraphs=True)
    assert mentions == ["7001"]
    post_bytes = encode(
        "lark_entities_PostContent",
        {"title": "Summary", "richText": rich_dict},
    )
    assert text_of(TYPE_POST, post_bytes) == (
        "Summary\nHello Feishu Docs@Alice\nSecond paragraph · 🤖 AI 代答，仅供参考"
    )
    assert text_without(TYPE_POST, post_bytes, "7001") == (
        "Summary\nHello Feishu Docs\nSecond paragraph · 🤖 AI 代答，仅供参考"
    )

    # 4. Markdown blocks -> rich_blocks -> RichText roundtrip (matching TestMarkdownBlocks)
    md = (
        "# Heading\n\n"
        "- first **bold** item\n"
        "- second [link](https://example.com/path).\n\n"
        "1. ordered one\n"
        "2. ordered two\n\n"
        "> quoted line\n"
    )
    assert has_markdown(md) is True
    blocks = markdown_blocks(md)
    assert [b.kind for b in blocks] == [
        BLOCK_PARAGRAPH,
        BLOCK_PARAGRAPH,
        BLOCK_BULLETS,
        BLOCK_PARAGRAPH,
        BLOCK_NUMBERS,
        BLOCK_PARAGRAPH,
        BLOCK_QUOTE,
    ]
    rb_dict, rb_mentions = rich_blocks(blocks)
    assert rb_mentions == []
    rb_bytes = encode("lark_entities_TextContent", {"richText": rb_dict})
    plain_rb = text_of(TYPE_TEXT, rb_bytes)
    assert "Heading" in plain_rb
    assert "first bold item" in plain_rb
    assert "ordered one" in plain_rb
    assert "quoted line" in plain_rb

    # 5. PutMessageRequest wire format with reply_to, rootId, parentId, isReplyInThread
    put_req_bytes = encode(
        "lark_messages_PutMessageRequest",
        {
            "type": TYPE_POST,
            "chatId": "7300000000000000001",
            "rootId": "7400000000000000010",
            "parentId": "7400000000000000010",
            "cid": "cid-unit-test-1",
            "isNotified": True,
            "version": 1,
            "isReplyInThread": False,
            "content": {"title": "", "richText": rich_dict},
        },
    )
    dec_put = decode("lark_messages_PutMessageRequest", put_req_bytes)
    assert dec_put["type"] == TYPE_POST
    assert dec_put["chatId"] == "7300000000000000001"
    assert dec_put["rootId"] == "7400000000000000010"
    assert dec_put["parentId"] == "7400000000000000010"
    assert dec_put["cid"] == "cid-unit-test-1"
    assert dec_put["isNotified"] is True
    assert bool(dec_put.get("isReplyInThread", False)) is False

    # 6. Describe message types (file, image, video_chat, system, card)
    file_bytes = encode("lark_entities_FileContent", {"name": "report.pdf", "size": 1024})
    assert describe(TYPE_FILE, file_bytes) == "[文件] report.pdf"
    assert describe(TYPE_IMAGE, b"") == "[图片]"

    vc_bytes = encode(
        "lark_entities_VideoChatContent",
        {
            "type": 1,
            "meetingCard": {
                "meetingId": "m1",
                "topic": "Sprint Sync",
                "sponsorId": "u1",
                "meetNumber": "123456789",
                "startTimeMs": 1000,
                "endTimeMs": 66000,
                "participants": [{"userId": "u1"}, {"userId": "u2"}],
            },
        },
    )
    vc_desc = describe_with(
        TYPE_VIDEO_CHAT, vc_bytes, "u1", lambda _ids: {"u1": "Alice", "u2": "Bob"}
    )
    assert "Sprint Sync" in vc_desc
    assert "123 456 789" in vc_desc
    assert "Alice" in vc_desc

    sys_bytes = encode(
        "lark_entities_SystemContent",
        {
            "typeNum": 902,
            "contents": {"numHour": "0", "numMin": "12", "numSec": "5"},
        },
    )
    assert describe(TYPE_SYSTEM, sys_bytes) == "[系统] 会议结束，时长 12 分 5 秒"

    card_json = json.dumps(
        {
            "header": {"title": {"tag": "plain_text", "content": "Deploy Status"}},
            "elements": [{"tag": "markdown", "content": "All checks **green**"}],
        }
    )
    assert card_text(card_json) == "Deploy Status\nAll checks **green**"


def test_msg_frontier_frame_and_gzip_push_messages_decoding() -> None:
    """Verify msg-frontier WebSocket Frame/Packet/PushMessagesRequest decoding, gzip decompression, and ACK generation."""
    text_content = encode(
        "lark_entities_TextContent",
        {"text": "/ai 帮我总结一下"},
    )
    push_req = encode(
        "PushMessagesRequest",
        {
            "messages": {
                "7500001": {
                    "id": "7500001",
                    "type": TYPE_TEXT,
                    "fromId": "7100001",
                    "createTime": 1760000000,
                    "content": base64.b64encode(text_content).decode("ascii"),
                    "status": 0,
                    "chatId": "7300001",
                    "position": 42,
                    "chatType": CHAT_P2P,
                }
            },
            "messagesAtMe": {"7500001": True},
        },
    )
    pkt = encode(
        "Packet",
        {
            "cmd": CMD_PUSH_MESSAGES,
            "payloadType": 1,
            "sid": "sid-push-42",
            "payload": base64.b64encode(push_req).decode("ascii"),
        },
    )
    gz_pkt = gzip.compress(pkt)
    raw_frame = encode(
        "Frame",
        {
            "seqid": 100,
            "logid": 100,
            "service": 1,
            "method": 1,
            "payloadEncoding": "gzip",
            "payloadType": "pb",
            "payload": base64.b64encode(gz_pkt).decode("ascii"),
        },
    )
    ack_bytes, cmd, pushed = parse_frontier_frame(raw_frame)
    assert cmd == CMD_PUSH_MESSAGES
    assert ack_bytes is not None
    ack_frame = decode("Frame", ack_bytes)
    ack_pkt = decode("Packet", base64.b64decode(ack_frame["payload"]))
    assert ack_pkt["cmd"] == CMD_ACK
    assert ack_pkt["sid"] == "sid-push-42"
    assert base64.b64decode(ack_pkt["payload"]) == b"\x08\x00"

    assert len(pushed) == 1
    pm = pushed[0]
    assert pm.message.id == "7500001"
    assert pm.message.from_id == "7100001"
    assert pm.message.chat_id == "7300001"
    assert pm.chat_type == CHAT_P2P
    assert pm.at_me is True
    assert describe(pm.message.type, pm.message.content) == "/ai 帮我总结一下"

    # Verify access_key MD5 formula from push.go
    expected_ak = compute_frontier_access_key("appKey123", "did456")
    assert len(expected_ak) == 32


def test_qr_code_matrix_unicode_and_svg_rendering() -> None:
    """Verify QR code generation produces validfinder patterns, terminal half-block art, and SVG data URL."""
    qr_payload = json.dumps({"qrlogin": {"token": "tok-qr-test-12345"}}, separators=(",", ":"))
    matrix = encode_qr_matrix(qr_payload)
    assert isinstance(matrix, QRCodeMatrix)
    assert matrix.size == 29  # Version 3 (17 + 4*3 = 29)
    assert len(matrix.rows) == matrix.size
    # Top-left 7x7 finder pattern outer border is all '1'
    assert matrix.rows[0][:7] == "1111111"
    assert matrix.rows[6][:7] == "1111111"
    # Top-right 7x7 finder pattern
    assert matrix.rows[0][-7:] == "1111111"
    # Bottom-left 7x7 finder pattern
    assert matrix.rows[-7][:7] == "1111111"
    assert matrix.rows[-1][:7] == "1111111"

    unicode_art = render_qr_unicode(matrix)
    assert "█" in unicode_art
    svg_url = render_qr_svg_data_url(matrix)
    assert svg_url.startswith("data:image/svg+xml;base64,")


def test_web_session_store_encryption_0600_permissions_redaction_and_larkx_import(
    tmp_path: Path,
) -> None:
    """Verify 0600 permissions, at-rest encryption, secret redaction, and ~/.larkx/credentials.json import."""
    session_file = tmp_path / "secrets" / "feishu_web_session.json"
    key_file = tmp_path / "secrets" / "feishu_web_session.key"
    larkx_file = tmp_path / "larkx_credentials.json"

    raw_cookie_secret = "super-secret-session-cookie-value-999"
    raw_csrf_secret = "super-secret-csrf-token-value-888"

    store = FeishuWebSessionStore(
        path=session_file,
        key_path=key_file,
        larkx_credentials_path=larkx_file,
    )
    sess = FeishuWebSession(
        region="feishu",
        cookies={
            "session": raw_cookie_secret,
            "passport_web_did": "did-12345",
            "swp_csrf_token": raw_csrf_secret,
        },
        saved_at=1760000000.0,
        device_id="did-12345",
        csrf_token=raw_csrf_secret,
        web_app_id="161471",
        user_id="7123456789012345678",
        name="王泽华",
        open_id="ou_owner_wzh",
    )

    # 1. Verify repr(), str(), and to_redacted_dict() NEVER leak cookie values or csrf_token
    assert raw_cookie_secret not in repr(sess)
    assert raw_cookie_secret not in str(sess)
    assert raw_csrf_secret not in repr(sess)
    redacted = sess.to_redacted_dict()
    assert redacted["cookies"] == REDACTED_SECRET
    assert redacted["csrf_token"] == REDACTED_SECRET
    assert raw_cookie_secret not in json.dumps(redacted)
    assert raw_csrf_secret not in json.dumps(redacted)

    # 2. Verify save() writes 0600 permissions and encrypts secrets at rest
    saved = store.save(sess)
    assert saved is not None
    assert session_file.is_file()
    assert key_file.is_file()
    assert stat.S_IMODE(os.stat(session_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600

    on_disk_text = session_file.read_text(encoding="utf-8")
    assert raw_cookie_secret not in on_disk_text
    assert raw_csrf_secret not in on_disk_text
    on_disk_json = json.loads(on_disk_text)
    assert "cookies_enc" in on_disk_json
    assert "cookies" not in on_disk_json

    # 3. Verify load() decrypts accurately
    loaded = store.load()
    assert loaded is not None
    assert loaded.cookies["session"] == raw_cookie_secret
    assert loaded.effective_csrf_token() == raw_csrf_secret
    assert loaded.user_id == "7123456789012345678"
    assert loaded.name == "王泽华"

    # 4. Verify clear() removes the file and import_from_larkx() imports ~/.larkx/credentials.json
    assert store.clear() is True
    assert store.load() is None

    larkx_file.write_text(
        json.dumps(
            {
                "region": "feishu",
                "cookies": {
                    "session": "larkx-imported-session-777",
                    "passport_web_did": "did-from-larkx",
                    "swp_csrf_token": "csrf-from-larkx",
                },
                "saved_at": 1760000123,
                "device_id": "did-from-larkx",
                "csrf_token": "csrf-from-larkx",
                "web_app_id": "161471",
                "user_id": "7999888777666555444",
                "name": "LarkX User",
            }
        ),
        encoding="utf-8",
    )
    imported = store.import_from_larkx()
    assert imported is not None
    assert imported.source == "larkx_import"
    assert imported.user_id == "7999888777666555444"
    assert imported.name == "LarkX User"
    assert imported.cookies["session"] == "larkx-imported-session-777"


@pytest.mark.asyncio
async def test_qr_login_state_machine_waiting_done_and_expired(tmp_path: Path) -> None:
    """Verify QR login init -> waiting -> done (with profile + ticket + appKey) and expiry handling."""
    store = FeishuWebSessionStore(
        path=tmp_path / "qr_session.json",
        key_path=tmp_path / "qr_session.key",
    )
    poll_attempt = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_attempt
        url = str(request.url)
        if url.endswith("/accounts/qrlogin/init"):
            return httpx.Response(
                200,
                headers={
                    "x-flow-key": "flow-init-1",
                    "set-cookie": "passport_web_did=did-qr-1; Path=/",
                },
                json={"code": 0, "data": {"step_info": {"token": "qr-tok-abc"}}},
            )
        if url.endswith("/accounts/qrlogin/polling"):
            poll_attempt += 1
            assert request.headers.get("x-flow-key") == "flow-init-1"
            if poll_attempt == 1:
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"step_info": {"status": 1}}},
                )
            if poll_attempt == 2:
                return httpx.Response(
                    200,
                    headers={
                        "set-cookie": "session=sess-live-qr-2; Path=/, swp_csrf_token=csrf-qr-2; Path=/",
                    },
                    json={"code": 0, "data": {"step_info": {"status": 2}}},
                )
            return httpx.Response(
                200,
                json={"code": 0, "data": {"step_info": {"status": 4}}},
            )
        if "/suite/passport/frontier_ticket/" in url:
            assert "session=sess-live-qr-2" in (request.headers.get("cookie") or "")
            return httpx.Response(200, json={"ticket": "frontier-ticket-xyz"})
        if "/accounts/web/user?app_id=12" in url:
            # 19-digit integer ID in raw JSON to verify precision preservation
            raw_body = '{"code":0,"data":{"user":{"id":7123456789012345679,"name":"扫码用户"}}}'
            return httpx.Response(
                200,
                content=raw_body.encode("utf-8"),
                headers={"content-type": "application/json"},
            )
        if "open-dev.feishu.cn/messenger/" in url:
            html = b'<html><script>window.cfg = {teaAppId: "161471", appKey: "key_messenger_99" };</script></html>'
            return httpx.Response(200, content=html)
        raise AssertionError(f"Unexpected URL: {url}")

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        manager = FeishuWebLoginManager(
            store,
            client_factory=lambda s: FeishuWebClient(s, http_client=mock_http),
        )
        qr_session = await manager.start_login(region="feishu")
        assert qr_session.token == "qr-tok-abc"
        assert qr_session.qr_content == '{"qrlogin":{"token":"qr-tok-abc"}}'

        # First poll: waiting
        st1, sess1, _ = await manager.poll_login(login_id=qr_session.login_id)
        assert st1 == "waiting"
        assert sess1 is None

        # Second poll: done + persisted
        st2, sess2, _ = await manager.poll_login(login_id=qr_session.login_id)
        assert st2 == "done"
        assert sess2 is not None
        assert sess2.user_id == "7123456789012345679"
        assert sess2.name == "扫码用户"
        assert sess2.web_app_id == "161471"
        assert sess2.app_key == "key_messenger_99"

        status_dict = await manager.astatus_dict(validate=True)
        assert status_dict["has_session"] is True
        assert status_dict["valid"] is True
        assert status_dict["user_id"] == "7123456789012345679"
        assert status_dict["cookies"] == REDACTED_SECRET

        # Third poll on a new login when phone cancels (status=4) -> expired
        qr_expired = await manager.start_login(region="feishu")
        st3, sess3, _ = await manager.poll_login(login_id=qr_expired.login_id)
        assert st3 == "expired"
        assert sess3 is None
    finally:
        await mock_http.aclose()


@pytest.mark.asyncio
async def test_web_session_1v1_dm_and_group_ai_command_in_place_reply_and_uncertain_delivery(
    tmp_path: Path,
) -> None:
    """Verify /ai in 1v1 DM (no bot in chat, no send_as_user scope) and group chat via web_session, plus UncertainDelivery duplicate suppression."""
    token_store = FeishuUserTokenStore(
        path=tmp_path / "tokens.json",
        key_path=tmp_path / "tokens.key",
    )
    # User has an OpenAPI token WITHOUT im:message.send_as_user and no bot in the 1v1 DM
    token_store.save_oauth_token(
        open_id="ou_owner_1v1",
        app_id="cli_no_send_scope",
        access_token="u-token-readonly",
        refresh_token="ur-token-readonly",
        user_name="王泽华",
        expires_in=7200,
        scope="im:message:readonly im:chat:read",
    )

    web_store = FeishuWebSessionStore(
        path=tmp_path / "web_session.json",
        key_path=tmp_path / "web_session.key",
    )
    web_store.save(
        FeishuWebSession(
            region="feishu",
            cookies={"session": "web-sess-cookie-1v1", "passport_web_did": "did-1v1"},
            saved_at=time.time(),
            device_id="did-1v1",
            csrf_token="csrf-1v1",
            web_app_id="161471",
            user_id="7111222333444555666",
            name="王泽华",
            open_id="ou_owner_1v1",
        )
    )

    # Track gateway commands received
    gateway_calls: list[tuple[int, dict[str, Any]]] = []
    sent_messages: list[dict[str, Any]] = []
    reactions_added: list[tuple[str, str]] = []
    reactions_deleted: list[tuple[str, str]] = []
    fail_final_put_message_network = False

    dm_chat_id = "7300000000000000101"
    peer_msg_bytes = encode(
        "lark_entities_TextContent",
        {"text": "请问这个 PR https://github.com/org/repo/pull/88 可以合入吗？"},
    )
    trigger_msg_bytes = encode(
        "lark_entities_TextContent",
        {"text": "/ai 结合上下文简短回复可以合入"},
    )

    def gateway_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert "/im/gateway/" in url, f"Expected web gateway call, got {url}"
        pkt = decode("Packet", request.content)
        cmd_id = int(pkt.get("cmd") or 0)
        inner_raw = base64.b64decode(pkt.get("payload") or "")

        if cmd_id == 1000:  # PullFeedCardsRequest
            req_dec = decode("lark_feed_PullFeedCardsRequest", inner_raw)
            gateway_calls.append((cmd_id, req_dec))
            resp_bytes = encode(
                "lark_feed_PullFeedCardsResponse",
                {
                    "cards": [
                        {
                            "id": dm_chat_id,
                            "type": CARD_CHAT,
                            "updateTimeMs": int(time.time() * 1000),
                        }
                    ],
                    "chats": {
                        dm_chat_id: {
                            "id": dm_chat_id,
                            "type": CHAT_P2P,
                            "name": "Peer Colleague",
                            "lastMessagePosition": 2,
                            "lastMessageId": "7400000000000000202",
                            "memberCount": 2,
                        }
                    },
                    "messages": {},
                    "nextCursor": 0,
                },
            )
        elif cmd_id == 64:  # PullChatsByIdsRequest
            req_dec = decode("lark_chats_PullChatsByIdsRequest", inner_raw)
            gateway_calls.append((cmd_id, req_dec))
            resp_bytes = encode(
                "lark_chats_PullChatsByIdsResponse",
                {
                    "chats": {
                        dm_chat_id: {
                            "id": dm_chat_id,
                            "type": CHAT_P2P,
                            "name": "Peer Colleague",
                            "lastMessagePosition": 2,
                            "lastMessageId": "7400000000000000202",
                            "memberCount": 2,
                        }
                    }
                },
            )
        elif cmd_id == 43:  # PullMessagesByPositionsRequest
            req_dec = decode("lark_messages_PullMessagesByPositionsRequest", inner_raw)
            gateway_calls.append((cmd_id, req_dec))
            now_s = int(time.time())
            resp_bytes = encode(
                "lark_messages_PullMessagesByPositionsResponse",
                {
                    "messages": {
                        1: {
                            "id": "7400000000000000201",
                            "type": TYPE_TEXT,
                            "fromId": "7999000111222333444",
                            "createTime": now_s - 20,
                            "content": base64.b64encode(peer_msg_bytes).decode("ascii"),
                            "chatId": dm_chat_id,
                            "position": 1,
                        },
                        2: {
                            "id": "7400000000000000202",
                            "type": TYPE_TEXT,
                            "fromId": "7111222333444555666",
                            "createTime": now_s - 5,
                            "content": base64.b64encode(trigger_msg_bytes).decode("ascii"),
                            "chatId": dm_chat_id,
                            "position": 2,
                        },
                    }
                },
            )
        elif cmd_id == 8:  # PullMessagesByIdsRequest
            req_dec = decode("lark_messages_PullMessagesByIdsRequest", inner_raw)
            gateway_calls.append((cmd_id, req_dec))
            now_s = int(time.time())
            resp_bytes = encode(
                "lark_messages_PullMessagesByIdsResponse",
                {
                    "messages": {
                        "7400000000000000202": {
                            "id": "7400000000000000202",
                            "type": TYPE_TEXT,
                            "fromId": "7111222333444555666",
                            "createTime": now_s - 5,
                            "content": base64.b64encode(trigger_msg_bytes).decode("ascii"),
                            "chatId": dm_chat_id,
                            "position": 2,
                        }
                    }
                },
            )
        elif cmd_id == 25:  # PutReactionRequest
            req_dec = decode("reactions_PutReactionRequest", inner_raw)
            gateway_calls.append((cmd_id, req_dec))
            reactions_added.append((req_dec["messageId"], req_dec["reactionType"]))
            resp_bytes = encode("reactions_PutReactionResponse", {"count": 1})
        elif cmd_id == 26:  # DeleteReactionRequest
            req_dec = decode("reactions_DeleteReactionRequest", inner_raw)
            gateway_calls.append((cmd_id, req_dec))
            reactions_deleted.append((req_dec["messageId"], req_dec["reactionType"]))
            resp_bytes = encode("reactions_DeleteReactionResponse", {})
        elif cmd_id == 5:  # PutMessageRequest
            req_dec = decode("lark_messages_PutMessageRequest", inner_raw)
            gateway_calls.append((cmd_id, req_dec))
            sent_messages.append(req_dec)
            # If testing uncertain delivery on the final answer (second PutMessageRequest for that run)
            if fail_final_put_message_network and len(sent_messages) >= 2:
                raise httpx.ReadTimeout("Simulated post-submit timeout on PutMessageRequest")
            sent_id = f"740000000000000900{len(sent_messages)}"
            resp_bytes = encode(
                "lark_messages_PutMessageResponse",
                {
                    "message": {
                        "id": sent_id,
                        "type": req_dec.get("type", TYPE_POST),
                        "fromId": "7111222333444555666",
                        "createTime": int(time.time()),
                        "chatId": req_dec.get("chatId", dm_chat_id),
                        "parentId": req_dec.get("parentId", ""),
                        "rootId": req_dec.get("rootId", ""),
                        "position": 2 + len(sent_messages),
                    }
                },
            )
        else:
            raise AssertionError(f"Unexpected gateway cmd {cmd_id}")

        out_pkt = encode(
            "Packet",
            {
                "payloadType": 1,
                "cmd": cmd_id,
                "status": 0,
                "cid": pkt.get("cid") or "cid-resp",
                "payload": base64.b64encode(resp_bytes).decode("ascii"),
            },
        )
        return httpx.Response(200, content=out_pkt)

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(gateway_handler))
    dispatched_prompts: list[str] = []

    async def fake_dispatcher(
        tab_id: str,
        prompt: str,
        turn_id: str,
        visible_text: str,
        metadata: Mapping[str, Any],
    ) -> str:
        dispatched_prompts.append(prompt)
        return "PR #88 检查通过，可以合入。"

    try:
        service = FeishuAiCommandService(
            state_path=tmp_path / "ai_state.json",
            token_store=token_store,
            web_session_store=web_store,
            dedup_store=FeishuMessageDedupStore(tmp_path / "dedup.json"),
            user_client_factory=lambda cfg: FeishuUserClient(
                token_store,
                open_id=cfg.owner_open_id or None,
                app_id=cfg.app_id or None,
                client=mock_http,
                web_session_store=web_store,
                send_mode=cfg.send_mode,
            ),
            tab_creator=lambda **kw: "tab-web-1v1",
            tab_getter=lambda tid: type("FakeTab", (), {"id": tid, "archived": False, "env": {}})(),
            tab_env_switcher=lambda tid, env: None,
            turn_dispatcher=fake_dispatcher,
        )
        service.update_config({"enabled": True, "send_mode": "auto"})

        # Verify preflight auto-selects web_session because OpenAPI token lacks im:message.send_as_user
        preflight = await service.apreflight_status()
        assert preflight.can_send is True
        assert preflight.can_send_as_user is True
        assert preflight.send_mode == "web_session"
        assert preflight.has_web_session is True
        assert preflight.web_user_id == "7111222333444555666"

        poller = FeishuAiPoller(service)
        processed = await poller.poll_once()
        await poller.wait_for_inflight(timeout=5.0)

        assert processed == ["7400000000000000202"]
        assert len(dispatched_prompts) == 1
        assert "https://github.com/org/repo/pull/88" in dispatched_prompts[0]

        # Verify reactions: OnIt added, then OnIt deleted and DONE added
        assert ("7400000000000000202", REACT_ON_IT) in reactions_added
        assert ("7400000000000000202", REACT_DONE) in reactions_added
        assert ("7400000000000000202", REACT_ON_IT) in reactions_deleted

        # Verify 2 PutMessageRequest calls (interim + final) sent in-place to the 1v1 DM as user
        assert len(sent_messages) == 2
        for sent_req in sent_messages:
            assert sent_req["chatId"] == dm_chat_id
            assert sent_req["parentId"] == "7400000000000000202"
            assert sent_req["rootId"] == "7400000000000000202"
            # In a 1v1 P2P DM (not in a topic), replies stay in the main DM stream (isReplyInThread=False)
            assert bool(sent_req.get("isReplyInThread", False)) is False
            inner_text = sent_req["content"]["richText"]["innerText"]
            assert "🤖 AI 代答，仅供参考" in inner_text

        final_inner = sent_messages[1]["content"]["richText"]["innerText"]
        assert "PR #88 检查通过，可以合入。" in final_inner

        # Now test UncertainDelivery duplicate suppression on post-submit timeout
        sent_messages.clear()
        fail_final_put_message_network = True
        msg_uncertain = await service.build_user_client().get_message("7400000000000000202")
        assert msg_uncertain is not None
        msg_uncertain = type(msg_uncertain)(
            **{**msg_uncertain.to_dict(), "message_id": "7400000000000000999"}
        )
        res_uncertain = await service.handle_message(msg_uncertain, source="poller")
        assert res_uncertain.status == "failed"
        assert "delivery may have occurred" in (res_uncertain.reason or "")
        assert len(sent_messages) == 2  # interim + 1 final attempt (no duplicate retry!)

        # Replay of the same message_id must be suppressed as a duplicate
        res_replay = await service.handle_message(msg_uncertain, source="poller")
        assert res_replay.handled is False
        assert res_replay.status == "duplicate"
        assert len(sent_messages) == 2
    finally:
        await mock_http.aclose()


def test_cli_and_api_web_session_routes_and_send_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify REST API (/api/feishu-bot/ai-command/web-session/*) and CLI (web-login, web-status, web-logout, --send-mode)."""
    token_store = FeishuUserTokenStore(
        path=tmp_path / "api_tokens.json",
        key_path=tmp_path / "api_tokens.key",
    )
    web_store = FeishuWebSessionStore(
        path=tmp_path / "api_web_session.json",
        key_path=tmp_path / "api_web_session.key",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/accounts/qrlogin/init"):
            return httpx.Response(
                200,
                headers={"x-flow-key": "flow-cli-1"},
                json={"code": 0, "data": {"step_info": {"token": "tok-cli-qr-1"}}},
            )
        if url.endswith("/accounts/qrlogin/polling"):
            return httpx.Response(
                200,
                headers={
                    "set-cookie": "session=sess-cli-ok; Path=/, passport_web_did=did-cli; Path=/"
                },
                json={"code": 0, "data": {"step_info": {"status": 2}}},
            )
        if "/suite/passport/frontier_ticket/" in url:
            return httpx.Response(200, json={"ticket": "t-cli-1"})
        if "/accounts/web/user?app_id=12" in url:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"user": {"id": "7888999000111222333", "name": "CLI User"}},
                },
            )
        if "open-dev.feishu.cn/messenger/" in url:
            return httpx.Response(200, content=b'teaAppId: "161471", appKey: "k1" ')
        return httpx.Response(200, json={})

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = FeishuWebLoginManager(
        web_store,
        client_factory=lambda s: FeishuWebClient(s, http_client=mock_http),
    )
    service = FeishuAiCommandService(
        state_path=tmp_path / "api_ai_state.json",
        token_store=token_store,
        web_session_store=web_store,
        web_login_manager=manager,
        dedup_store=FeishuMessageDedupStore(tmp_path / "api_dedup.json"),
    )
    poller = FeishuAiPoller(service)

    test_app = FastAPI()
    r_bot = APIRouter(prefix="/api/feishu/bot")
    r_alias = APIRouter(prefix="/api/feishu-bot")
    register_ai_command_routes(
        r_bot,
        require_hub_user=lambda: {"open_id": "ou_test_user"},
        service_getter=lambda: service,
        poller_getter=lambda: poller,
    )
    register_ai_command_routes(
        r_alias,
        require_hub_user=lambda: {"open_id": "ou_test_user"},
        service_getter=lambda: service,
        poller_getter=lambda: poller,
    )
    test_app.include_router(r_bot)
    test_app.include_router(r_alias)
    client = TestClient(test_app)
    if True:
        # 1. Start QR login via /api/feishu-bot/ai-command/web-session/login/start
        r_start = client.post(
            "/api/feishu-bot/ai-command/web-session/login/start",
            json={"region": "feishu"},
        )
        assert r_start.status_code == 200
        start_data = r_start.json()
        assert start_data["status"] == "waiting"
        login_id = start_data["login"]["login_id"]
        assert start_data["login"]["qr"]["size"] == 29
        assert "█" in start_data["login"]["qr_unicode"]

        # 2. Poll QR login via /api/feishu-bot/ai-command/web-session/login/poll
        r_poll = client.post(
            "/api/feishu-bot/ai-command/web-session/login/poll",
            json={"login_id": login_id},
        )
        assert r_poll.status_code == 200
        poll_data = r_poll.json()
        assert poll_data["login_status"] == "done"
        assert poll_data["web_session_status"]["has_session"] is True
        assert poll_data["web_session_status"]["user_id"] == "7888999000111222333"
        assert poll_data["web_session_status"]["cookies"] == REDACTED_SECRET
        assert poll_data["token_status"]["send_mode"] == "web_session"

        # 3. Check CLI commands (web-status, configure --send-mode, web-logout, web-login --import-larkx)
        class _TestClientBridge:
            def _request(
                self,
                method: str,
                path: str,
                *,
                params: dict[str, Any] | None = None,
                json: Any = None,
            ) -> Any:
                resp = client.request(method, path, params=params, json=json)
                assert resp.status_code < 400, resp.text
                return resp.json()

        monkeypatch.setattr(
            "claude_hub.cli.main.get_client",
            lambda _ctx: _TestClientBridge(),
        )
        runner = CliRunner()

        res_status = runner.invoke(cli, ["feishu", "ai-command", "web-status", "--validate"])
        assert res_status.exit_code == 0, res_status.output
        status_json = json.loads(res_status.output)
        assert status_json["web_session_status"]["valid"] is True

        res_cfg = runner.invoke(
            cli,
            ["feishu", "ai-command", "configure", "--send-mode", "web_session"],
        )
        assert res_cfg.exit_code == 0, res_cfg.output
        cfg_json = json.loads(res_cfg.output)
        assert cfg_json["config"]["send_mode"] == "web_session"

        res_logout = runner.invoke(cli, ["feishu", "ai-command", "web-logout"])
        assert res_logout.exit_code == 0, res_logout.output
        logout_json = json.loads(res_logout.output)
        assert logout_json["cleared"] is True
        assert logout_json["web_session_status"]["has_session"] is False

        # Test web-login --no-wait prints QR and returns JSON
        res_login_nw = runner.invoke(cli, ["feishu", "ai-command", "web-login", "--no-wait"])
        assert res_login_nw.exit_code == 0, res_login_nw.output
        assert "login" in res_login_nw.output

        # Test web-login --import-larkx
        larkx_tmp = tmp_path / "cli_import_larkx.json"
        larkx_tmp.write_text(
            json.dumps(
                {
                    "region": "feishu",
                    "cookies": {
                        "session": "sess-from-cli-larkx",
                        "passport_web_did": "did-cli-larkx",
                    },
                    "user_id": "7666555444333222111",
                    "name": "Imported LarkX",
                }
            ),
            encoding="utf-8",
        )
        res_imp = runner.invoke(
            cli,
            [
                "feishu",
                "ai-command",
                "web-login",
                "--import-larkx",
                "--larkx-path",
                str(larkx_tmp),
            ],
        )
        assert res_imp.exit_code == 0, res_imp.output
        imp_json = json.loads(res_imp.output)
        assert imp_json["imported"] is True
        assert imp_json["web_session_status"]["user_id"] == "7666555444333222111"

        # Test send_mode="openapi" ignores active web_session and preserves OpenAPI / bot_fallback behavior
        r_openapi = client.patch(
            "/api/feishu/bot/ai-command",
            json={"send_mode": "openapi"},
        )
        assert r_openapi.status_code == 200
        openapi_data = r_openapi.json()
        assert openapi_data["config"]["send_mode"] == "openapi"
        assert openapi_data["token_status"]["has_web_session"] is False
        assert openapi_data["token_status"]["send_mode"] == "none"

        # Test group chat reply in thread when send_mode="web_session"
        assert feishu_host("internal-api-lark-api.feishu.cn") is True
        assert feishu_host("evil-feishu.cn") is False
        jar: dict[str, dict[str, str]] = {}
        carry_cookies(jar, "https://bytedance.feishu.cn/messenger/", {"session": "s1"})
        assert jar["bytedance.feishu.cn"]["session"] == "s1"
