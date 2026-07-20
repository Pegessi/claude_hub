# Prompt compaction and revision-resume briefing for autonomous tasks

**Date**: 2026-07-17
**Scope**: backend `workspace_manager/_prompts.py`, tests
**Goal**: reduce prompt size and combat "context rot" on long autonomous+complex tasks.

## Problem

Under `autonomous` + `complex` mode, a task that takes several iterations
accrues:

1. Long static prompt scaffolding (orchestrator contract, review workflow,
   model-pinning rules, sub-ledger schemas) that is repeated on every dispatch,
   continue, and hard recovery.
2. Unbounded reviewer history: `_build_review_prompt` replayed the last 12 full
   reports verbatim — including full `validation` subagent-ledger text, full
   `risks`, full `acceptance_check` evidence arrays, and full `profile_results`.
   After ~3 review cycles this alone could be 6–10k tokens of largely redundant
   text.
3. Hard recovery after API errors replayed the full assignment prompt, including
   full Goal Packet JSON and the full orchestrator contract — even on iteration
   3+, when the worker's context has already seen those instructions several
   times.

Combined, these produce the reported "context rot" symptom: late iterations
carry so much text that the agent loses the thread of early decisions and
issues contradictory instructions, even with multi-agent coordination.

## Three-legged fix

All changes are in `backend/claude_hub/services/workspace_manager/_prompts.py`;
no schema changes, no frontend changes. Auto-clear on every continue is
explicitly out of scope (V2 follow-up — aggressive clearing risks throwing
away useful in-flight reasoning on the common fast-review path).

### (a) Static scaffold compaction

Shortened the static wording while preserving every semantic invariant:

| Block | Before (AST tokens) | After |
|---|---:|---:|
| `_build_workspace_agent_prompt` (bootstrap) | 452 | 264 |
| `_build_task_assignment_prompt` (scaffold literals) | 844 | 549 |
| `_orchestrator_contract_block` | 1230 | 664 |
| `_review_workflow_block` | 1338 | 797 |
| `_build_review_prompt` (scaffold literals) | 646 | 436 |

What was cut:

- The worked `/api/orders` compact-skeleton example in the orchestrator
  contract. It demonstrated envelope/ledger shape but added ~400 tokens and is
  redundant with the inline schema lines that remain.
- Duplicate "Bilingual reporting" paragraph that appeared in both assignment
  and review prompt scaffolding.
- Rephrased observability rules and enforcement wording onto tighter lines.
- Collapsed model-pinning lines to slash-separated form:
  `P-PLAN/P-EXECUTE/P-JUDGE/P-INTEGRATE -> opus; P-VALIDATE/P-RESEARCH -> sonnet`.
- Collapsed the GP-plan-gate and regular-review workflow sections via a shared
  template with a conditional `gp_intro`/`gp_extra`, preserving the
  GP-specific "review_passed means the implementation agent may begin
  development" line that existing plan-gate tests assert on.

What was preserved (with updated/relaxed tests where assertions were
string-exact rather than semantic):

- All six role primitives: P-PLAN, P-EXECUTE, P-VALIDATE, P-JUDGE,
  P-INTEGRATE, P-RESEARCH.
- Opus/sonnet pinning per primitive, with the runtime-aware escape clauses
  for codex ("Claude opus/sonnet pinning is NOT required") and terminal
  ("no native sub-agent capability").
- Subagent-ledger schema and verification requirement for reviewers
  (including `review_failed` mandate when the ledger is absent).
- Observability rule: opaque delegated/external work must heartbeat, name the
  blocker on `blocked`/`needs_input`, contract violation language,
  "needs your response" anchor.
- P-JUDGE pre-flight requirement even for simple tasks.
- Per-CLI capability hints (Task tool / model param for claude; "version or
  unsupported" caveat for cursor/codex; graceful-degrade wording for
  terminal).
- Report-endpoint curl example that the endpoint-restatement tests rely on.
- Goal Packet plan-gate exit criteria phrasing.

### (b) Tiered reviewer report history

New helpers:

