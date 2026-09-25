# 2026-09-25 — TraeX native chat turn wedge: Stop recovery + orphan-replay composer lock

## Symptom

Chat tab `658818cf-03ce-4a5e-88b2-1ec6ca7f79d0` (TraeX, local): after one turn,
the composer was permanently busy — new messages rejected, **Stop appeared to
do nothing**, spinner never cleared. Confirmed stable wedge hours later; zero
`error` events.

## Evidence (read-only, live :8173 + durable jsonl)

Durable stream (`workspaces/terminal-tabs/agent_streams/terminal-tab-658818cf….jsonl`):

- `2026-09-25T00:41:08Z` — final normal `turn_completed(status=completed)` for
  turn `79e4efe6…` (Goal continuation turn, run_epoch 6).
- `2026-09-25T01:31:32Z` — a `tool_call_completed` for call id
  `code-mode-nested:29:call_eNKTj…:exec-9f0c…` with **`turn_id=null`**. The
  same call id already completed *inside* turn `79e4efe6…` at 22:47:15 the
  previous day (the event is a **duplicate replay** of a nested code-mode item).
- `2026-09-25T01:50:53Z` — `status` event `thread/goal/updated`, payload
  `turnId=null`, goal `status=complete`.
- Nothing after. No `turn_started` after 00:41. Backend log: only one
  "subscriber queue full (slow client)" at 02:48; no interrupt/turn errors.

## Root cause (file:line on base 71236cd)

The backend active-turn guard was **not** stuck. An in-flight provider turn
always has a durable `turn_started` (`tailer.py` send path publishes it before
`transport.send_message`); there is none after 00:41, and the last
`turn/completed` released the guard normally (`tailer.py` ~1609–1614). The
wedge lived entirely in the **frontend timeline**:

1. `frontend/src/utils/agentStreamTimeline.ts` `resolveTurn()` — any non-status
   event with a null `turn_id` that arrives while no legacy turn exists opens a
   **new legacy turn**. The 01:31 duplicate `tool_call_completed` therefore
   created `legacy-turn-27887` (`turnId=null`, `completed=false`). The existing
   guard at the top of `applyEventToState` only ignored empty-text `status`
   events; tool records passed through.
2. `isChatModeLocked()` (`chatTurnLifecycle.ts:26`) treats the latest turn as
   active while `!completed && errors.length===0` → returned **true forever**.
   The composer Send button/`turnInFlight` in `StructuredPane.vue:1530` stayed
   locked; the optimistic pending turn from a failed send was removed on the
   409, so the UI never recovered even on reload (the orphan is durable).
3. Why Stop did nothing: `POST /tabs/{id}/stream/cancel` →
   `TailerManager.cancel_turn` → `SessionTailer.cancel_turn`
   (`tailer.py:786`): `transport.turn_in_flight` is False (guard released), so
   it fell to `_recover_orphaned_turn_locked` →
   `store.latest_unfinished_turn()` (`store.py:298`), which found no orphan —
   the last lifecycle edges are a matched `turn_started`/`turn_completed` for
   `79e4efe6…`. It returned `cancelled:false` and wrote nothing. The frontend
   lock is derived from the timeline, which the cancel endpoint cannot touch.

Proven deterministically by running the **real** reducer + `isChatModeLocked`
over the production jsonl (full history): before the fix
`last=legacy-turn-27887 completed=false → LOCKED=true`; after the fix
`last=turn-79e4efe6 completed=true → LOCKED=false`.

Secondary backend hardening gaps found while tracing:

4. The push consumer persisted every normalized event. An unreferenceable
   replay from a persistent app-server was durably stored and re-locked every
   fresh UI load.
5. TraeX Stop fallback (`native.py` `TraexNativeSession.cancel_active_turn`):
   when `turn/interrupt` could not be confirmed it called `self.stop()`, and
   the persistent app-server EOF then **failed the session closed**
   (`_run_native` EOF branch), stranding the tab on the Retry affordance
   rather than resuming. Codex `turn/cancel` failure only logged and released
   the guard — the next send could still target a dead pipe.
6. A turn silenced with an **outstanding tool** had no outer bound:
   `_stream_inactive()` is suppressed while `_active_tool_call_ids` is
   non-empty, so a frozen app-server mid-tool would suppress the watchdog
   forever (the prod case was the frontend lock above; this is the
   corresponding runtime fail-safe).

## TraeX terminal signals

- `turn/completed` (params.turn.status `completed|interrupted|failed`) is the
  authoritative turn end; mapped to `turn_completed`
  (`codex_jsonl.py:_normalize_notification`).
- `turn/interrupt` is async: the client must wait for the provider's later
  `turn/completed(interrupted)`. Hub waits on `_interrupted` for
  `_STARTUP_GRACE_S`; the completion is set either on direct delivery or found
  pre-queued while draining the notification queue.
- `thread/goal/updated` (`turnId` may be null — goal/thread control plane, not
  a turn event) maps to a user-ignored `status` event and is never turn
  attribution.
