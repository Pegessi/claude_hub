# claude_hub Prompt Format Reference

> Reference document for how worker and reviewer prompts are assembled across
> all `task_mode × execution_complexity` combinations.
>
> Source of truth: `backend/claude_hub/services/workspace_manager/_prompts.py`.
> Line references in this doc are accurate as of `main@70921a7` plus
> `feat/adhd-skill-analysis-v2@515694c`.

---

## 1. Quick-reference matrix

|                           | `simple` complexity | `complex` complexity | `auto` complexity |
|---------------------------|--------------------|-----------------------|--------------------|
| **`direct` mode**         | single-agent linear execution, no reviewer, no primitives | soft "act as orchestrator", no reviewer, no primitives | self-pick simple/complex, no reviewer, no primitives |
| **`reviewed` mode**       | single-agent + **Goal Packet gate** + independent reviewer | soft "act as orchestrator" + Goal Packet gate + reviewer; **no primitive list, no ledger, no model pinning enforcement** | declare strategy in Goal Packet + gate + reviewer |
| **`autonomous` mode**     | execute directly + **mandatory 1 P-JUDGE pre-flight**; evaluator = reviewer | **orchestrator REQUIRED**; ≥1 P-EXECUTE + ≥1 P-JUDGE; **subagent-ledger mandatory (contract violation if missing)**; evaluator = reviewer | declare simple/complex in `goal_packet.assumptions`; complex contract applies if orchestrator; still 1 P-JUDGE if single-agent |

Reviewer always uses the same adversarial-defect-hunt workflow. The only
reviewer-side variation is:

- `direct`: no reviewer session exists.
- `reviewed`: standard code/design/scope review.
- `autonomous`: reviewer acts as **evaluator**, adds ledger/model/workflow
  verification, and drives iteration toward a numeric pass threshold.

---

## 2. Layering model

Every prompt is assembled by concatenating blocks. Blocks that are always
present (**common blocks**) are listed first; blocks that vary by mode or
complexity (**conditional blocks**) follow.

