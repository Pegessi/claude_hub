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
local queue.

The terminal store coordinates only cross-instance lifecycle: it protects a
pane's keyed Chat cache while local drafts exist and serializes ready hidden
Chats through one foreground activation owner. Draft text and attachments stay
inside their owning component. Layout reduction is refused while it would
remove a pane with a queued Chat.

## Verification

- The focused queue regression failed before the fix because no Chat activation
  was requested, then passed after the implementation.
- `pnpm exec node --test tests/chatFlowReliability.test.mjs
  tests/tabKeepAlive.test.mjs tests/sidebarStore.test.mjs`: 24 passed, including
  simultaneous completion FIFO, activation retry, cache protection, and layout
  reduction guards.
- `./scripts/verify.sh all` frontend and static targets passed: documentation,
  backend formatting and typing, frontend lint and typing, 711 frontend tests,
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
