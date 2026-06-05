# Feedback Harness Plan

## System Overview

The OpenAI harness-engineering article argues that agent-first engineering
scales when humans design the environment, clarify intent, and build feedback
loops instead of manually supplying every correction. It also emphasizes making
the application, logs, metrics, reviews, and project knowledge directly readable
by agents so they can reproduce failures, validate fixes, and feed learned
judgment back into the repository or tooling.

Claude Hub already has strong pieces of that harness:

- Workspace tasks produce structured reports with Goal Packets, acceptance
  checks, validation, risks, profile results, artifacts, confidence, and review
  routing.
- Reviewed mode has an independent reviewer loop and a final human Done gate.
- Autonomous mode stores run iterations and evaluation reports, but still keeps
  human acceptance as the final authority.
- Completed tasks are archived as task records containing the task, session,
  reports, timeline, artifacts, and final summary.
- Recent Auto Mode observability work added timing metadata and heartbeat
  expectations for delegated or long-running work.

The missing layer is a durable feedback loop after each task. Today, useful
feedback mostly enters the system when a human writes it into the next task or
when a reviewer sends a one-off failed-review continuation. Claude Hub should
harvest every completed or failed task into agent-readable feedback, then use
that feedback to improve future dispatch prompts, review profiles, validation
expectations, and operator dashboards.

## Source Article Mapping

Relevant article ideas and their Claude Hub interpretation:

| Article idea | Claude Hub implication |
| --- | --- |
| Human attention is scarce; agents need feedback loops. | Human review should remain final, but routine feedback generation should be automated from task records, review results, runtime timing, and validation gaps. |
| Make UI, logs, metrics, and traces readable by agents. | Feedback generation should consume structured reports, archived timelines, artifact refs, logs, and future span data rather than prose-only terminal scrollback. |
| Treat the repo as the record system. | Stable feedback rules, playbooks, and accepted learnings should become versioned docs or policy fixtures, not private chat memory. |
| Encode taste and invariants into tools. | Repeated reviewer comments should graduate into review profiles, prompt contract changes, linters, tests, or workspace policy rules. |
| Use garbage-collection loops for entropy. | A scheduled feedback reaper should find stale patterns, missing evidence, slow loops, and repeated failures before they become normal. |

Source: https://openai.com/zh-Hans-CN/index/harness-engineering/

## Current Baseline

### What Exists

- `backend/claude_hub/models/schemas.py` has the core evidence containers:
  `GoalPacket`, `AcceptanceCheck`, `EvaluationReport`,
  `ReviewProfileResult`, and `AgentReport`.
- `backend/claude_hub/services/workspace_manager.py` routes report-gate states
  through reviewer or autonomous evaluator policy.
- `_write_task_record()` archives completed tasks under the workspace task
  records directory with timeline and artifact summaries.
- `_handle_review_report()` can send targeted failed-review feedback back to
  the original worker.
- `docs/working-logs/2026-06-04-auto-mode-observability.md` already calls out
  that child agent/tool spans still need a future schema.

### Gap

The system records evidence, but it does not yet learn from it. There is no
first-class feedback artifact that says:

- what slowed this workspace down,
- which acceptance criteria were weak or missing,
- which reviewer findings are recurring,
- which validation commands should have been required,
- which prompt assumptions caused rework,
- which agent/runtime performed poorly for this task shape,
- which human intervention could be avoided next time,
- which repeated comments should become policy or tests.

## Proposal: Feedback Harness V1

Add a per-workspace feedback harness that runs after task terminal events and
turns task records into reusable, bounded, auditable feedback.

The V1 design is additive. It does not replace reviewed mode, autonomous
evaluators, final human acceptance, or reviewer routing. It adds a second-order
learning loop around them.

### 1. Feedback Reaper

Introduce a workspace-scoped Feedback Reaper process. The reaper runs when a
task reaches one of these milestones:

- human marks a task Done,
- autonomous evaluation passes or exhausts,
- reviewed task receives `review_failed` more than once,
- task is manually aborted,
- task enters `needs_input` or `blocked` and stays there beyond a configured
  threshold.

Input:

- archived task record from `_write_task_record()`,
- recent reports for the same task,
- reviewer/evaluator reports,
- AI reviewer feedback from `review_failed`, `review_needs_input`,
  `profile_results`, `evaluation_report`, and non-blocking notes,
- human request-changes notes and final acceptance comments when available,
- task mode and execution complexity,
- task timeline durations,
- changed files and artifact refs,
- optional backend logs or future trace/span records.

Output:

- `FeedbackRecord` JSON for machine use,
- short Markdown digest for humans,
- optional candidate changes to docs, review profiles, tests, or prompt policy.

The reaper can initially be implemented as a managed reviewer-like session or a
backend background job that asks an available reviewer agent to analyze the
record. A dedicated role can come later if the queue pressure justifies it.

### 2. Feedback Record Schema

Add a structured record with fields like:

```json
{
  "schema_version": 1,
  "workspace_id": "...",
  "task_id": "...",
  "created_at": "...",
  "source_record_path": "...",
  "task_shape": {
    "mode": "reviewed|autonomous|direct",
    "complexity": "simple|complex|auto",
    "agent_type": "codex|claude|cursor|terminal",
    "changed_file_globs": ["backend/**", "docs/**"]
  },
  "outcome": {
    "final_state": "done|blocked|needs_input|aborted|exhausted",
    "review_attempts": 0,
    "human_acceptance_required": true,
    "human_intervention_reason": ""
  },
  "efficiency": {
    "total_elapsed_seconds": 0,
    "working_elapsed_seconds": 0,
    "idle_gap_seconds": 0,
    "review_elapsed_seconds": 0,
    "auto_continue_count": 0
  },
  "quality": {
    "acceptance_criteria_quality": "strong|weak|missing",
    "validation_quality": "strong|partial|missing",
    "review_findings": [
      {
        "source": "ai_reviewer|human|evaluator|reaper",
        "severity": "blocking|non_blocking|note",
        "summary": "...",
        "evidence": "..."
      }
    ],
    "regression_risks": []
  },
  "learnings": [
    {
      "kind": "prompt|review_profile|validation|tooling|docs|architecture",
      "summary": "...",
      "evidence": "...",
      "confidence": 0.0,
      "recommended_action": "observe|suggest|apply_after_human_approval"
    }
  ]
}
```

Keep the schema small enough for prompt injection. Store bulky transcripts and
artifacts by reference, not inline.

### 3. Feedback Store

Store feedback outside task reports so it can be queried independently:

- `~/.claude_hub/workspaces/<workspace_id>/feedback/records/*.json`
- `~/.claude_hub/workspaces/<workspace_id>/feedback/digests/*.md`
- `~/.claude_hub/workspaces/<workspace_id>/feedback/index.json`
- `~/.claude_hub/workspaces/<workspace_id>/feedback/lesson-index.json`

The workspace board can later surface a compact health panel from the index:

- top recurring review failures,
- slowest task shapes,
- missing validation patterns,
- human-blocker categories,
- candidate policy/doc updates awaiting approval.

AI reviewer output should be treated as first-class feedback. A failed review is
not only a continuation prompt for the current worker; it is also evidence that
the harness missed something earlier. The reaper should preserve both blocking
and non-blocking reviewer comments, then classify them into validation gaps,
prompt gaps, review-profile gaps, docs gaps, tooling gaps, or genuine product
judgment.

Only promoted learnings should enter the repository. Candidate files:

- `docs/working-logs/YYYY-MM-DD-*.md` for design history,
- `REVIEW.md` for reviewer guidance,
- `AGENTS.md` / `CLAUDE.md` only for stable navigation and retrieval rules,
- backend policy tests or frontend lint/tests when a repeated issue can be
  mechanically enforced.

### 4. Doc Navigation Index

`AGENTS.md` and `CLAUDE.md` should stay short. They should keep their current
semantic contract, but add a task-oriented document navigation section instead
of absorbing every lesson directly.

The navigation should answer: "For this kind of task, where should the agent
look first?"

Example shape:

| Task shape | Read first | Why |
| --- | --- | --- |
| workspace task lifecycle / reports / review routing | `docs/working-logs/2026-05-23-workspace-goal-packet-v1.md`, `docs/working-logs/2026-05-23-state-machine-assessment.md` | Goal Packet, report states, review transitions |
| autonomous mode / evaluator loop | `docs/working-logs/2026-05-26-autonomous-mode-v1.md`, `docs/working-logs/2026-06-01-auto-mode-cli-subagent-orchestration.md` | autonomous phases, reviewer-as-evaluator, subagent ledger |
| task timing / long-running agents | `docs/working-logs/2026-06-04-auto-mode-observability.md` | heartbeat, elapsed timing, archive timeline |
| UI task board changes | `frontend/src/components/AgentWorkspaceView.vue`, relevant working log | board/detail behavior and display pitfalls |

This keeps the root agent files useful as routers, not dumping grounds. The
feedback system can promote stable lessons into this navigation index by adding
links and short retrieval cues, while detailed reasoning stays in working logs,
`REVIEW.md`, tests, or policy code.

### 5. Lesson Retrieval Index

Lessons should be retrievable the same way docs are retrievable. Do not rely on
agents remembering prior chat, and do not inject the entire feedback history
into every task.

Maintain a small lesson index with:

- normalized tags such as `workspace-review`, `frontend-overflow`,
  `auto-mode-heartbeat`, `backend-validation`, `task-abort`,
- file globs and code owners,
- task-mode applicability,
- confidence and promotion stage,
- source feedback record IDs,
- expiration or superseded-by metadata.

Assignment prompts should query this index with the task title, task prompt,
task mode, changed-file hints, and runtime target. The result should be a short
ranked list of relevant lessons plus doc links. Reviewer prompts should receive
the same lesson IDs so reviewers can check whether the worker used or ignored
known project knowledge.

### 6. Prompt-Time Feedback Injection

Add a bounded feedback block to future task assignment prompts. It should be
small, recent, and relevant:

```text
Workspace feedback hints:
- Similar backend review tasks often missed acceptance_check evidence. Include
  one acceptance_check item per Goal Packet criterion before requesting review.
- Recent frontend UI tasks failed due to text overflow. Run the responsive
  screenshot checklist when touching AgentWorkspaceView.vue.
- Long autonomous external-tool tasks need working heartbeats every checkpoint.
```

Selection rules:

- same workspace,
- similar task mode/complexity,
- matching file globs or task keywords,
- matching doc-navigation tags or lesson-index tags,
- high confidence,
- not expired,
- not superseded by a code/policy change.

This must be a hint block, not an invisible override. The original task prompt
and stored Goal Packet stay authoritative.

### 7. Feedback Application Ladder

Feedback should move through explicit maturity stages:

1. `observe`: record one-off feedback; do not change behavior.
2. `index`: add tags and doc links so future agents can retrieve it.
3. `suggest`: inject as a task hint or dashboard recommendation.
4. `enforce_in_review`: add or strengthen reviewer profile guidance.
5. `enforce_mechanically`: convert to tests, linters, policy helpers, or schema
   checks.
6. `retire`: remove stale feedback after the underlying issue disappears.

This avoids turning a single bad run into permanent prompt clutter.

### 8. Human Safety Boundary

The feedback harness should reduce human burden, not remove human authority:

- Reviewed and autonomous tasks still require the existing human Done gate.
- Feedback Reaper records cannot silently mark tasks done.
- Reaper suggestions that change prompts, docs, tests, or policy should be
  committed through the normal reviewed task flow.
- Low-confidence or product-taste feedback should require explicit human
  approval before becoming a prompt hint.
- Feedback records must avoid secrets and should reference logs/artifacts by
  path with redaction rules.

## Phased Implementation

### Phase 0: Read-Only Feedback Spike

Goal: prove the signal exists without changing task behavior.

- Add `FeedbackRecord` models and persistence helpers.
- Add a CLI or backend service method that reads completed task records and
  writes feedback records.
- Generate feedback only for completed tasks, manually triggered from a dev
  command or admin endpoint.
- Include AI reviewer findings from the task's existing report history.
- No prompt injection yet.

Validation:

- unit tests for record normalization and storage,
- fixture-based test using a sample archived task record,
- manual run on the latest 5 task records to inspect signal quality.