- TraeX background/nested ("code-mode") executions can re-emit `item/*`
  records for earlier items outside any live turn; those records carry no
  usable turn id and must not be treated as a new turn.

## Fix

- **Frontend (`agentStreamTimeline.ts`)**: `ReducerState.attributedToolIds`
  records tool/approval call ids seen on an event with a real turn id. A later
  null-turn event with the same identity is dropped as a provider replay
  (before opening a legacy turn). Identity-scoped (not a blanket null rule) so
  legitimate null-turn streams (provider/transcript paths before
  `turn_started`) and genuinely novel orphan rows still render.
- **Backend drop boundary (`tailer.py` `_run_native`)**: unattributed
  turn-interior records (text/thinking/tool/approval/turn_completed) arriving
  with `_active_turn_id is None` and a null stamped id are dropped for
  persistent transports (`eof_is_fatal`) only, and only after a turn has
  completed in this consumer (`_turn_completed_seen`) — one-shot providers and
  cold-start resume replays of the still-active turn are unaffected.
  `turn_started`/`status`/`error` are never dropped.
- **Recoverable Stop (`native.py`)**: new `ProviderSession.restart_for_recovery()`
  — mark `_client_requested_stop`, `stop()`, `start()` (Codex/TraeX start does
  `thread/resume` for the verified conversation id). `CodexNativeSession` and
  `TraexNativeSession.cancel_active_turn` now restart+resume when the cancel
  RPC fails/times out (TraeX override also clears `_discard_turn_id` and
  `_interrupted` for the new process). The `_run_native` EOF branch treats a
  flagged EOF as an expected restart handoff (`continue`) instead of failing
  closed; the flag is cleared at loop top once the replacement transport is
  healthy. Restart-in-place keeps the push consumer alive across both the
  explicit Stop and the watchdog reaps.
- **Outer liveness bound**: `ACTIVE_TURN_HARD_LIVENESS_TIMEOUT_S` (default
  7200s, env `CLAUDE_HUB_ACTIVE_TURN_LIVENESS_TIMEOUT_S`) +
  `_turn_hard_liveness_expired()`. Refreshed by every accepted provider
  record; not suppressed by an outstanding tool; suppressed by an open
  blocking approval card. The existing 600s streaming reap and this new reap
  both go through `_reap_active_turn_locked()`, which terminalizes (durable
  `error` + `turn_completed(cancelled)`), then either resumes in place or
  stops the process and exits the consumer for a later clean restart.

## Tests

- `backend/tests/test_traex_turn_wedge.py` (8) — scriptable fake JSON-RPC
  app-server with `confirm`/`timeout`/`error` interrupt modes: unconfirmed
  interrupt kills/restarts/resumes at transport level (TraeX + Codex); full
  production sequence through `SessionTailer` (tool completes → no
  turn_completed → streaming reap → resume → next send succeeds, no 409);
  confirmed-interrupt Stop keeps the server; hard-liveness reaps an
  outstanding-tool silent turn; continuously emitting turns and approval-card
  waits are not reaped; post-completion duplicate + `goal/updated(null)` is
  dropped and the next send works.
- `frontend/tests/chatTurnReplayWedge.test.mjs` (5) — exact prod sequence over
  the real reducer, incremental reducer, goal/null-only, novel orphan still
  rendered, live null-turn legacy still receives records.
- Updated `test_traex_interrupt_failure_*` to the restart contract.
- Targeted suites: 448 backend tests (`test_agent_stream*`, `test_traex_agent`,
  `test_codex_sessions`, `test_chat_fork_seed_history`,
  `test_scheduled_tasks*`) + 496 frontend unit tests pass (3 pre-existing
  `forkFromTurn` localStorage-in-node failures, identical on main).
  black/isort/mypy/vue-tsc/eslint/build clean.

## Recovering the already-stuck production tab (runbook for a human)

No code change retroactively edits the durable jsonl, and the running live
backend must not be touched from a task worktree. In order of preference:

1. **Easiest (no backend action):** ship + build the frontend fix, then hard
   reload the tab's Chat pane. The reducer drops the duplicate orphan on
   rehydration; composer unlocks immediately. The backend guard was never
   stuck, so sending works right away.
2. If an immediate UI-only workaround is needed before deploy: the orphan is
   the last-but-one line in the tab's jsonl (the 01:31 `tool_call_completed`
   with `turn_id: null`). Stop the live backend first, remove that single
   record from
   `~/.claude_hub/workspaces/terminal-tabs/agent_streams/terminal-tab-658818cf-03ce-4a5e-88b2-1ec6ca7f79d0.jsonl`,
   restart, reload. (Manual surgery — prefer #1; the `thread/goal/updated`
   status line can stay.)
3. Future wedges where the backend guard actually is stuck: Stop now
   interrupts in place or kills+resumes automatically; the 2h hard-liveness
   bound converges silent turns without any human action.
