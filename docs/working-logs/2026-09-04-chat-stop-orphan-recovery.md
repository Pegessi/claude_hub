# Chat Stop Orphan Recovery

## System overview

Structured Chat has two layers of turn state:

- `SessionTailer` and `ProviderSession` keep the live provider process and its
  active turn in backend memory.
- `AgentStreamStore` persists `turn_started`, output events, and
  `turn_completed`; the browser reconstructs Stop/Queue state from this durable
  lifecycle.

Those layers must terminalize in that order: persist and fan out the terminal
event, then release or stop the provider runtime.

## Failure

At 02:52:25 on 2026-09-04, the affected direct Claude Chat persisted its final
`thinking_delta` for turn `338c979c-27a3-4b37-8ccf-44a5d8a549a2`. The backend
started graceful shutdown 53 ms later and restarted, but the stream never
received `turn_completed`.

Two gaps made that state permanent:

1. Application shutdown stopped only the Workspace tailer manager, not the
   separate manager that owns top-level Chat tabs.
2. `SessionTailer.stop()` stopped the native transport without first
   terminalizing an active turn. After restart, Stop found no in-memory active
   provider and returned `cancelled=false`, while the browser correctly kept
   the durable unfinished turn locked.

## Recovery design

- Application shutdown now stops every initialized tailer manager.
- Tailer shutdown and idle reaping cancel and persist an active turn before
  stopping its provider process.
- If Stop finds no live provider turn, the store scans for the latest durable
  `turn_started` that has no matching `turn_completed` or terminal `error` and
  appends one `turn_completed(status=cancelled)` event. A repeated Stop sees the
  terminal event and is a no-op.
- If a live turn is stopped but its completion cannot be persisted, the
  backend surfaces an error instead of acknowledging a durable state change
  that never happened.

## Key pitfalls

- HTTP success alone is not a turn lifecycle boundary; the browser intentionally
  derives the lock from durable events.
- `error` must count as terminal during orphan detection because the frontend
  uses the same rule to unlock turns whose provider fails without a completion.
- Shutdown must cover both manager namespaces; creating or stopping only the
  Workspace manager does not affect direct Terminal-page Chat tabs.