```
┌─────────────────────────────────────────────────────────┐
│ WORKER SESSION PROMPT (per task assignment)             │
├─────────────────────────────────────────────────────────┤
│ [W1] Session bootstrap      (always, once at session)   │
│ [W2] Task header            (always, per task)          │
│ [W3] Lesson context         (always, may be empty)      │
│ [W4] Complexity block       (SIMPLE / COMPLEX / AUTO)   │
│ [W5] Cost guard             (always, after W4)          │
│ [W6] Subagent capability    (always, per runtime)       │
│ [W7] Model evidence         (always)                    │
│ [W8] Mode block             (direct/reviewed/autonomous)│
│ [W9] Task footer + curl     (always)                    │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ REVIEWER SESSION PROMPT (per review cycle)              │
├─────────────────────────────────────────────────────────┤
│ [R1] Reviewer bootstrap     (always, once at session)   │
│ [R2] Review header          (always, per cycle)         │
│ [R3] Complexity review ctx  (always)                    │
│ [R4] Stored Goal Packet     (always, JSON inline)       │
│ [R5] Autonomous eval block  (autonomous only)           │
│ [R6] Review profiles        (always, JSON + checklist)  │
│ [R7] Repository REVIEW.md   (when present, ≤6 files)    │
│ [R8] Lesson context         (always, may be empty)      │
│ [R9] Review workflow        (Goal Packet gate / full)   │
│ [R10] Report format + curl  (always)                    │
│ [R11] Trigger + recent reps (always, JSON inline)       │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Worker-side block reference

### W1 — Session bootstrap `_build_workspace_agent_prompt` (89–121)

Key invariants told to the agent on session start (before any task arrives):

- You are a **resident** workspace agent. Wait for task assignment; do not
  pre-emptively inspect the repo.
- Check for uncommitted changes before touching files; respect local state.
- An **independent reviewer session** will evaluate your work; do not assume
  your own confidence means acceptance.
- Final reports must include `review_decision ∈ {auto, request, skip}` and
  `review_reason`.
- Reports are submitted via `curl -X POST
  /api/workspaces/sessions/{session_id}/reports`.
- Bilingual `message_en` + `message_zh` required; legacy `message` is a short
  English fallback.

### W2 — Task header `_build_task_assignment_prompt` (255–320)

```
Workspace: {workspace.name}
Task ID: {task.id}
Task title: {task.title}
Task mode: {task.task_mode.value}           # direct | reviewed | autonomous
Task execution complexity: {…}              # simple | auto | complex
Environment: {env block}
State snapshot: {snapshot_path}
Dispatch reason: {…}
[clear_note: prior context is stale — …]     # when resuming after idle
[attachments: …]                             # when attachments present
```

### W3 — Lesson context `_lesson_context_block_from_payload` (301–312)

Zero or more prior lessons retrieved from `FeedbackLessonStore` that match the
task title/prompt/context. May be empty.

### W4 — Complexity block `_execution_complexity_assignment_block` (459–492)

**`simple`:**
```
Execution complexity: simple
Execute directly in this session. Keep your plan compact. Avoid spawning
subagents unless you hit a blocker that genuinely requires a separate
context.
```

**`complex`:**
```
Execution complexity: complex
Act as task orchestrator. Decompose the work, delegate pieces to subagents,
keep ownership of the plan explicit, and personally integrate, validate,
and accept the final result before reporting ready.
```

**`auto`:**
```
Execution complexity: auto
Judge whether this task is simple or complex. State your strategy in your
first (started / working) report. If you judge complex, act as orchestrator
(see complex instructions). If you judge simple, execute directly (see
simple instructions).
```

### W5 — Cost guard (487–492)

Appended **after every complexity variant**, verbatim:

```
Treat orchestrator mode as expensive. Choose it only when at least one of
the following holds: (1) breadth-first parallel exploration across ≥3
threads is needed, (2) the material does not fit one context window, or
(3) clean isolation between sub-steps matters. Otherwise prefer a single
linear agent.
```

### W6 — Subagent capability hint `_subagent_capability_hint` (494–525)

Per runtime. Claude branch (current):

```
You are running on the Claude Code runtime. To spawn subagents, use the
Task tool, e.g.:
  Task(subagent_type="general-purpose", model="opus", description="…",
       prompt="…")
Pass model explicitly: "opus" for synthesis/heaviest reasoning, "sonnet"
for volume work and validation, "haiku" for cheap classification.
```

> **Known gap (A5 from the ADHD analysis):** this block currently does NOT
> mention the native `Workflow` tool (`parallel / pipeline / agent / phase`).
> Once A5 lands, this block will additionally tell orchestrators to prefer
> `Workflow.parallel([...])` for independent fan-out (concurrency ≤ 4) over
> serial `Task` calls.

Cursor/Codex/terminal branches give runtime-native equivalents; terminal
degrades to "no subagents available, work linearly."

### W7 — Model evidence contract `_model_evidence_contract_block` (527–555)

Claude runtime:

```
Record model evidence for every subagent in your subagent ledger:
  P-PLAN / P-EXECUTE / P-JUDGE / P-INTEGRATE → opus
  P-VALIDATE / P-RESEARCH                     → sonnet
