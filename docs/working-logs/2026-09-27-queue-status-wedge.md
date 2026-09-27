# Provider capacity-queue turn wedge (`queue/status`) — Stop recovery

Date: 2026-09-27
Branch: `fix/provider-queue-status-wedge` (base `main` 053bf11; supersedes on top
of the TraeX null-turn wedge d69f79b and the cold-wake fix).
Surface: structured Chat, persistent app-server family (TraeX; Codex shares the
transport/lifecycle code).

## 1. Incident (real persisted events, not a hypothesis)

Tab `2feb646a-5293-4b6f-93bb-6c0c9a5c85b8` (`session_kind=chat`,
`agent_type=traex`, solo), stream jsonl under
`~/.claude_hub/workspaces/terminal-tabs/agent_streams/`.

Turn `dc09c24d-48b5-447e-a9f5-4f2e77e2669c`:

| wall clock (UTC) | event |
| --- | --- |
| 02:28:32 | `turn_started` |
| 02:28:39 → 02:37:17 | genuine generation: 29 text_delta, 118 thinking_delta, 65 tool_call_started / 76 completed |
| 02:37:04 | first `queue/status` snapshot ("Queued for model capacity.", then position **357**) |
| 02:37:17 | last real-output event (one in-flight tool result landed just after queue began) |
| 02:37 → 06:07 | **12,597 `queue/status` snapshots, ~1/s**, position 357→300→200→150→120→100→**96** (~1/minute), no `turn_completed` |
| 04:36:58 | **one** provider `error` notification: `"Reconnecting… 1/5"` (run_epoch 3) |
| 06:07:19 | after manual `POST /api/workspaces/tabs/{id}/stream/cancel` → `turn_completed{status:cancelled}` |

Total: 12,889 events for the turn; 12,598 status rows; exactly one error; zero
error events from Hub itself. The queue was progressing the whole time — slow
but **alive**, not dead — which is why a "the provider crashed, restart it"
response would have been wrong. The actual defect was that the user was
**trapped**: Stop did not free the turn from the UI, while a hand-issued HTTP
cancel did.

## 2. Root causes (file:line on base)

