# TraeX branch review and protocol fixes

## Scope and system overview

- Reviewed source: `feat/traex-agent` at `bd724cd3a126043c022f7d5429f62b81e0f0d5e3`.
- Target and merge-base: `75d4bb8fc8f34e9b08e592e6eeaa4df4bcb5605b`.
- Fix checkout: `~/claude_hub_worktree/traex-review`, branch
  `fix/traex-agent-review`.
- Read the complete branch diff and traced tab creation, native Chat, model
  updates, cancellation, tool/approval events, Terminal launch/replay, and
  workspace admission. TraeX managed workers and terminal transcript recovery
  remain outside the feature scope.

The Vue composer sends through the tab stream API and SessionTailer. A native
transport owns one TraeX app-server, and a shared Codex adapter turns its
notifications into persisted Hub events. Terminal sessions instead run the
TUI under tmux/ttyd. The same framing does not imply identical RPC contracts.

## Findings and changes

1. **P1: Stop did not stop TraeX.** The inherited implementation called
   `turn/cancel` with empty parameters and swallowed the RPC error before
   releasing the turn guard. TraeX 0.205.1 rejects that method with
   `unknown variant turn/cancel`. Its generated schema requires
   `turn/interrupt {threadId, turnId}`. The transport now retains the provider
   turn ID and waits for interruption completion before releasing ownership.
   Failed or timed-out interruption terminates the app-server. Output queued
   before Stop and output already dequeued by the tailer are both checked
   against the retired turn, preventing it from completing a later turn.
2. **P1: Chat ignored explicit permission settings.** A Solo session actually
   returned `approvalPolicy=on-request` and `sandbox=workspaceWrite`, inherited
   from global configuration. The opposite global configuration could also
   override a non-Solo tab. Thread creation/resume and turn start now receive
   explicit policies: normal workspace-write/on-request, Solo full-access/never,
   Plan read-only/on-request. A selected model is also passed before thread
   initialization, so a stale global default cannot prevent a valid selected
   model from starting. Updating tab settings propagates Solo to the owner.
3. **P1: Permission requests were rejected without a usable UI.** The inherited
   request handler accepted only requestUserInput; command/file approval requests
   received method-not-found. TraeX now renders them through the existing Chat
   card component and sends the protocol's decision response. Answers are
   matched to request/question IDs, claimed before async writes, and unknown
   selections do not grant access. Resolving one card does not close another.
4. **P2: Chat dropped tool activity and error details.** Live command execution
   produced item/started and item/completed, but the adapter ignored both while
   advertising tool-timeline support. It also ignored error notifications and
   treated interrupted as completed. The shared normalizer now emits tool
   starts/results, provider error details and cancelled completion. Text and
   reasoning items continue through deltas, avoiding duplicate text.

These are gaps in applying the existing Codex implementation to TraeX; the
legacy Codex cancellation contract was not migrated in this review. Shared
normalization improvements apply to both providers.

## Module design and simplification

- Keep framing, process ownership and image staging in CodexNativeSession.
  Thread/turn configuration and notification hooks let TraeX supply its
  protocol differences without duplicating the reader or send pipeline.
- Reuse the existing approval UI and persistence rather than introducing a
  second frontend permission system. Question IDs distinguish concurrent cards.
- Combine Cursor/TraeX Terminal shell wrappers and keep Solo flags in one
  TraeX command builder.
- Read the adapter's declared transcript capability directly and shorten
  repeated design commentary. Use TraeX consistently in the selector/labels;
  make excludeTypes honor every provider.

## Validation and pitfalls

- Original baseline: 200 native/stream/TraeX tests passed, demonstrating that
  they did not exercise the missing protocol contracts.
- Final focused suite: **486 passed** across test_traex_agent,
  test_agent_stream_native, test_agent_stream, test_ttyd_manager, test_tabs,
  test_terminal_proxy, test_workspace_sessions,
  test_cursor_cli_transcript_agent_stream, test_agent_stream_attachments and
  test_agent_stream_coalescer. Use a fresh CLAUDE_HUB_HOME and a named isolated
  tmux socket; existing stream tests with fixed session IDs can replay stale
  events when a previous failed run reused its runtime directory.
- Black and isort passed for touched Python files; mypy passed all 95
  production source files. Existing Pydantic/datetime deprecation warnings
  remain. Frontend lint, type check, build and all **390 tests** passed; the
  existing bundle-size warning remains.
- Live CLI: generated TraeX 0.205.1 JSON schemas; reproduced the invalid cancel
  request and ignored Solo settings; verified selected Seed-Evolving model,
  full-access/never settings, command item notifications, and interruption of
  a running sleep command with the app-server remaining reusable.
- Isolated UI on 5279/8279: created TraeX Chat, sent a real command request,
  observed exec_command COMPLETED and TRAEX_REVIEW_OK, refreshed and verified
  history. Filtered the model picker and switched to Seed-Code; edit-resend
  stayed hidden. No browser page errors. Terminal creation displayed TraeCode CLI
  0.205.1 with YOLO permissions on the isolated tmux socket.
- Permission-card and concurrent-answer cases use protocol-shaped mocks;
  they are not a live verification of every tool or approval variant. Remote
  SSH launches, all model choices, and a full machine reboot were not exercised.
- Terminal transcript discovery/resume and Chat edit-resend are deliberately
  unavailable; static model options still accept custom IDs.

The live 5173/8173 services and default tmux server were not modified. Test
tabs and dedicated review servers are cleaned up before handoff.

## Follow-up: model-aware reasoning effort

TraeX Chat originally exposed model selection but silently inherited the
global `model_reasoning_effort`. The app-server's live `model/list` response is
now the source of truth for each model's default and supported effort values.
The composer shows a TraeX-only Thinking picker when the active model reports
choices and persists the override through the existing tab environment hot
update path. Switching to an incompatible model removes a stale override in
the same update.

The protocol detail is important: `TurnStartParams.collaborationMode` takes
precedence over top-level `model` and `effort`. Hub therefore writes the user
selection to `collaborationMode.settings.reasoning_effort`, overriding a mode
preset while retaining its mode and permissions. Repeated capabilities polls
must restore the cached live TraeX catalog after Codex's inherited static model
refresh; otherwise the effort metadata disappears after the first request.

Validation used an isolated runtime and tmux socket on 5279/8279. GPT-5.6-Sol
advertised Default (medium), low, medium, high and xhigh; selecting high was
persisted and a real turn returned `TRAEX_EFFORT_OK`. Switching to Seed-Code
hid the picker and removed the override. No browser page errors occurred.