For external API calls record model_or_api=external:<api>.
Wrong-tier model on a key primitive is a contract violation.
```

Cursor/Codex get softer fallback language; terminal gets honest-degradation
language (no fabricated subagent claims).

### W8 — Mode block

**`direct`** and **`reviewed`**: `_autonomous_assignment_block` returns `""` —
**no extra text** beyond W1–W7 and W9. The Goal Packet gating sentence in W9
still appears for reviewed.

**`autonomous`** — `_autonomous_assignment_block` (557–580) +
`_orchestrator_contract_block` (582–677):

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Autonomous Mode V1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Max iterations: {policy.max_iterations}
Evaluation strictness: {policy.evaluation_strictness.value}
Allow web research: {policy.allow_web_research}
Require artifact review: {policy.require_artifact_review}
Human checkpoint policy: {policy.human_checkpoint_policy}
Current phase: {run.current_phase if run else 'n/a'}

You are running under autonomous orchestration. The reviewer acts as the
evaluator for each iteration. You will iterate until the evaluator passes
the run or the iteration budget is exhausted.

Orchestrator contract:
  simple → execute directly, but you MUST spawn one P-JUDGE subagent to do
           a pre-flight review before you post your review-gate report.
  complex → orchestrator mode REQUIRED. Your workflow MUST include at
            least one P-EXECUTE and one P-JUDGE. Posting ready_for_review
            without a complete subagent ledger is a CONTRACT VIOLATION.
  auto → declare "orchestrator" or "single-agent" in
         goal_packet.assumptions of your first working report. If
         orchestrator, the complex contract applies. If single-agent, you
         still MUST spawn one P-JUDGE before the review gate.

Role primitives (compose freely — there is no fixed template):
  P-PLAN       high-level planning and decomposition
  P-EXECUTE    implementation subtasks
  P-VALIDATE   independent verification (tests, checks, builds)
  P-JUDGE      critical review of artifacts or plans before handoff
  P-INTEGRATE  merge subtask outputs back into the main workspace state
  P-RESEARCH   information gathering, doc reading, codebase exploration

Model pinning (Claude runtime):
  P-PLAN/P-EXECUTE/P-JUDGE/P-INTEGRATE → opus
  P-VALIDATE/P-RESEARCH                → sonnet

Declare your workflow shape in the first working report using:
  workflow:
    roles:
      - id: planner
        primitive: P-PLAN
        objective: "…"
        success_criteria: "…"
        inputs: ["…"]
        output_schema: "…"
        tools_allowed: [Read, Bash, …]
        context_budget: "medium"
      - id: implementer
        primitive: P-EXECUTE
        …
    deps:
      - ["planner", "implementer"]
      - ["implementer", "tester"]
      …
    notes: "…"

Skeleton example (linear implement):
  planner → implementer → tester → reviewer → integrator

Record each completed subtask in the subagent ledger appended to your
review-gate report's validation field, with:
  - role.id
  - primitive
  - agent (tool/type used)
  - model_or_api
  - decision (1-2 sentence outcome)
  - evidence (path / commit / short summary)
```

> **B2 (ADHD follow-up):** after B2 lands, the primitives list will include
> `P-DIVERGE`, `P-CLUSTER`, `P-DEEPEN`, a second skeleton example for
> divergent ideation will be added (`frame-N branches (parallel, sonnet) →
> cluster → deep(top-K) → judge → integrate`), and the text will explicitly
> point at `Workflow.parallel()` for fan-out.

### W9 — Task footer (320–343)

Always appended:

```
Start by reading the state snapshot at {snapshot_path}. Before substantive
work, derive a Goal Packet capturing objective, acceptance criteria,
validation plan, assumptions, out-of-scope boundaries, editable vs
non-editable areas, and final handoff requirements.

[For reviewed/autonomous tasks:] For this task the first Goal Packet report
is an APPROVAL GATE: stop after submitting it and wait for the reviewer to
approve the packet before beginning implementation.

Report state machine: started → working (→ blocked → working) →
  needs_input OR ready_for_review → completed.
- started: first report, acknowledge receipt.
- working: progress updates.
- blocked: waiting on something external; include what.
- needs_input: need a human decision you cannot safely infer.
- ready_for_review: implementation complete and self-validated; the
  reviewer will take over.
- completed: (post-review) task done.

Every completed / ready_for_review report must include review_decision
(auto | request | skip) and review_reason.

Examples:
  curl -sS -X POST {report_url} -H 'Content-Type: application/json' \
    -d '{{"task_id":"{task.id}","state":"started","message":"…"}}'
  curl -sS -X POST {report_url} -H 'Content-Type: application/json' \
    -d '{{"task_id":"{task.id}","state":"working","goal_packet":{{…}}}}'
```