```python
_SUMMARY_VERBOSE_FIELD_MAX = 240
_FULL_REPORT_WINDOW = 4
_MAX_REPORT_HISTORY = 12

def _truncate_verbose(self, value, limit=_SUMMARY_VERBOSE_FIELD_MAX) -> str | None
def _serialize_report_for_review(self, report, *, full: bool) -> dict[str, Any]
def _serialize_task_reports_for_review(self, task, trigger_report) -> list[dict[str, Any]]
```

Strategy:

- The last `_FULL_REPORT_WINDOW = 4` reports (ending at the trigger) are
  serialized verbatim.
- Earlier reports (up to `_MAX_REPORT_HISTORY = 12` total) are summarized:
  - `validation` and `risks` truncated to 240 chars.
  - `acceptance_check` and `profile_results` arrays dropped entirely;
    `artifact_refs_count` and `acceptance_check_count` integer counts are
    substituted so the reviewer still sees whether evidence/artefacts exist.
  - Core fields (`state`, `session_id`, `message`, `changed_files`,
    `review_decision`, `risk_level`, `created_at`) kept verbatim.

Bounds prompt growth linearly at ~80 tokens per additional past-cycle report
instead of ~600–1200 tokens. The reviewer still sees full ledger/evidence for
the current cycle (the trigger + 3 immediately prior verdicts).

### (c) Revision-resume briefing on hard recovery

New helpers:

```python
def _latest_reviewer_blocking_feedback(self, task) -> str | None
def _current_changed_files(self, task) -> list[str]    # dedup across worker reports, last 20
def _build_revision_resume_prompt(self, workspace, task, session, *, interruption_reason) -> str
```

`_build_hard_recovery_worker_prompt` now branches:

- On **first iteration** (`iteration < 2` and `review_cycle < 2`, or any
  non-autonomous task) it keeps the existing cold-start behavior: full
  warning, re-read instruction, full Goal Packet JSON, endpoint curl.
- On **iteration ≥ 2** or **review_cycle ≥ 2** (autonomous only) it emits a
  tight briefing (~380 tokens) starting with "⚠️ Context refreshed after
  error" and containing:
  1. Interruption reason.
  2. Task metadata (id, title, mode, complexity, iteration, review_cycle).
  3. **Compact Goal Packet**: objective, up to 8 acceptance bullets, up to 6
     out-of-scope bullets (full GPs can be thousands of tokens once examples
     and assumptions are included; the briefing only carries what the worker
     needs to re-orient).
  4. **Changed files** so far (deduped across worker reports, capped at 20).
  5. **Latest reviewer blocking feedback** verbatim, truncated to 1500 chars
     if needed — this is the highest-leverage piece of context because it
     tells the worker exactly what the reviewer wants fixed next.
  6. 4-step resume instructions (read changed files, address blocking
     feedback, validate, POST report).
  7. Report endpoint curl.

A one-line orchestrator reminder was added to
`_autonomous_continue_orchestrator_reminder`: "If your own context feels
decayed (confused about earlier decisions, contradictory instructions),
prefer a fresh sub-agent rather than reasoning in the main thread." This
teaches the orchestrator itself to push work down rather than fight a
bloated main-thread context.

## Pitfalls / guardrails

- **mypy dict-type inference**: the two payload literals in
  `_serialize_report_for_review` have different value types across the
  `full=True`/`full=False` branches; mypy infers a too-narrow type from the
  common prefix and complains on the update calls. Fix: annotate `payload:
  dict[str, Any]` at construction (`Any` is already imported via
  `from ._constants import *`).
- **Anchor phrases for tests**: the reviewer prompt and reviewer
  hard-recovery prompt must emit standalone lines for
  `Task ID:`, `Task title:`, `Task mode:`, `Task execution complexity:` and
  the GP-plan-gate block must still contain
  "review_passed means the implementation agent may begin development" —
  existing tests and (more importantly) downstream prompt parsing rely on
  those exact anchors. When I first collapsed them into a combined header
  line, 6 tests failed; I restored the individual lines.
