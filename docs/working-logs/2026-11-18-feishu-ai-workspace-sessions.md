# Route Feishu /ai Chat Sessions into an Agent Workspace

- **Task ID**: `8b0a6386-62f9-4966-8a2f-b78cfb6fbb76`
- **Branch**: `feat/create-feishu-ai-sessions-inside-8b0a6386`

## Problem

Personal Feishu `/ai` Chat tabs were built by `FeishuAiCommandService._ensure_execution_tab`
via `ttyd_manager.create_tab(...)` without any `workspace_id`. They therefore landed in the
ungrouped Terminal/Chat list grouped by their bare cwd (e.g. a `tiger` group) instead of under
an Agent Workspace, which is where assistant-created sessions belong.

Two platform constraints rule out the obvious "just stamp `workspace_id` on the tab" fix:

1. **State-load heal** (`terminal_runtime/manager/state.py`): a tab carrying `workspace_id`
   without `workspace_role`/`managed_session_id`/`parent_tab_id`/`subagent_id` is healed back to
   an ungrouped tab on restart, so display-only attribution is not durable.
2. **Dispatch guard** (`api/agent_stream.py::_terminal_tab_session_or_404`): a Chat tab with a
   `workspace_role`/`managed_session_id` returns 409 from `dispatch_tab_chat_and_wait`, because
   managed sessions must use the managed tailer, not the terminal-tab stream.

The in-repo analog `services/demand_patrol/feishu_triage.py` already solves automated
Feishu → managed workspace Chat → synchronous answer, and was used as the reference pattern.

## Summary of Changes

1. **Config surface (backward compatible)** — `services/feishu_ai_command_models.py`
   - New optional `FeishuAiCommandConfig.workspace_id: str = ""` and
     `DEFAULT_AI_COMMAND_WORKSPACE_NAME = "Feishu /ai"`. Old persisted configs without the field
     deserialize to `""`; `to_dict`/`from_mapping` round-trip it.
   - Exposed through the REST PATCH model `FeishuAiCommandPatchInput.workspace_id`
     (`api/routes/feishu_bot/ai_command.py`) and the CLI
     `claude-hub feishu ai-command configure --workspace-id ... / --clear-workspace-id`
     (`cli/commands/feishu.py`). Empty string clears the pin (so default resolution resumes).

2. **Managed workspace execution** — new `services/feishu_ai_workspace.py`
   - `resolve_workspace()`: use a pinned, existing `workspace_id`; otherwise reuse an existing
     workspace named `Feishu /ai`, else create it on demand at the /ai cwd. A pinned id that no
     longer exists logs a warning and falls back rather than failing silently. The on-demand create
     sends `allow_duplicate=True` so a cwd that already hosts another workspace does not raise
     `DuplicateWorkspaceError` (the dedicated workspace is keyed by name, not path identity).
   - `ensure_execution_tab()`: keeps the existing channel→tab reuse map (keyed by
     `chat:thread:provider`) and env hot-switch semantics, but provisions new tabs as managed
     `WORKER` Chat sessions via `workspace_manager.ensure_workspace_agent(..., reuse_existing=False,
     bootstrap=False)`. `reuse_existing=False` is deliberate: each Feishu channel owns its own
     session through the channel map, and advisory workspace-manager reuse would hand one channel an
     idle WORKER session built for another. Before reusing a mapped managed record it now verifies
     the backing session is still live and non-archived (`_get_managed_session`); a stale/deleted
     session falls through to fresh provisioning. The validated /ai cwd is propagated into the
     request so a pinned workspace cannot silently relocate the session. The resulting tab carries
     `workspace_id` + `workspace_role` + `managed_session_id`, so it renders under the workspace and
     survives restart.
   - `dispatch_turn()` / `dispatch_managed_turn()`: route managed sessions through the shared
     managed tailer (`subscribe_completion` + `send_message`), preserving the
     `feishu-ai-<message_id>` turn id and full turn metadata; await `TURN_COMPLETED` for the final
     answer; retire the native turn on cancellation. On cancellation, when
     `ExternalTurnRetirement.safe_to_retry` (matching turn cancelled or never started) it raises the
     same `ExternalDispatchRetired(turn_id)` as the tab-dispatch path so `handle_message` marks the
     command `retired_retry_safe` and releases the dedup claim for safe replay; non-retryable
     retirements preserve the claim and re-raise the plain cancellation. Standalone tabs still use
     `dispatch_tab_chat_and_wait`.
   - `switch_managed_env()` mirrors the HTTP switch-env managed branch (tab env +
     `update_managed_chat_configuration` + tailer `set_env`) for model/effort hot-switches.
   - `recover_completed_stream_answer()` reads both the managed `(workspace_id, session_id)` and
     legacy `terminal-tabs` durable stream namespaces so interrupted-command reconcile works
     regardless of where the tab lives.
   - Direct-Chat-only providers not on the `MANAGED_CHAT` surface (TraeX) fall back to the
     standalone-tab path so `/ai` keeps working for every supported provider.

