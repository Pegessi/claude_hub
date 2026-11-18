# Working Log: Feishu `/ai` Bot Fallback Without `im:message.send_as_user` (2026-11-18)

## Problem & Investigation

When authenticating `lark-cli` for the personal Feishu `/ai` command on `cli_a9f715f8d6b89bcb` (`MacWorker`) / `cli_a943e6081bf8dbcd` (`MerlinTed`) / `cli_aa47b1efcd78dbdb` (`包老师`), Feishu device verification rejected `im:message.send_as_user` because that user-identity send scope is not enabled in the Feishu Developer Console.

### Verified Findings (Sanitized Probe & Codebase Evidence)

1. **Direct Feishu OpenAPI & `lark-cli` behavior with `user_access_token` (`im:message` only)**:
   - **Local `lark-cli` keychain & binary inspection**:
     - `~/.lark-cli/config.json` and `~/.local/share/lark-cli/` contain encrypted user tokens (`token_cli_a9f715f8d6b89bcb_ou_a24354b644a3bef61a41bd05f07c1aac.enc` with 75 granted user scopes including `im:message`, `im:chat:read`, `im:chat.members:write_only`, and `im:message.reactions:write_only`; `token_cli_aa47b1efcd78dbdb_ou_e6c73de7313bf222d1951de75ef2c2ba.enc` with 210 granted user scopes) and AES-256-GCM encrypted bot secrets (`appsecret_cli_a9f715f8d6b89bcb.enc`, `appsecret_cli_aa47b1efcd78dbdb.enc`).
     - Inspecting the `lark-cli` v1.1.0 binary (`~/.nvm/versions/node/v24.21.0/lib/node_modules/@larksuite/cli/bin/lark-cli`) confirmed that `lark-cli im +messages-send --as user` and `im +messages-reply --as user` call `POST /open-apis/im/v1/messages` and `POST /open-apis/im/v1/messages/:message_id/reply` with `user_access_token`, and that `im:message.send_as_user` is explicitly placed in `lark-cli`'s internal `recommend.deny` scope list (`cmd/lark-cli/scope_DefaultScopeSet.go`).
   - **Sanitized live OpenAPI probe receipts**:
     - Calling `POST https://open.feishu.cn/open-apis/im/v1/messages/om_invalid_dry_run_probe/reply` with the live `user_access_token` (`cli_a9f715f8d6b89bcb`, 75 scopes including `im:message` but without `im:message.send_as_user`) returned HTTP 400 before message ID validation:
       ```json
       {
         "code": 230027,
         "msg": "Lack of necessary permissions, ext=requires im:message.send_as_user scope.",
         "error": {
           "permission_violations": [
             {"type": "action_scope_required", "subject": "im:message.send_as_user"}
           ]
         }
       }
       ```
     - Querying `GET https://open.feishu.cn/open-apis/application/v6/scopes` with each app's `tenant_access_token` confirmed that none of `cli_a9f715f8d6b89bcb` (`MacWorker`, 284 active scopes), `cli_aa47b1efcd78dbdb` (`包老师`, 666 active scopes), or `cli_a943e6081bf8dbcd` (`MerlinTed`, 7 active scopes) has `im:message.send_as_user` enabled in the Feishu Developer Console, whereas all three apps have `im:message:send_as_bot` (`scope_status=1`) active.

2. **How internal `openlark` / `lark-desk` / `lark-bridge` send `> 🤖 AI 代答，仅供参考` replies**:
   - The public GitHub Rust crate `foxzool/openlark` is an unrelated third-party OpenAPI SDK wrapper.
   - Internal ByteDance Codebase (`code.byted.org`) repositories producing the `> 🤖 AI 代答，仅供参考` personal reply footer were verified at exact immutable commit SHAs:
     - `hejiang.110/hejiang.110` (`commit=157b2fe33e7818211c7d4242d227dcad24633149`, `branch=master`, file `mew/plugin/openlark-standin/src/channel.ts` lines 76–135 and `mew/plugin/openlark-standin/skills/openlark/SKILL.md` lines 1–90): connects to a local **OpenLark** desktop daemon (`http://127.0.0.1:8081/agent/v1` or `http://127.0.0.1:8095/agent/v1`) and posts replies via `POST /agent/v1/messages/:messageId/replies` (`client.replyMessage(targetMessageId, { type: "post", text: replyText })`).
     - `suyue.rd/persona-extensions` (`commit=a17d5840f32f5f732c9eb1747b567aae14452642`, `branch=main`, file `extensions/lark-desk/index.ts` lines 1–48 and `extensions/lark-desk/src/send.ts` lines 1–95) and `byteapi/persona-plugins` (`commit=2819a96f2322d4edc67ebbe4643a7a78428c1a88`, `branch=master`, file `extensions/lark-bridge/index.ts` lines 1–65 and `extensions/lark-bridge/README.md`): use `lark-cli` strictly for reading/searching messages while sending personal replies via **`LarkAgentX`** (`https://internal-api-lark-api.feishu.cn/im/gateway/`, command `PUT_MESSAGE=5` protobuf over an authenticated Feishu web session stored in `~/.larkx/credentials.json` via `larkx login` QR scan).
   - **Chosen architectural strategy for Claude Hub**:
     - Neither the local OpenLark desktop daemon (`127.0.0.1:8081`/`8095`) nor `~/.larkx/credentials.json` is installed on this host, whereas the user already has valid `lark-cli` user tokens (`im:message`, `im:message.reactions:write_only`) and bot `app_secret` credentials (`im:message:send_as_bot`) for the bound app.
     - Claude Hub therefore uses a **hybrid single-app user-read/react + bot-send fallback**: when `im:message.send_as_user` is granted, `/ai` sends directly with `user_access_token` (`send_mode="user"`); when `im:message.send_as_user` is absent, `/ai` reads chat context and posts/removes `OnIt`/`DONE` reactions as the authenticated user via `user_access_token`, and sends the interim/final `/ai` reply via the single bound app's bot `tenant_access_token` (`send_mode="bot_fallback"`).

