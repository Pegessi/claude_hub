# Working Log: Feishu `/ai` Model Selector Dropdown & Live `/ai 你好` Trigger/Delivery Fix (2026-11-18)

## Task & Provenance Boundary

- **Task ID**: `7c5e8cb2-ea85-4586-b4cc-95b07e826eef`
- **Dedicated Worktree & Branch**: `/home/tiger/claude_hub_worktree/fix-feishu-ai-model-selector-dro-7c5e8cb2` (`codex/fix-feishu-ai-model-selector-dro-7c5e8cb2`, branched cleanly from `main@fd5eaed7bc3099aece893a77cf73282a31a94931`).
- **Predecessor Worktree Freeze**: Aborted predecessor task `37093dfc-27a1-46ab-9066-15a9e38617b4` (`claude-hub-agent-12`) left uncommitted edits in `/home/tiger/claude_hub_worktree/fix-feishu-ai-model-selector-dro-37093dfc`. Per `AGENTS.md` and approved Cycle 2 Goal Packet boundaries, that predecessor checkout was kept completely frozen and untouched, and its unsafe draft behaviors (`user_name` display-name owner matching, `auto_invite_bot_to_chat=True`, mutating low-level `reply_as_user()` default `230002` behavior, and flat duplicate `_reasoning_*` dropdown rows) were explicitly excluded.

## Live Read-Only Investigation (`~/.claude_hub` + Feishu OpenAPI)

1. **Server Startup vs. `fd5eaed` Merge Timing**:
   - Production `uvicorn` (`PID 3343140`) restarted at `2026-11-18 16:44:39` CST — *after* `fd5eaed` merged at `16:43:54`.
   - `FeishuAiPoller` was actively polling (`poll_count=328+`, `watermarks_count=26`, `commands_detected=0`) with `enabled=true`, `provider="maso"`, `model="gemini-4-argon_reasoning_high"`, `reasoning_effort="high"`, `owner_open_id="ou_a24354b644a3bef61a41bd05f07c1aac"`, `app_id="cli_a9f715f8d6b89bcb"`, `watch_chats=[]`, and `send_mode="bot_fallback"`.
2. **Exact Missed `/ai 你好` Messages & Verified Root Causes**:
   - At `16:46:25` (`om_x100b60e210d970a8c0ee197a6fa3340`, `/ai 你好`), `16:47:19` (`om_x100b60e22fbffcbcc1f144d329dd662`, `/ai hi`), and `16:47:26` (`om_x100b60e22f130d08df04373fe500f1c`, `/ai 你好`), the owner (`ou_a24354b644a3bef61a41bd05f07c1aac` on `MacWorker` `cli_a9f715f8d6b89bcb`) sent `/ai` commands inside `oc_c629575a578376c054af305d790dd43f` (a P2P chat with `Merlin Bot` `cli_9c68ce7e1c73d10c`).
   - **Root Cause A (`list_chats` omitted `types="p2p,group"`)**: `FeishuUserClient.list_chats()` called `GET /open-apis/im/v1/chats` without `types`, which in Feishu OpenAPI defaults to group/topic chats only. Passing `types="p2p,group"` immediately includes active P2P chats (`oc_c629575a578376c054af305d790dd43f` and `oc_48cb29e5da54bed9d35faf2b3ac24303`).
   - **Root Cause B (No bounded owner message-search supplement in `poll_once()`)**: When `watch_chats` was empty, `FeishuAiPoller.poll_once()` only inspected the top `max_active_chats` from `list_chats()` and did not supplement discovery via `POST /open-apis/search/v2/message` (`from_ids=[owner_open_id]`) for recent `/ai` messages in chats outside that top slice.
   - **Root Cause C (Cross-app `open_id` mismatch on WebSocket events)**: Feishu `open_id`s are app-scoped (`ou_a24354b644a3bef61a41bd05f07c1aac` on `MacWorker` `cli_a9f715f8d6b89bcb` vs. `ou_bf53ae2080ab1645dac8951aedce4e4b` on `MerlinTed` `cli_a943e6081bf8dbcd` in `oc_48cb29e5da54bed9d35faf2b3ac24303`). `handle_ws_event()` only compared `event.sender_open_id` directly against `cfg.owner_open_id`.
   - **Root Cause D (Definite `230002` `bot not in chat` on `bot_fallback` replies)**: When `im:message.send_as_user` is not granted (`send_mode="bot_fallback"`) and `/ai` runs in a P2P chat with another bot (`oc_c629575a578376c054af305d790dd43f`) or a group where `cli_a9f715f8d6b89bcb` is not a member (`auto_invite_bot_to_chat=False`), Feishu returns definite error `230002` (`bot not in chat`), and `_reply_via_bot_fallback()` had no bound-bot or owner-DM fallback.

## Solution

