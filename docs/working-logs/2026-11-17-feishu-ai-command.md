# Working Log: Feishu Personal `/ai <instruction>` Command Workflow

- Date: 2026-11-17
- Task: `63bb0d51-fd00-4453-9a5f-2e2a758d2de1`
- Branch: `codex/feishu-ai-command`

## Context & Goal

Implement Option 1 from `/tmp/feishu-ai-command-research-20261117.md`: an end-to-end personal-account `/ai <instruction>` workflow in Claude Hub where the owner's own `/ai ...` message in a Feishu thread or chat triggers:
1. Immediate user-identity reaction (`OnIt`) and thread/chat context lookup (extracting Codebase MR references, commit SHAs, and Feishu doc links).
2. Immediate user-identity `msg_type="post"` interim reply (`正在派 Subagent 对 <repo>!<mr>（最新 commit <sha>）... \n> 🤖 AI 代答，仅供参考`).
3. Turn dispatch to a Hub Chat session configured with the default (or inline-overridden) `provider`, `model`, and `reasoning_effort` (thinking intensity).
4. Final user-identity `msg_type="post"` reply with the `> 🤖 AI 代答，仅供参考` footer and transition from `OnIt` to `DONE` reaction.

## Architecture & Reuse Decisions

- **No duplicate Feishu HTTP/reaction/dedup primitives**:
  - `FeishuUserClient` (`backend/claude_hub/services/feishu_user_client.py`) wraps `FeishuBotClient` (`services/feishu_bot.py`), reusing `create_feishu_http_client`, `_remote_error`, `_chat_info_from_payload`, `_forward_body`, and `_forward_content`.
  - `FeishuAiCommandService` (`backend/claude_hub/services/feishu_ai_command.py`) reuses `FeishuMessageDedupStore` for durable at-most-once message claiming and `build_feishu_post_content` (`services/feishu_message_format.py`) for bounded `post` payloads.
  - WebSocket intake in `api/feishu_bot.py` reuses `message_event_from_sdk(..., require_group_mention=False)` (`services/feishu_bot_websocket.py`) so unmentioned `/ai` commands from the owner are caught with zero duplicated SDK parsing when a Hub Bot is in the group, while `FeishuAiPoller` (`services/feishu_ai_poller.py`) polls active/watched chats with per-chat watermarks when no Bot is in the group.
- **User Token Persistence & `~/.lark-cli` Keychain Import**:
  - `FeishuUserTokenStore` (`backend/claude_hub/services/feishu_user_token_store.py`) persists user tokens in Hub's SQLite document store (`secrets/feishu_user_tokens.json`) encrypted with AES-256-GCM using a `0600` key file (`secrets/feishu_user_tokens.key`).
  - Supports both Hub OAuth login callbacks (`api/auth.py`) and importing `~/.lark-cli/config.json` + `~/.local/share/lark-cli/<appId>_<openId>.enc` (AES-256-GCM encrypted with `~/.local/share/lark-cli/master.key`).
  - `preflight_scopes()` checks `im:message.send_as_user` upfront and provides exact Developer Console + `lark-cli auth login --scope "im:message.send_as_user ..."` remediation steps without silently downgrading to bot identity.
- **Configurable Execution Agent (`provider`, `model`, `reasoning_effort`)**:
  - Validated against `SUPPORTED_AI_COMMAND_PROVIDERS` (`claude`, `codex`, `traex`, `maso`, `cursor`, `antigravity`) and `SUPPORTED_REASONING_EFFORTS` (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`) using `ProviderSpec` (`services/agent_stream/providers/spec.py`).
  - Supports inline per-message overrides (`/ai --provider ... --model ... --effort ... <instruction>`).
  - Exposed across `/api/feishu/bot/ai-command` (`GET`/`PUT`/`PATCH`, `/import-token`, `/dry-run`, `/auth-guide`), `claude-hub feishu ai-command status|configure|dry-run|auth-guide`, and the Personal `/ai` Assistant view in `FeishuBotSettingsDialog.vue`.

## Verification

- `./scripts/verify.sh docs`
- `./scripts/verify.sh checkout-safety`
- `./scripts/verify.sh backend-format`
- `./scripts/verify.sh backend-types`
- `./scripts/verify.sh backend-tests -- tests/test_feishu_ai_command.py tests/test_feishu_commands.py tests/test_feishu_bot_http.py tests/test_feishu_bot_websocket.py tests/test_feishu_bot_pool.py`
- `./scripts/verify.sh frontend-lint`
- `./scripts/verify.sh frontend-types`
- `./scripts/verify.sh frontend-tests`
- `./scripts/verify.sh frontend-build`
