# Scheduled and direct Chat delivery unification

Task: c7cd7181-e2d4-42a2-858e-5c7d2bac49e7. Local branch based on inbox
`2ba9277`; no deploy, restart, push, merge or real Chat messages.

## Outcome

The timer retains schedule identity/policy and legacy history; delivery belongs
to `ChatMessageInbox`. Removed the scheduler FIFO drain, independent dispatch
markers/reaper loop, native submission adapter and separate retry task owner.
Both front doors use the inbox sequence, native submission marker, completion
observer and evidence reconciliation. Dot switching uses the same admission lock.

The existing scheduler transport/readiness injection APIs remain for standalone
embeddings and behavioral tests. They instantiate the same inbox core, never a
second native delivery engine. Running service scheduling only enqueues/wakes;
the inbox coordinator owns independent per-tab submission tasks.

## Compatibility

Keep scheduled_tasks.json and original run/turn IDs. Import existing active runs
before starting the coordinator, bypassing new-intent cap rejection for already
persisted work. Inbox state wins over stale active projections. Keep per-schedule
terminal history retention and direct inbox tombstones. Schedule policy may cancel
unsent work and explicitly release an admitted run; direct cancellation remains
unsent-only. Scheduled dead uncertainty releases its slot, direct uncertainty
continues to fence its queue.

The two JSON files cannot commit atomically. Set policy terminal projection before
inbox cancellation callbacks, so superseding cannot persist an advanced timer
without its replacement occurrence. Idempotent import recovers interruption
between inbox persistence and projection. Native possible-submission markers
prevent unsafe replay when transcript start evidence is absent.

## Verified bug and review

New isolated chat_turn one-shot test failed on the base (CANCELLED versus RUNNING,
1 failed / 86 deselected), confirming timer auto-disable self-cancelled the turn.
Consumed one-shot occurrences now remain eligible; explicit disable still stops
pending work. Archive and Goal hold regressions cover delayed dispatch.

Read-only audit found and prompted regression fixes for supersede crash ordering,
deleted-run liveness using envelope identity, cold-attempt budget across restart
and busy holds, and late completion of terminal scheduled uncertainty.

## Validation and handoff

Initial broad selection passed 432 tests; audit-fix focused selection passed 168.
Final full checks and exact logs/counts are recorded in the separately requested
workspace artifact `hub-chat-inject-20261020/UNIFICATION.md`. Frontend source is
unchanged. Full independent Workspace review and human acceptance remain required.
Pre-ship requires explicit deploy/restart and push/MR decisions; integrate inbox
ancestor first/with this branch, then separately reconcile the busy-status sibling
and retain both changelog entries. Exactly-once provider execution is not claimed.
No live provider acceptance has been exercised. **no lessons needed**.
