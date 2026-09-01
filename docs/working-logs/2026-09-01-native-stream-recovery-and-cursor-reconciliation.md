# Native stream recovery and Cursor snapshot reconciliation

## System overview

Paseo Agent sessions use one provider-native process for both composer input
and structured output. The frontend hydrates persisted events, subscribes to
live events, and applies a paced text reveal. A raw Terminal session remains a
separate product choice; recovery must never switch an Agent session to tmux
input or create a second provider conversation.

This change hardens two lifecycle edges in that path:

1. a Codex native transport can be stopped by the idle tailer reaper and later
   restarted against the same persisted provider thread;
2. Cursor stream-json can emit incremental chunks followed by an exact replay
   of the completed assistant message, including replays that still carry a
   timestamp.

## Module design

### Codex process generations

`CodexNativeSession.start()` now gives every app-server process generation a
new notification queue. The stdout drain task places an EOF sentinel in the
queue it owns. Reusing that queue after an idle stop allowed a newly started
app-server to consume the old sentinel immediately, even though the new
process remained alive.

`TailerManager.retry()` is the explicit recovery boundary. It stops and drops
the failed tailer/transport, clears process-local hard-failure and source-cache
state, and constructs a new adapter plus native transport. The managed
session's provider conversation id is retained, so retry resumes rather than
forks the conversation.

The tab and managed-session stream APIs expose this as `POST .../stream/retry`.
The frontend Retry button calls that endpoint before hydrating and subscribing
again.

### Cursor message replay reconciliation

The shared turn accumulator records both accumulated text and provider chunk
boundaries. A Cursor record is treated as a replay only when its text exactly
matches a suffix composed of at least two complete prior chunks. This narrow
proof removes the observed full-message replay while preserving a legitimate
single repeated delta.

The same check is applied to timestamped assistant records and to final
snapshot records. It is scoped to the most recent message suffix, so a single
turn may still contain multiple assistant messages.

The timeline fold mirrors this conservative check during hydration. This does
not mutate or renumber the append-only event log; it only prevents an already
persisted exact multi-chunk replay from being rendered a second time. Thus the
fix repairs existing preview conversations as well as future provider output.

### First-activity feedback

The structured timeline derives an `awaitingAgentActivity` state for an active
turn that has no thinking, assistant text, tool activity, status, or error yet.
It renders a low-motion three-dot waiting card on the agent side. The card is
replaced by the existing thinking card or paced text reveal as soon as the
first provider event arrives, and its animation respects reduced-motion user
preferences.

## Key issues and pitfalls

- An OS process being alive does not prove its consumer task is attached to the
  correct process generation. Generation-owned queues must not retain terminal
  sentinels across restart.
- Retry must rebuild state rather than call a no-op poll on a hard-failed
  tailer. It must also retain the provider conversation id to avoid silently
  creating a new conversation.
- Timestamp presence is not a sufficient Cursor delta/snapshot discriminator.
  Deduplication must be content- and boundary-based, but broad suffix matching
  would incorrectly remove intentionally repeated text.
- UI pacing cannot repair duplicated source events. Reconcile the authoritative
  event stream first, then use the reveal scheduler only for visual cadence.

## Validation

- Backend focused tests: 79 passed.
- Frontend unit tests: 136 passed.
- Existing Codex preview tab: explicit retry restored structured capabilities,
  then one turn produced four text deltas followed by `turn_completed` with the
  exact reconstructed response `CODEX_RECONNECTED`.
- Existing Cursor preview tab: one turn produced 21 text deltas followed by
  `turn_completed`; `ALPHA-ONE`, `BETA-TWO`, `GAMMA-THREE`, and
  `EXPLANATION-FOUR` each appeared exactly once in reconstructed text.
- Frontend: 139 unit tests, ESLint, Vue type checking, and production build
  passed. Python Black, isort, and mypy passed.
- Headless Chromium hydration of the existing Cursor tab rendered the already
  persisted poem title and explanation once each; the new four response markers
  also appeared exactly once each within assistant bubbles, with no structured
  failure banner.
- The broad backend run reached 1,156 passed and 1 skipped, with 17 failures in
  pre-existing task-followup/session-seat and real ttyd/Playwright suites that
  do not touch the agent-stream modules. Representative failures reproduced in
  isolation; they remain a separate branch gate rather than being reported as
  validation of this change.

## Claude multi-message tool turns: per-message reconciliation and ordered parts

### Symptom

A native Claude turn that uses a tool (for example `WebSearch`) produced two
defects:

1. the UI rendered the final assistant text before the tool card;
2. a reconciliation error banner appeared:
   `assistant final thinking does not match streamed deltas; cannot safely
   reconcile`.

### Evidence

The failing turn's event stream (bounded to that turn) had the semantic order:
thinking segment → tool_call_started → tool_call_completed → error → text
segment. The raw provider JSONL showed two distinct assistant messages within
the single native turn:

- message `msg_389…`: one row carrying `thinking`, a second row carrying
  `tool_use`;
- message `msg_3b0…`: one row carrying `thinking`, a second row carrying
  `text`.

A user `tool_result` message separated the two assistant messages.

### Root causes

