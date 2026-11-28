# Chat session switch, model hot-update, and status polling latency

## Baseline and root causes

Baseline: `0af68882`. Host workload at investigation time: 169 active tabs on
the shared loopback Hub (`50` native Chat + `119` Terminal). All benchmarks and
repros below ran against isolated `CLAUDE_HUB_HOME` directories and dedicated
`CLAUDE_HUB_TMUX_SOCKET` servers without touching the shared `:8173` runtime.

1. **New Tab / Chat launch latency (`~1650ms` initial, `660.8ms` after removing `ttyd` sleep)**:
   `TTYDProcess.start` waited on a hardcoded `asyncio.sleep(1)` after spawning
   `ttyd`, and `_configure_tmux` slept another `0.5s` while spawning 18
   sequential `tmux set-option` subprocesses even though `ttyd` bound its
   loopback port in `<12ms`.
2. **Periodic `/api/tabs/status` event-loop stalls (`296.0ms` cold / `281.6ms` per 5s poll on 170 isolated tabs; `1.30s` on live 169-tab host)**:
   Each 5s poll spawned up to 4 `tmux` subprocesses per live terminal tab
   (`list-sessions`, redundant `has-session` + `capture-pane`, redundant
   `has-session` + `display-message`), dispatched 50 separate worker-thread
   SQLite summary reads, repeated `shutil.which` binary probes per Chat tab,
   and scanned all workspace tasks per idle Chat tab.
3. **Cold/repeated capability preparation and SQLite read overhead**:
   `CodexConfiguredSession.prepare_capabilities` blocked on `model/list` (`~6.6s`
   cold) on every new session and re-waited its foreground budget if a slow
   discovery remained in flight; `_codex_model_options` re-parsed
   `models_cache.json` on every call; `list_tabs` opened one SQLite connection
   per Chat tab (`401.5ms` for 170 tabs); `read_conversation` unpacked hidden
   shell tool bodies and counted per-turn events individually (`209.2ms` for
   5,000 events).
4. **Frontend Chat switch & hot model update churn**:
   `StructuredPane.vue` keyed stream restarts on `streamCapabilityConfiguration`,
   tearing down `/stream/live` and `/stream/wait` on hot `switch-env` model or
   reasoning-effort updates; `terminalStore.switchEnv` awaited a full
   `loadTabs()` round-trip; `useChatTimeline` flashed `Updating conversation…`
   on sub-250ms reconciliations; revisiting a cached Chat cleared
   `capabilities` until incremental `/events` resolved.

## Changes

- **Tab launch & `tmux` configuration (`process/launch.py`, `process/capture.py`, `process/host.py`)**:
  Replaced `asyncio.sleep(1)` with `_await_ttyd_ready` loopback socket readiness
  probing, recorded `_tmux_session_known_ready`, removed `asyncio.sleep(0.5)` in
  `_configure_tmux`, and combined global/session `tmux` options into a single
  compound `tmux` invocation.
- **Batched status polling (`tmux_control.py`, `manager/status.py`, `agent_stream/store.py`)**:
  Added `_tmux_list_session_panes` (`list-panes -a`) to collect live sessions
  and active-pane foreground commands in one subprocess, skipped `tmux` probing
  when all terminal tabs sit within `_STATUS_CACHE_TTL_SECONDS`, passed
  `known_alive=True` under `asyncio.Semaphore(16)` to avoid duplicate
  `has-session` calls, added `read_stream_summaries_batch_sync` for single-connection
  SQLite Chat summary reads, memoized `native_provider_binary_available` per
  poll, and pre-indexed non-terminal workspace tasks once per poll. Defensive
  method-identity checks preserve monkeypatched unit-test seams.
- **Capabilities & SQLite storage (`providers/codex.py`, `native.py`, `runtime.py`, `observation.py`, `local_storage.py`, `store.py`, `history.py`)**:
  Coalesced concurrent `capabilities_for` calls, removed duplicate capability
  preparation in `stream_live_for`, cached discovered Codex model/mode catalogs
  for 30m with a one-shot 250ms foreground budget when `models_cache.json` is
  warm, cached parsed `_codex_model_options` keyed by raw file bytes, extended
  Cursor/Antigravity catalog TTL to 30m with background pre-expiry refresh,
  skipped redundant `_SCHEMA_LOCK`/`chmod`/PRAGMA work on initialized SQLite
  files, batched unread turn checks in `list_tabs`, short-circuited empty
  incremental `conversation_page` reads, and pre-filtered hidden tool rows with
  grouped turn counts.
- **Frontend zero-teardown switching (`StructuredPane.vue`, `agentStreamCapabilitiesCache.ts`, `stores/terminal/index.ts`, `useAgentStream.ts`, `useChatTimeline.ts`)**:
  Split `streamSourceIdentityConfiguration` from `streamCapabilityConfiguration`
  so hot model/effort updates keep live subscriptions open, made `switchEnv`
  optimistic with atomic rollback, synchronously populated warm
  `agentStreamCapabilitiesCache` entries during `start()` while writing updated
  capabilities back to `agentStreamHistoryCache`, and gated the reconciliation
  banner behind a 250ms timer while keeping `Stop turn` immediately armed.

## Isolated Before vs After Measurements

| Workload | Before | After |
| --- | --- | --- |
| `create_tab(session_kind=CHAT)` | `~1650ms` (`660.8ms` / `640.2ms` before `_configure_tmux` fix) | `116.87-125.83ms` (1st) / `94.89-95.22ms` (2nd) |
| `list_tab_agent_statuses` (50 Chat + 40 live Terminal + 80 offline) | `296.0ms` cold / `281.6ms` 5s cycle | `95.16ms` cold / `65.07ms` 5s cycle / `5.68ms` within TTL |
| `list_tabs` unread check (170 tabs) | `401.5ms` | `1.79ms` |
| Codex `prepare_capabilities` | `6614.6ms` cold | `333.3ms` first / `230.4ms` next session / `3.32ms` same session |
| `_codex_model_options` (`models_cache.json`) | `~3.30ms` | `2.95ms` cold / `0.87ms` byte-verified hit |
| 5,000-event `read_conversation` / empty catch-up | `209.2ms` full | `13.60ms` full / `0.85ms` empty incremental page |

## Validation

Commits on `fix/chat-switch-model-latency`: `8fc919b4`, `5da70027`.

- `./scripts/verify.sh all` (with declared Python `3.11.16` and Node `24.21.0`):
  passed `docs`, `checkout-safety`, `backend-format`, `backend-types`,
  `backend-tests` (`7,952` passed), `frontend-lint`, `frontend-types`,
  `frontend-tests` (`1,577` passed), and `frontend-build`.
- `./scripts/verify.sh browser-mocks`: passed all mocked Chromium UI suites
  (`exit_code=0`).
- `./scripts/verify.sh check-staged`: passed on every commit.
- Unrun / deployment note: the shared host service on `:8173` was intentionally
  not restarted per repository hard boundaries; live `:8173` picks up the new
  backend/frontend bundle only when the operator restarts/redeploys it.