---

## 4. Worker prompt by cell — concrete shape

### 4.1 `direct / simple`
```
[W1] bootstrap (resident worker, no reviewer mentioned)
[W2] task header (mode=direct, complexity=simple)
[W3] lesson context
[W4] SIMPLE complexity block
[W5] cost guard
[W6] subagent hint (Task tool only; no Workflow pointer pre-A5)
[W7] model contract
[W8] (empty — no mode block for direct)
[W9] footer (NO Goal Packet gate sentence for direct)
```
No reviewer session is ever started. The agent reports `completed` directly;
no `review_decision=request` will trigger a review.

### 4.2 `direct / complex`
Same as 4.1 except W4 = COMPLEX block ("act as orchestrator"). No P-*
primitive list, no ledger schema, no model pinning enforcement. Orchestration
is entirely soft-prompted.

### 4.3 `direct / auto`
Same as 4.1 except W4 = AUTO block (self-pick strategy, announce in first
report). Same lack of hard contract as 4.2.

### 4.4 `reviewed / simple`
```
[W1] bootstrap (mentions independent reviewer)
[W2] task header (mode=reviewed, complexity=simple)
[W3] lesson context
[W4] SIMPLE complexity block
[W5] cost guard
[W6] subagent hint (Task tool only)
[W7] model contract
[W8] (empty — no orchestrator contract for reviewed)
[W9] footer WITH Goal Packet gate sentence
```
Flow: submit Goal Packet → wait reviewer → implement → ready_for_review →
reviewer does full adversarial review → pass/fail/needs_input → completed or
back to W10.

### 4.5 `reviewed / complex`
Same as 4.4 except W4 = COMPLEX block ("act as orchestrator"). **Still no
P-* list, no ledger schema, no model pinning enforcement.** This is the
primary "rigidity" gap: the agent is told to orchestrate but not given a
shape or tooling pointer for non-linear fan-out.

### 4.6 `reviewed / auto`
Same as 4.4 except W4 = AUTO block (declare strategy in Goal Packet
assumptions); same soft contract gap as 4.5.

### 4.7 `autonomous / simple`
```
[W1] bootstrap (mentions independent reviewer = evaluator)
[W2] task header (mode=autonomous, complexity=simple)
[W3] lesson context
[W4] SIMPLE complexity block
[W5] cost guard
[W6] subagent hint
[W7] model contract
[W8] Autonomous V1 header + orchestrator contract (simple enforcement):
       execute directly, but MUST spawn 1 P-JUDGE pre-flight.
     + P-* primitive list
     + workflow: block shape
     + linear skeleton example
     + subtask envelope schema
     + subagent-ledger schema
[W9] footer WITH Goal Packet gate sentence
```

### 4.8 `autonomous / complex`
Same as 4.7 except W4 = COMPLEX block and W8 enforces complex tier:

```
Orchestrator contract (complex):
- orchestrator mode REQUIRED.
- workflow MUST include ≥1 P-EXECUTE and ≥1 P-JUDGE.
- posting ready_for_review without a complete subagent-ledger section in
  validation is a CONTRACT VIOLATION → review_failed.
- P-VALIDATE required when the task has any objectively-checkable success
  criterion.
```

### 4.9 `autonomous / auto`
Same as 4.7 except W4 = AUTO block and W8 enforces auto tier:

```
Orchestrator contract (auto):
- declare "orchestrator" or "single-agent" in goal_packet.assumptions of
  your first working report.
- if orchestrator: complex contract (≥1 P-EXECUTE, ≥1 P-JUDGE, ledger
  mandatory) applies.
- if single-agent: still MUST spawn one P-JUDGE pre-flight before the
  review gate.
```

---

## 5. Reviewer-side block reference

### R1 — Reviewer bootstrap `_build_reviewer_bootstrap_prompt` (141–202)