1. **Backend reconciliation scope.** The text/thinking accumulator was
   turn-scoped. Across the two assistant messages the accumulated thinking
   became `msg_389_thinking + msg_3b0_thinking`, so the second message's final
   thinking snapshot failed the strict `final.startswith(accumulated)` check
   and tripped the fail-closed error.
2. **Frontend flattening.** `TimelineTurn` stored flat `thinkingText`,
   `assistantText`, and `tools` buckets, so even a correctly-ordered event
   stream could not preserve interleaving (thinking → tool → thinking → text).
   The fixed render order placed assistant text before tool cards even though
   the persisted tool events preceded the final text.
3. **Native tool observation gap.** The adapter ignored streamed
   `content_block_start` / `input_json_delta` / `content_block_stop` tool
   records and depended on top-level assistant snapshots. The captured Claude
   2.1.159 snapshot was timely, so this did not cause the original visual
   reordering, but it left incomplete-provider streams unobservable and needed
   explicit cross-source deduplication once both paths were supported.

### Backend fix

- Added `active_provider_message_id` to `_TurnAccumulator`. Text/thinking
  accumulation is now scoped to the provider `message.id`.
- `_begin_provider_message(ctx, message_id)` resets per-message buffers
  (`text`, `thinking`, `text_chunks`, `pending_tool_inputs`,
  `pending_tool_meta`) only when the id changes; consecutive rows sharing an
  id keep accumulating.
- `message_start` and `_normalize_assistant` (final-only/backfill rows) both
  call `_begin_provider_message`, so live streams and transcript hydration
  share the same scoping.
- `emitted_tool_call_ids` stays turn-scoped so a tool emitted from the
  streamed block is not re-emitted from the final snapshot.
- Streaming tool args are assembled by content block index from
  `content_block_start` → `input_json_delta` → `content_block_stop`.
  Claude 2.1.x may publish the top-level assistant `tool_use` snapshot before
  `content_block_stop`; other producers may use the inverse order. Both paths
  record/check the same id, so the first complete representation emits
  `TOOL_CALL_STARTED` and the later representation is suppressed.
- Genuine text/thinking mismatches still emit `error` (fail-closed preserved).

### Frontend fix

- `TimelineTurn.parts: TimelinePart[]` is the authoritative render order.
  Part kinds: `thinking`, `text`, `tool`, `error`, `status`.
- `appendTextPart` extends the last same-kind part or pushes a new one, and
  updates the flat aggregates (`thinkingText`/`assistantText`) as derived
  views for existing callers/tests.
- `tool_call_started`, `error`, and `status` push ordered parts so they appear
  where the provider emitted them (Paseo does not defer protocol errors to the
  turn end).
- `StructuredPane` renders `turn.parts` directly. Paced reveal is keyed per
  text part (`revealStates[part.key]`), not per turn, so each text segment
  reveals independently while preserving stream order.

### Key issues and pitfalls

- Resetting on `message_start` alone covers the live native stream but not
  final-only transcript/backfill rows; `_normalize_assistant` must also scope
  by `message.id`.
- `emitted_tool_call_ids` must stay turn-scoped, not message-scoped, so the
  final snapshot's `tool_use` can be suppressed even though it belongs to a
  different provider message than the streamed block.
- The reconciliation contract remains strict: exact match → skip, strict
  extension → emit suffix, empty accumulator → emit full, otherwise fail
  closed. The fix narrows the accumulator scope, it does not relax the check.
- Flat aggregates are kept only as derived views; `parts` is the source of
  truth for render order.
- A malformed or non-object streamed tool-argument payload is not authoritative:
  do not emit or deduplicate it. Wait for the final assistant snapshot so a
  truncated native stream cannot permanently replace complete tool args.

### Validation

- Backend focused agent-stream tests: 53 passed (fresh `CLAUDE_HUB_HOME` +
  unique `CLAUDE_HUB_TMUX_SOCKET`). New RED-then-green fixtures:
  - streamed tool args with real block index → exactly one `TOOL_CALL_STARTED`
    with parsed args;
  - final tool snapshot before block stop → exactly one `TOOL_CALL_STARTED`;
  - malformed streamed tool args → defer to the complete final snapshot;
  - two-assistant-message turn scoped by message id → no error, both thinking
    segments present, text correct;
  - consecutive same-id rows accumulate;
  - genuine text mismatch still emits `error`.
- Frontend `agentStreamTimeline` tests: 19 passed (interleaved
  thinking→tool→thinking→text produces ordered parts).
- Frontend full unit suite: 140 passed; ESLint, vue-tsc, production build
  pass.
- Black, isort, mypy on `base.py` and `claude_jsonl.py`: clean.
- Real browser E2E on the isolated preview (`5275`/`18173`) reused the day1
  Claude tab and required `WebSearch`. The first audit caught two persisted
  `tool_call_started` events even though the UI identity map showed one card;
  direct Claude 2.1.159 stream-json evidence established the real ordering as
  assistant tool snapshot before `content_block_stop`, leading to the
  bidirectional deduplication fix and regression test.
- The final E2E turn `f8e34d80-4269-41b0-ad3e-a9c734e89bb1` observed the
  waiting placeholder, then rendered `thinking → tool → thinking → text`.
  Its persisted stream had exactly one `tool_call_started`, one matching
  `tool_call_completed`, no `error`, one final marker, and one completed turn.
