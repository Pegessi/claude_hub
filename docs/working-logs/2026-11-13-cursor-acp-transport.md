# Cursor persistent `agent acp` Chat transport and stream reconciliation fixes

Base: `c2a97ba`; branch: `feat/cursor-acp-transport`.
Worktree: `/home/tiger/claude_hub_worktree/cursor-acp-transport`.

## Root causes diagnosed

1. ** ~15–23s per-turn cold start on Cursor Chat**: `CursorSession` previously
   used `CLI_ONESHOT` (`agent --trust --print --output-format stream-json
   --stream-partial-output`). In `--print` (headless) mode, Cursor CLI bypasses
   `serverConfigCache`, re-fetches `getServerConfig` over the corporate proxy
   (1.6–5.0s), reloads all 67 local skills/rules uncached (4.4–7.8s), times out
   parameterized model probing (2.0s), and runs a failing Linux Landlock V3
   sandbox preflight on every single turn.
2. **Frequent mid-stream drops and 60s stalls (`bidi_append_deadline_exceeded`,
   `Stream ended without turnEnded`, `repeated resume attempts made no progress`)**:
   Machine-local `~/.cursor/cli-config.json` had `network.useHttp1ForAgent: true`,
   forcing Cursor CLI onto HTTP/1.1 split-bidi emulation against `api2.cursor.sh`
   instead of its 4-session HTTP/2 multiplexed pool against
   `agentn.us.api5.cursor.sh` (verified working over the local `CONNECT` proxy).
3. **False `assistant final text does not match streamed deltas; cannot safely reconcile`
   errors**: Cursor's `stream-json` loop flushes accumulated assistant text via
   `fe({includeTimestamp: true})` before `modelCallCompleted`, `toolCallStarted`,
   and `interaction_query`. When a short reply (such as a post-`AskQuestion`
   notice) arrived in a single delta chunk, `_TurnAccumulator._is_recent_text_snapshot`
   rejected it because `matched_chunks == 1 < 2`, doubling `state.text` and
   failing final reconciliation.
4. **Misleading `ActionRequiredError: Unauthorized request. The provider refused
   to serve this request based on the content`**: Cursor CLI wraps gRPC code 16
   (`Unauthenticated`) content-policy rejections from upstream Gemini models into
   `ActionRequiredError(action="login")`.
5. **Repeated 3s direct-probe timeouts**: `_SUCCESS_TTL_SECONDS` in
   `provider_network` was 30s, shorter than typical human turn intervals, causing
   proxied hosts without direct egress to re-pay a 3s direct HEAD timeout before
   selecting the proxy.
6. **Feishu `cursor-turbo.sh` v19 compatibility audit**: Unpacked and audited
   against Cursor CLI `2026.11.11-043ef28`: its preload unconditionally appends
   `.cursor.sh,cursor.sh` to `NO_PROXY` (breaking proxy-only devboxes), 7 of 8
   bundle regexes no longer match `2026.11.11`, and its `live: true` transcript
   rewrites trip `CursorCliTranscriptAdapter`'s fail-closed row validator. Left
   the official CLI binary unmodified.

## Changes and ownership

- `backend/claude_hub/services/agent_stream/providers/cursor.py`: Replaced
  one-shot `--print` process spawning with a persistent `agent acp` stdio
  JSON-RPC session (`initialize`, `session/new`, `session/load`,
  `session/set_mode`, `session/prompt`, `session/cancel`). Supports native
  base64 image blocks, blocking `cursor/ask_question` and
  `session/request_permission` cards via `answer_pending_question`, safe legacy
  `~/.cursor/chats` -> `~/.cursor/acp-sessions` SQLite store migration, and
  explicit diagnostic hints on upstream content-safety refusals.
- `backend/claude_hub/services/agent_stream/cursor_cli_transcript.py`: Added
  normalization for ACP `session/update` notifications (`agent_message_chunk`,
  `agent_thought_chunk`, `plan`, `tool_call`, `tool_call_update`,
  `subagent_spawned`, `subagent_state_update`) and `acp_error` records while
  retaining backward compatibility with `terminal_transcript` JSONL snapshots and
  legacy `stream-json` records.
- `backend/claude_hub/services/agent_stream/providers/spec.py`,
  `runtime/input.py`, `frontend/src/features/chat/StructuredPane.vue`, and
  `backend/tests/fixtures/provider_spec_matrix.json`: Updated `AgentType.CURSOR`
  to `CLI_PERSISTENT`, `RPC_NOTIFICATIONS`, `SDK_BASE64`, `RPC_BLOCKING`,
  `eof_is_fatal=True`, and `edit_resend=False` (Cursor ACP stores turns in
  SQLite `store.db` rather than a truncatable JSONL transcript); derived
  `input.py` question mode from `ProviderSpec` instead of hardcoded agent-type
  sets.
- `backend/claude_hub/services/agent_stream/base.py`: Recognized single-chunk
  full-message timestamped flushes (`>= 12` chars) in `_is_recent_text_snapshot`
  and tracked `completed_tool_call_ids` / `acp_tool_meta` per turn.
- `backend/claude_hub/services/restart_continuation.py`: Extended
  `_cursor_database_exists` to validate `~/.cursor/acp-sessions/<sid>/store.db`
  in addition to legacy `~/.cursor/chats` buckets.
- `backend/claude_hub/services/agent_stream/providers/network_types.py`:
  Increased `_SUCCESS_TTL_SECONDS` from `30.0` to `300.0`.
