# Review verdict delivery hardening

Task: `0c86ba8c-7839-4327-b3c6-f92246ab8ae7`. Branch-only candidate;
no running service was changed. Detailed local incident evidence and validation
are in `/home/tiger/evo_workspace/hub-review-delivery-fix-20261021/FINDINGS.md`.

## Incident and root cause

On 2026-10-21, reviewers for `ccc4f7ed` and `5f6839f9` POSTed both
`review_started` and `review_passed` to the **correct** workspace report path,
but the generated commands targeted localhost:11004 (MASO). They received 404;
the plan gates remained pending until operator recovery about 2h51m later.
`preview-report` appeared in OpenAPI discovery output from that wrong service;
it was not either reviewer's POST target.

`_report_base_url` uses `settings.port` for local sessions. Unprefixed Settings
read inherited `PORT=11004` while the supervised Uvicorn command independently
bound port 8173; `CLAUDE_HUB_PORT=8173` was present but unused by Settings.
Settings now prefer the Hub-specific variable, retaining legacy PORT and explicit
Python keyword configuration. The supervisor exports its selected bind port.
Remote forward ports retain precedence. Direct Uvicorn operators must likewise
set `CLAUDE_HUB_PORT` to their selected bind port.

## Recovery design

The existing five-second workspace monitor/dispatch/review-reaper loop now checks
open review dispatches with no terminal verdict. It uses existing refreshed
runtime state and last activity, preserving frozen-frame/45-second classification
and native queue watchdogs; it does not add a polling loop or change those rules.

`REVIEW_VERDICT_IDLE_TIMEOUT_SECONDS` defaults to 180 (allowed 45–1800). Both
managed and runtime status must be Idle, with the reviewer still bound to the
same task. The most recent dispatch time, terminal activity or current-reviewer
report postpones recovery. Busy, fresh, sealed, closed, cross-bound and terminal
tasks are excluded. The check applies to plan and implementation reviews.

Persist a dispatch identity (cycle, attempt, requested-at and reviewer), nudge
time and immutable canonical command message before enqueue. Reuse the existing
call-ID mailbox transport and its pending/processing/uncertain/ACK rules. A
restart never creates a second logical nudge. An intent-only delayed restart
still enqueues the same reminder and gives it a response window. Ambiguous
transport failures stay fail-closed and are not repasted automatically.

After another idle/silent timeout, emit one durable `needs_input` TaskMailbox
event with action `review:delivery_stalled`, the exact report URL and reminder
ID. Preserve the open review: this is an operational attention event, not a
fabricated `review_needs_input` verdict, review-round reopening or task failure.
Mailbox event call-ID deduplication covers a crash between event append and task
save. Reports/activity resume the grace window, but do not authorize another
reminder in the same dispatch. One implementation writer owned recovery changes;
independent read-only forensics/review and disjoint operator docs/tests were
parallelized. Review found and corrected the delayed-restart and route-test
isolation gaps before handoff.

## Submission and operator recovery

Normal review prompts and recovery use one canonical command renderer. It states
that wrong-path/preview endpoints 404, requires a persisted HTTP report receipt,
and tells reviewers to surface errors rather than stop silently. No route alias:
an alias cannot fix a wrong service/port and risks hiding a misconfiguration.
Wrong paths still 404 without side effects; normal authentication, session scope,
verdict validation, review cycles and human acceptance remain in force.

See [operator resubmission](2026-10-21-review-resubmit.md) for the existing public
CLI command and verification procedure. No new privilege or internal-only method
is required.

## Validation and shipping boundary

Regression tests cover persisted transport deduplication/restart/uncertainty,
idle timeout, busy/recent-report/fresh-round suppression, one reminder then one
attention event, distinct later dispatches, wrong-path 404/no mutation, provider
PORT collision and launcher propagation, and public CLI auth/error behavior.
Exact command results and toolchain deviations are recorded in FINDINGS.md.
Do not ship until independent review and human acceptance; deploy/restart was
explicitly outside this task. Silent thinking misclassified as idle can receive
one reminder; no busy session is interrupted and no acceptance is automated.

Full-suite validation exposed a pre-existing status test identity collision:
Claude/Codex lifecycle parameters used the same role-only tab/session IDs.
Retained owners in the global weak tailer registry could answer with the previous
provider's idle state. Independent reproduction omitted all new delivery tests;
retaining old owners or disabling GC reproduced the failure. Only test identities
now include provider as well as role; production status detection is unchanged.
Escalation payloads retain the original immutable recovery commands too, so a
configuration change after event append/before marker save cannot change the
event fingerprint on restart.
