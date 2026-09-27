# Manual Stop resets any wedged/reconnecting turn — idempotent cancel + late-completion lock

Date: 2026-09-27
Branch: `fix/stop-resets-any-wedged-turn` (WIP `302cd62` on top of the queue
fix `1c4ef5c` and the TraeX null-turn fix; finalized in `71a6f61` + a gate/docs
follow-up).
Surface: structured Chat, all native transports (one-shot Claude/Cursor and the
persistent Codex/TraeX app-server family).

## 1. Incident

Two field tabs that the queue fix (`1c4ef5c`) did not fully close:

- **`896983b0`** — the backend had already persisted `turn_completed{cancelled}`
  and released the Hub active-turn guard, so a *repeat* `POST /stream/cancel`
  returned `{ "ok": true, "cancelled": false }` ("nothing new to
  terminalize"). The browser, however, was still painted on the provider's
  **"Reconnecting… 1/5"** banner: the durable cancelled edge was trapped behind
  a long-poll/SSE observation plane that was itself wedged. Stop looked dead
  and the composer never unlocked.
- **`b9cfeae6`** — a live process kept emitting records (real activity) but the
  turn **never closed**. That shape is deliberately exempt from every
  wall-clock auto-watchdog (a legitimately long task must not be false-killed),
  which left only an explicit user Stop as the escape hatch — and Stop had to be
  guaranteed to free the composer immediately, not after the provider finally
  noticed an interrupt.

The common requirement: **manual Stop is unconditional recovery**, and it must
converge the UI even when its own terminal edge cannot be observed yet.

## 2. Manual Stop vs the automatic watchdogs

The system already distinguishes two terminators; this change makes the boundary
explicit and safe to run concurrently.

*Automatic watchdogs* (`tailer.py` poll loop) are conservative and transport-led:

- goal-terminal grace, capacity-queue wait cap, queue-heartbeat stall,
  streaming-inactivity, and the outer hard-liveness bound;
- recent model/tool activity or an open approval card suppresses them — **there
  is intentionally no absolute duration cap on a turn that keeps emitting**;
- they run *inline*: kill/reap, relaunch and resume before continuing, because
  the loop immediately needs the replaced transport.

*Manual Stop* is user intent and must work for any unfinished turn, including one
the watchdogs deliberately spare. It now:

1. persists the terminal `cancelled` edge and releases the **Hub** guard
   (`_active_turn_id = None`) **first**;
2. returns to the HTTP caller immediately;
3. leaves the bounded **provider** teardown (interrupt grace, otherwise
   kill + relaunch + resume) to a background task.

`watchdogs_armed = transport.turn_in_flight and not self._turn_teardown_pending`,
re-checked inside `_send_lock` in every branch, disarms all five watchdogs while
a Stop teardown owns the provider guard, so a stale/queued/long turn can never be
double-reaped or race the kill/relaunch.

## 3. Background teardown barrier

`SessionTailer` gained `_turn_teardown_task` / `_turn_teardown_pending` and
`_await_turnteardown()`:

- `_cancel_active_turn_locked(..., await_teardown=False)` (manual Stop) spawns the
  teardown; the task re-acquires `_send_lock` (FIFO) before calling
  `transport.cancel_active_turn()`, so the provider is never replaced while
  another send holds the lock;
- `await_teardown=True` is kept for the inline owners — `steer` (which re-sends
  on the same transport), every watchdog reap, and shutdown;
- `send_message()` awaits the barrier **before** taking `_send_lock`, so a fast
  resend after Stop is ordered after the kill/relaunch and reaches the fresh
  app-server instead of the process being killed;
- `stop()` drains/inherits the teardown first so a fresh resume cannot outlive a
  tailer that is shutting down.

## 4. Idempotent cancel and the no-active-turn reset

`cancel_turn(expected_turn_id=…)`:

- with a live in-flight turn whose id matches (or no hint) → terminalize once;
- while a teardown is already pending (a second Stop, or one already queued on
  the lock) → return `True` without persisting a second edge;
- with no live turn (backend restart dropped the process-local guard but the
  durable stream still has an open turn) → `_recover_orphaned_turn_locked`
  terminalizes the latest orphan with `status=cancelled` **and notifies the
  post-persist observers** (the wake signal that drains scheduled-run / Goal
  FIFOs); a repeat call then finds no open turn and is a clean no-op;
- `expected_turn_id` that matches neither the live turn nor the orphan fences a
  delayed Goal/scheduled stop so it can never cancel a newer user turn.

The boolean stays honest — `True` iff *this* call terminalized a turn — because
the scheduled-run liveness probe (`agent_stream.py
_scheduled_chat_turn_liveness`) and `goal_runs.py` depend on `False` meaning
"nothing changed". The idempotent re-Stop therefore returns `False`; the UI must
not treat that as failure (see §6).

## 5. Late `turn_completed` vs Stop — one terminal edge per turn

The provider's own terminal record can land *after* Stop already persisted
`cancelled`. Publishing it used to happen outside the send lock. It now goes
through `_consume_turn_completion_record(event, observer_event, transport)`,
which holds `_send_lock` and re-checks turn identity:

- completion for the currently active Hub turn, or a null-id completion with no
  active turn (cold-start replay / one-shot result) → persist, release guard,
  notify;
- completion for an id that is no longer active (Stop won) or is older than a
  newer running turn → **drop**, and only release an orphaned *provider* guard
  when no newer Hub turn is on the transport.

This guarantees a single terminal edge per turn and prevents a stale completion
from clearing a newer turn's guard. The fatal "process exited" path was likewise
moved under the lock with an identity re-check so it and Stop cannot both
terminalize the same turn.

## 6. Frontend: reset on Stop even when the edge is unobservable (the real gap)

The WIP's backend was correct; the defect that reproduced `896983b0` was in
`StructuredPane.cancelActiveTurn`. It applied its optimistic local terminal edge
only when

```ts
if (data?.cancelled !== false && cancelledTurnId) { … }
```

For the idempotent re-Stop the body is exactly `{cancelled:false}`, so the UI
did nothing — the precise state the user was stuck in. The contract is now:

> A **200** means Stop intent was honored. That includes an already-terminal /
> idempotent re-Stop. Apply the local reset on any 200; reconcile when the real
> edge arrives.

On success StructuredPane appends one synthetic
`turn_completed{status:cancelled}` for `expected_turn_id` (extracted to the pure
`utils/agentStreamStopReset.ts`), which:

- completes the reducer turn → `isChatModeLocked` flips false → composer
  unlocks immediately, with no round-trip;
- renders a "Stopped" marker instead of a forever-spinning working placeholder;
- uses `stream_sequence = -1` (never a real durable id), so the incremental
  reducer rebuilds cleanly and drops the optimism as soon as the backend's own
  terminal edge lands — whether `cancelled` (Stop won) or `completed` (a natural
  finish won the race); the reducer's `turn_completed` is idempotent.

Stop is now mounted on the **same** `stopArmed` gate in three places — the
composer, the reconnecting/loading banner, and the hard-`failed` banner (where
Retry remains available) — so it stays clickable in working / Reconnecting /
FAILED states. `stopArmed` is intentionally slightly wider than `turnInFlight`:
a turn still unfinished after a terminal error can be explicitly stopped.

A new composable `nudge()` aborts only the in-flight `POST /wait` (one
short-lived `AbortController` per request, separate from the generation-level
teardown signal) so after Stop the cancelled edge is fetched in one RTT instead
of waiting out the previous 30s long-poll. The abort is **not** counted as a
transport failure, so it never trips the failure budget / 5-of-5 path.

## 7. Tests

Backend — `tests/test_stop_resets_wedged_turn.py` (event replay on the scriptable
fake TraeX app-server):

1. a turn that emits continuously but never closes is not watchdog-reaped across
   many hair-trigger windows, yet Stop frees the Hub guard *before* the
   unconfirmed-interrupt restart;
2. no live turn → durable orphan terminalized once with observer notified,
   repeat Stop a no-op (no second edge, no teardown left pending); nothing open
   → clean no-op;
3. late same-id completion after Stop is dropped (one edge, `cancelled` wins);
   old-id completion never clears the newer turn's guard;
4. near-simultaneous double Stop → one edge, tailer still usable;
5. every watchdog primed-to-fire is suppressed while a Stop teardown is pending
   (one cancelled edge, no extra restart).

Plus the WIP-touched `test_queue_status_wedge.py` / `test_traex_turn_wedge.py`,
re-validated against the backgrounded-teardown semantics.

Frontend — `tests/agentStreamStopReset.test.mjs` (`node --test`): the optimistic
edge shape/idempotency, reducer reconciliation for both cancelled and
race-won-completed, lock release, the removal of the `cancelled !== false` gate,
Stop mounted in both banners, and a live composable test that `nudge()` re-issues
`/wait` immediately without leaving `live` or setting an error.

## 8. Scope / non-goals

- The provider's "Reconnecting… n/m" counter belongs to the app-server; Hub does
  not track it. The banner clears via the local Stop reset + edge reconciliation,
  not by resetting a Hub-owned counter.
- No new absolute turn timeout: a live, productive turn remains the user's to
  stop manually.
- Live backend/frontend untouched; branch is local-only and not merged or pushed.
