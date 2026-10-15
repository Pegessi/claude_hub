# Managed Chat control and lifecycle repairs

## Scope

Follow-up to `feat/managed-workspace-chat` at `9a50cb31`. The user requested repairs
for rejected question-card answers and an investigation of similar problems.
Cross-verification found gaps that the initial layer-isolated tests did not cover.
This work remains in the existing feature worktree; no shared Hub deployment,
restart, merge or push is authorized.

The same Hub Task is retained under initiator control in execution epoch 2. Native
implementation agents do not receive reporter credentials or Task management rights.

## Required behavior

- A question response targets the exact active provider request and turn. It is
  not arbitrary steering and must not wait behind the turn that is awaiting it.
  Stale, duplicate, malformed and same-question-ID/different-card answers must not
  affect a later or different question.
- Managed input accepted while busy belongs to the durable Workspace mailbox,
  rather than only a page-local draft queue. Retrying an ambiguous response keeps
  its original call ID and payload identity.
- A managed model/reasoning update targets and persists the actual ManagedSession
  and its runtime. Unsupported process-level environment changes fail explicitly;
  in particular, a persistent Codex app-server cannot silently adopt new process
  credentials through a per-turn model update.
- Task-controlled Chat mode stays default. Ordinary direct Chat retains its mode,
  plan implementation, edit/fork and Goal behaviors.
- Direct tab/stream routes cannot create another provider owner for a managed Chat
  backing tab. Managed history rewrites remain disallowed. Images and other valid
  managed resources use the managed stream namespace.
- Stop distinguishes a matching active turn, an already completed request and an
  obsolete request targeting a different live turn. UI feedback must not claim the
  newer turn stopped when it did not.
- Successful Agent deletion reconciles stale pane/queue state. A transient failed
  fetch must not silently discard protected local input.
- Failed bootstrap cannot release an ID while a native owner remains. Teardown
  must fence the provider without discarding evidence. Durable identity removal
  must precede evidence cleanup; an unsuccessful save cannot make an accepted
  effect replayable after recovery.
- Task start uses the same provider/surface validation as creation, including
  compatibility overrides and the final Task state after awaited preparation.

## Lifecycle ordering

Mutating managed operations use workspace ownership before the per-session pump
lock. Reentrancy belongs to the current asyncio Task, not merely inherited context.
A caller admitted before an await retains its expected workspace, tab and creation
identity; a reused friendly session name is not sufficient to authorize a later
write. Wait/SSE release admission locks before long waits and recheck that identity
when they return or resume streaming. Ordinary tab stream descriptors are rebuilt
on lookup and use the backing tab's persisted creation time; repeated reads must
not appear to replace a healthy direct Chat owner.

Retiring a tailer permanently prevents detached references from restarting it.
Only the retiring owner's terminalization may publish during its teardown; ordinary
late callbacks cannot recreate deleted stream files. Creation compensates failures
after a tab exists, while raw pre-tab validation errors retain their existing
classification. Residual backing tabs reserve their managed identity across restart.
Unconfirmed Terminal teardown now retains the session for inspection/retry rather
than silently reporting deletion; normal successful Terminal behavior is unchanged.

## Ownership and validation approach

One implementation owner handles UI/API/config controls, another handles runtime
fencing and deletion, and the parent handles Task admission and integration.
A separate read-only audit enumerates neighboring control entrypoints. Final
independent review is performed after the candidate is fixed and frozen.

Tests must cross the Workspace manager, stream manager and provider adapter
boundaries with only the lowest-level provider replaced when possible. The
validation set includes persistence failure, bootstrap failure, exact question
identity, lost response retry, busy queue reload, wrong-namespace API calls, model
application, obsolete Stop and legacy direct-Chat behavior. Browser mocks verify
UI request contracts but do not establish real provider execution.

## Final acceptance

The final standard gate passed on the frozen product and test candidate:

| Check | Result | Evidence |
| --- | --- | --- |
| `scripts/verify.sh all` | All standard targets passed | `/tmp/managed-chat-repair-verify-final6.log` |
| Default backend suite | 3,536 passed, 8 skipped | `/tmp/claude-hub-verify.P3pDjW` |
| Backend format and types | Passed; 271 source files type-checked | Standard gate above |
| Frontend lint, types and production bundle | Passed | Standard gate above |
| Frontend unit tests | 850 passed | Standard gate above |
| Canonical mocked browser scenarios | 10 passed, including busy Enter/Ctrl+Enter and exact-card retry | `/tmp/claude-hub-browser-mocks.JPzFj6/run` |
| Real isolated Terminal HMR | 12 passed | `/tmp/claude-hub-verify.1N0X4i` |

The default-suite skips are seven opt-in browser cases and the clean-delivery
Git provenance test, which requires committing the worktree first. The latter
is a separate post-commit delivery check. Browser mocks use real Vue/Pinia DOM
but mocked APIs; HMR uses an isolated backend and private terminal resources.
Neither establishes real model behavior or performance improvements.

Independent controls and lifecycle reviewers found no remaining actionable issue
in their final scopes. Review covered the exact-card identity, durable keyboard
input, ambiguous retry, wrong-namespace controls, creation rollback, retained
receipts, delete/recreate waiters, lock order and late monitor callbacks. Final
runtime evidence includes 156 real-adapter stream tests, 83 affected lifecycle
tests and an independent 25-test monitor/interleaving run. The runtime source
manifest is `/tmp/final-lifecycle-product-sha256-v3.txt`; controls are pinned in
`/tmp/managed-chat-control-repair-snapshot.json`.

Final integration found two additional production regressions and retained their
checks: direct tab descriptors must use persisted creation time, and automatic
recovery must leave the REVIEW transition to `_request_task_review` so a reviewer
is actually dispatched. Earlier failed diagnostic runs are not acceptance passes.
Legacy test doubles were also completed with stable creation/archive fields and
an explicit publication guard; production ownership checks were not weakened.

Toolchain: Python 3.11.16, uv 0.12.15, Node 24.21.0, pnpm 9.15.9 and Playwright
1.58.0, with the explicit local Chromium executable. Existing Pydantic/OpenAPI
warnings and the Vite large-chunk warning remain non-blocking. Terminal replay,
live provider smoke and performance/manual acceptance were not run in this repair.
Final documentation is updated after the source/test freeze; staged checks and
post-commit provenance are required before handoff. No merge, push or deployment
is part of this delivery.
