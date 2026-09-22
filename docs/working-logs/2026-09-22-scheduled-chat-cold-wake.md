# 2026-09-22 — Scheduled chat_turn cold-wake: idle-reap kills the cold turn

## Symptom (P1)

Register a `chat_turn` schedule against a structured Chat, leave it idle past
the native provider / subprocess lifetime, and let it fire. The turn produced
**no reply**, the UI showed a red bar:

> Turn interrupted because its backend runtime was no longer available.

and the durable run landed `status=cancelled`,
`error="Chat turn was cancelled or its backend runtime was lost"`. Real
evidence: schedule `d1066a76…` (interval 1800 s, tab
`8700ea70-0a7e-4bdd-b098-6ff14afef726`, provider `claude`), failed run
`2be7215e-a09c-5ea1-95ed-4445a5c4c4ab`, fired 21:00:39, completed 21:05:10.

This breaks the core agent self-scheduling loop once a Chat is idle longer
than the process lifetime.

## Evidence trail

The tab's durable event store had exactly three events for that turn:

```
21:00:39  turn_started      scheduled-2be7215e…
21:05:10  error             Turn interrupted because its backend runtime was no longer available.
21:05:10  turn_completed    status=cancelled
```

Zero provider records in between. The run reached `running` (`dispatched_at`
set), so the one-shot `claude` subprocess was spawned, but it produced nothing
for ~271 s and was then terminalized as an **orphan**. A human composer send
on the same tab ~22 s later (21:05:32) streamed `thinking_delta` within 13 s —
the warm path worked. The backend process (PID 70665, up since 18:05) never
restarted, so this was an in-process wedge, not cold-restart reconciliation.

## Root cause (two coupled defects)

A scheduled `chat_turn` has **no viewer**. Its tailer is created/restarted by
the scheduler's headless dispatch path
(`TailerManager.ensure_started` → `_get_or_create` → `SessionTailer.start`),
never by `subscribe()`.

1. **Stale idle clock across a headless restart.** An idle-reaped tailer is
   *stopped but left registered* in `TailerManager._tailers`.
   `SessionTailer.start()` recreated the consumer but did **not** reset
   `_last_subscriber_at` (`services/agent_stream/tailer.py`). Only
   `__init__` and `subscribe()` refreshed it. So the fresh consumer was born
   already past `IDLE_TTL_S` (300 s).

2. **Idle-reaper TOCTOU.** The native consumer's idle gate read
   `transport.turn_in_flight` **outside** `_send_lock`, then acquired the lock
   and called `transport.stop()` **without rechecking** (the stream-inactivity
   reaper just above it double-checks; the idle reaper did not). For a
   one-shot provider (Claude/Cursor) the cold spawn is async: the provider
   guard is raised only after `create_subprocess_exec` + stdin write. The
   scheduler's `send_message` publishes `turn_started`, awaits the spawn, and
   yields; the fresh consumer's first tick saw the stale idle clock, read the
   guard while it was still down, and `stop()`-killed the just-spawned cold
   process before it emitted anything.

The killed turn stayed `running` in the scheduler with no provider owner. When
the UI later attached, the first-touch orphan repair
(`_recover_orphaned_turn_locked`, `tailer.py`) terminalized the unfinished
durable turn with the "backend runtime was no longer available" message and
`turn_completed(cancelled)` — the red bar.

The contrast with the composer is the missing viewer: the UI always holds a
`subscribe()` before sending, which refreshes `_last_subscriber_at`; the
scheduler's headless `ensure_started` did not. The argv/env of the one-shot
spawn are identical cold or warm, so this was purely lifecycle, not command
construction.

## Fix

Minimal, aligned with the existing tailer/scheduler architecture.

**A. Tailer — never reap a headless cold turn** (`services/agent_stream/tailer.py`):
- `start()` resets `_last_subscriber_at = time.monotonic()` when it creates a
  new consumer. Restarting the consumer (via a UI subscriber *or* a headless
  background dispatch) is explicit liveness evidence.
