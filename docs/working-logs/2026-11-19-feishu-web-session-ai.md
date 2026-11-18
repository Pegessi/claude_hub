# 2026-11-19: Feishu Web-Session (`larkweb` / `LarkAgentX`) User-Identity Transport for `/ai`

## Background & Problem

In Feishu OpenAPI, replying in-place as the authenticated user requires the restricted `im:message.send_as_user` permission, and bot tenant fallback (`bot_fallback`) requires a bot to be a member of the chat (which is impossible in 1v1 DMs between two human users and often unavailable in external or restricted group chats).

The internal `lark-desk` / `LarkAgentX` web-session protocol (`[redacted]` `extensions/lark-desk/internal/web/*` and `[redacted]` `extensions/lark-bridge/larkx_persona/*`) authenticates via a standard Feishu web QR scan (`POST /accounts/qrlogin/init` and `/accounts/qrlogin/polling`) and communicates with `POST https://internal-api-lark-api.feishu.cn/im/gateway/` using `proto2` `Packet` frames (`X-Command`, `X-Command-Version: 5.7.0`) and `wss://msg-frontier.feishu.cn/ws/v2`. Because the web gateway acts as the signed-in human user, `PutMessageRequest` (`cmd=5`), `PutReactionRequest` (`cmd=25`), `DeleteReactionRequest` (`cmd=26`), `PullFeedCardsRequest` (`cmd=1000`), and `PullMessagesByPositionsRequest` (`cmd=43`) work in **any** 1v1 DM or group chat without `im:message.send_as_user` or a bot in the chat.

## Design & Changes

1. **Native Python Feishu Web-Session Package (`backend/claude_hub/services/feishu_web_session/`)**:
   - `protocol_schema.json` & `codec.py`: Pure-Python `proto2` wire encoder/decoder (`encode`, `decode`, `command_of`) covering the full `lark-desk` gateway command and entity schema (`Packet`, `Frame`, `PushMessagesRequest`, `PutMessageRequest`, `PullMessagesByIdsRequest`, `PullMessagesByPositionsRequest`, `PullChatsByIdsRequest`, `PullChattersByIdsRequest`, `PullChatChattersRequest`, `CheckP2PChatsExistByUserRequest`, `PutChatRequest`, `PullFeedCardsRequest`, `PutReactionRequest`, `DeleteReactionRequest`, `PullThreadsByIDsRequest`, `PullThreadMessagesByPositionsRequest`, `RichText`, etc.) with zero external C/protobuf runtime dependencies.
   - `rich.py`: Rich-text element/block builder (`Node`, `Block`, `rich_text`, `rich_blocks`, `markdown_blocks`, `formatted_out`) and message content extractor (`describe`, `text_of`, `text_without`, `card_text`, `meeting_text`, `system_text`, `extract_text_and_title_from_openapi_content`).
   - `qr.py`: Pure-Python ISO/IEC 18004 Byte-mode QR Code matrix generator (`encode_qr_matrix`, `render_qr_unicode`, `render_qr_svg_data_url`), verified bit-for-bit against Node `qrcode`.
   - `store.py`: `FeishuWebSession` and `FeishuWebSessionStore` with AES-256-GCM encryption at rest, `0600` file permissions, strict secret redaction (`[REDACTED]`) across `repr()`, `str()`, and API/CLI status serialization, plus `import_from_larkx()` for `~/.larkx/credentials.json`.
   - `client.py`: `FeishuWebClient` (QR login `start_login` / `poll_login`, `ticket` validation, 64-bit safe `profile` extraction, cross-tenant cookie redirect propagation, gateway `call`, `send`, `react`, `unreact`, `feed_cards`, `messages_at`, `messages_by_id`) and `FeishuWebFrontierListener` (`msg-frontier` WebSocket frame/gzip push decoding, ACKs, and 30s heartbeats). Post-submit transport/decode errors and empty message IDs on `PutMessageRequest` raise `FeishuDeliveryUncertainError` so `/ai` never duplicates an uncertain reply.
   - `adapter.py`: `FeishuWebUserTransport` (maps web gateway chats, messages, reactions, and replies to the `FeishuUserClient` contract, keeping 1v1 P2P DM replies in the main DM stream while respecting thread mode in groups/topics) and `FeishuWebLoginManager`.

2. **Feishu `/ai` Transport Selection (`send_mode`) & CLI / API Lifecycle**:
   - Added `send_mode` (`"auto"` | `"web_session"` | `"openapi"`, default `"auto"`) to `FeishuAiCommandConfig`.
   - In `"auto"` mode, `FeishuUserClient` and `evaluate_scope_preflight()` automatically select `web_session` when a valid web session is active and OpenAPI `im:message.send_as_user` is absent (or when no OpenAPI token is configured), while preserving existing OpenAPI + `bot_fallback` behavior when `web_session` is unconfigured or `send_mode="openapi"`.
   - Added REST API endpoints under both `/api/feishu/bot/ai-command/web-session/*` and `/api/feishu-bot/ai-command/web-session/*` (`GET status`, `POST login/start`, `POST login/poll`, `POST import-larkx`, `POST logout`) and CLI commands (`claude-hub feishu ai-command web-login`, `web-status`, `web-logout`, and `configure --send-mode`).

## Verification

- Focused unit & protocol suite: `./scripts/verify.sh backend-tests -- tests/test_feishu_web_session.py tests/test_feishu_ai_command.py -q` (26 passed).
- Full repository gate: `./scripts/verify.sh all` (9/9 targets passed).
