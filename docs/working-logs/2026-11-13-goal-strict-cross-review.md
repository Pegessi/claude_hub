# Working Log: Guidance-First Chat Goal Strict Cross-Review Upgrade (`2026-11-13`)

## Context & Problem Statement
Chat Goals support an optional `strict_review` gate where `<goal-status state="complete">` is verified by an independent cross-model reviewer subagent. Three practical limitations existed in the initial implementation:
1. **Duplicate / Quota-Blind Routing**: Cross-reviewer candidate selection lived separately in `services/goal_run/strict_review.py` and `services/cross_reviewer.py`, did not consult `provider_quota` readings (risking timeouts on exhausted Codex or MASO models), and could fall back to same-model self-review when `MASO_MODEL` was omitted from `tab.env`.
2. **Missing Situational Context & Rubric**: The reviewer subagent received only the worker's prose excerpt and checkpoint JSON without an objective full-diff snapshot, Workspace Feedback Lessons, `REVIEW.md` guidance, or a structured prior-round P0/P1 ledger with rebuttal fairness rules.
3. **Rigid 3-Round Ceiling**: A fixed 3-round cutoff stopped Goals that were actively converging (`4 -> 2 -> 1` blockers) while wasting a third round when rounds 1 and 2 were already stuck on the exact same blocker impasse.

## Design Decisions (Guidance-First, Zero Rigid Ceremony)
- **Single Quota-Aware Cross-Reviewer Router (`backend/claude_hub/services/cross_reviewer.py`)**:
  - Unified `CrossReviewCandidate`, `is_candidate_quota_exhausted`, `select_cross_reviewer_candidates`, `resolve_active_maso_tab_model`, and `resolve_cross_reviewer_target` in `cross_reviewer.py` while keeping all existing Workspace Task signatures backward-compatible.
  - Resolved implicit MASO service defaults via `maso_model_family(parent_env, default_model=default_maso_model)` so heterogeneous models are tried first, while appending a quota-filtered clean-context same-model/same-provider independent subagent candidate (`keep=False`) at the tail so independent review still completes when cross-providers are unavailable or over quota.
  - Added non-blocking cached quota filtering (`read_cached_quota_map`) that skips `codex`/`traex` when `state == "over"`, `maso` models only when `enforcing is True` and the matching model limit is `"over"`, and `antigravity` when its matching family bucket is `"over"`.
- **Situational Context & Surgical Review Guidance (`services/goal_run/strict_review.py`, `controller.py`)**:
  - Added worker situational guidance in `_strict_review_prompt` (worktree hygiene when modifying a Git repo on a shared branch, baseline evidence for perf/memory work, and evidence-backed rebuttals for false positives) without imposing rigid Git worktree/commit/push gates on read-only or non-Git Goals.
  - Added `collect_strict_review_context` using bounded async `git -C <dir> --no-optional-locks` subprocesses (`<= 3s` timeout, process-group cleanup), automatic detection of referenced `*_worktree/<slug>` checkouts, and read-only lookup of Workspace `Feedback Lessons` and `REVIEW.md`.
  - Upgraded `build_strict_review_prompt` with full-diff inspection instructions, a causal P0/P1 counterexample bar, an anti-nitpick guard, and a prior-round blocker ledger that honors valid evidence-backed rebuttals.
- **Convergence-Aware Adaptive Round Budget (`models/goal_run.py`, `strict_review.py`, `controller.py`, `GoalStatusBar.vue`)**:
  - Kept `GOAL_STRICT_REVIEW_MAX_ROUNDS = 3` as the base budget and added `GOAL_STRICT_REVIEW_HARD_MAX_ROUNDS = 5` as the upper bound.
  - Implemented `evaluate_review_continuation`:
    - **Early Impasse Detection ($r \ge 2$)**: Stops early on `BLOCKED` when blocker count does not decrease ($B_r \ge B_{r-1}$) and $\ge 50\%$ of current blockers match the previous round's normalized file + summary token signature.
    - **Monotonic Convergence Grace ($3 \le r < 5$)**: Grants a grace round (`effective_max_rounds = min(5, r + 1)`) when blocker count strictly decreased ($B_r < B_{r-1}$).
    - **Fresh Budget on User Steer / Resume with Cycle-Scoped History**: Explicitly steering or resuming a `BLOCKED` Goal increments `strict_review_cycle`, resets `strict_review_rounds = 0` and `strict_review_max_rounds = 3`, and retains up to `GOAL_STRICT_REVIEW_HISTORY_LIMIT = 10` rounds of audit history while scoping `evaluate_review_continuation` to the active `strict_review_cycle`.
  - Updated `GoalStatusBar.vue` to compute `strictMaxRounds` dynamically so grace rounds render as `4/4` or `5/5`, and keyed `v-for` by `${round.cycle ?? 1}-${round.round_number}`.

## Round 2 Hardening (Astra Cross-Review Fixes)
- **Symlink-Safe & Off-Loop Context Collection**: `_read_bounded_review_md` verifies `REVIEW.md` resolves inside `root_real`, opens with `O_NOFOLLOW`, and reads at most `8192` bytes; `_detect_referenced_worktrees_sync` and `_collect_workspace_memory_sync` execute via `asyncio.to_thread`.
- **Bounded Subprocess Teardown**: `_run_git_command` reads bounded streams under `asyncio.timeout`, kills the process group (`os.killpg`), awaits `proc.wait()` with a 2s cap, and closes `proc._transport` without calling unbounded `proc.communicate()`.
- **Exact Canonical Base Model Matching**: `canonical_maso_base_model` strips only reasoning/thinking effort suffixes so `[redacted]: over` never falsely marks `[redacted]-reasoning-high` exhausted.
- **Live MASO Session Model Detection**: `run_subagent_strict_review` checks `_resolve_active_tab_model(goal.tab_id)` when `MASO_MODEL` is unset in `tab.env` before falling back to the MASO service's current global default.
- **Objective Revision & `plan_first` Guards**: `_execute_strict_review` checks `reviewed_objective_revision` and `reviewed_cycle` before recording round results; `_on_turn_completed_locked` enforces the `planning_turn or planned_now` pause before `signal == "complete"`, and `parse_strict_review_output` checks all findings (up to 500) for `P0`/`P1` before prioritizing blockers in the 30-finding slice.

## Verification
- `./scripts/verify.sh backend-format`
- `./scripts/verify.sh backend-types`
- `./scripts/verify.sh docs`
- `./scripts/verify.sh backend-tests -- tests/test_goal_strict_review.py tests/test_cross_reviewer_quota.py tests/test_goal_run.py -q`
- `./scripts/verify.sh frontend-types`
- `./scripts/verify.sh frontend-lint`
- `./scripts/verify.sh frontend-tests`
- `./scripts/verify.sh frontend-build`
