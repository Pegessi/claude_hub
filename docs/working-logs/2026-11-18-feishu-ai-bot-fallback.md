# Working Log: Feishu `/ai` Bot Fallback Without `im:message.send_as_user` (2026-11-18)

## Problem & Investigation

When authenticating `lark-cli` for the personal Feishu `/ai` command on `cliexample_a30368` (`Example Person`) / `cliexample_e35673` (`Example Person`) / `cliexample_564e44` (`Example Person`), Feishu device verification rejected `im:message.send_as_user` because that user-identity send scope is not enabled in the Feishu Developer Console.

### Verified Findings (Sanitized Probe & Codebase Evidence)

1. **Direct Feishu OpenAPI & `lark-cli` behavior with `user_access_token` (`im:message` only)**:
   - **Local `lark-cli` keychain & binary inspection**:
     - `~/.lark-cli/config.json` and `~/.local/share/lark-cli/` contain encrypted user tokens (`token_cli_a9f715f8d6b89bcb_ou_a24354b644a3bef61a41bd05f07c1aac.enc` with 75 granted user scopes including `im:message`, `im:chat:read`, `im:chat.members:write_only`, and `im:message.reactions:write_only`; `token_cli_aa47b1efcd78dbdb_ou_e6c73de7313bf222d1951de75ef2c2ba.enc` with 210 granted user scopes) and AES-256-GCM encrypted bot secrets (`appsecret_cli_a9f715f8d6b89bcb.enc`, `appsecret_cli_aa47b1efcd78dbdb.enc`).
     - Inspecting the `lark-cli` v1.1.0 binary (`~/.nvm/versions/node/v24.21.0/lib/node_modules/@larksuite/cli/bin/lark-cli`) confirmed that `lark-cli im +messages-send --as user` and `im +messages-reply --as user` call `POST /open-apis/im/v1/messages` and `POST /open-apis/im/v1/messages/:message_id/reply` with `user_access_token`, and that `im:message.send_as_user` is explicitly placed in `lark-cli`'s internal `recommend.deny` scope list (`cmd/lark-cli/scope_DefaultScopeSet.go`).
   - **Sanitized live OpenAPI probe receipts**:
     - Calling `POST https://open.feishu.cn/open-apis/im/v1/messages/om_invalid_dry_run_probe/reply` with the live `user_access_token` (`cliexample_a30368`, 75 scopes including `im:message` but without `im:message.send_as_user`) returned HTTP 400 before message ID validation:
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
     - Querying `GET https://open.feishu.cn/open-apis/application/v6/scopes` with each app's `tenant_access_token` confirmed that none of `cliexample_a30368` (`Example Person`, 284 active scopes), `cliexample_564e44` (`Example Person`, 666 active scopes), or `cliexample_e35673` (`Example Person`, 7 active scopes) has `im:message.send_as_user` enabled in the Feishu Developer Console, whereas all three apps have `im:message:send_as_bot` (`scope_status=1`) active.

