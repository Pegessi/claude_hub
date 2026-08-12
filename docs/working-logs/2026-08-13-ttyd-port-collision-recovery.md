# TTYD Port Collision Recovery

## System overview

Each local terminal tab owns a loopback `ttyd` listener and a tmux session.
`TTYDManager` persists successful tabs in `~/.claude_hub/tabs.json`, then sets
the next port to one greater than the largest persisted port after a backend
restart.

On 2026-08-13, duplicating the QFO Codex tab failed three times. Backend logs
showed `ttyd` receiving ports `10391`, `10392`, and `10393`, then exiting with
`EADDRINUSE`. All three ports were held by parentless `ttyd` processes, but no
current tab in `tabs.json` owned any of them.

## Module design

`TTYDManager._get_next_port()` now advances past any candidate with a live
loopback listener. This protects tab creation from stale Hub processes and
unrelated local processes without changing persisted tab ports.

`TTYDManager.create_tab()` now treats startup as a rollback boundary. If
`TTYDProcess.start()` fails or the request task is cancelled during a backend
reload, the unpersisted process is stopped and its newly-created tmux session
is removed before the original exception is propagated.

## Key issues and pitfalls

- The old allocator trusted only `tabs.json`; it did not inspect the operating
  system before assigning a port.
- A `ttyd` process can bind successfully during the one-second startup check
  while its tab is still absent from manager state. Abrupt or cancelled reloads
  can therefore leave a listener that normal shutdown cleanup cannot find.
- Removing a stale workspace tab does not remove an already-orphaned listener,
  because the reloaded manager only knows the replacement process object that
  failed to bind.
- Runtime cleanup must compare exact listener ports against current persisted
  tab ports before terminating anything. Existing tmux-backed tabs must remain
  untouched.

## Verification

- Added a unit test proving allocation skips three consecutive occupied ports.
- Added an async unit test proving cancellation stops the unpersisted process
  with tmux cleanup enabled.
- Confirmed both tests fail on the old implementation and pass on the fix.
- Confirmed a live duplicate of the QFO Codex tab returned active on port
  `10394` after the three verified orphan listeners were stopped.
- After rebasing the fix onto `origin/main@fa76748`, the targeted manager,
  route, Codex-session, and cold-recovery suites pass `117/117`; Black, isort,
  and mypy report no issues in the touched production source. The earlier
  repository-wide backend run
  reached `549 passed, 63 failed`; nearly all failures share the pre-existing
  `Runner.run() cannot be called from a running event loop` test-runner
  contamination, plus one Playwright scroll-alignment failure. The new async
  regression passes both alone and in the targeted 117-test run.
