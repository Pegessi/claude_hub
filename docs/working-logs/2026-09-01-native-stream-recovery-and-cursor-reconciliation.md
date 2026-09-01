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
