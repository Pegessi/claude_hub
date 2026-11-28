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

## Shared default subagent priority & lightweight Cross-Review & Slim

### Product goal alignment

Per user direction, per-run model matrices, proliferating presets (`mr_slim`, `bug_only`, `readonly_brief`), and `single`/`dual` reviewer modes were retired from new Goal creation in favor of:

1. **One shared default subagent priority configuration in global Settings**:
   - `SubagentPrioritySettings` (`preferred_candidates`, `push_remote_branch`, and legacy-compatible `default_preset`, stored in `subagent_settings.json` with automatic fallback/sync to `cross_review_settings.json` and exposed at `GET`/`PUT`/`PATCH` `/api/subagents/settings` + `/api/goals/cross-review-settings`).
   - Managed from global Settings (`SubagentSettingsDialog.vue` in the Extensions menu and linked directly via `Configure in Settings` inside `CrossReviewDialog.vue`), with deep-cloned state snapshots, HTTP error/retry protection on failed or malformed loads (`503` / missing `settings`), narrow conflict-safe `PATCH` updates from `CrossReviewDialog.vue` so push/mode toggles never overwrite concurrent candidate edits across tabs, and `overflow-wrap: anywhere` containment for up to 120-character custom model IDs on `390×844` mobile viewports.
   - Consumed by normal subagent delegation (`claude-hub agent run` without `--provider` and `POST /api/subagents` with optional `agent_type`, filling omitted provider/model/effort from the highest-priority eligible entry while preserving explicit `--provider`, `--model`, `--effort`, and `env` / `env_preset` model/effort overrides, excluding authoritatively `over` or `not_configured` automatic targets and returning `503` when no automatic provider is eligible), Goal cross-review (`run_subagent_strict_review` and `_AGENT_RUN_GUIDANCE`), and Workspace cross-review target resolution (`resolve_cross_reviewer_target`, honoring both MASO and non-MASO `Surface.MANAGED_CHAT` candidates).
2. **Lightweight Cross-Review & Slim entry**:
   - `CrossReviewDialog.vue` is a compact entry for code cross-review and justified simplification across four pillars: (1) bugs and correctness, (2) elegant and reasonable design, (3) restraint against overengineering and excessive defensive code, and (4) performance.
   - Displays the active shared subagent priority summary (`Configure in Settings`), a `Push feature branch on PASS` wrap-up option, and `Scope / focus notes` with one-click `Reset to default`, contrast-safe (`>=4.5:1`) validation feedback, and `>=44px` mobile touch targets at `390×844` with zero horizontal overflow.
3. **Historical `readonly_brief` safety**:
   - Persisted `goals.json`, `cross_review_settings.json`, and `subagent_settings.json` records with `readonly_brief` retain their strict read-only (`STRICT READ-ONLY MODE` worker prompt and read-only reviewer rubric) and no-push (`strict_review_push_branch=False`) invariants across saved default settings, cold load, resume, steer, and review rounds (with an explicit banner, one-click `Switch to Cross-Review & Slim` control, and fail-closed launch blocking with `Retry load` when settings hydration fails or returns malformed data in `CrossReviewDialog.vue`), while legacy `mr_slim` and `bug_only` records normalize cleanly to `cross_review_slim`.