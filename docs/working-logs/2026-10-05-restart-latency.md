# Supervised restart latency and cleanup

## Incident

The live request at 22:47:27.648 completed at 22:48:22.388 (54.74 seconds).
The replacement process started about 30 seconds after the request. The old
backend never logged application shutdown/ttyd cleanup; an old ttyd was still
listening with parent PID 1, and the replacement logged 32 ttyd start failures.
Startup also spent 12.8 seconds before the saved-tab partition and another
8.5 seconds on reattachment and readiness.

Uvicorn's default shutdown waits without a deadline for response tasks before
entering ASGI lifespan shutdown. A Chat SSE response can stay open indefinitely.
The launcher then reaches its 30-second kill fallback, bypassing the code that
flushes stream state and releases owned terminal listeners. A real open-SSE
regression reproduced that failure before the fix.

## Changes

- The launcher supplies `UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=3` to its backend
  child. Uvicorn cancels remaining response tasks after that interval and then
  runs normal lifespan shutdown; the outer 30-second limit remains a fallback.
- Stop workspace dispatch first. Stream owners stop concurrently while each
  still flushes/terminalizes its own turn; ttyd cleanup also runs concurrently.
  All cleanup attempts complete before reporting an error. The backend never
  kills tmux sessions as part of normal shutdown.
- Backfill checks use one local Codex rollout snapshot per startup. Candidate
  matching, ambiguity checks, and live-session checks remain intact. Cold
  launch attribution retains independent before/after scans inside its lock.
- Hot reattachment skips cold-launch scans and per-tab global tmux environment
  refreshes. The manager still refreshes the environment once at startup, and
  cold starts still refresh it before creating a pane.
- Launcher output records shutdown, readiness, and total elapsed time.

## Validation

- Five targeted regressions failed before their fixes: live SSE cleanup,
  repeated backfill scans, unnecessary hot-start scans, serialized ttyd stop,
  and serialized stream-owner stop.
- 323 restart/terminal/recovery/stream tests and 140 native provider tests
  passed. The additional shared-snapshot ambiguity regression passed, for
  464 tests in total. Black/isort, scoped mypy, and diff checks passed.
- Full-backend A/B against `e7a80de`: identical fixture shapes with 176 saved
  Codex tabs, 32 already-live tmux sessions, 200 synthetic rollout headers,
  and a continuously open SSE response. Separate runtime homes and named tmux
  sockets; sleeping shell panes only, no model requests.

| Measurement | Before | After |
| --- | ---: | ---: |
| Restart to matching healthy instance | 41.327 s | 9.980 s |
| Healthy owned ttyd children after restart | 0 / 32 | 32 / 32 |
| Preserved original tmux sessions | 32 / 32 | 32 / 32 |
| ttyd startup failures | 32 | 0 |

The new run spent 3.33 seconds stopping and 6.65 seconds reaching readiness.
This is an isolated fixture comparison, not a live production timing promise.
A preliminary 2,000-rollout baseline exceeded the fixture's 90-second initial
startup limit; it was cleaned up and is excluded from the A/B numbers above.
All test process groups and named tmux servers were stopped after measurement.

## Activation boundary

The launcher is a persistent process: the existing menu restarts only its
backend child. This change to the launcher's child environment therefore needs
one external-terminal restart of the launcher (the normal `./start.sh` entry).
After activation, subsequent menu restarts use the bounded drain automatically.
Merging this change alone does not change the already-running launcher.
No production restart or live terminal cleanup was performed by this task.

## 2026-10-18 follow-up: Feishu route drain blocked restart cleanup

A later live restart took 44.67 seconds. Launcher output split the observed run
into about 9.5 seconds of dependency/frontend preparation, 29.7 seconds stopping
the old backend, and 5.5 seconds starting the replacement. The old backend
entered lifespan shutdown but did not persist `chat_recovery`, stop its stream
owners, or release 18 ttyd listeners before the launcher used its 30-second kill
fallback. The replacement then failed to bind all 18 saved ports.

The blocking turn came through the Feishu WebSocket path. Lifespan shutdown
waited first for the supervisor's 30-second accepted-route drain, while that
accepted route was itself waiting for the native Chat turn that later shutdown
steps would stop. Cleanup therefore could not make progress before the launcher's
outer deadline.

The follow-up separates Feishu shutdown into two phases. Shutdown first stops
new Feishu intake and closes its connections without draining accepted routes.
It then captures eligible restart continuations, stops stream owners, and releases
ttyd listeners. Only after those cleanup-critical steps does it cancel any
accepted Feishu routes that are still running; the app shutdown path gives them
no additional 30-second wait, so cancellation starts before the launcher's outer
deadline. Launcher output now reports preparation and backend shutdown separately
instead of combining them under `backend stopped`.

The interrupted Dot turn in this incident had `origin=feishu`. Planned restart
continuation intentionally accepts direct Web Chat turns and eligible Goal turns,
not Feishu/Bot turns; this change does not alter that ownership boundary. It
restores the capture path that the forced old-process kill skipped, and deployment
documentation now states the Feishu/Dot limitation explicitly.

Focused validation covers the two-phase supervisor behavior and the real
launcher/app process restart order. The process regression requires the final
Feishu stop call to observe no provider turn in flight, a completed return from
the configured ttyd cleanup hook, and a zero-second pre-cancellation wait; it
also retains the same-conversation, one-attempt continuation checks. Provider,
Feishu transport, and ttyd cleanup are replaced in that process fixture, so it
proves lifecycle ordering and persisted continuation, not real listener release
or live Bot delivery. An isolated HEAD copy with the current process regression
failed under the old shutdown order: final Feishu stop saw a live provider turn,
the cleanup hook had not run, and no zero-second cancellation budget was supplied.
The focused Feishu/restart/Goal selection passed 175 tests; a broader Feishu and
restart cross-validation passed 402 tests. Changed source modules passed scoped
mypy and Ruff checks. The shared service was inspected only; this follow-up did
not restart it, clean its orphan listeners, or run a live Feishu/provider
continuation.
