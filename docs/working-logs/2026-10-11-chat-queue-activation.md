# Queued Chat activation (2026-10-11)

## Symptom

A message queued behind an active Chat turn could remain on `Waiting for
response…` after the user switched to another Chat. Returning to the original
Chat allowed it to continue.

## Root cause

Each Chat owns its local draft queue inside a `StructuredPane` cached by
`KeepAlive`. Deactivation stopped that pane's structured stream and changed its
connection state to `idle`. The queue watcher needed both the authoritative
turn-completed event and a `live` connection before it could flush, while only a
later manual activation restarted the stream. The hidden queue therefore could
not observe the state transition that made it sendable.

## Resolution

An actionable queue keeps its own structured observer alive while hidden. Once
the active turn ends, the watcher selects the pane that owns the cached
`StructuredPane`, selects its Chat, and waits for KeepAlive activation before
flushing. Hidden panes never send directly. Empty queues and queues paused after
an uncertain POST still stop their stream; the latter continues to require the
existing explicit user resend.

The owning pane ID is passed from `TerminalPane` so multi-pane layouts restore
the cached instance instead of moving the Chat into another pane and losing its
local queue. Queue and activation ownership migrates at the store's tab-to-pane
assignment boundary, including when Vue reuses an already-cached pane instance.
An automatic send settles its FIFO request by the globally unique Chat ID, so
moving the Chat while its POST is in flight cannot strand the old pane owner.
The captured request generation must also match, preventing an old POST from
retiring a same-Chat request created after archive and restore.

The terminal store coordinates the minimal cross-instance queue state and
serializes ready hidden Chats through one foreground activation owner. Full
Chat panes stay under the ordinary bounded `KeepAlive`; a lightweight observer
keeps only the hidden completion stream needed to reactivate an evicted queued
Chat. Layout reduction is refused while it would remove a pane with a queued
Chat, while archive/delete explicitly discard that Chat's local queue and
advance the FIFO. Failed streams or Goal locks yield activation ownership, and
automatic queued POSTs time out into the existing delivery-uncertain pause. A
full pane's ordinary KeepAlive eviction leaves the minimal observer's ready
activation request intact.

## Verification

- The focused queue regression failed before the fix because no Chat activation
  was requested, then passed after the implementation.
- `pnpm exec node --test tests/chatFlowReliability.test.mjs
  tests/tabKeepAlive.test.mjs tests/sidebarStore.test.mjs`: 32 passed, including
  simultaneous completion FIFO, activation retry, cache protection, and layout
  reduction, removal cleanup, permanent-failure, and POST-timeout guards.
- `./scripts/verify.sh all` frontend and static targets passed: documentation,
  backend formatting and typing, frontend lint and typing, 719 frontend tests,
  and the production frontend build.
- The task-owned Vite/Playwright smoke rendered the real SPA with mocked APIs.
  It queued a draft, rotated through ten other Chats (past the ordinary cache
  bound), released the first turn's completion, observed the UI switch back,
  and recorded exactly one
  `/stream/send` request with no unknown API calls or page errors. The review
  server was stopped after the run.
- The backend suite has no changed files in this branch. Its 2,819-test full
  run remains blocked by pre-existing environment-sensitive tests: one run
  reached 1,843 passes before process creation returned `EPERM`, and two runs
  reached 1,679 passes before the same async queue-status assertion observed a
  persisted completion just before `turn_in_flight` cleared. Both failing tests
  pass when run alone in the same worktree; no backend code was changed to mask
  those unrelated failures.
