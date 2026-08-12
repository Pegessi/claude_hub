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

`TTYDManager._get_next_port()` now advances past any candidate that cannot be
bound on loopback. The bind probe detects listeners and bound-but-not-listening
sockets without connecting to unrelated services. Because the probe cannot
reserve the port for ttyd, startup retries with a new candidate if another
process wins the check-to-bind race.

`TTYDManager.create_tab()` now treats startup as a rollback boundary. It tracks
whether this request created the tmux session, so failure never kills a
pre-existing session. If `TTYDProcess.start()` fails or the request task is
cancelled during a backend reload, cleanup finishes despite repeated
cancellation before the original exception is propagated.

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

- Added unit tests proving allocation skips three consecutive occupied ports,
  handles both outcomes at the maximum TCP port, retries a raced bind, and
  immediately propagates non-collision bind errors such as `EACCES`.
- Added async tests that externally cancel an in-flight creation task, cancel
  it again during suspended cleanup, and cancel while tmux ownership is being
  resolved. They prove cleanup finishes and only removes sessions owned by the
  failed request.
- Confirmed both tests fail on the old implementation and pass on the fix.
- Confirmed a live duplicate of the QFO Codex tab returned active on port
  `10394` after the three verified orphan listeners were stopped.
- After rebasing the fix onto `origin/main@fa76748`, the targeted manager,
  route, Codex-session, and cold-recovery suites pass `124/124`; Black, isort,
  and mypy report no issues in the touched production source. The earlier
  repository-wide backend run
  reached `549 passed, 63 failed`; nearly all failures share the pre-existing
  `Runner.run() cannot be called from a running event loop` test-runner
  contamination, plus one Playwright scroll-alignment failure. The new async
  regression passes both alone and in the targeted 124-test run.
