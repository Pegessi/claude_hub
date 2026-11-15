# Working Log: Guidance-First Chat Goal Strict Cross-Review Upgrade (`2026-11-13`)

## Context & Problem Statement
Chat Goals support an optional `strict_review` gate where `<goal-status state="complete">` is verified by an independent cross-model reviewer subagent. Three practical limitations existed in the initial implementation:
1. **Duplicate / Quota-Blind Routing**: Cross-reviewer candidate selection lived separately in `services/goal_run/strict_review.py` and `services/cross_reviewer.py`, did not consult `provider_quota` readings (risking timeouts on exhausted Codex or MASO models), and could fall back to same-model self-review when `MASO_MODEL` was omitted from `tab.env`.
2. **Missing Situational Context & Rubric**: The reviewer subagent received only the worker's prose excerpt and checkpoint JSON without an objective full-diff snapshot, Workspace Feedback Lessons, `REVIEW.md` guidance, or a structured prior-round P0/P1 ledger with rebuttal fairness rules.
3. **Rigid 3-Round Ceiling**: A fixed 3-round cutoff stopped Goals that were actively converging (`4 -> 2 -> 1` blockers) while wasting a third round when rounds 1 and 2 were already stuck on the exact same blocker impasse.

## Design Decisions (Guidance-First, Zero Rigid Ceremony)
- **Single Quota-Aware Cross-Reviewer Router (`backend/claude_hub/services/cross_reviewer.py`)**:
  - Unified `CrossReviewCandidate`, `is_candidate_quota_exhausted`, `select_cross_reviewer_candidates`, and `resolve_cross_reviewer_target` in `cross_reviewer.py` while keeping all existing Workspace Task signatures backward-compatible.
  - Resolved implicit MASO service defaults via `maso_model_family(parent_env, default_model=default_maso_model)` and removed silent same-model fallback.
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
    - **Fresh Budget on User Steer / Resume**: Explicitly steering or resuming a `BLOCKED` Goal resets `strict_review_rounds`, `strict_review_max_rounds`, and `strict_review_history`.
  - Updated `GoalStatusBar.vue` to compute `strictMaxRounds` dynamically so grace rounds render as `4/4` or `5/5`.

## Verification
- `./scripts/verify.sh backend-format`
- `./scripts/verify.sh backend-types`
- `./scripts/verify.sh docs`
- `./scripts/verify.sh backend-tests -- tests/test_goal_strict_review.py tests/test_cross_reviewer_quota.py tests/test_goal_run.py -q`
- `./scripts/verify.sh frontend-types`
- `./scripts/verify.sh frontend-lint`
- `./scripts/verify.sh frontend-tests`
- `./scripts/verify.sh frontend-build`