- The idle reaper now re-checks `transport.turn_in_flight or
  self._active_turn_id is not None` **under `_send_lock`** and only stops the
  transport when still idle — the same double-checked locking the inactivity
  reaper already uses. A viewer-less scheduled turn now survives exactly like
  a watched one; a genuinely idle tailer is still reaped.

**B. Scheduler — ensure runtime ready + bounded cold retry**
(`services/workspace_manager/_scheduling.py`, `api/agent_stream.py`):
- New injected readiness probe `configure_scheduled_chat_readiness`,
  implemented by `_ensure_scheduled_chat_runtime(tab_id)`: it
  `ensure_started`s the direct-tab tailer (spawning a cold provider) and
  returns ready only when the transport exists, has no native error, is not
  hard-failed, is `_started` (one-shot flips synchronously on the first
  consumer tick; persistent Codex stays false until spawn), and — for
  persistent app-servers (`eof_is_fatal`) — has completed its handshake.
- The FIFO drain gates immediately before the `DISPATCHING` transition:
  - ready → dispatch as before;
  - not ready → park `WAITING` ("waiting for the Chat runtime to start"),
    increment durable `ScheduledTaskRun.dispatch_attempts`, and schedule one
    de-duplicated per-tab redrain (`_schedule_chat_redrain`, 5 s);
  - still not ready after `_SCHEDULED_CHAT_COLD_MAX_ATTEMPTS` (12 ≈ 60 s) →
    `FAILED` with an explicit reason rather than retrying forever.

## Preserved guards (no behavior regressions)

- Busy Chat / in-flight turn → still `WAITING` ("current Chat response") via
  the dispatch `RuntimeError`, and readiness is consulted only after the Goal
  admission gate, so an active Goal is never spawned-into or interrupted.
- Archived / deleted / backend-changed targets → existing skip/cancel rules.
- No double fire: readiness is reached only after the head selection, and the
  existing per-tab drain lock + per-task fire lock/cooldown are unchanged.
- No wake-up pile-up: at most one redrain task per tab; backlog still bounded
  by `_SCHEDULED_CHAT_ACTIVE_RUN_LIMIT` (100) and occurrence superseding.

## Tests

- `tests/test_agent_stream.py::test_headless_restart_after_idle_does_not_reap_cold_scheduled_turn`
  — deterministic regression: stale idle clock + headless `start()` + a cold
  one-shot whose spawn yields before raising the guard must keep the consumer
  alive and never call `stop()`. (Fails on the old code: consumer exits,
  `stop_called=True`.)
- `tests/test_scheduled_tasks.py`: warm runtime dispatches immediately; cold
  runtime parks (not cancelled/never delivered) then dispatches when ready;
  bounded `FAILED` after the attempt cap; the self-scheduled redrain delivers
  with no manual tick and leaves ≤1 redrain task; an active Goal parks before
  the readiness gate is even consulted.

## Pitfalls / notes

- The idle reaper reads `turn_in_flight` in the `if` each loop; the fix moved
  the authoritative decision under `_send_lock` so a concurrent send cannot
  slip between check and action. Keep it that way — the outside read is now
  only a cheap pre-filter.
- `_started` for a one-shot provider is set by the consumer's first
  `transport.start()` tick, so the readiness probe yields once (`sleep(0)`)
  before deciding; a persistent app-server additionally needs
  `_handshake_complete`.
- Readiness failing (probe error / unavailable transport) is treated as
  "not ready yet" and retried within the bounded cap — never as a silent
  success.

## Possible follow-ups (not done — minimal change)

- End-to-end isolated-runtime verification (own port + STATE_ROOT +
  `tmux -L`) of a real cold Claude Chat with a short interval; pytest covers
  the lifecycle deterministically.
- Surfacing `dispatch_attempts` / the cold-start waiting reason in the
  scheduled-task API view/UI is optional (the run's `waiting_reason` already
  records it).