2. **How internal `openlark-standin` / `lark-desk` / `lark-bridge` send `> 🤖 AI 代答，仅供参考` replies**:
   - The public GitHub Rust crate `foxzool/openlark` is an unrelated third-party OpenAPI SDK wrapper.
   - Internal ByteDance Codebase (`git.example.invalid`) repositories producing the `> 🤖 AI 代答，仅供参考` personal reply footer were verified by reading the actual files at their pinned commit SHAs:
     - **`openlark-standin` (`example/project` at commit `157b2fe33e7818211c7d4242d227dcad24633149`)**:
       - Verified in `mew/plugin/openlark-standin/README.md` (lines 1–39), `mew/plugin/openlark-standin/runtime/src/openlark.mjs` (lines 3–128), `mew/plugin/openlark-standin/runtime/src/worker.mjs` (lines 157–212), and `mew/plugin/openlark-standin/runtime/src/filter.mjs` (lines 1–4, 103–117).
       - **Read/receive**: Connects to a local **OpenLark** desktop agent API (`config.openlarkBase`, authenticated with `Bearer ${OPEN_LARK_AGENT_TOKEN}` and `X-Open-Lark-Account` header in `openlark.mjs:5-29`), receiving `message.upserted` events over WebSocket `/events?ol_account=<account>` (`openlark.mjs:104-121`) and reading conversations/messages via `GET /conversations/:feed/messages` and `GET /messages/:rootId/thread` (`openlark.mjs:69-103`).
       - **Reaction & reply**: Adds the `OnIt` reaction via `POST /messages/:message_id/reactions` (`worker.mjs:160`), formats the reply with `FOOTER = "🤖 AI 代答，仅供参考"` (`filter.mjs:2, 103-117`), and posts the user-identity reply via `POST /messages/:message_id/replies` (`worker.mjs:205-211`).
     - **`lark-desk` (`example/project` at commit `a17d5840f32f5f732c9eb1747b567aae14452642`)**:
       - Verified in `extensions/lark-desk/README.md` (lines 1–34), `extensions/lark-desk/internal/web/client.go` (lines 31–100), and `extensions/lark-desk/internal/web/messages.go` (lines 200–359).
       - **Read/receive & send**: Does **not** use `lark-cli`; instead, its `larkweb` Go connector (`internal/web`) uses a Feishu web-client QR login session (`Session` cookies/CSRF token in `client.go:31-73`, compatible with the JSON shape that standalone `send-as-user` / `LarkAgentX` stores in `~/.larkx/credentials.json` so an existing hub login can be taken over as-is, `README.md:3`). Incoming DMs, group `@me`, and replies arrive over the Feishu web client's `msg-frontier` WebSocket long connection (`README.md:5, 13`), chat history is pulled via web gateway protobuf commands (`PullMessagesByPositionsRequest` / `PullMessagesByIdsRequest` in `messages.go:200-215`, exposed as `larkweb_chats` / `larkweb_messages` / `larkweb_search`, `README.md:9`), and replies (including `/agent` replies ending with `> 🤖 AI 代答，仅供参考`, `README.md:11, 27`) are sent as the user via `PutMessageRequest` protobuf commands over the web client gateway (`messages.go:315-359`).
     - **`lark-bridge` (`example/project` at commit `2819a96f2322d4edc67ebbe4643a7a78428c1a88`)**:
       - Verified in `extensions/lark-bridge/README.md` (lines 1–68), `extensions/lark-bridge/larkx_persona/openlark.py` (lines 74–231), `extensions/lark-bridge/larkx_persona/real.py` (lines 51–263), and `extensions/lark-bridge/larkx_persona/cli.py` (lines 1–26).
       - **Read/receive & send backends**: Supports two user-identity chat backends selected by setting (`README.md:12-21`):
         1. **Existing local Open Lark (`OpenLarkBackend` in `larkx_persona/openlark.py:74-231`)**: Connects to loopback `http://127.0.0.1:8095/agent/v1` (with Hub-encrypted agent token, `README.md:15-16`), receives `message.upserted` over WebSocket (`openlark.py:180-183`), reads history via `/conversations` (`openlark.py:197-211, 252-269`), and sends/replies as the user via `POST /messages/:reply_to/replies` or `POST /conversations/:chat/messages` (`openlark.py:220-230`).
         2. **Separately installed `LarkAgentX` (`RealBackend` in `larkx_persona/real.py:51-263`)**: Adapts an external `cv-cat/LarkAgentX` checkout (pinned to `97ad48e69e34bd8c78ee30f795b7c3759dd81659`, `README.md:29-36`) for web-protocol WebSocket receive (`real.py:204-240`), history pull (`real.py:254-255`), and user send (`self._client.send_msg`, `real.py:242-243`). Unlike standalone `LarkAgentX`, `lark-bridge` overrides `LarkAuth` with `HubAuth(Path(os.devnull))` (`real.py:109-134`) so QR login credentials are encrypted in Persona Hub and **not** written to `LarkAgentX`'s default `~/.larkx/credentials.json` (`README.md:49`, `real.py:113-133`).
       - **Role of `lark-cli` in `lark-bridge`**: `lark-cli` is invoked only through Persona's controlled `cli.call` (`HostCLI` in `larkx_persona/cli.py:5-21`) for document retrieval (`search(..., kind="docs")`) and login status checks (`auth_status`); it is **not** the chat receive/send channel and never sends messages as the user (`README.md:8, 21, 51, 68`).
   - **Chosen architectural strategy for Claude Hub**:
     - Neither the local OpenLark desktop daemon (`127.0.0.1:8081`/`8095`) nor `LarkAgentX` web-session credentials are installed on this host, whereas the user already has valid `lark-cli` OpenAPI user tokens (`im:message`, `im:message.reactions:write_only`) and bot `app_secret` credentials (`im:message:send_as_bot`) for the bound app.
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
