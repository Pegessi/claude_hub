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

A later live restart took 44.67 seconds. Launcher output recorded 39.16 seconds
through backend stop (including preparation) and 5.51 seconds to replacement
readiness. Combining that with the lifecycle timestamps and stop deadline gives
approximately 9-10 seconds of dependency/frontend preparation and 30 seconds
stopping the old backend; the old log did not measure those phases separately.
The old backend entered lifespan shutdown but did not persist `chat_recovery`,
stop its stream owners, or release 18 ttyd listeners before the launcher's
30-second kill fallback. The replacement then failed to bind all 18 saved ports.

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
no additional 30-second wait before cancellation. This removes the known drain
wait but is not a hard bound on preceding cleanup or cancellation finalizers.
Launcher output now reports preparation and backend shutdown separately
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

## Latest-main integration and independent cross-validation

The user authorized merging the latest main into this feature branch, with a
separate decision still required before merging the feature back to main.
Work remained in `~/claude_hub_worktree/restart-shutdown-fix` on
`codex/restart-shutdown-fix`; the primary checkout stayed clean and its main ref
was not advanced by this task.

- Original fix commit: `066ced8193a27497ab315b3c3791dcf425675439`.
- Latest local main: `3f935616eee4c41bf95395875a7d310d42cd8edc`. A fresh
  `fetch origin main` returned `d728236c1c787fc0cc65a39cc0fa594134d16da6`;
  local main already contained that ref and seven additional commits.
- Fixed merge: `816e188d71c324de7f79ec6f5ca6e1496b425c6d`, with the fix as
  first parent and latest local main as second parent. No textual conflicts or
  manual merge resolutions; `verify.sh merge-decisions` passed with zero decision
  paths. That structural check is not the independent semantic review.

Main's module extraction moved substantial provider, tailer, terminal and Feishu
behavior behind compatibility facades. Two independent reviewers traced the
new consumers and verified that the restart hooks still share the original
manager, queue and dedup owners. Runtime patch points used by the existing tests
still resolve at call time. No concrete new P0-P3 defect remained after review.

Validation on the fixed merge used worktree-local locked dependencies with
strict declared versions: Python 3.11.16, uv 0.12.15, Node 24.21.0 and pnpm 9.15.9.

| Check | Result |
| --- | --- |
| Focused Feishu/restart/Goal/facade selection | 410 passed, 37.52 seconds |
| `CLAUDE_HUB_VERIFY_STRICT_VERSIONS=1 ./scripts/verify.sh all` | Passed |
| Default backend scope with coverage | 4302 passed, 9 skipped, 227.32 seconds |
| Frontend unit tests | 1029 passed, none skipped |
| Black/isort, complete backend mypy, frontend lint/types/build | Passed |
| Checkout safety, docs, fixed-merge decision coverage | Passed |

Evidence is retained at `/tmp/ch-restart-main-merge.MF6W89/`, including
`focused.log`, `all.log`, `all.exit`, and the verification-owned backend result
directories. The Vite build emitted its existing large-chunk advisory; it did
not fail. This task did not run browser-mocks, terminal-hmr or terminal-replay.

Two additional bounded in-memory integration probes used real Feishu
handler/dedup objects and route cancellation, with external transport replaced:

1. An unstarted route cancelled during profile preparation releases its claim;
   a synthetic same-ID replay dispatches once.
2. A started turn terminalized by the real SessionTailer before route cancellation
   retains its claim; a synthetic same-ID replay does not dispatch a second time.

Both passed, including an independent owner rerun (`2 passed in 2.11s`). Probe
source is `/tmp/restart_dedup_probe.py`; the rerun log is
`/tmp/ch-restart-main-merge.MF6W89/probe-repeat/results.log`.

A third probe initially assumed that any pre-submission rejection should release
its message ID. It failed because a quiesced manager produces an ordinary failure
reply and the dedup claim is retained. Review against the common ancestor and
both parents confirmed unchanged at-most-once policy: a handled failure also
consumes the message ID, while only explicit safe cancellation/retirement releases
it. Claims are retained for seven days, not forever. The failure was an incorrect
probe expectation, not a new merge defect; no delivery policy was changed.

The final documentation-only follow-up records these results and the changelog;
it does not alter the reviewed merge's executable code. Live Feishu/provider
behavior, real ttyd listener release, and worst-case combined shutdown duration
remain unverified. No remote push, main merge, shared-service restart, or orphan
listener cleanup was performed.
