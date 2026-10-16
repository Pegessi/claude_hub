# Planned restart continuation for Chat Goals

## Request and delivery boundary

The user requested the existing planned-restart continuation behavior for Goal
mode, which previously stayed paused. This task uses
`codex/goal-restart-continuation` in the dedicated `goal-restart-continuation`
worktree. Baseline `a72cf9d45c88aa05865e9619140caec38c874d8b` contains both the
prior managed Chat cleanup delivery and fetched `origin/main` at `39ca52e`.

The existing completed managed Chat Task is not reused. This is a new
initiator-controlled Task; no Workspace execution is dispatched. Branch delivery
is authorized, not a new main merge, remote push, deployment or shared-service
restart. Tests use private runtime homes and in-memory provider I/O.

## Existing behavior and chosen boundary

- Ordinary restart recovery uses the existing restart mailbox, an expiring
  operation/launcher identity, durable one-attempt claims, original-turn evidence,
  and exact provider-conversation/configuration guards. It deliberately excluded
  active Goals and Goal-protocol turns.
- Goal shutdown completion and startup `recover()` both pause interrupted work.
  Startup recovery remains fail-closed; it must not resume every paused Goal or
  reinterpret a prior crash as an authorized planned restart.
- Capture now records the exact active Goal and a digest of its human-mutation
  history and immutable tab/Goal incarnation. Only an accepted, currently active
  Goal turn, or an active deferred Goal waiting on an ordinary turn, is eligible.
  Existing approvals, capacity waits, uncertain dispatches, unsupported ownership,
  archive, mode and provider-conversation exclusions remain.
- The normal restart switch also governs Goals. Already paused, blocked, terminal,
  uncertain, or uncaptured Goals are not automatically resumed. An upgrade from a
  version that never captured Goal eligibility cannot synthesize that evidence.

## Implementation contracts

1. Optional Goal identity/control/deferred fields stay inside `ChatContinuation`;
   partial Goal identities are invalid. No second Goal store or runner is added.
2. Immediate recovery uses `GoalRunController.resume` with a fixed first step ID
   equal to the restart continuation ID and a per-call guarded dispatch callback.
   The Goal objective/checkpoint/protocol remain authoritative. Later Goal steps
   retain the normal callback and fresh IDs; the global callback is never swapped.
3. Goal dispatch exceptions can be returned as uncertain Goal state. Restart
   success therefore requires the callback's positive handoff, not merely a
   return from `resume()`. A claimed uncertain attempt is never replayed.
4. A deferred Goal first resumes its interrupted ordinary turn, then continues
   through the normal Goal dispatcher. Only successful ordinary completion
   releases deferral. Cancellation/error pauses it, and handled ordinary boundary
   IDs are deduplicated without charging Goal turn usage. A failed recovery does
   not leave its own deferred automation armed or overwrite a later human control.
5. New Goal controls revoke the exact matching ticket before waiting for admission.
   Replaying an old idempotent control does not revoke newer recovery. Native Stop
   can target the original or resumed first-step ID; the old capture instance must
   still stop the original live turn rather than translate to a future ID.
6. Archive records a private operation-scoped veto, including before capture's
   local candidate list is published. A subsequent unarchive cannot undo it.
   Public restart status omits the veto list and full recovery identities.
7. Existing resumed-conversation and late input guards remain active through the
   first handoff. They are cleared before a fast completion can dispatch a later
   ordinary Goal step. A different Goal, incarnation, control history, mode,
   configuration, newer recorded turn or expired/revoked claim rejects recovery.

## Initial verification and remaining acceptance

The locked worktree-local environment is required: reusing another worktree's
venv was rejected by the provenance gate, then replaced with a local `uv sync`
environment. The existing restart baseline passed 85 tests.

Controller changes passed 53 focused checks. The combined initial restart/Goal/
API/service suite passed 204 checks (`/tmp/claude-hub-verify.NPJ0zj`). Integration
coverage uses actual restart/Goal stores, native tailers and callback flow with
provider execution replaced in memory: same Goal/checkpoint, deferred ordering,
repeated restart generations, one-shot claims, human controls, original/resumed
Stop IDs, capture-time archive veto, failed sends and fast next-step dispatch.

Two test-oracle corrections were made without weakening production behavior:
Stop returns before deferred transport teardown, so the test joins that owned
teardown before asserting Goal state; a Claude completion test must supply an
assistant message as well as its result envelope to exercise Goal status parsing.

## Independent-review correction

The first fixed-source review reproduced a public API idempotency gap. Existing
already-deferred Resume shortcuts returned a waiting Goal or released deferral
without recording the accepted request ID. Retrying that exact request after a
second planned restart then looked like a new control: it revoked the valid
restart ticket and dispatched an extra Goal step. The correction removes those
shortcuts and routes both waiting and immediately released Resume requests through
`controller.resume`, retaining the existing same-request completion postcheck.
The second-restart counterexample and two repository regressions now pass;
108 combined checks passed in `/tmp/claude-hub-verify.hDsayu`.

## Final validation

Revision 2 retained the seven-file backend hash manifest at
`/tmp/goal-restart-candidate.json` throughout validation.

- `scripts/verify.sh all`: passed docs, checkout safety, backend formatting/types,
  **3,861 backend tests** (8 skipped), frontend lint/types, **872 frontend tests**,
  and production frontend build. Log: `/tmp/goal-restart-all-r2.log`; backend
  evidence: `/tmp/claude-hub-verify.oBnOpQ`.
- Browser mocks: all **11 runners passed**, summary
  `/tmp/claude-hub-browser-mocks.MVQRog/run/summary.json`. This ran before the
  API-only idempotency correction; frontend source was unchanged afterward.
- Independent full-candidate review: **200 checks passed**, including the original
  failing public-API counterexample and eight deeper native-input revocation
  probes. Evidence: `/tmp/claude-hub-verify.wxCnXt`.
- Separate review of the parent-authored integration and final API correction:
  **163 checks passed**, including late native preparation and two public Resume
  variants replayed across two restart generations. Evidence:
  `/tmp/claude-hub-verify.SiKpHi`; report:
  `/tmp/goal-restart-parent-review-rev2-7833.md`.
- The parent independently reran the original counterexample among 108 checks,
  verified both reviewers' result logs, and rechecked the frozen source hashes.

Both bounded independent reviews found no remaining concrete blocker. Validation
uses isolated local test services and provider doubles; there is no real external
provider or shared-service planned-restart end-to-end result. Delivery is limited
to the feature branch: no main integration, push, deployment or shared restart.
