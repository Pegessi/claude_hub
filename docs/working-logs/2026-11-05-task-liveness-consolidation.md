# Task liveness consolidation

Task `ce373ec6-dec6-44ca-9a67-aa4d13a74efb`; base
`aa359fd17ccd804cbf296db3644552481e140667`; branch
`codex/task-liveness-consolidation`. Goal Packet approved in review cycle 1;
implementation is in cycle 2. No push or main-service restart is authorized.

## Change boundary

One writer owns implementation. Two native read-only audits mapped legacy policy,
provider facts and regression coverage. An additional safety audit identified
condition-age reuse, repeated migration, skipped claims, attention resolution and
replacement races; dedicated regression cases cover those findings.

The convergence map in `docs/design/agent-liveness-guarantees.md` is the ownership
contract. Removed policy files: `monitoring/recovery.py`, `tmux/review_delivery.py`,
`scheduling/hub_tasks.py`. Lifecycle observation/notice code moved from
`monitoring/lifecycle.py` into the central `monitoring/liveness.py`; the former
`tmux/review_reaper.py` is reduced to `tmux/review_resources.py` (resource selection,
orphan tab cleanup and historical report parsing only). Refresh no longer decides
prompt retry, auto-review or SUBAGENT failure. Dispatch/scheduler no longer invoke
review/orphan recovery sweeps. Dot no longer compares inactivity timestamps.
Legacy per-session counters have no writers and are read only for cold migration.

Kept: normal Task dispatch/verdict transitions, TaskMailbox/Chat receipt delivery,
provider transport watchdogs, scheduled Chat delivery, pure lifecycle projection,
Dot result cards and explicit report-question tracking. These do not decide Task
stall recovery. The single owner receives configured watch/review timeout inputs.

## Validation and handoff

Locked dependencies installed in the isolated feature worktree. Focused regression
runs cover historical stalls, restart/idempotency, provider fences, scheduled
replacement, prerequisites, explicit SUBAGENT timeouts, notices and Dot tracking.
The provider audit additionally found mailbox admission races and a Dot watch
re-arm regression; central admission guards and activity markers now cover both.
143 focused tests pass, including real-pump lock contention, readiness/retry/native
question boundaries, receipt preservation and later ordinary messages. Backend
types pass all 613 files. The first full run had one retired-counter fixture
failure (4,254 passed) and test typing failures; those fixtures are corrected.
Full verification and independent final review are pending. This log is updated
with final results before handoff; no passing final review or integration is claimed.

## Limits

No durable background-job runner or Stop hook. Notice acceptance is Dot inbox
acceptance, not human receipt. Unknown quota/permission causes stay explicit
uncertainty. No lessons needed.