1. **Queue heartbeats counted as model-stream liveness.** In the native consumer
   every *accepted* provider record refreshed `_last_event_at`
   (`tailer.py`, record-consume block), and `_note_raw_provider_wait` set
   `_waiting_for_model_capacity` for `queue/status`. Consequences:
   - `_stream_inactive()` returned false because `_waiting_for_model_capacity`
     is in `_waiting_for_external_activity()`.
   - `_turn_hard_liveness_expired()` was reset by every ~1/s queue snapshot
     ("refreshed by EVERY accepted record … capacity-queue notifications …
     included"), so a turn queued for hours could never age out.

2. **A recoverable provider notice released the frontend turn lock.**
   TraeX/Codex transport their own reconnect/backoff as an `error` JSON-RPC
   notification; the adapter mapped it to an `error` event. The frontend lock
   predicate `isChatModeLocked` treats **any** error as turn-terminal
   (`!latest.completed && latest.errors.length === 0`,
   `chatTurnLifecycle.ts`). One "Reconnecting… 1/5" therefore flipped
   `turnInFlight` to false while the backend turn was still alive:
   - the Stop button is `v-if="turnInFlight"` — it **unmounted**;
   - `cancelActiveTurn()` early-returns when `!turnInFlight.value`;
   - Send unlocked, so the backend rejected it with "a turn is already in
     flight".

3. **Stop was additionally gated on the observation plane.** The Stop button
   was disabled when `connectionState !== 'live'`, and the authoritative
   long-poll loop (`useAgentStream.longPollLoop`) failed closed on the **first**
   `/wait` error with no retry — a single transient blip during a 3.5h wait
   dropped the surface to `failed`, greying out both Stop and Send.

4. **`_cancel_active_turn_locked` released the Hub guard only after awaiting
   provider teardown.** Persistent transports can take the bounded interrupt
   grace or run kill+relaunch+resume; the persisted cancelled edge and the
   `_active_turn_id=None` / observer notify happened across that await, so the
   "one shot" feel depended on the provider path returning promptly.

The manual curl worked because it bypassed all three frontend gates and hit the
same endpoint; the cancelled edge landed on the next tick and released the
backend guard.

## 3. The queue vs. generating state machine

Queue means **accepted but generation has not started**. It must not hold the
same liveness meaning as streaming tokens.

Per turn the tailer now tracks (`tailer.py`):

- `_queued_since` — monotonic start of the *current contiguous* queued stretch;
- `_last_queue_at` — latest queue heartbeat;
- `_waiting_for_model_capacity` — `state ∈ {queued, waiting}`;
- `_last_persisted_queue_text` — for snapshot suppression.

Rules:

- A `queue/status` `queued|waiting` record updates queue timestamps but **does
  not** refresh `_last_event_at` and does not feed the streaming/hard-liveness
  clocks. `ready` and any real activity (`text/thinking/tool_call_*`) call
  `_reset_queue_wait_state()`, so a later re-queue starts a fresh stretch.
- **Live-but-capped** (`_queued_wait_cap_expired`): heartbeats keep arriving but
  capacity never does within `QUEUED_TURN_MAX_WAIT_S` → publish an error +
  `turn_completed{cancelled}` and call `transport.cancel_active_turn()`, but do
  **not** stop/relaunch — resending simply re-queues. Stop remains available at
  all times; `0` disables the cap for sites that wait indefinitely.
- **Stalled queue** (`_queued_heartbeat_stalled`): the provider *declared* the
  turn queued and then stopped heartbeating past `QUEUE_HEARTBEAT_STALL_S` →
  treat as a dead runtime and go through the existing
  `_reap_active_turn_locked` kill+relaunch+resume path (the tab stays live).
  A slow-but-live queue never trips this; only a silent one does.
- **No false kills of real work**: any thinking/text/tool event clears the
  wait, so (a) queue → ready → generation → `turn/completed(completed)` is
  untouched, and (b) a single stray `queue/status` inside an otherwise active
  turn is reset by the very next activity. Open approval cards suppress both
  bounds (a human wait).

Knobs (env), with conservative defaults:

| var | default | meaning |
| --- | --- | --- |
| `CLAUDE_HUB_QUEUED_TURN_MAX_WAIT_S` | `1800` | live queue max wait before cancel; `0` = unbounded |
| `CLAUDE_HUB_QUEUE_HEARTBEAT_STALL_S` | `90` | silence after a declared queue ⇒ dead runtime, reap+resume |

30 minutes is a deliberate middle ground: short enough to end the multi-hour
lockout, long enough to absorb normal bursts. The incident position crawl
(~1/min) would still be cancelled at the cap, which is the desired escape hatch
— but Stop, not the cap, is the primary recovery and is always immediate.

## 4. Stop path (one shot, queue included)

`_cancel_active_turn_locked` now:

1. persists the terminal `error` (when given) + `turn_completed{cancelled}`;
2. sets `_active_turn_id = None`, clears cards/tools/queue state, and notifies
   post-persist observers — **before** touching the provider;
3. then awaits `transport.cancel_active_turn()` inside a try/except so a
   teardown failure cannot re-wedge the guard (the turn is already terminal).

Late records from the retired turn are filtered by the transport's
`_discard_turn_id`; a stale `queue/status` that slips through after
`turn_completed_seen` with no owning turn is dropped before normalization (it
cannot mint an unattributed status legacy turn). Frontend: Stop is enabled
whenever `turnInFlight`, regardless of observation-plane state; Send stays
gated on `live`.

## 5. Snapshot flooding and the reconnect banner

- **Backend**: `_is_duplicate_queue_snapshot` suppresses consecutive
  `queue/status` snapshots whose rendered text is unchanged (one stable
  `message_id`, `snapshot:true`). Only a changed position/state is persisted,
  shrinking both the durable stream and the board poll payload; the raw
  heartbeat still drives the queue watchdog (suppression is persistence-only).
- **Adapter**: messages on the error channel matching
  `^(reconnect(ing|ed)?|retrying)\b` are emitted as a single coalesced
  `status{provider_status:"provider/reconnecting", snapshot:true,
  message_id:"provider-status:reconnect"}` instead of an `error`. Genuine
  failures (`turn/completed` error, process death, non-reconnect messages)
  remain terminal errors.
- **Frontend timeline** already replaced same-`message_id` status parts in
  place, so the rendered transcript and `statuses[]` show one "queueing
  (position N)" indicator that updates as N moves and disappears with the turn.
- **Long-poll resilience**: `/wait` failures retry with capped exponential
  backoff (6 tries, 1s→5s) while staying `live`; only exhausting the budget
  fails the surface. This removes the spurious reconnect banner/lockout from a
  lone dropped poll during a long queue.

## 6. Why real long generations and the prior fixes are safe

- Long turns with continuous thinking/text/tool activity clear the queue wait
  on every activity event; neither new gate fires without a *declared* live
  queue (cap) or a *silent after-declared* queue (stall).
- The TraeX null-turn wedge (unattributed drops, kill+relaunch+resume, hard
  liveness), the cold-wake readiness path, and chat_turn scheduling are
  unchanged; the queue gates sit ahead of the pre-existing inactivity/hard
  checks and reuse the same reap/cancel primitives.
- Claude/Cursor (one-shot) never emit `queue/status`, so their behavior is
  unchanged; Codex uses the same base transport and benefits identically if it
  ever emits the notification.

## 7. Tests

- `backend/tests/test_queue_status_wedge.py` — 13 cases using the scriptable
  fake TraeX app-server (real raw records through the actual
  adapter/tailer/transport/store): adapter reconnect classification; Stop
  during an endless queue (incl. unconfirmed interrupt → relaunch+resume) with
  guard release and clean resend; guard-release precedes slow teardown;
  double-cancel idempotency; identical-snapshot suppression vs position-change
  persistence; reconnect keeps the lock; live-queue cap cancels without
  restart; silent-queue stalls reap+resume; queue→ready→generation completes;
  single stray queue mid-generation does not cancel.
- `frontend/tests/chatQueueWedge.test.mjs` — 5 cases: thousands of snapshots
  coalesce to one mutable part and keep `isChatModeLocked` true (Stop mounted);
  incremental reducer parity; reconnect STATUS is non-terminal; genuine ERROR
  stays terminal; queue→generation completes.
- Existing suites green: `test_traex_turn_wedge.py`, `test_traex_agent.py`,
  `test_provider_question_protocol.py`, `test_chat_network_access.py` and the
  broader `test_agent_stream*` / codex / cursor suites (372) plus targeted
  re-runs (105). `black`/`isort`/`mypy` clean on changed backend files;
  frontend `vue-tsc` build, ESLint on changed files, and node:test pass
  (3 pre-existing `forkFromTurn` node-25 `localStorage` failures are unrelated
  to this change).

Run (targeted, no real-IO/tmux e2e):

```bash
cd backend
uv run pytest tests/test_queue_status_wedge.py tests/test_traex_turn_wedge.py \
  tests/test_traex_agent.py tests/test_provider_question_protocol.py -q
cd ../frontend
node --test tests/chatQueueWedge.test.mjs tests/chatTurnReplayWedge.test.mjs
```

## 8. Incident recovery record

The single affected tab was recovered operationally on 2026-09-27 by a manual
`POST /api/workspaces/tabs/2feb646a…/stream/cancel` (the cancelled edge landed
at 06:07:19 and freed the guard); the conversation was resumed with the next
message (the persistent thread resumes in place). With this fix that recovery
is the in-UI Stop button (immediate), and an unattended queue self-resolves at
the cap or heartbeat-stall bounds.