Told once on session start:

- Your primary job is to **find defects and risks**, not to confirm success.
- **Approval is the exception, not the default.**
- Do not defer to the implementation agent's confidence.
- Post `review_started` immediately; finish with exactly one of
  `review_passed / review_failed / review_needs_input`.
- Keep the message body SHORT (~12 lines). Put evidence in the structured
  fields (`validation`, `risks`, `acceptance_check`, `profile_results`,
  `artifact_refs`), not duplicated in the message.
- Bilingual `message_en` + `message_zh` required.

### R2 — Review header `_build_review_prompt` (845–857)

```
Review workspace task.

Workspace: {workspace.name}
Task ID: {task.id}
Task title: {task.title}
Task mode: {task.task_mode.value}
Task execution complexity: {task.execution_complexity.value}
Implementation agent session: {task.session_id or 'unknown'}
Reviewer session: {reviewer.id}
{environment lines}
State snapshot: {snapshot_path}

Task description:
{task.prompt}
```

### R3 — Complexity review context `_execution_complexity_review_block` (993–1002)

```
Execution complexity review context:
- Selected complexity: {simple|complex|auto}
- Verify the implementation strategy matched the selected complexity.
  - simple: unnecessary delegation and process overhead are scope risks.
  - complex: lack of decomposition, delegated specialist work, or missing
    integrator-level validation can be blocking.
  - auto: verify the agent explicitly chose and followed a simple or
    complex strategy.
```

### R4 — Stored Goal Packet (859–860)

Inline JSON:
```
Stored Goal Packet JSON:
{task.goal_packet.model_dump_json() if task.goal_packet else 'null'}
```

### R5 — Autonomous evaluation context `_autonomous_review_block` (1004–1055)

Empty for direct/reviewed. For autonomous:

```
Autonomous evaluation context:
- Run JSON: {run.model_dump_json() if run else 'null'}
- Worker runtime: {task.agent_type.value}
- Max iterations: {policy.max_iterations}
- Evaluation strictness: {policy.evaluation_strictness.value}
- Require artifact review: {policy.require_artifact_review}

For Autonomous Mode V1 you are the evaluator for this iteration. Score
against the Goal Packet, rubric/run evidence, validation, artifacts, and
prior evaluation history.
  review_passed     → run moves to passed, awaits human acceptance.
  review_failed     → targeted revision possible within budget.
  review_needs_input → product judgment / credentials / unavailable
                        artifact / unsafe scope prevents evaluation.

Subagent ledger verification (orchestrator contract enforcement):
- Complex autonomous tasks MUST embed a `subagent-ledger:` section in the
  review-gate report's validation field. Missing/empty ledger → contract
  violation → review_failed with a blocking issue.
- Each ledger entry needs: role.id, primitive
  (P-PLAN/P-EXECUTE/P-VALIDATE/P-JUDGE/P-INTEGRATE/P-RESEARCH), agent,
  model_or_api, decision, evidence.
{model_verification — per runtime, see below}
- Verify workflow.roles (from the first working report) matches the ledger;
  at least one P-EXECUTE and one P-JUDGE actually ran; P-VALIDATE ran when
  there are objectively-checkable criteria.
```

**Model verification** (runtime-dependent, 1010–1030):
- Claude runtime: P-PLAN/EXECUTE/JUDGE/INTEGRATE → opus; P-VALIDATE/RESEARCH
  → sonnet; external APIs → `external:<api>`. Wrong tier → contract
  violation.
- Cursor/Codex: do NOT fail solely on missing opus/sonnet pinning. Accept
  `runtime-default / unsupported:<reason> / <actual model> / external:<api>`
  with explanation. Missing evidence is a ledger quality issue, not a
  wrong-tier violation.
- Terminal: verify honest degradation (`direct` or `runtime-default`); do
  not accept fabricated subagent/model claims.

### R6 — Review profiles `_review_profile_prompt_block` (711–717)

