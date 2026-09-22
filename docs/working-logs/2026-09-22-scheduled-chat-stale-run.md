# 2026-09-22 — Scheduled Chat runs: dead-turn wedge, unbounded backlog, recovery UX

## Incident

An agent in a Chat tab set itself a `chat_turn` schedule (`interval_seconds=900`)
that re-ran a memory-monitor prompt every 15 minutes and was supposed to
disable itself when the serving job ended. The first scheduled turn died with:

> Turn interrupted because its backend runtime was no longer available.

After that the schedule looked enabled but never ran again. Durable state
(`scheduled_tasks.json`) showed **one run stuck `running` since 17:45 and 14
later occurrences piled up as `queued`** (run_count 15) — the exact FIFO head
blockage the design assumed could not last.

## Root cause

`SessionTailer._recover_orphaned_turn_locked()` (runtime-lost path, reached
via `start()` first-touch or a Stop after restart) called
`_publish_turn_completion()`, which persists the synthetic `error` +
`turn_completed(cancelled)` edges, but — unlike every other terminalization
path (`_cancel_active_turn_locked`, `_fail_active_turn`, the normal completion
path) — **never called `_notify_post_persist()`**.

Goal admission and scheduled Chat runs only advance through the
`post_persist_observers` bridge (`_notify_goal_turn_completed` →
`workspace_manager.on_scheduled_chat_turn_completed`). Persistence alone does
not release the scheduler's FIFO. So the durable transcript correctly showed
the cancelled turn while the scheduler kept the run `running`; the drain's
"a run is DISPATCHING/RUNNING" guard then blocked every later occurrence.

Cold-restart reconciliation (`_recover_scheduled_chat_runs`, runs once at
startup) would eventually have healed it from the transcript, but the backend
never restarted — the wedge happened in the live process, which had no
liveness backstop.

## Fixes (backend)

1. **Observer on orphan terminalization** — `_recover_orphaned_turn_locked`
   now notifies observers with the published completion edge
   (`services/agent_stream/tailer.py`).
2. **Live stale-run reaper** — `_reap_stale_scheduled_chat_runs()` runs in the
   5s tick. A run in DISPATCHING/RUNNING older than
   `_SCHEDULED_RUN_STALE_GRACE` (10 min; legit long turns keep streaming / the
   provider guard raised, so they are untouched) is first reconciled against
   the durable transcript (authoritative), then against a new injected
   liveness probe (`configure_scheduled_chat_liveness`,
   `api/agent_stream.py:_scheduled_chat_turn_liveness`): provider owns this
   guard raised (by this turn OR a newer unrelated manual turn) → active;
   provider idle → `tailer.cancel_turn(expected_turn_id=…)` terminalizes this
   run's orphan and the resulting observer releases the queue
   (`terminalized`); idle + no open orphan → dead → started→`uncertain`,
   unstarted→requeued; idle + a DIFFERENT open orphan / any probe error →
   unknown (leave alone, fail safe). Transcript scans run outside the tab lock
   and are throttled per run (`_SCHEDULED_REAP_RECHECK` = 60 s).
3. **Occurrence superseding** — while the target Chat is blocked, each new
   interval occurrence marks earlier never-dispatched occurrences of the same
   task as terminal `skipped` ("superseded by a newer occurrence"), keeping a
   single pending head. Previously every occurrence accumulated and would
   replay as a burst of identical stale prompts when the Chat freed up.
4. **Disable/Stop semantics** — disabling a task, or the drain discovering a
   disabled/deleted/backend-changed target, cancels queued/waiting
   occurrences (`cancelled`, explicit reason) instead of firing them later.
   The drain now re-checks `task.enabled` at the head and skips disabled
   tasks; an already-running turn is always left to finish.
5. **Status mapping** — a `cancelled` completion edge maps the run to
   `cancelled` (previously bucketed as `failed`).
6. **Recovery API/CLI** — `POST /runs/{run_id}/cancel` (cancel one
   wedged/queued run, then drain) and `POST /{task_id}/runs/clear` (cancel the
   queued backlog); CLI `schedule runs`, `schedule cancel-run`,
   `schedule clear-runs`.
7. **Visibility** — list/get/create/update/patch return a new
   `ScheduledTaskView` (extends `ScheduledTask`) with non-persisted
   `active_run_count`, `queued_run_count`, `in_flight_run_count`,
   `in_flight_run_id`, `in_flight_since`.

## Fixes (frontend)

- `ScheduledTasksPanel` rows show an "● run in flight · <elapsed>" pill
  (informational; long runs are normal) and an "N queued" pill, each with an
  inline cancel / clear action backed by confirm dialogs.
- Store gains `cancelRun` / `clearBacklog`.

## Pitfalls / notes

- The liveness probe touches tailer internals (`get_tailer`,
  `_native_transport`, `_active_turn_id`) deliberately: manager code must not
  import the API layer, and no public "who owns turn X" surface existed. The
  probe fails safe — no tailer / exception → `unknown` → run left alone.
  **A raised provider guard is always "busy"**, even when a newer unrelated
  turn owns it (the human composer bypasses the scheduler): returning "dead"
  there would false-kill the stale run and dispatch its successor into a live
  manual turn. With the guard down, if a *different* unfinished orphan exists
  the probe returns `unknown` rather than "dead" so the run is not
  redelivered into someone else's orphan.
- The transcript reconciliation scan runs OUTSIDE the per-tab lock (a full
  history can span many 5000-event pages); run status is re-checked under the
  lock before mutation. Re-probing is throttled per run, and the throttle
  map drops stamps for runs that have gone terminal.
- Cross-task FIFO ordering on one tab is preserved: superseding is scoped to
  the same `scheduled_task_id`; another task's queued run still blocks/orders
  normally.
- `_publish_turn_completion` callers now all notify; if a future caller wants
  silent persistence it must say so explicitly.
- A persisted `error` edge is treated as terminal by both the reaper and
  cold-start recovery — true today because every error emission site is
  immediately followed by a completion; a future non-terminal error event
  would break this (comment at the scan site marks the coupling).
- mtime/reap checks use `dispatched_at or queued_at`; WAITING runs
  (archived/Goal/chat-busy) have `dispatched_at=None` and are not reaper
  candidates — they are owned by the drain, not by a dead provider turn.
- The UI in-flight pill shows elapsed duration, never an alarm: legit turns
  run well past the 10-minute reaper grace (which only acts once the provider
  is actually idle), so labeling long runs "stuck" was misleading.

## Adversarial review follow-ups (post-commit)

- M1 fixed: other-turn guard ownership → `active`, not `dead`.
- M2 fixed: different open orphan after idle → `unknown`, no redelivery.
- Reaper lock scope narrowed; throttle map pruned; error-terminal coupling
  documented; UI no longer warns on duration alone. Six probe-level tests
  added in `test_agent_stream.py`.
