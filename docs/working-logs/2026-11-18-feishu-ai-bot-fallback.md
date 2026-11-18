# Working Log: Feishu `/ai` Bot Fallback Without `im:message.send_as_user` (2026-11-18)

## Problem & Investigation

When authenticating `lark-cli` for the personal Feishu `/ai` command on `[redacted]` (`[redacted]`) / `[redacted]` (`[redacted]`), Feishu device verification rejected `im:message.send_as_user` because the scope is not enabled in the Feishu Developer Console.

### Verified Findings

1. **Direct Feishu OpenAPI behavior (`user_access_token` with `im:message` only)**:
   - Calling `POST /open-apis/im/v1/messages` or `POST /open-apis/im/v1/messages/:message_id/reply` with a `user_access_token` that has `im:message` (plus 74 other scopes) but lacks `im:message.send_as_user` returns HTTP 400 `{"code": 230027, "msg": "Lack of necessary permissions, ext=requires im:message.send_as_user scope."}` from Feishu's server before message validation.
   - Querying `GET /open-apis/application/v6/scopes` for all three configured apps (`[redacted]` `[redacted]`, `[redacted]` `[redacted]`, `[redacted]` `[redacted]`) confirmed none of them has `im:message.send_as_user` enabled in the Feishu Developer Console (`im:message.send_as_user` is also listed in `lark-cli`'s internal `recommend.deny` list). However, all three apps have `im:message:send_as_bot` active.

2. **How internal `openlark` / `lark-desk` / `lark-bridge` send `> 🤖 AI 代答，仅供参考` replies**:
   - The public GitHub Rust crate `foxzool/openlark` is an unrelated OpenAPI wrapper.
   - Internal ByteDance Codebase (`[redacted]`) repositories producing the `> 🤖 AI 代答，仅供参考` footer (`[redacted]` `mew/plugin/openlark-standin`, `[redacted]` `extensions/lark-desk`, and `[redacted]` `extensions/lark-bridge`) send messages as the user via either:
     - an internal **OpenLark** local client daemon (`http://127.0.0.1:8081/agent/v1` or `:8095/agent/v1` `/messages/:id/replies`), or
     - **`LarkAgentX` / `lark-desk`** Feishu web gateway protobuf calls (`https://internal-api-lark-api.feishu.cn/im/gateway/` using a QR web session in `~/.larkx/credentials.json`),
     while using `lark-cli` strictly for reading/searching messages.

## Solution

- Removed `im:message.send_as_user` from `RECOMMENDED_AI_COMMAND_SCOPES` so `lark-cli auth login` commands work on apps without `im:message.send_as_user`.
- Added `_pool_bot_secret_state()`, `resolve_app_secret_for_app()`, and `resolve_fallback_bot_configs()` on `FeishuUserTokenStore` (`backend/claude_hub/services/feishu_user_token_store.py`) to resolve the bot `app_secret` strictly for the single selected/authorized `/ai` app (`record.app_id` or `preferred_app_id`) from the token record, `lark-cli` encrypted keychain (`appsecret_<appId>.enc`), or an enabled `FeishuBotPoolStore` bot, while rejecting disabled pool bots and never sweeping unrelated pool bots or other stored user-token records.
- Updated `evaluate_scope_preflight()`, `require_send_as_user_scope()`, `send_as_user()`, `reply_as_user()`, and `FeishuAiCommandService.handle_message()` (`backend/claude_hub/services/feishu_user_client.py`, `backend/claude_hub/services/feishu_ai_command.py`), plus `FeishuBotSettingsDialog.vue` and `feishuBotConfig.ts`, so:
  - `can_send_as_user` truthfully reports whether `im:message.send_as_user` is granted on the user token, while `can_send` (`send_mode="bot_fallback"`) reports operational readiness when bot fallback is available for the bound app.
  - `/ai` reads context and adds/deletes emoji reactions (`OnIt` / `DONE`) using the user's token (`im:message`, `im:message.reactions:write_only`) and sends/replies via the single authorized bot `tenant_access_token` without cross-identity retries on uncertain delivery or unrequested group-membership mutations (`auto_invite_bot_to_chat=False` by default).