```
Enabled review profiles JSON:
[general, code, ui, artifact, delivery, boundary, …]   # from _effective_review_profiles

Review profile checklist:
{review_profile_prompt_lines(profiles) — one checklist section per profile}
```

Profile selection merges:
1. `task.review_profiles` (explicit on the task)
2. `trigger_report.review_profiles` (explicit on the trigger report)
3. `policy.review_profiles` (autonomous only, from autonomy policy)

Then `state_policy.infer_review_profiles` adds inferred profiles based on
task title/prompt/changed_files/state/strictness/attachments.

### R7 — Repository review guidance `_review_guidance_block` (719–793)

Walks up the directory tree from each file in `changed_files` (≤12), plus
workspace root, collecting `REVIEW.md` files. Up to 6 files, each truncated
at 4000 chars. Empty if no REVIEW.md found or workspace is remote.

```
Repository review guidance:
### path/to/REVIEW.md
{contents}

### other/path/REVIEW.md
{contents}
```

### R8 — Lesson context
Same block as W3 but keyed off the task+trigger report. May be empty.

### R9 — Review workflow `_review_workflow_block` (908–991)

Two shapes. The Goal Packet approval variant fires when the trigger report
is the first Goal Packet (checked by `_is_goal_packet_approval_review`).

**Goal Packet approval review (913–945):**
```
Goal Packet approval review:
1. Stay read-only. Do not edit files or run formatters that write changes.
2. This is a pre-implementation plan gate. Do not judge implementation
   completeness (there should be none yet).
3. Check whether the stored Goal Packet faithfully preserves the original
   task prompt, attachments, ambiguity, and requested outcome. FAIL if the
   packet narrowed or distorted scope.
4. Verify reviewer-checkable acceptance criteria, validation plan,
   assumptions, out-of-scope boundaries, and final handoff requirements.
   Missing editable/non-editable boundaries or vague validation →
   blocking.
5. Check execution order: implementation must wait for this approval, then
   stay within the approved packet unless a revised packet is submitted.
6. Produce one final verdict.

Acceptance standards:
- Goal fidelity: preserves original prompt, doesn't hide ambiguity.
- Boundary quality: editable areas, non-goals, deps to avoid, rejected
  approaches all explicit.
- Reviewability: acceptance criteria + validation plan concrete enough for
  a later reviewer to check without reconstructing intent.
- Handoff quality: final report requirements include changed files,
  validation evidence, risks, acceptance_check mapping.

Exit criteria:
- review_passed → implementation agent may begin development on the
  approved packet. (Does NOT mean the task is done.)
- review_failed → implementation agent must revise only the Goal Packet
  and resubmit; include Required fixes.
- review_needs_input → packet cannot be judged without user/product
  clarification, credentials, unavailable environment, or another decision
  the implementation agent cannot infer.
```

**Full review (946–991):**
```
Review workflow:
1. Stay read-only. Do not edit files or run formatters that write changes.
2. Check whether the stored Goal Packet faithfully preserves the original
   task prompt. FAIL if the packet narrowed or distorted the requested
   outcome.
3. Derive a task-specific acceptance checklist using all of:
   - task title and description,
   - stored Goal Packet (objective, acceptance criteria, validation plan,
     assumptions, out-of-scope, handoff),
   - explicit user requirements and attachments,
   - changed_files, validation, risks, acceptance_check from
     implementation reports,
   - enabled review profiles, profile-specific evidence, artifact_refs,
     REVIEW.md guidance,
   - repository conventions and nearby behavior,
   - any blocked/needs_input context from the trigger report.
4. Inspect changed files and related code paths enough to verify
   correctness and scope.
5. Adversarial defect hunt (BEFORE deciding the verdict): actively try to
   break the change. Check:
   - edge/boundary inputs, empty/null/large values,
   - error/exception paths, partial failures, retries,
   - concurrency, ordering, shared-state races,
   - regressions to existing flows, persistence, migrations,
   - scope leakage and side effects in untouched areas,
   - security/permission and input-trust assumptions when relevant.
   Anything you cannot rule out by reading the code is a candidate
   defect, NOT "fine".
6. Evaluate validation evidence. Independently spot-check the highest-risk
   claimed checks instead of accepting them at face value. Decide whether
   missing tests/checks are acceptable or blocking.
7. Produce one final verdict.

Acceptance standards:
- Goal fidelity          - Functional correctness
- Scope control          - Integration fit
- Regression safety      - Validation quality
- Handoff quality

Exit criteria:
- review_passed:    actively attempted to break the change (step 5) and
                    found no blocking defect; every acceptance criterion
                    satisfied; validation adequate or gaps explicitly
                    non-blocking; residual risks acceptable for final
                    human acceptance. Do NOT pass on the absence of an
                    attempt or because the implementation report looked
                    confident.
- review_failed:    at least one blocking defect/regression/scope issue/
                    missing required validation fixable by the
                    implementation agent. Include Required fixes.
- review_needs_input: cannot finish without user/product clarification,
                    credentials, unavailable environment, or another
                    decision outside the implementer's control.
```