- **Non-claude runtimes untouched**: cursor and codex capability hints still
  carry the runtime-default model caveats; terminal still carries the
  "no native sub-agent capability" hint and "Degrade gracefully"; simple
  tasks still carry the soft-enforcement wording and P-JUDGE pre-flight
  requirement. Existing tests for all four runtimes still pass.
- **Auto-clear deferred**: clearing on every continue would further reduce
  context size but risks throwing away useful in-flight reasoning on the
  fast-path reviews (a reviewer may already have the worker's code and
  report in active context). We instead only use the new briefing on hard
  recovery (which already /clears the context) — this is the highest-impact
  point because /clear already wiped the slate. V2 can explore proactive
  clear on iteration≥3 thresholds.

## Validation

- `black --check` and `isort --check` clean on all touched files.
- `mypy` clean across all 59 backend source files.
- `pytest`: 271 tests pass across `test_workspace_orchestrator_contract.py`,
  `test_workspaces.py`, `test_hard_recovery.py`, `test_workspace_state_policy.py`,
  and `test_feedback_lessons.py`.
- Prompt sizes measured via `scripts/measure_prompts.py` (uses tiktoken
  cl100k_base with a char/4 fallback), exercising autonomous+complex,
  autonomous+simple, reviewed+complex, direct+simple, both hard-recovery
  branches, and a 10-prior-report history scaling case:

  ```
  bootstrap worker                                         tok~=  305
  ASSIGN autonomous+complex claude                         tok~= 1731
  ASSIGN reviewed+complex claude                           tok~=  920
  REVIEW autonomous+complex (1 prior report)               tok~= 2119
  CONTINUE (autonomous)                                    tok~=  241
  HARD-RECOVERY worker (iter=1, cold)                      tok~=  481
  HARD-RECOVERY worker (iter=3, resume briefing)           tok~=  379
  REVIEW (10 prior verbose reports, iter=3) [tiered]       tok~= 8504
  ```

  Pre-change equivalents (from AST static measurement + historical review
  prompt sizes on similar tasks):

  - Cold-start ASSIGN autonomous+complex ~2.4k tokens → 1.7k (≈30% reduction
    in static scaffold; exact number depends on GP/lesson content).
  - Cold-start HARD-RECOVERY (iter=1) ~600 tokens → 481.
  - Post-iteration HARD-RECOVERY (iter≥2, replayed full assignment) ~1.7k
    → 379 (≈78% reduction; this is the biggest win for context rot).
  - Reviewer history scales sub-linearly: tiered 8.5k for 10 seeded
    verbose-ledger reports vs an estimated 12–14k for the prior
    verbatim-all-12 behavior; on less-pathological real data (validation
    200-800 chars per report) the tiered prompt typically lands under 4k
    even after several cycles.

## Files changed

- `backend/claude_hub/services/workspace_manager/_prompts.py` — primary
  changes (compacted scaffolding, new tiered serializers, new resume
  briefing, branching hard recovery).
- `backend/tests/test_workspace_orchestrator_contract.py` — relaxed a few
  assertions to match shortened wording; renamed
  `test_autonomous_block_complex_includes_orchestrator_contract_and_skeleton`
  to `...and_primitives`; accepts slash-separated model pinning.
- `backend/tests/test_workspaces.py` — one relaxed assertion (orchestrator
  guidance check on the complex block).
- `scripts/measure_prompts.py` — new ad-hoc measurement script (not a
  pytest; used to verify size budgets).
- `CHANGELOG.md` — entry under Unreleased.

## Follow-up candidates (V2, not in this PR)

- Proactive /clear on continue when iteration ≥ 3 and the last resume
  briefing already carried the compact GP — at that point the main-thread
  context is almost certainly decayed and a fresh briefing beats another
  full-prompt replay.
- Token budget assertions as a real pytest (e.g.,
  `test_prompt_sizes.py` asserts `_orchestrator_contract_block` stays below
  a char threshold and that tiered serialization of N verbose reports
  doesn't grow linearly). Currently the measurement is a separate script;
  capturing it as a regression guard would prevent future bloat.
- Summarize older reports more aggressively (e.g., collapse all reports
  older than review_cycle-1 into a single "prior cycles: N reviews, K
  passed, M failed" line instead of per-report dicts).
