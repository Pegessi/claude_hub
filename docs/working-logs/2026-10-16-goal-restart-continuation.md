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

## Authorized local-main integration

After branch acceptance, the user authorized local-main integration. The fixed
parents are feature `cae8e53271e0b6a345dcbf40254ccff868ad5939` and main
`525b84e0a69c331de6e584f15c4a27284753d51a`, with merge base
`a72cf9d45c88aa05865e9619140caec38c874d8b`. The fetched `origin/main` was an
ancestor of that local main; no remote push is included.

The native merge only conflicted in CHANGELOG; both sets of entries are retained.
Incoming Goal images, objective editing, queue controls and private proxy settings
remain intact. Independent integration review found three concrete interactions:

1. Private proxy diagnostic redaction can mask a word in the trusted runtime-loss
   message. Hub-generated interruption errors now retain a boolean
   `runtime_interrupted` field through redaction. Recovery accepts that strict
   marker or the legacy exact message, never arbitrary redacted prose.
2. Private deployment proxy values are intentionally absent from tab data and
   process environment. The recovery configuration digest now uses the shared
   effective `provider_subprocess_env` calculation. Effective route changes veto
   recovery; explicit overrides still win, and only a digest is persisted.
3. A queued objective edit could change the Goal control digest before failed
   deferred recovery reacquired admission for cleanup, leaving an active deferral.
   Cleanup now runs and joins under the original admission lock, with the existing
   cancellation shielding and identity checks. The edit then applies to a paused
   Goal; a later explicit Resume still takes effect normally.

Objective editing retains the incoming next-step contract. It does not gain an
early pause/stop hook: an edit queued during successful recovery preparation
applies to the following step. An edit persisted before recovery still invalidates
the captured control digest. Accepted image inputs are not replayed, and pending
image delivery still requires explicit reattachment.

### Integration validation

The repaired source remained pinned by the 43-file manifest
`/tmp/goal-main-integration-candidate.json`, revision 1.

- Canonical `all`: **3,925 backend tests passed, 10 skipped; 885 frontend tests
  passed**; docs, checkout safety, formatting, types, lint and build passed.
  Evidence: `/tmp/goal-merge-all-r1.log`, `/tmp/claude-hub-verify.udCiHX`.
- Browser mocks: **11 runners passed**, owned Vite exited and its port closed.
  Evidence: `/tmp/claude-hub-browser-mocks.Epu7KC/run/summary.json`.
- Isolated terminal HMR/recovery: **12 passed**, no skips.
  Evidence: `/tmp/claude-hub-verify.n3NkxY`.
- Independent Goal/image/objective review: **234 passed**, plus **2** extra
  repeated-cancellation/queued-control probes. Evidence:
  `/tmp/claude-hub-verify.zzpE3b`, `/tmp/claude-hub-verify.LrEQYl`.
- Independent proxy/native/recovery review: **222 passed, 2 platform skips**.
  Evidence: `/tmp/claude-hub-verify.ib3bHv`; report:
  `/tmp/goal-main-integration-review-7833-final.md`.
- Parent verification: reproduced all six original proxy failures, then reran
  those unchanged probes and the objective counterexample among **275 passing**
  focused checks; separately reran the two cancellation/control probes.
  Evidence: `/tmp/claude-hub-verify.OLblfi`, `/tmp/claude-hub-verify.gwkDhn`,
  `/tmp/claude-hub-verify.vTRUDI`.

Both bounded independent reviews found no remaining concrete blocker. Browser
APIs and external providers remain mocked; the HMR gate uses its own real backend
and private terminal runtime, not the shared service. No live external-provider
planned restart, deployment, shared-service restart or remote push was performed.
The immutable merge decision record is added in a following documentation commit.