### R10 — Required final report format (866–905)

```
Required final report format:
Keep the message SHORT and human-scannable (~≤12 lines). Detailed evidence
belongs in structured fields (validation, risks, acceptance_check,
profile_results, artifact_refs), not duplicated in the message.

Message body sections:
  Verdict: review_passed | review_failed | review_needs_input
  Summary: 1–2 sentences on what was actually delivered.
  Acceptance criteria: short rollup, e.g. "3/4 passed (1 partial: …)";
    full per-criterion evidence → acceptance_check field.
  Required fixes: (review_failed only) 1–3 highest-priority concrete fixes.
  Notes: ≤1 line residual risk/gaps/follow-up; deeper detail → risks.

Bilingual reporting:
- Include message_en AND message_zh with the structure above.
- Legacy message field is a short English fallback.
- Acceptance details, validation, profile results, findings, required
  fixes → structured fields (acceptance_check, validation, risks,
  profile_results, artifact_refs).

curl -sS -X POST {reviewer_report_url} …  # review_passed example

Use review_failed when fixes are required. Use review_needs_input only for
genuine blockers outside the implementer's control.
```

### R11 — Trigger + recent reports (885–888)

Inline JSON:
```
Trigger report JSON:
{trigger_report.model_dump_json()}

Recent task reports JSON:
{json.dumps(report_payload, indent=2)}   # last ≤12 reports for the task
```

---

## 6. Continue / retry prompt

When the reviewer returns `review_failed`, the dispatcher sends
`_build_continue_prompt` (1057–1082) back to the worker:

```
Continue workspace task from review.

Task ID: {task.id}
Task title: {task.title}
Follow-up instructions:
{reviewer's Required fixes verbatim + optional attachment block}

{_autonomous_continue_orchestrator_reminder}

The task is back in working state. Report progress with the same task_id.

{report endpoint curl}
```

`_autonomous_continue_orchestrator_reminder` (1083–1092) — only for autonomous
tasks:

```
Orchestrator-mode reminder: if you ran in orchestrator mode for this task,
stay in orchestrator mode for this revision. Address the evaluator's
blocking issues by dispatching new sub-agent subtasks (P-EXECUTE for fixes,
P-VALIDATE for re-tests, P-JUDGE for re-review) rather than folding the
work into your own context. Append the new ledger entries to your existing
subagent ledger; do not restart it.
```

---

## 7. Reviewer prompt by cell — concrete shape

### 7.1 `direct / *`
No reviewer session is started. Reports progress directly from worker to
`completed`.

### 7.2 `reviewed / *`
```
[R1] reviewer bootstrap (independent critic mindset)
[R2] review header
[R3] complexity review context
[R4] stored Goal Packet JSON
[R5] (empty — no autonomous block)
[R6] review profiles (general/code/ui/artifact/delivery/boundary, inferred)
[R7] REVIEW.md guidance (if present)
[R8] lesson context
[R9] review workflow (Goal Packet approval OR full adversarial review)
[R10] report format + curl
[R11] trigger report JSON + recent task reports JSON
```

