# Configurable Cross-Review & Slim

The Chat composer opens a configurable strict-review Goal using the existing GoalRun controller and review gate. Runtime defaults are atomically persisted, while explicit Goal choices participate in create replay identity. Dual review uses concurrent independent candidates, backup routing and deterministic verdict merging. The read-only preset rejects branch pushing and gives the worker a read-only instruction.

The existing Goal setup and standard review prompt remain the compatibility benchmark. New settings storage and the dialog own the configuration and interaction boundaries; routing, execution and Goal history remain with their existing owners.

Validation evidence and limitations are recorded in the delivery handoff. Browser checks use mocked API responses and do not establish provider availability or production integration.

Historical source-branch validation (before integration): `./scripts/verify.sh all` passed all nine standard targets (backend: 7,787 passed, 10 skipped; frontend: 1,554 passed). The first complete mocked browser run passed; a later run found a scenario setup race, corrected and checked in a focused browser rerun. The complete browser suite was then repeated. Live provider routing and an actual branch push were not exercised.

## Round 2 review

The push continuation now binds the reviewed worktree, branch, HEAD and clean status, invalidates authorization on interruption, and rechecks the target before completion. Protected or dirty checkouts skip automatic pushing. Settings reads tolerate damaged files, create replay uses the original normalized request, and provider effort validation shares capability data with the provider registry. The dialog now gates submission on settings hydration, isolates save responses by session, handles radio-group focus and uses the application theme tokens. Focused tests cover the interruption and Git identity paths, corrupted settings and native Goal replay; the browser scenario exercises delayed settings and save responses.

Round 2 standard verification passed all nine `verify.sh all` targets: 7,797 backend tests passed, 10 skipped, and 1,554 frontend tests passed. The standalone ESLint check reported zero ESLint warnings or errors. Browser acceptance uses isolated mocks; the focused Goal scenario passed after fixing the select label associations.


## Round 3 review

Ambiguous worktree references now skip automatic pushing, push-turn references are checked separately, and repeated checkout invalidations stop at the existing hard cap. Switching Chats closes the Cross-Review dialog, and a failed start can be retried while that dialog remains open. Focused verification passed 40 strict-review tests and 45 Chat lifecycle and wiring tests.

Round 3 `verify.sh all` passed all nine targets: 7,800 backend tests passed with 10 skipped, and 1,557 frontend tests passed. Touched frontend files passed ESLint with `--max-warnings=0`.

## Round 4 review

A persisted push-attempt budget now bounds incomplete push turns and changes during review as well as post-push target mismatches. Pauses and recovery preserve the budget; explicit user recovery resets it. Focused strict-review verification passed 45 tests, including missing-signal and continuation retries, user recovery, and repeated changes during review.

Round 4 `verify.sh all` passed all nine targets: 7,805 backend tests passed with 10 skipped, and 1,557 frontend tests passed. The isolated mocked-browser acceptance suite passed all 25 scenarios.


## Integration on the current Goal lifecycle

The four source commits were replayed onto the current main base. The integration
retains recoverable completion metadata and the existing non-interrupting review
steer queue; no second queue, review state machine or resource store was added.
Queued input wins over PASS/push and the automatic push retry cap, including when
the candidate changes during review. Pause still cancels every live reviewer.

Self-review corrected two fail-open edges: dual review may not complete after
only one valid verdict, and a missing Chat may not authorize an unbound push.
An independent read-only UI/configuration audit identified hydration dropping saved
presets after note edits, and a narrower effort vocabulary than the runtime. Both
were addressed: configuration controls wait for hydration while notes survive;
effort values share the subagent/runtime definition. Effort remains a requested
provider option, not a model-capability guarantee, and the dialog discloses that
unsupported combinations may use provider defaults.

### Concept survival and reuse

| Addition | Existing owner / reason to retain |
| --- | --- |
| Cross-review options | Extend GoalRun creation, persistence and replay identity rather than a separate workflow engine. |
| Settings file | Stores only reusable user defaults; active Goal state remains in GoalRunStore. |
| CrossReviewDialog | Owns opt-in configuration; submission reuses the Chat Goal creation path. |
| Worker prompt builders | Pure rendering moved from the controller and strict-review executor into `goal_run/prompts.py`; preserves the controller size ceiling without adding state or a parallel execution path. |
| Push target/attempt budget | Binds permission to a reviewed clean candidate and bounds automatic retries; interruption invalidates permission, explicit recovery controls retry. |
| Tests | Extend existing Goal, strict-review, queue, API and browser suites; no standalone regression-test module. |

### Verification scope

Focused tests exercise the actual review slot runner with controlled subagent
transports for single/dual PASS, BLOCK, unavailable reviewer and cancellation;
queued steering retains order and waits for both dual slots. Clean Git fixtures
exercise push identity, and recoverable push failures retain PAUSED/UNCERTAIN
semantics without retaining push authorization. Browser checks render the real
SPA with mocked APIs, including delayed defaults, scope-note preservation and
effort-request disclosure. Full verification results are recorded in the managed
Task handoff. Live providers and a real remote branch push are not exercised;
prompt-level read-only/push restrictions are not an OS sandbox.


The final validation also exposed two existing test-order assumptions. The browser
review-queue fixture attached an image after the optimistic text clear but before
the send receipt; it now waits for the composer to be enabled. New test counts
changed shard placement and exposed a launcher wiring test leaking its bind-host
environment into the restart-process test. The ordered two-module run reproduced
the failure, then passed after registering that environment key for fixture
restoration. No production listen guard or service lifecycle behavior was changed.