### Phase 1: Automatic Reaper on Task Archive

Goal: make every terminal task produce feedback.

- Call the reaper after `_write_task_record()` when a task is marked Done.
- Add skipped/failed feedback reasons if analysis cannot run.
- Add dashboard-only feedback summaries in Agent Workspace detail or a new
  workspace Feedback tab.
- Track repeated issues by `(kind, file_glob, task_shape, normalized_summary)`.
- Write `lesson-index.json` entries for high-confidence repeated feedback.

Validation:

- backend tests for task Done -> archive -> feedback record creation,
- tests that failures do not block task completion,
- UI smoke check for feedback summaries.

### Phase 2: Prompt Hint Injection

Goal: make feedback improve future runs.

- Add a bounded feedback selector for assignment prompts.
- Inject only high-confidence, recent, task-relevant feedback.
- Add doc-navigation links from `AGENTS.md` / `CLAUDE.md` and the lesson index.
- Include feedback IDs in the assignment prompt and final report validation so
  reviewers can tell whether hints were followed.
- Add a reviewer check: if injected feedback is relevant and ignored, the
  reviewer may flag it as a non-blocking or blocking issue depending on risk.

Validation:

- prompt snapshot tests,
- reviewer prompt tests,
- regression test that user prompt and Goal Packet remain primary.

### Phase 3: Feedback Promotion Workflow

Goal: convert repeated feedback into durable project improvements.

- Add a UI action or reviewed workspace task generator for "promote this
  learning."
- Promotion can open a normal task to update doc navigation, working logs,
  review profiles, tests, or policy.
- Track promoted feedback IDs and retire old hints after the new rule lands.

Validation:

- end-to-end task from feedback candidate -> generated reviewed task -> merged
  doc/policy update,
- tests that retired hints no longer appear in prompts.

### Phase 4: Metrics and Autoregressive Optimization

Goal: use feedback to improve efficiency over time.

- Add workspace metrics:
  - review failure rate by task shape,
  - average task cycle time,
  - idle/no-report gaps,
  - validation missing rate,
  - human blocker frequency,
  - feedback hint hit/miss rate.
- Add a weekly "workspace harness review" task that uses the metrics to propose
  targeted improvements.
- Use feedback to recommend task mode defaults, review profiles, and validation
  checklists for new tasks.

Validation:

- metric aggregation tests,
- manual review of dashboard against archived records,
- compare before/after review-failure and rework rates.

## Minimal First PR

The smallest valuable implementation PR should not change prompts yet. It
should add:

- `FeedbackRecord` schema and normalization,
- feedback storage directory helpers,
- a read-only generator from archived task record to feedback record,
- AI reviewer feedback extraction from existing report records,
- `lesson-index.json` writer with tags and source record IDs,
- tests with a fixture task record,
- one docs update explaining how to interpret records.

This gives Claude Hub a real feedback artifact while keeping runtime behavior
unchanged.

## Open Questions

- Should the reaper be a backend-local summarizer, a managed reviewer-like
  session, or both depending on task risk?
- How much feedback belongs in local `~/.claude_hub` state versus committed
  repository docs?
- What is the retention policy for feedback records that reference local logs
  or artifacts?
- Should feedback be workspace-only, project-path scoped, or branch/worktree
  scoped?
- How should direct human comments be linked to generated feedback records?
- What confidence threshold is high enough for prompt injection?
- Should `AGENTS.md` and `CLAUDE.md` maintain identical navigation sections, or
  should one be canonical and the other reference it?
- What is the right retrieval ranking: tags only, embeddings, simple keyword
  search, or a hybrid?

## Key Pitfalls

- Prompt bloat: feedback must be selected, bounded, and retired.
- Self-confirming loops: a worker should not be the only judge of its own
  feedback; reviewer/evaluator evidence should be preferred.
- Silent policy drift: generated feedback must not become an invisible rule
  without a visible record and promotion path.
- Human gate erosion: feedback can recommend and enforce checks, but final
  acceptance remains human-controlled.
- Low-quality task records: if reports omit validation or acceptance evidence,
  the feedback reaper should report that as the primary learning rather than
  inventing conclusions.
