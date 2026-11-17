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
  - Hub OAuth login (`/api/auth/login`) requests `DEFAULT_FEISHU_OAUTH_SCOPES` (including `im:message.send_as_user` and `offline_access`), accepts access-only OAuth tokens when `offline_access` is not granted, and exchanges/refreshes tokens via OAuth v2 (`/open-apis/authen/v2/oauth/token`) / OIDC v1 (`/open-apis/authen/v1/oidc/access_token`) / legacy v1 (`/open-apis/authen/v1/refresh_access_token`) while routing legacy non-`rt-` refresh tokens past v2 `20026` errors to OIDC/v1 refresh endpoints (`api/auth.py`, `auth/feishu.py`).
  - Supports importing `~/.lark-cli/config.json` + `~/.local/share/lark-cli/<appId>_<openId>.enc`, honoring official `lark-cli` `currentApp` profile precedence (`explicit profile/app_id > currentApp > apps[0]`, failing closed on a dangling `currentApp`).
  - Coordinates single-use refresh token rotation and all async token resolution/preflight/OAuth-save entrypoints (`aresolve_token`, `aimport_from_lark_cli`, `asave_oauth_token`, `asave_refreshed_token`, `aresolve_record`, `apreflight_scopes`, `arequire_send_as_user_scope`, `aget_config`, `apreflight_status`) under `async_refresh_lock()` (per-loop `asyncio.Lock`) and cancellation-safe `async_file_lock()` (non-blocking `LOCK_EX | LOCK_NB` polling across Hub's lock and `lark-cli`'s `locks/refresh_<appId>_<openId>.lock`, closing unacquired fds on `CancelledError`/timeout); compares against the captured pre-refresh `previous` generation/fingerprint before committing so concurrent Hub OAuth or imported CLI updates are never overwritten; persists authoritative granted scopes without unioning stale CLI scopes; and serializes `expiresAt`/`refreshExpiresAt` in Unix milliseconds matching `lark-cli`'s `StoredUAToken` contract.
  - `preflight_scopes()` checks `im:message.send_as_user` upfront and provides exact Developer Console + `lark-cli auth login --scope "im:message.send_as_user ..."` remediation steps without silently downgrading to bot identity.
- **Paginated Poller Intake & Durable Queue**:
  - `FeishuUserClient.list_messages_page()` and `FeishuAiPoller._collect_unprocessed_chat_messages()` page backwards across the full unprocessed interval down to `watermark_ms` and persist `scan_cursors` (`page_token` + `max_seen_ms`) when a chat burst exceeds `_max_pages_per_chat` (25 pages) so deep backlogs continue across ticks without advancing `watermark_ms` prematurely.
  - Detected owner `/ai` commands are durably recorded in `pending_queue` (`queued` -> `in_flight` -> `interrupted` / dequeued on terminal completion) with separate `dispatch_state` (`not_started` / `dispatched` / `turn_completed` / `retired_retry_safe` / `uncertain`). On restart, `reconcile_interrupted_command()` resumes only final Feishu reply posting for turns that already completed (in `pending_queue` or `AgentStreamStore`), replays only proven retry-safe interruptions (`not_started` / `retired_retry_safe`), and preserves dedup claims (`interrupted_uncertain`) when turn retirement is uncertain.
- **Configurable Execution Agent (`provider`, `model`, `reasoning_effort`)**:
  - Validated against `SUPPORTED_AI_COMMAND_PROVIDERS` (`claude`, `codex`, `traex`, `maso`, `cursor`, `antigravity`) and provider-specific `PROVIDER_SUPPORTED_REASONING_EFFORTS` / `model_reasoning_efforts` using `ProviderSpec` (`services/agent_stream/providers/spec.py`, `services/feishu_ai_command_models.py`).
  - Resolves `reasoning_effort` per provider transport (`CODEX_REASONING_EFFORT` / `TRAEX_REASONING_EFFORT` for Codex/TraeX; suffix replacement on `CURSOR_MODEL` for Cursor; real model variant selection on `MASO_MODEL` for MASO; rejects unsupported separate `reasoning_effort` on Claude/Antigravity) and syncs live Chat stream tailers on reused tabs.
  - Supports inline per-message overrides (`/ai --provider ... --model ... --effort ... <instruction>`).
  - Exposed across `/api/feishu/bot/ai-command` (`GET`/`PUT`/`PATCH`, `/import-token`, `/dry-run`, `/auth-guide`), `claude-hub feishu ai-command status|configure|dry-run|auth-guide`, and the Personal `/ai` Assistant view in `FeishuBotSettingsDialog.vue` (with dedicated `aiLoadGeneration` + `retireAiCommandLoad()` on navigation/close, loading/error/retry hydration guards, draft preservation across panel re-entry and token import, and coherent `(provider, model, reasoning_effort)` tuple conflict resolution guarded during pending writes).

## Verification

- `./scripts/verify.sh docs`
- `./scripts/verify.sh checkout-safety`
- `./scripts/verify.sh backend-format`
- `./scripts/verify.sh backend-types`
- `NO_COLOR=1 ./scripts/verify.sh backend-tests`
- `./scripts/verify.sh frontend-lint`
- `./scripts/verify.sh frontend-types`
- `./scripts/verify.sh frontend-tests`
- `./scripts/verify.sh frontend-build`
- `CLAUDE_HUB_BROWSER_EXECUTABLE=... ./scripts/verify.sh browser-mocks` (including `browser_feishu_bot_components.py` at 1280×900, 720×900, and 390×844)