## Solution & Safety Boundaries

- **Scope preflight & token import (`backend/claude_hub/services/feishu_user_token_store.py`)**:
  - Removed `im:message.send_as_user` from `RECOMMENDED_AI_COMMAND_SCOPES` so `lark-cli auth login` commands succeed on apps without `im:message.send_as_user`.
  - Added `_pool_bot_secret_state()`, `resolve_app_secret_for_app()`, and `resolve_fallback_bot_configs()` on `FeishuUserTokenStore` to resolve the bot `app_secret` strictly for the single selected/authorized `/ai` app (`record.app_id` or `preferred_app_id`) from the token record, `lark-cli` encrypted keychain (`appsecret_<appId>.enc`), or an enabled `FeishuBotPoolStore` bot.
  - **Revocation tombstone & fail-closed pool state (`backend/claude_hub/services/bot_pool/store.py`, `feishu_user_token_store.py`)**: `FeishuBotPoolStore.delete_bot()` preserves `app_id` on secret-free tombstones (`revoked_at`), and `_pool_bot_secret_state()` checks both active pool bots and raw tombstoned entries while failing closed (`(False, "")`) on any `FeishuBotPoolStore` read/state error so disabled, deleted, or unreadable pool bots never re-enable cached secrets.
- **Single-identity reply receipt validation & post-submit uncertain-delivery normalization (`backend/claude_hub/services/feishu_bot.py`, `backend/claude_hub/services/feishu_user_client.py`, `backend/claude_hub/services/feishu_ai_command.py`)**:
  - `FeishuBotClient._send_message_payload()`, `send_message_with_identity()`, and `_reply_message_payload()` normalize post-submit transport/protocol failures (`httpx.ReadError`, `httpx.WriteError`, `httpx.RemoteProtocolError`, `httpx.DecodingError`), HTTP 200 malformed JSON responses, and HTTP 200 responses with missing `code == 0` into `FeishuDeliveryUncertainError`, while preserving `FeishuBotRemoteError` / `httpx.HTTPStatusError` for definite HTTP 4xx/5xx or Feishu error-code rejections.
  - `FeishuUserClient.reply_as_user()` and `_reply_via_bot_fallback()` validate that `FeishuReplyReceipt.message_id` is a non-empty string via `_require_reply_receipt_identity()`, raising `FeishuDeliveryUncertainError` when Feishu returns HTTP 200 without a `message_id`.
  - `FeishuAiCommandService.handle_message()` validates interim and final reply receipts, never switches identities after ambiguous delivery errors, and suppresses duplicate error fallback replies and `DONE` reactions when final reply delivery is uncertain (`FeishuDeliveryUncertainError`, `httpx.TimeoutException`, `httpx.ReadError`, `httpx.WriteError`, `httpx.RemoteProtocolError`, `httpx.DecodingError`).
  - Default `auto_invite_bot_to_chat=False` prevents unrequested group membership mutations on `230002`.
- **Truthful Settings UI & browser acceptance (`frontend/src/features/feishu-bot/FeishuBotSettingsDialog.vue`, `frontend/tests/feishuBotSettingsBehavior.test.mjs`, `frontend/tests/browser_feishu_bot_components.py`)**:
  - `FeishuScopePreflightResult` separates `can_send_as_user` (`has_send_as_user_scope`) from `can_send` / `can_send_via_bot` / `send_mode` (`"user" | "bot_fallback" | "none"`).
  - `FeishuBotSettingsDialog.vue` renders `Scope ready` (`send_mode="user"`), `Bot fallback (<fallback_bot_app_id>)` (`send_mode="bot_fallback"`, displaying both the user identity and fallback bot app ID), and `Missing send_as_user` (`send_mode="none"`, surfacing `remediation_steps[0]` and `auth_login_command`), verified by unit tests and the real Playwright browser harness across desktop (`1280x900`), narrow (`720x900`), and mobile (`390x844`) viewports.
