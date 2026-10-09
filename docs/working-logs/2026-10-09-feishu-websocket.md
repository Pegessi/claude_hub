# Feishu Bot WebSocket migration

## Outcome

Claude Hub now receives Feishu Bot messages through the official long-connection
SDK instead of exposing one HTTP callback URL per Bot. An enabled, fully
configured Bot needs only a display name, App ID, and App Secret. Verification
Token, Encrypt Key, public callback URL configuration, and URL verification are
not part of this mode.

This work was developed in `~/claude_hub_worktree/feishu-websocket` on
`codex/feishu-websocket`. It does not restart the primary Hub or connect test
credentials to the live Feishu service.

## Runtime and lifecycle

The process-level supervisor reconciles the effective Bot pool into one
`lark-oapi` WebSocket client per enabled Bot. It starts during FastAPI lifespan,
stops before the remaining Hub background services, applies pool mutations
immediately, and periodically retries failed connections. Credential rotation,
disable, and deletion replace or stop the affected connection without changing
the existing pairing semantics.

The pinned `lark-oapi==1.5.3` public `start()` method owns an event loop, so it
cannot be embedded in FastAPI. The adapter uses that pinned version's async
`_connect()` and `_disconnect()` core, takes ownership of receive/ping tasks,
and bounds shutdown. Synchronous SDK imports run off the Hub event loop.
Connection-URL discovery uses a bounded asynchronous request instead of the
SDK's unbounded synchronous request, while retaining the SDK response model and
dynamic client configuration. An in-flight handshake is
cancelled before close to avoid waiting on the SDK connection lock. Already
accepted message-routing tasks are independent of the transport: a transient
disconnect does not cancel a Chat dispatch that has already passed into Hub.
Transport identity deliberately excludes the pool's business revision and
generation, so issuing or claiming a pairing code cannot invalidate an
unchanged WebSocket or discard the next event. Only Bot identity, App identity,
credentials, API origin, enablement, and deletion affect transport replacement.

Accepted routes are owned by one process-level bounded task set rather than an
individual socket. When the set is full or shutdown has stopped intake, the
synchronous SDK callback raises so the frame receives a retryable failure
instead of a success acknowledgement. Transport replacement leaves accepted
routes running. Final process shutdown stops intake, closes transports, and
drains routes for a bounded interval before cancelling the remainder. A
cancelled external dispatch first retires its matching native Chat turn; only
after that bounded cancellation completes (including an already-absent matching
turn) is its message dedup claim released for a Feishu retry, before the
remaining Chat runtime is torn down.

The SDK-authenticated `im.message.receive_v1` object is normalized into the
existing `FeishuMessageEvent`. App identity, user sender, one-to-one Chat, text
message type, content size, decimal millisecond timestamp, pairing admission,
deduplication, target validity, and outbound reply checks remain fail-closed.

## Configuration and migration

The environment Bot now requires only:

- `CLAUDE_HUB_FEISHU_BOT_APP_ID`
- `CLAUDE_HUB_FEISHU_BOT_APP_SECRET`

The former Verification Token and Encrypt Key variables are reported as
deprecated and ignored. Stored pool state advances from version 2 to version 3.
Version 2 files remain readable, but their callback secrets are discarded in
memory and omitted from the next atomic write. The legacy version 1 single-Bot
file still migrates when App ID and App Secret are present; any retired fields
in that file are ignored.

The HTTP `/api/feishu/bot/events/{bot_id}` route and callback-only public origin
resolver are removed. Bot summaries expose `connection_status` as `connected`,
`connecting`, `failed`, or `stopped`; pairing-code responses no longer return an
event URL. The management dialog and pairing panel use the same contract and no
longer ask operators to copy or enter callback configuration.

## Verification

- Feishu backend pool, security, state migration, admission, protocol bridge,
  and WebSocket tests pass, including one-connection-per-Bot reconciliation,
  credential replacement, disable/delete shutdown, in-flight handshake
  cancellation, pairing-revision transport stability, bounded endpoint
  discovery and route capacity, disconnect-safe in-flight routing, final
  shutdown drain/cancellation, retry-safe dedup release, SDK event
  normalization, and rejected payloads.
- The complete frontend Node suite passes 700 tests. `pnpm lint:check` and
  `pnpm exec vue-tsc --noEmit` pass.
- The full backend suite was run once. Its only failure was
  `test_goal_question_followup_pauses_before_manual_send`, where a pre-existing
  test double rejects the current `visible_text` keyword. The same node fails
  identically in an isolated worktree at the unchanged base SHA `fd3edac`; no
  Feishu code participates in that path.
- A live Feishu connection is not exercised because no test App credentials
  are stored in this task. Runtime compatibility is pinned to `lark-oapi`
  1.5.3 and covered with deterministic adapter/supervisor tests.