3. **Service wiring** — `services/feishu_ai_command.py`
   - Added injectable seams (`workspace_resolver`, `managed_session_provider`,
     `managed_session_getter`, `managed_turn_dispatcher`) and delegated the heavy logic to the
     helper module to keep this file under the 1500-line AI-metrics ceiling (1488 lines).
   - The pre-existing `tab_creator` injection seam remains the explicit legacy/standalone path,
     used by all earlier unit tests, so their behavior is unchanged.
   - `channel_tabs` entries may now be a dict `{tab_id, session_id, workspace_id}`; legacy plain
     string entries are still normalized and read.

## Tests

- `tests/test_feishu_ai_command_workspace.py` (22 tests): default resolution (reuse +
  on-demand create with `allow_duplicate=True`), explicit `workspace_id`, missing-id fallback,
  channel reuse (no second session), managed dispatch routing, managed env hot-switch, TraeX
  standalone fallback, legacy string-record compatibility, config persist/clear, PATCH schema
  validation, plus the review-fix coverage: distinct channels provision distinct sessions
  (`reuse_existing=False` + cwd propagation), stale/archived managed-session recovery, safe-retry
  cancellation raising `ExternalDispatchRetired` (and claim preservation when not retry-safe), and a
  real `provide_managed_session` → `workspace_manager.ensure_workspace_agent` →
  `ttyd_manager.create_tab` integration test asserting the runtime tab carries
  `workspace_id`/`workspace_role=WORKER`/`managed_session_id` and the request carries
  `reuse_existing=False`.
- Existing `tests/test_feishu_ai_command.py` (19) unchanged and green; the combined feishu/workspace/
  agent-stream suites pass.
- `./scripts/verify.sh` gates run locally with `NO_COLOR=1 FORCE_COLOR=` (the `docs` AI-metrics line
  ceiling, `backend-format`, `backend-types`, `frontend-lint`, `frontend-types`, `frontend-tests`,
  `frontend-build`, and `backend-tests` all green).
- The pre-existing, environment-only failure
  `test_ci_health.py::test_payload_id_stops_before_execution_with_bounded_diagnostics`
  is a pytest stderr/color-format mismatch. It reproduces on the untouched base commit and passes
  when color is disabled (`NO_COLOR=1 FORCE_COLOR=`); it is unrelated to this change.

## Manual verification

```bash
# Pin a workspace (optional; omit to auto-use/create the dedicated 'Feishu /ai' workspace)
claude-hub feishu ai-command configure --workspace-id <id>
claude-hub feishu ai-command status            # config.workspace_id is echoed
# Send a /ai message in a watched Feishu chat, then confirm the new tab:
#  - carries workspace_id/workspace_role/managed_session_id
#  - appears under the Agent Workspace (not the bare Terminal/cwd group)
#  - a second message in the same thread reuses the same tab
```

## Risks / Notes

- No frontend change was required: `stores/terminal/index.ts` already splits manual vs managed
  tabs purely on `workspace_id`, and the workspace board/agent inbox already render and open
  managed Chat tabs.
- The two existing stray `/ai` tabs mentioned in the task are intentionally not migrated (out of
  scope; the task explicitly excludes touching them). New tabs use the managed path; old tabs
  keep working through the legacy record shape.
- Managed MASO sessions still require a loopback `MASO_BASE_URL`, the same constraint as the
  prior standalone MASO Chat; the resolved /ai env is passed through unchanged.
