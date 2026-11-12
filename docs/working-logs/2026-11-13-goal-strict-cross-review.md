# 2026-11-13 Goal strict cross-review consensus gate

## Overview

Adds an optional `strict_review` gate to Chat Goals (`Set a Goal` dialog).
When enabled (default remembered on under `claude-hub.goal-strict-review.v1`),
completion is gated by an independent cross-model reviewer subagent before the
Goal transitions to `complete`.

## Behavior and boundaries

- **Setup UI (`GoalSetupDialog.vue`, `ChatGoalSetup.vue`, `useChatGoal.ts`)**:
  - Adds a `Strict cross-review` option card alongside `Plan first`, defaulting
    on (`Consensus gate` badge) and remembered in `localStorage`.
  - Existing API/CLI/Feishu callers that omit `strict_review` default to
    `strict_review: false`, preserving existing start and completion behavior
    byte for byte.
- **Execution ownership (`services/goal_run/native.py`, `controller.py`)**:
  - Provider-native Goals (e.g., Codex `thread/goal/*`) never route completions
    through Hub continuation gates. When `strict_review=True`, creation bypasses
    provider-native Goal ownership (after checking that no unfinished native
    Goal already occupies the thread) and runs as `hub_managed`, enabling both
    the cross-model review gate and `Plan first` on Codex Chats.
- **Cross-model review gate (`services/goal_run/strict_review.py`)**:
  - When an implementing Goal turn emits `<goal-status state="complete">`,
    `GoalRunController` enters `strict_review_in_progress=True` and invokes an
    ephemeral read-only reviewer via `SubagentService` (`solo_mode=True`,
    `keep=False`).
  - Reviewer selection picks a heterogeneous model/provider relative to the
    parent tab (e.g., Codex -> MASO Gemini Argon High; MASO Argon -> MASO
    GPT-6-Astra High / Codex).
  - Parsing `<goal-review>` fails closed: any `P0` or `P1` finding forces a
    `BLOCK` verdict even if the reviewer prose or label says `PASS`.
  - On `PASS`, the Goal transitions to `complete` and records the round in
    `strict_review_history`.
  - On `BLOCK` below `strict_review_max_rounds` (3), the controller formats the
    blocking defects into `strict_review_feedback` and automatically dispatches
    the next Goal continuation turn.
  - Reaching `strict_review_max_rounds` without `PASS` (or encountering an
    unrecoverable reviewer transport error) parks the Goal on `blocked` for
    human inspection/steering. Pausing or clearing cancels an in-flight review.