- Host configuration (`~/.cursor/cli-config.json`, backed up to
  `~/.cursor/cli-config.json.bak-20261113`): Switched `network.useHttp1ForAgent`
  from `true` to `false`.

## Verification

- Focused unit/integration checks: `./scripts/verify.sh backend-types`,
  `./scripts/verify.sh backend-format`, `./scripts/verify.sh frontend-lint`,
  `./scripts/verify.sh frontend-types`, `./scripts/verify.sh frontend-tests`,
  `./scripts/verify.sh frontend-build`, and focused `backend-tests` (801 passed).
- Live end-to-end smoke check: Ran `CursorNativeSession` + `SessionTailer`
  against the installed `agent acp` (`2026.11.11-043ef28`) over HTTP/2 proxy across
  two consecutive turns on one persistent process, confirming conversation
  memory continuity and clean process-group shutdown.
- Full `./scripts/verify.sh all` and `./scripts/verify.sh check-staged` run on
  the final candidate commit. Browser mock gates and production service restart
  remain separate per user authorization.

## Independent MASO Argon adversarial review & fixes

Ran independent read-only adversarial reviews with `gemini-4-argon_reasoning_high`
via `claude-hub agent run --provider maso` across all changed files and upstream
consumers (`native_consumer.py`, `retirement.py`, `reducer.ts`, `subthreads.ts`,
`_cursor_verify.py`, and the installed `2026.11.11-043ef28` ACP bundle). Fixed
all actionable findings and added regression coverage:

1. **Generation-fenced persistent queue & `_client_requested_stop` handoff (P0)**:
   Kept a single generation-tagged `_notification_queue` across restarts, stopped
   `read_line()` from clearing `_client_requested_stop` before `native_consumer`
   observes expected restart EOFs, and discarded superseded reader sentinels via
   `_stdout_generation`.
2. **Model re-assertion after `session/load` (P1)**: `agent acp`'s `loadSession`
   resets `modelManager` to `store.db`'s `lastUsedModel`; added
   `session/set_model` synchronization after `session/load` / `session/new`.
3. **Sub-agent thread isolation & capability negotiation (P1)**: Exposed
   `CursorSession.active_thread_id` and negotiated `clientCapabilities.subagents`
   so child `session/update` frames carry `subagent_thread` and never pollute the
   parent turn's `final_text`.
4. **Crash ordering, stdin lock & cancellation resilience (P1)**: Ordered
   `_await_prompt_completion` error records ahead of the `None` EOF sentinel on
   server crash, serialized `proc.stdin` writes with `_stdin_lock`, cleared
   pending questions/permissions on reader exit, and protected
   `cancel_active_turn()` with `try/finally` so `CancelledError` never strands
   `_turn_in_flight`.
5. **SQLite WAL-safe migration & unified path verification (P1)**: Replaced raw
   `shutil.copy2` with SQLite online `backup()` (preserving uncheckpointed `-wal`
   frames), enforced target-side symlink checks and atomic `meta.json` writes,
   and unified `resolve_cursor_roots` across `cursor.py`, `restart_continuation.py`,
   and `_cursor_verify.py`.
6. **Tool lifecycle, error deduplication & cold rotation safety (P1/P2)**:
   Forwarded late `Edit`/`Delete` diff `args` and `cancelled` status on
   `TOOL_CALL_COMPLETED` (and updated `reducer.ts` / `subthreads.ts`), preserved
   structured `Task` dict results for `subagentTool.ts`, deduplicated inline
   `acp_error` + RPC error responses via `saw_turn_error`, included
   `toolCall.content` details in permission cards, marked rejected permissions
   as `failed`, swept stale `run_epoch` `_TurnAccumulator` entries on cancel/fail,
   and deferred session adoption during cold `rotate_conversation()` fallbacks.
7. **Parameterized model resolution & `session/load` error boundary (`fix/cursor-acp-model-selection`)**:
   `agent --list-models` emits flat legacy CLI slugs (`gemini-4-carbon-high`,
   `cursor-grok-4.6-high-fast`, `gpt-5.3-codex-xhigh`, `auto`), whereas
   `agent acp`'s `session/set_model` rejects legacy variant slugs with JSON-RPC
   `-32602 Invalid model value`. Negotiated
   `clientCapabilities._meta.parameterizedModelPicker = True`, resolved legacy
   CLI slugs into base model IDs + `session/set_config_option` parameter updates
   (`reasoning_effort`, `reasoning`, `effort`, `fast`, `thinking`), narrowed the
   `session/load` `try/except RuntimeError` block to the `session/load` RPC call
   alone so post-load setup errors never fall through to `session/new` and
   overwrite an existing conversation, and deferred `_persist_conversation_id` on
   `session/new` until model/mode setup completes.
8. **Checkpoint-based `resumeAction` retries for `RetriableError: [resource_exhausted]` in `agent acp`**:
   Cursor CLI `2026.11.11`'s `./src/acp/agent-session.ts` wires
   `onConnectionStateChange` and `onErrorNotRetried` into `agentClient.run`
   `runOptions`, but omits `enableAgentRetries: true` (unlike `--print`, TUI, and
   subagent runners), causing `agentClient.run` (`retriesEnabled:
   p.enableAgentRetries ?? false`) to abort turns on `attempt=0` on any transient
   `RetriableError: [resource_exhausted]` or stream reset instead of resuming from
   the latest SQLite checkpoint via `resumeAction`. Added `cursor_acp_preload.cjs`
   (injected via `NODE_OPTIONS` and immediately scrubbed so child tool shells do
   not inherit it) to supply `enableAgentRetries = true` and `maxRetries = 8` on
   the ACP `runOptions` object.
