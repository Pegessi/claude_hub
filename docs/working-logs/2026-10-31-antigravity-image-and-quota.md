# Antigravity Image Inference, Provider Quota & Quota Modal Dialog — 2026-10-31

Branch: `codex/antigravity-image-and-quota` (worktree `~/claude_hub_worktree/antigravity-image-and-quota`).

## Summary

1. **Antigravity CLI Image Inference (`ImageTransport.PROMPT_FILE_REF`)**:
   - Updated `AgentType.ANTIGRAVITY` in `backend/claude_hub/services/agent_stream/providers/spec.py` and `backend/tests/fixtures/provider_spec_matrix.json` to advertise `images="prompt_file_ref"`.
   - Implemented `_stage_images`, `_clear_staged_images`, and `_cleanup_images` in `AntigravityNativeProvider` (`backend/claude_hub/services/agent_stream/providers/antigravity.py`), validating image attachment bytes, writing them to `self._runtime.cursor_image_temp_dir()`, wrapping turn text via `self._runtime.wrap_image_guidance(text, image_paths)` for Antigravity's native `read_file` tool, and cleaning up temporary files on `_end_turn` (completion/cancellation) and `stop()`.
2. **Antigravity Provider Quota (`QuotaSource.CLI_USAGE_COMMAND`)**:
   - Added `QuotaSource.CLI_USAGE_COMMAND = "cli_usage_command"` to `ProviderSpec` and implemented `read_antigravity_quota` in `backend/claude_hub/services/provider_quota.py`.
   - Checks deployment presence via `agyx` executable and OAuth token (`$ANTIGRAVITY_HOME/antigravity-oauth-token` or `~/.gemini/antigravity-cli/antigravity-oauth-token`), returning `absent=True` when unconfigured.
   - Runs `agyx -p /usage --output-format json` with bounded timeout and parses `command.data.groups[*].buckets[*]` into `QuotaLimit` entries (`Gemini Models · Weekly limit`, `Gemini Models · 5-hour limit`, `Claude and GPT models · Weekly limit`, `Claude and GPT models · 5-hour limit`), handling `within`/`near`/`over`/`disabled` and expired windows.
   - Added `antigravity` to `read_provider_quota`, API docs (`backend/claude_hub/api/quota.py`), CLI help (`backend/claude_hub/cli/commands/rest.py`), and frontend `PROVIDER_TITLES` (`frontend/src/utils/quotaDisplay.ts`).
3. **Provider Quota Modal Dialog (`QuotaMenu.vue`)**:
   - Replaced the inline `<details>` expansion inside the extensions menu with a menu trigger button (`frontend/src/components/QuotaMenu.vue` and `frontend/src/components/AppExtensionActions.vue`) that emits `select` to close the parent dropdown and opens a teleported modal dialog (`.quota-modal-backdrop` / `.quota-modal`).
   - Supports responsive multi-column provider cards, manual refresh, backdrop click-to-close, `Close` button, `Escape` key dismissal, initial focus into the dialog (`closeButtonRef`), `Tab` / `Shift+Tab` focus containment (`trapFocus`), and focus restoration (`returnFocusEl`) on close while remaining compatible with custom test renderers in `frontend/tests/quotaMenu.test.mjs`.

## Verification

- `./scripts/verify.sh docs` — PASS
- `./scripts/verify.sh checkout-safety` — PASS
- `./scripts/verify.sh backend-format` & `./scripts/verify.sh backend-types` — PASS
- `./scripts/verify.sh backend-tests -- tests/test_antigravity_native.py tests/test_provider_spec_registry.py tests/test_provider_quota.py tests/test_quota_api.py tests/test_cli.py` — 257 passed
- `./scripts/verify.sh frontend-lint`, `./scripts/verify.sh frontend-types`, `./scripts/verify.sh frontend-tests`, `./scripts/verify.sh frontend-build` — PASS
- Real Chromium browser regression check (`frontend/tests/browser_service_restart.py`) — PASS (verifies Extensions -> Provider quota modal open, focus move to Close button, Tab / Shift+Tab focus loop, Escape dismissal, and focus restoration to Extensions trigger).
- Live smoke check of `read_antigravity_quota()` against local `agyx -p /usage --output-format json` returned `available=True`, `state="within"`, and all 4 model/window buckets.