Two review cycles per task in the normal path:
1. **Goal Packet review** (R9 = approval shape) before implementation.
2. **Full review** (R9 = full adversarial shape) after ready_for_review.

More cycles possible if review_failed (continue prompt → fix → another full
review).

### 7.3 `autonomous / *`
```
[R1] reviewer bootstrap (acts as evaluator)
[R2] review header
[R3] complexity review context
[R4] stored Goal Packet JSON
[R5] Autonomous evaluation context (evaluator role + ledger/model/workflow
     verification, runtime-dependent model strictness)
[R6] review profiles (same as reviewed + possible policy.review_profiles;
     B3 will add 'ideation')
[R7] REVIEW.md guidance (if present)
[R8] lesson context
[R9] review workflow (Goal Packet approval OR full review; on full review
     the exit criteria effectively include the evaluation pass threshold
     of 0.8 per schemas.EvaluationReport)
[R10] report format + curl
[R11] trigger report JSON + recent task reports JSON
```

Iteration count is bounded by `policy.max_iterations`; each full review is
an evaluation cycle. `review_passed` moves the run to passed awaiting human
acceptance; `review_failed` loops back via W10 (continue prompt +
orchestrator-remainder reminder); `review_needs_input` pauses for human.

---

## 8. Key schemas referenced in prompts

All from `backend/claude_hub/models/schemas.py`.

- `WorkspaceTaskMode` (52): `direct | reviewed | autonomous`
- `WorkspaceTaskExecutionComplexity` (in task payload): `simple | auto | complex`
- `AgentType`: `claude | cursor | codex | terminal`
- `ReviewProfile` (181): `general | code | ui | artifact | delivery | boundary`
  (B3 adds `ideation`)
- `RubricCriterion` (267), `CriterionResult` (279), `EvaluationReport` (289)
  with `pass_threshold = 0.8` (335)
- `AutonomyPolicy` fields: `max_iterations`, `evaluation_strictness`,
  `allow_web_research`, `require_artifact_review`, `human_checkpoint_policy`,
  `review_profiles`
- Report `state` enum: `started | working | blocked | needs_input |
  ready_for_review | completed | review_started | review_passed |
  review_failed | review_needs_input`
- `review_decision` enum: `auto | request | skip`
- Subagent ledger entry (inlined into `validation` string):
  `role.id`, `primitive`, `agent`, `model_or_api`, `decision`, `evidence`

---

## 9. Three rigidity gaps this doc surfaces

Mapping the assembly rules above to concrete pain points:

1. **W6 omits the `Workflow` tool.** The Claude subagent hint only documents
   `Task(...)`. The native `Workflow` tool (`parallel / pipeline / agent /
   phase`) that implements fan-out without hand-rolled Promise management is
   never mentioned, so agents default to linear/serial orchestration or
   avoid orchestration entirely. A5 fixes this with a ~100-token addition.
2. **P-* primitives are execution-only; the only skeleton example is linear.**
   P-DIVERGE / P-CLUSTER / P-DEEPEN are missing, and the only example is the
   planner→implementer→tester→reviewer→integrator chain. The "compose
   freely" disclaimer at line 674 is too weak against the exemplar. B2
   adds creative primitives and a divergent-ideation example that uses
   `Workflow.parallel`.
3. **reviewed/complex has no orchestration contract.** Only autonomous mode
   gets the P-* list, workflow block, subtask envelope, ledger schema, and
   model pinning. reviewed/complex relies entirely on the soft "act as
   orchestrator" line in W4 and never sees a primitive list, ledger
   requirement, or model pinning rule, so it effectively degenerates to
   single-agent + reviewer review. B1/B2 together will extend a lighter
   contract (at least workflow: block + ledger evidence) to reviewed/complex.