1. **Provider-Linked Logical Model Selector & Synced `reasoning_effort` (`FeishuBotSettingsDialog.vue`, `feishuBotConfig.ts`, `feishu_ai_command_models.py`, `feishu_ai_command.py`, `api/routes/feishu_bot/ai_command.py`)**:
   - Added `provider_model_options` (`StreamModelOption` catalog per provider across `claude`, `codex`, `traex`, `maso`, `cursor`, and `antigravity`) via `capabilities_dict()` / `acapabilities_dict()` with fast warm-cache reads, a bounded `0.8s` MASO catalog probe, and curated logical fallbacks.
   - Added `gemini-4-carbon` and `gpt-6.1-sol` to `_MASO_MODEL_VARIANTS`, added `max` and `ultra` to `PROVIDER_SUPPORTED_REASONING_EFFORTS["codex"]` / `["traex"]`, and placed non-exhausted MASO logical defaults ahead of `gemini-4-argon` while retaining full backward compatibility.
   - Replaced `<input data-testid="feishu-ai-model-input">` in `FeishuBotSettingsDialog.vue` with a Provider-linked `<select data-testid="feishu-ai-model-input">` displaying clean logical model options (without duplicating `_reasoning_*` variant rows), mapping persisted variant IDs (e.g. `gemini-4-argon_reasoning_high`) without mutating `aiEdit.model` on hydration (`tupleDirty` stays `false`), preserving unlisted custom/current models (`<id> (custom/current)`) and `Provider default` (`""`), and syncing `aiEffortOptions` + disabling `<select data-testid="feishu-ai-effort-select">` when unsupported.
2. **Live `/ai` Discovery, Authoritative Cross-App WS Verification & Safe `230002` Fallback (`feishu_user_client.py`, `feishu_ai_poller.py`, `feishu_ai_command.py`, `feishu_bot.py`)**:
   - Updated `FeishuUserClient.list_chats()` to pass `types="p2p,group"` by default and `search_messages()` to accept `from_ids`.
   - Added `_discover_recent_owner_command_chats()` in `FeishuAiPoller.poll_once()` when `watch_chats` is empty, querying recent owner messages matching the command prefix within a bounded timeout and non-fatal fallback if search scope is unavailable.
   - Added `_verify_cross_app_ws_owner()` in `FeishuAiCommandService.handle_ws_event()`: when an enabled `/ai` candidate arrives over WebSocket with a different app's `sender_open_id`, Hub fetches `event.message_id` via `user_client.get_message()` under `cfg.app_id` and verifies `fetched.sender_type == "user"` and `hmac.compare_digest(fetched.sender_open_id, cfg.owner_open_id)` — zero display-name or unverified binding-alias matching.
   - Added `receive_id_type` support (`chat_id` default, `open_id` for owner DM) in `FeishuBotClient.send_message_with_identity()` and opt-in `allow_bound_or_dm_fallback_on_not_in_chat=True` on `/ai` replies (`_reply_ai_command()`): ONLY on definite Feishu `230002` (`bot not in chat`) — never on post-accept timeouts or `FeishuDeliveryUncertainError` — `_reply_via_bot_fallback()` / `_send_via_bot_fallback()` first tries an enabled non-tombstoned `FeishuBotPoolStore` bot bound to `chat_id`, and otherwise delivers via direct message to `record.open_id` (`receive_id_type="open_id"`) using the primary fallback bot (`record.app_id == bot_app_id`). Low-level `reply_as_user()` / `send_as_user()` keep `auto_invite_bot_to_chat=False` and `allow_bound_or_dm_fallback_on_not_in_chat=False` by default.

## Fresh Validation Executed in `7c5e8cb2` Worktree

- **Backend unit & integration tests**:
  - `./scripts/verify.sh backend-tests -- tests/test_feishu_ai_command.py -q` (`17 passed`, including `test_ai_capabilities_provider_model_options_and_new_model_validation` and `test_live_feishu_p2p_discovery_search_cross_app_ws_owner_and_230002_fallback`).
  - `./scripts/verify.sh backend-tests -- tests/test_feishu_ai_command.py tests/test_feishu_bot_pool.py tests/test_feishu_channel_modes.py -q` (`174 passed`).
- **Frontend unit tests**:
  - `(cd frontend && node --test tests/feishuBotConfigWiring.test.mjs tests/feishuBotSettingsBehavior.test.mjs)` (`16 passed`).
- **Mocked Playwright UI gate + negative oracle**:
  - Negative oracle verified: reverting `<select data-testid="feishu-ai-model-input">` to `<input>` caused both `feishuBotConfigWiring.test.mjs` and `browser_feishu_bot_components.py` (`assert await model_select.evaluate("el => el.tagName") == "SELECT"`) to fail (`exit_code=1`), then restored cleanly.
  - `CLAUDE_HUB_BROWSER_EXECUTABLE=/home/tiger/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome CLAUDE_HUB_BROWSER_PORT=5298 ./scripts/verify.sh browser-mocks` (`result: PASS`, all 25 scenarios passed including `browser_feishu_bot_components.py` across `1280x900`, `720x900`, and `390x844`; mocked HTTP/WebSocket boundary, not live Feishu connectivity).
- **Full repository verification**:
  - `./scripts/verify.sh check-staged` and `./scripts/verify.sh all` (`PASS`).
