# ADHD Skill Analysis — v2 (deep code read, concrete absorption plan)

> Task: `31ba5786-310b-48a8-b0f8-ee3d5c383de0` · "adhd skill"
> Author: cb-agent-1 (Claude) · 2026-06-26
> Status: Analysis / recommendation only — **no source-code behavior change in this PR**
> Source: https://github.com/UditAkhourii/adhd @ 9ef4b9a (MIT licensed)
> Supersedes: draft `docs/working-logs/2026-06-25-adhd-project-analysis.md` on the
>   unmerged `docs/adhd-analysis` branch (that branch is not merged; this is the
>   authoritative version with file:line references into claude_hub).
> Related docs: `ARCHITECTURE.md`,
>   `docs/working-logs/2026-06-01-auto-mode-cli-subagent-orchestration.md`,
>   `docs/working-logs/2026-05-26-review-profiles-v1.md`,
>   `docs/working-logs/2026-05-26-autonomous-mode-v1.md`,
>   `docs/working-logs/2026-06-06-feedback-harness-plan.md`,
>   `docs/working-logs/2026-06-15-claude-hub-cli.md`.

---

## 1. Executive summary (TL;DR)

ADHD is a small, well-engineered TypeScript library + Claude Code skill that
attacks one failure mode: **premature convergence in autoregressive reasoning**
— a single LLM trajectory anchors on whatever it says first, and even in-context
Tree-of-Thought leaks that anchor across branches because they share one
context. ADHD's answer is architectural, not prompt-engineering: **spawn N
isolated stateless LLM calls under deliberately distorted "cognitive frames"
with the critic disabled, then run a single separate critic pass to score,
cluster, prune traps, and deepen the survivors.**

The strongest finding for claude_hub: **we already own the two load-bearing
pieces** — (a) an orchestrator that can fan out parallel sub-agents and (b) a
fully independent reviewer session acting as a mechanical critic. ADHD is
external validation that our worker-vs-reviewer split is correct and worth
keeping strict. What we do *not* have is the *ideation orchestration shape*
that sits on top: deliberate frame-driven divergence, isolation-as-feature,
scoring rubric, clustering, the `nonObviousPick` heuristic, the pre-flight cost
gate, and a lessons system that captures *successful creative patterns* rather
than only avoidance patterns.

**Top 5 adoptable ideas, ordered by ROI:**

1. **Cognitive frames as a reusable prompt asset** — lift a curated ~8-frame
   subset of ADHD's 15 frames as a static reference our orchestrator/reviewer
   can inject when a task is open-ended. Zero runtime cost, pure prompt text,
   MIT-friendly with attribution.
2. **Pre-flight cost gate** before fanning out — extend the existing
   "judge complexity first" instruction in the orchestrator contract with
   ADHD's sharper three-question check (open-ended? high-stakes? open-phrased?).
3. **A new `P-DIVERGE` workflow primitive + an `IDEATION` review profile** so
   open-ended design/naming/architecture tasks get the divergent-then-convergent
   treatment instead of being forced into P-PLAN→P-EXECUTE→P-VALIDATE.
4. **Trap-detection rubric extension for reviewers** — our reviewer already
   hunts defect-traps (edge cases, races, security); add "cognitive traps"
   (premature convergence, echo-chamber ideas, missing contrarian frames).
5. **Creative-strategy lessons** — extend the feedback harness to capture
   what *worked* (which frames/analogies unlocked the problem) rather than only
   what broke.

Section 4 gives concrete files/line numbers for each. Section 5 lists skips.

---

## 2. What ADHD actually is (mechanics, not marketing)

ADHD ships three artifacts:

1. **`skills/adhd/SKILL.md`** — a Claude-Code / Codex / Cursor skill. ~200 lines
   of markdown with YAML frontmatter. When `/adhd <problem>` is invoked (or the
   skill auto-triggers on ideation intents), it instructs the agent to: run a
   pre-flight gate; if it passes, spawn 5 parallel Agent/Task tool calls with
   distinct cognitive frames and a "do not evaluate" system prompt; then run
   critic passes (score/cluster/deepen) and render structured output.
2. **`adhd-agent` npm package** (`src/*.ts`, ~500 LOC total) — a programmatic
   implementation of the same loop. Not a framework; just a `run(opts)` function
   with `onEvent` streaming, plus a terminal renderer.
3. **Docs + evals** — `documentation/` explains the two-phase model;
   `bench/` contains 6 problems, an LLM judge, and results.

### 2.1 The loop (from `src/engine.ts`)

**Phase 1 — Diverge** (`engine.ts:61-101`, framed via `DIVERGE_SYSTEM` at
`engine.ts:28-36`):

- Pick frames via `selectFrames(n, codeMode)` (`frames.ts:126-137`). The
  selection logic is small but worth stealing: when `codeMode=true`, restrict
  the pool to frames tagged `code`/`design`, shuffle, take `n-1`, then always
  append exactly one random `wild` frame (10-year-old, biology, speedrunner,
  ant-colony, markets, infinite-budget, hardware, remove-assumption — the
  absurdity engines). Guarantees breadth *and* at least one wildcard without
  manual tuning.
- Spawn N parallel LLM calls via `Promise.all` + a `p-limit(n)` semaphore
  (`concurrency` default 4). Each call gets: system prompt that *forbids*
  evaluation/ranking/hedging, forces JSON array output, bans the first three
  obvious answers; user prompt = problem + optional context + one frame's
  vantage prompt.
- **Isolation invariant** (`SKILL.md:77-80`, `how-it-works.md:16`): branches
  never see each other's output. This is the load-bearing difference from
  in-context ToT. ADHD is emphatic that serializing branches in one context
  "is not ADHD; it is a wider single thought."

**Phase 2 — Focus** (all in `engine.ts`), runs after all branches return:

1. **Score** (`scoreIdeas`, `engine.ts:103-147`) — one LLM call, every idea on
   `novelty` (0-10, distance from obvious), `viability` (0-10, could ship),
   `fit` (0-10, addresses problem), plus optional `trap: "reason"` field.
   Weighted total = `0.35*novelty + 0.40*viability + 0.25*fit`
   (`engine.ts:137`). The weighting is deliberate: viability is the gatekeeper
   (brilliant unshippable = trap), novelty is the whole point of running ADHD,
   fit is the weakest weight because a novel adjacent idea can still unlock.
2. **Cluster** (`clusterIdeas`, `engine.ts:149-175`) — one LLM call that groups
   ideas into 3-6 clusters by *underlying angle*, not by surface keywords (e.g.
   "remove-the-server plays", "cache-shaped plays"). This is an unusually sharp
   instruction — most "group these" prompts produce synonym clusters.
3. **Rank + prune** (`engine.ts:276-290`): filter out traps, sort by weighted
   total, take shortlist of 2-4; pick `nonObviousPick` = highest
   `novelty + viability*0.5` in the shortlist (not the highest overall score —
   a deliberate bias toward surprise).
4. **Deepen top-K** (`deepenIdea`, `engine.ts:177-229`, default K=3, parallel
   again under the same semaphore). Each gets: the idea, sibling ideas for
   recombination, system prompt "connect dots"; returns sketch (4-8 sentences:
   how it works + load-bearing risk + first concrete step) + 3-5 child ideas
   (variations, hybrids, unlocks).
5. **Provocation** (`engine.ts:307-312`) — cheap, no extra LLM call: take the
   highest-novelty idea in the whole pool (traps included) and rephrase as
   "What if we took this seriously: …" Opens a door for the user without
   spending more tokens.

### 2.2 Properties worth noticing

- **Generator/critic split is mechanical, not rhetorical.** Different LLM
  calls, opposite system prompts. One says "don't evaluate"; the other says
  "evaluate only." This is the central architectural claim, made against
  in-context ToT and persona-flipping prompts.
- **Cost is linear, not quadratic.** `O(N × per_branch)` — no branch's output
  is fed into another during divergence.
- **Pre-flight gate** (`SKILL.md:15-45`): explicit invocation (e.g. `/adhd`)
  skips the gate; otherwise three self-judge questions must ALL be yes:
  open-ended? (not one canonical answer); high-stakes? (cost of obvious answer
  being wrong is actually high — fuzzy bugs, schema design, naming a real
  product = yes; "side project at 11pm" = no); open phrasing? (if user said
  "quick / standard / canonical / just / one-line", abort and answer directly).
  If any fails: answer normally, optionally advertise `/adhd` in one line.
- **Scoring axes are intentionally few and crisp.** Novelty / viability / fit.
  No "eloquence", no "completeness", no safety double-count.
- **The non-obvious pick is a separate signal from the top-scoring pick.** The
  highest weighted-total idea is usually the sensible hybrid; the non-obvious
  pick is the highest `(novelty + 0.5*viability)` idea — surfaces surprise
  without requiring it to beat the safe pick.
- **OnEvent streaming** (`types.ts:60-67`): fine-grained events
  (`frame:start`, `frame:done`, `score:done`, `cluster:done`, `deepen:start`,
  `deepen:done`, `warn`) let a UI render progress without polluting the final
  output. A small thing but valuable for an ~10-call run that can take 30-90s.
- **Renderer discipline** (`render.ts`): the output is *not* a wall of prose.
  It uses: score chips `[N7 V8 F9]` (dim ANSI), a green ★ for the non-obvious
  pick, red ⚠ for traps, indentation by cluster, bold section headers.
  "Structure is half the value" (`SKILL.md:170`).
- **Packaging discipline** (`SKILL.md:1-5`): the `description:` in YAML
  frontmatter is a single line ≤600 chars because "some Codex builds truncate
  or reject multi-line YAML block descriptions." This is a concrete
  portability lesson for any SKILL.md we author.
- **15 frames, tagged** (`frames.ts`; `SKILL.md:118-134`). Each frame has an
  id, label, prompt, and tags `("code"|"design"|"general"|"wild")[]`. Tagging
  drives the `selectFrames` codeMode bias.
- **Anti-patterns are documented** (`SKILL.md:160-177`): convergence-disguised-
  as-divergence (10 minor variations of one idea), weird-for-weird's-sake
  without convergence, walls of equally-weighted prose, refusing to commit
  after diverging, skipping the isolation invariant. This reads like a list of
  lessons learned from dogfooding; high signal.

### 2.3 What ADHD is not

- It is not a research breakthrough with validated science behind it. The
  eval is LLM-judged on 6 problems by the project's own harness; headline
  numbers (trap detection 9.5 vs 1.8; novelty 7.8 vs 2.7) are consistent with
  the method but not independently reproduced. We should treat the mechanism as
  well-crafted engineering, not as a proven result.
- It is not a multi-agent framework. The entire library is ~500 TS LOC plus
  one SKILL.md. It is a **control structure** on top of ordinary LLM calls —
  closer to a design pattern than a product.
- It does not do recursive tree search (despite the ToT comparison). It is a
  flat fan-out → converge → deepen-K; the deepening step produces child ideas
  but does not recursively score them. This is a deliberate "two walls"
  simplicity choice.

---

## 3. Where claude_hub already matches (and does not)

A prior exploration pass read the relevant code paths. Findings, with file:line
references:

| ADHD mechanism | claude_hub component | Status |
|---|---|---|
| Mechanical generator/critic split (separate sessions, opposite stances) | Worker (orchestrator) session + independent `cb-reviewer-*` ManagedSession; reviewer bootstrap is explicitly adversarial ("approval is the exception, not the default", `_prompts.py:153-163`) | ✅ **Already have it.** ADHD is external validation; keep it strict. |
| Parallel fan-out of sub-agents | Orchestrator can spawn multiple P-EXECUTE sub-agents via the CLI runtime's native Task tool; orchestrator contract lists "breadth-first parallel across ≥3 independent threads" as an orchestrator-mode trigger (`_prompts.py:481-486`). V1 contract recommends serial P-EXECUTE (`2026-06-01` log Q4); parallel is deferred to the V2 team design (`docs/working-logs/2026-05-27-auto-mode-team-design.md`). | 🟡 Partial — possible at the sub-agent layer, not at the Hub-session layer, and not prompted/structured for ideation. |
| Isolation between branches | Native to the Task tool (each sub-agent call is a fresh context). | ✅ Already the default; our prompts just don't *name* isolation as a deliberate feature or forbid cross-branch reading. |
| A structured critic pass with numeric rubric | `EvaluationReport` + `RubricCriterion` + `CriterionResult` (`schemas.py:267-308`); reviewer produces numeric scores per criterion and an `overall_score` (pass threshold 0.8, `schemas.py:335`). | 🟡 Structure exists but all 7 shipping rubric axes are code-correctness / scope / integration / regression / validation / handoff focused (`_prompts.py:974-981`). No novelty/diversity/trap-detection axes. |
| A convergence / integrate step | `P-INTEGRATE` primitive (`_prompts.py:623`) — "combine partial outputs into the final deliverable." | ✅ Primitive exists, but tuned for code-artifact merge, not idea clustering. |
| A judge / evaluate step | `P-JUDGE` primitive + the external reviewer. | 🟡 Tuned for defect finding; no cognitive-trap lens. |
| Cluster by underlying angle | — | ❌ No equivalent. |
| Deepen top-K with risk + first step + child ideas | `review_failed → _build_continue_prompt` (`_prompts.py:1057-1092`) returns the task to the same worker to fix defects. | 🟡 Loop exists, but is a fix-defects loop, not an explore/expand loop. |
| Non-obvious-pick heuristic | — | ❌ No equivalent. |
| Trap detection (cognitive: premature convergence, anchoring, echo chambers, missed frames) | Adversarial defect hunt (`_prompts.py:961-969`) covers edge cases, error paths, races, regressions, scope leaks, security/permission assumptions. | 🟡 Defect-traps are covered, cognitive-traps are not. |
| Pre-flight cost gate | The orchestrator contract already says "before implementation, judge whether this task is simple or complex. State the chosen execution strategy in your first working report." | 🟡 We have a binary simple/complex gate; ADHD's three-question gate is sharper (open-ended AND high-stakes AND open-phrased) and includes a one-line "don't use this" advertisement when it refuses. |
| A reusable frame library | — | ❌ No equivalent. |
| Fine-grained progress events during a multi-step fan-out | Backend reports (state: started/working/blocked/ready_for_review/completed) are coarse (task-level only); worker sub-agent fan-out does not emit frame-level events to the frontend. | ❌ Sub-agent progress is invisible to the task UI today. |
| Structured output rendering (score chips, labeled clusters, ★ non-obvious pick, ⚠ traps, provocation) | Frontend renders reports as free-form markdown; Feishu card rendering (`backend/claude_hub/cli/feishu_cards.py`) is structured but does not have ideation-specific blocks. | ❌ No ideation-shaped rendering. |
| A skill system (SKILL.md) that end-users can invoke | The only SKILL.md in the repo (`backend/claude_hub/cli/SKILL.md`) teaches an external Feishu-bot agent how to drive the `claude-hub` CLI — it's a consumer skill, not a plugin registry. Hub itself has no extensible skill/agent registry. | ❌ Hub does not ship user-invocable skills; skills are left to the CLI runtime (Claude Code `.claude/agents/`, Cursor sub-agents). |
| Lessons that capture successful creative patterns | `FeedbackLessonStore` (`services/feedback_lessons.py`) captures avoidance patterns from failures only. Lesson creation requires an iteration-signal (≥1 review_failed OR ≥2 needs_input). `learnings.kind` enum in the feedback harness plan (`2026-06-06`, line 165) is `prompt|review_profile|validation|tooling|docs|architecture` — all quality/process, no creative-pattern kind. | ❌ Only bug/avoidance patterns today; success patterns are invisible to the store. |
| /adhd or `task create --mode ideate` entry point | CLI (`cli/main.py:80-96`) has 6 command groups: workspace/agent/task/session/lessons/feishu. `task create` supports `--task-mode [direct|reviewed|autonomous]` and `--review-profile [general|code|ui|artifact|delivery|boundary]`. | ❌ No ideate entry point. |
| Concurrency semaphore for parallel fan-out | Native `Promise.all` exists in JS; in Python we have `asyncio.gather` / `asyncio.Semaphore` but no orchestrator-level concurrency cap policy for sub-agents. | 🟡 Inference is concurrent via the runtime; there is no explicit `concurrency=N` budget surfaced. |

The most architecturally significant alignment: **we already built the load-bearing
hard part** (the mechanical generator/critic split). ADHD is a
control-structure that sits on top of that split for a *specific task shape*
(open-ended ideation), which is exactly what we are missing.

---

## 4. Recommendations — adopt / adapt / skip

Concrete file:line targets are given for each "adapt" item so a follow-up task
can pick up work without another research pass.

### 4.1 Adopt — cheap, zero-behavior-change, do these in any docs pass

These are ideas we can absorb into docs, prompts, and conventions without
touching runtime behavior.

**A1. Cognitive frames as a reusable prompt asset (subset, not verbatim).**

Create a small reference file, e.g.
`backend/claude_hub/services/workspace_manager/_ideation_frames.py` (or just a
section in `_prompts.py`) holding a curated ~8-frame subset biased to our
domain (agent tooling, infra, UX). Recommended starters:

- **3am on-call** (what design lets me not get paged)
- **Remove the load-bearing assumption** (name the fixed thing, delete it)
- **Regulator / auditor** (what must be provable, traceable, refusable)
- **Competitor trying to break it** (exploit the obvious solution, invert)
- **Inversion** (guarantee NOT X, negate back)
- **$0 budget, 1 hour** (crudest version that still works)
- **Speedrunner** (glitches, skips, frame-perfect shortcuts)
- One rotating wild frame (biology, hardware-engineer, 10-year-old,
  ant-colony — pick per-run)

Markets, game design, infinite-budget, logistics are lower-priority for our
infra/agent domain; they should remain available for product/strategy tasks
but not in the default set. Attribute ADHD (MIT) in a comment.

This asset is useful immediately for (a) the orchestrator when it encounters
an open-ended design task and chooses to fan out, (b) the reviewer when
checking for premature convergence ("did the worker consider at least one
contrarian frame?"), and (c) any `/diverge` skill we ship.

**A2. Codex/Cursor SKILL.md portability rule.**

Add a one-line note wherever we document authoring SKILL.md files (future
docs/skill-authoring.md if we create one; otherwise in `backend/claude_hub/cli/SKILL.md`
as a comment): *"The frontmatter `description:` must be a single line ≤600 chars
because some Codex/Cursor builds truncate or reject multi-line YAML block
descriptions."* ADHD learned this the hard way (`SKILL.md:130`); no reason we
should rediscover it.

**A3. Document our mechanical critic split as an explicit architectural choice.**

In `ARCHITECTURE.md` (review-loop section) and in `_prompts.py` near the
reviewer bootstrap block (`~line 141`), add one sentence citing ADHD as
external validation of the separate-session critic: *"The independent reviewer
session is not just a quality gate; it prevents the in-context generator from
anchoring on its own early ideas. See also the ADHD skill (UditAkhourii/adhd,
MIT) which makes the same architectural argument for parallel-divergent
ideation."* Costs nothing; reinforces why we resist "self-review in one
context" shortcuts.

**A4. Add "cognitive traps" to the reviewer defect-hunt checklist.**

Extend the adversarial defect-hunt list at `_prompts.py:961-969` by two items:

> - Premature convergence / anchoring: did the worker commit to the first
>   obvious approach before considering alternatives? For open-ended tasks,
>   require at least one contrarian framing.
> - Echo chamber: if sub-agents were used, did they independently reach
>   distinct angles, or do they all decorate the same assumption?

This is a prompt-only change, no schema change. It gives reviewers language to
reject a premature-convergence answer on *ideation-shaped* tasks without
adding new rubric structure. Useful even before we add a formal IDEATION
profile.

### 4.2 Adapt — concrete follow-up work, each worthy of its own reviewed task

These are behavioral changes; they should NOT be done in this task (which is
analysis-only) but the file/line targets below let a future task skip this
research step.

**B1. Pre-flight gate as an extension to the orchestrator "simple vs complex"
judgment. Effort: ~XS, prompt change only.**

File: `backend/claude_hub/services/workspace_manager/_prompts.py`, the
"choose execution strategy" section (near line 481 where orchestrator mode is
triggered). Replace the binary simple/complex prompt with the three-question
gate plus an advertising tail:

> Before choosing orchestrator mode for a task, ask three questions. If ALL
> three are yes, the task is a candidate for divergent ideation (P-DIVERGE);
> if any is no, answer linearly and do not pay the fan-out cost:
> 1. **Open-ended?** Would an experienced engineer give multiple viable
>    answers, or is there one canonical answer?
> 2. **High-stakes?** Is the cost of the obvious answer being wrong
>    meaningfully high (architecture, public API, naming a user-visible
>    feature, fuzzy bug with no known root cause)?
> 3. **Open phrasing?** Did the user avoid words like "quick", "standard",
>    "canonical", "just", "one-line"?
> If any fails, proceed in direct/reviewed mode as usual; optionally append
> one line to the first report: "If you want a wider exploration under
> parallel frames with trap detection, mark the task mode as ideation or
> rephrase as a design question."

This is a tiny prompt edit. It (a) prevents paying ADHD cost on trivia and
(b) surfaces the capability to users.

**B2. Add `P-DIVERGE` / `P-CLUSTER` / `P-DEEPEN` primitives to the orchestrator
contract. Effort: ~S, prompt + enums.**

Files:
- `backend/claude_hub/services/workspace_manager/_prompts.py` lines 618-624
  (the primitives list). Add P-DIVERGE (fan out N parallel ideation
  sub-agents under distinct frames from the frame asset, critic disabled by
  instruction), P-CLUSTER (synthesis step — group ideas by underlying angle
  and surface shape), P-DEEPEN (take top-K clustered ideas, produce sketch +
  risk + first step + child ideas).
- Same file, `_model_evidence_contract_block` (~lines 527-555): pin models per
  new primitive. Suggest: Sonnet for P-DIVERGE branches (volume job), Opus
  for P-CLUSTER and P-DEEPEN (synthesis/focus job).
- Same file, cost-guard (~lines 481-486): allow parallel P-DIVERGE fan-out
  (V1 currently serializes P-EXECUTE; P-DIVERGE is explicitly parallelizable
  because branches must be isolated). Cap concurrency to N (default 4,
  mirroring ADHD's `p-limit(4)`) to avoid runaway sub-agent creation.
- Model-pinning precedent: existing primitives are already pinned (Opus for
  PLAN/EXECUTE/JUDGE/INTEGRATE, Sonnet for VALIDATE/RESEARCH).

P-INTEGRATE already covers converge; P-JUDGE already covers scoring. The three
new primitives let an orchestrator compose an explicit
DIVERGE→CLUSTER→JUDGE(score/trap)→DEEPEN→INTEGRATE workflow for open-ended
tasks without inventing a new execution engine.

**B3. Add an `IDEATION` review profile. Effort: ~S, schema + policy + prompt.**

Files:
- `backend/claude_hub/models/schemas.py` ~line 181 (`ReviewProfile` enum):
  add `ideation`.
- `backend/claude_hub/services/workspace_state_policy.py` ~line 110
  (`REVIEW_PROFILE_GUIDANCE`): add guidance text that asks the reviewer to
  check for: breadth of angles, presence of at least one contrarian/wild
  frame, explicit trap list with mechanistic reasons, a stated position
  (not "here are 20 ideas you decide"), absence of premature convergence.
- Same file, `infer_review_profiles()` ~line 146: trigger on keywords
  ("brainstorm", "ideate", "design", "name", "compare options", "architecture
  for", "strategy") or when the worker's workflow declaration contains
  P-DIVERGE.
- `_prompts.py` ~lines 974-981 (rubric axes): add novelty, diversity, and
  trap-detection axes when the ideation profile is active, with a numeric
  score expectation (mirroring ADHD's 0-10 scoring but reusing our existing
  CriterionResult schema).

The existing pass/fail rubric is not well-matched to creative output; an
ideation profile lets us keep the current profiles strict for code and add a
creativity-appropriate rubric only for design/ideation tasks.

**B4. Creative-strategy lessons. Effort: ~M, feedback harness + schema.**

Files:
- `backend/claude_hub/services/feedback_lessons.py`: allow lesson creation
  on a positive signal (human-accepted with "this approach was
  novel/surprising" signal, OR ideation rubric scoring ≥8 on novelty) in
  addition to the existing review_failed/needs_input signal.
- Feedback harness plan (`docs/working-logs/2026-06-06-feedback-harness-plan.md`)
  `learnings.kind` enum (~line 165): add `ideation_strategy` kind.
- Lesson payload: add optional `frame_used`, `analogy_domain`,
  `novelty_score` fields. Tags already support free-form entries; add
  `divergence`, `analogy`, `reframing`, `cluster-quality` to the suggested
  tags.
- `_prompts.py` ~lines 340-361 (`_lesson_context_payload`): when injecting
  relevant lessons into an assignment or review, also inject past
  ideation_strategy lessons if the task looks open-ended.

Why this matters: our lessons system is currently a "never make this mistake
again" memory. ADHD's frames and analogies are the opposite kind of knowledge
— "this reframing unlocked the problem once, it might again." Both kinds
compound.

**B5. (Optional, ~M) A `/adhd` or `claude-hub ideate` CLI skill. Effort: ~M, CLI
+ Feishu card + task mode.**

Two viable paths; pick one:

- **Skill-only path (simpler, no Hub changes):** install the upstream
  `skills/adhd/SKILL.md` into the Claude Code environment that Hub worker
  agents run in (e.g. vendor into `.claude/skills/adhd/SKILL.md` on agent
  bootstrap, or document `npx skills add UditAkhourii/adhd` as an
  environment-setup step). Zero Hub code. Worker agents can then invoke
  the skill as part of any task when the pre-flight gate fires.
- **First-class Hub path:** add `WorkspaceTaskMode.IDEATE` alongside
  `direct/reviewed/autonomous` in schemas.py, with a default orchestrator
  contract that pre-populates the workflow:
  `P-DIVERGE → P-CLUSTER → P-JUDGE → P-DEEPEN → P-INTEGRATE`, routes to the
  `ideation` review profile, and renders a structured Feishu card (clusters
  as labeled groups, ★ non-obvious pick as highlighted, ⚠ traps as a
  red-tinged group, score chips as small dim labels). Add `claude-hub task
  create --mode ideate` to expose it.

The skill-only path is a day of work; the first-class Hub path is more work
but gives us UI, audit trail, Feishu rendering, and lesson capture.
Recommendation: do the skill-only path first (B5a, ~XS), and consider the
first-class path only after we have real usage showing people want it.

**B6. (Optional, ~S) Fine-grained progress events during multi-call runs.**

Today, a multi-step agent run (whether P-DIVERGE or other long workflows)
emits only task-level state changes; sub-agent progress is invisible. ADHD's
`onEvent` (`types.ts:60-67`) is a good model: frame:start/frame:done per
branch, score:done, cluster:done, deepen:start/deepen:done. If we implement
B2, it is worth adding a small event channel so the frontend can show
"diverging (2/5 frames complete) … scoring … clustering … focusing … done"
rather than a 30-90s silent spinner. Files to touch:
`backend/claude_hub/api/sessions.py` (websocket events),
`frontend/src/stores/workspaceStore.ts`,
`frontend/src/components/tasks/` (progress indicator).
Low priority for a v1; ergonomics only.

### 4.3 Skip — not worth absorbing

- **`adhd-agent` npm package as a runtime dependency.** Our orchestration is
  prompt-layer + Python + native sub-agent calls, not TypeScript + Agent SDK
  `query()`. Adding a Node dep for a pattern that fits in ~200 lines of
  Python prompt + primitives adds surface area with no upside.
- **Reproducing ADHD's eval harness.** Their EVALS are LLM-judged on 6
  problems and exist to sell the method; our quality bar is human-acceptance
  after reviewer pass, which is a different gate.
- **Importing all 15 frames verbatim.** Several (markets, ant-colony, game
  design, infinite-budget) rarely fit infra/agent-tooling work and would be
  noise in the default set. Keep them available in the extended frame library
  for product/strategy tasks, but curate the default.
- **Recursive tree-of-thought (deepening beyond one level).** ADHD itself
  doesn't do this (it has exactly one deepen step producing child ideas but
  doesn't score them recursively). The complexity/cost tradeoff is bad for
  our domain; if the first DEEPEN pass doesn't land, it's cheaper to re-run
  with different frames than to recurse.
- **Adopting the renderer verbatim (ANSI codes).** Our terminals already
  support markdown and the Feishu card system is a different medium; adopt
  the *structure* (clusters, score chips, ★, ⚠, provocation), not the escape
  sequences.
- **Built-in skill registry in the Hub.** B5 skill-only path is sufficient; a
  Hub-managed plugin registry is a much larger investment (vetting, version
  pinning, permission sandboxing) that should be driven by a concrete need,
  not by one project's SKILL.md.

---

## 5. Prior v1 analysis deltas

The draft `2026-06-25-adhd-project-analysis.md` on the `docs/adhd-analysis`
branch captured the high-level thesis correctly. This v2 adds:

- File:line references into both ADHD (`src/engine.ts`, `src/frames.ts`,
  `src/render.ts`, `src/types.ts`, `skills/adhd/SKILL.md`,
  `documentation/how-it-works.md`) and claude_hub (`_prompts.py` at specific
  line neighborhoods, `schemas.py`, `workspace_state_policy.py`, CLI
  `main.py`, `feedback_lessons.py`, lesson injection in `_prompts.py`).
- Concrete weighted-scoring formula (`0.35*N + 0.40*V + 0.25*F`) and
  `nonObviousPick` heuristic (`N + 0.5*V`), both worth reusing as-is.
- The `selectFrames` logic (codeMode bias + always-1-wild) as a small
  algorithm worth lifting directly.
- The provocation wildcard as a "free" final step — zero extra LLM call,
  high delight.
- The `onEvent` streaming shape and the concurrency-semaphore pattern.
- The renderer-structure-as-value insight (score chips, ★, ⚠, indentation)
  and where it maps to our Feishu cards vs terminal.
- The SKILL.md ≤600-char single-line-description portability lesson (new).
- Explicit mapping of *all seven* of our current rubric axes vs ADHD's three,
  and a concrete proposal for an IDEATION profile rather than stretching
  existing code-correctness axes.
- The lessons-catalog gap: we capture avoidance from failure but not
  creative-strategy from success.
- The A/B split (cheap adopt-now vs adapt-via-follow-up-task vs skip).
- Two paths for delivering `/adhd` to users (skill-only first vs first-class
  Hub mode) with an explicit recommendation.

The high-level verdict ("we already own the mechanical critic split; add
frames, a pre-flight gate, and an ideation primitive/profile") is unchanged
from v1; this v2 makes it implementable without another research pass.

---

## 6. Risks and caveats

- **Benchmark numbers are unverified.** ADHD's headline gains (trap detection
  5.2× baseline, novelty 2.9×) are from the project's own LLM-judged evals on
  6 problems. The mechanism is well-engineered and consistent with how humans
  brainstorm, but we should not treat the magnitude as guaranteed.
- **Cost is real.** ADHD quotes ~10 agent calls per run (5 diverge + 1 score
  + 1 cluster + 3 deepen), 5–10× a single-shot answer, 30–90s wall clock.
  The pre-flight gate is not optional nice-to-have; without it, the ideation
  path will burn money on trivia. This is why B1 is XS and should land at
  the same time as any divergent capability.
- **"Weird-for-weird's-sake" is a failure mode.** ADHD explicitly names this
  in its anti-patterns (`SKILL.md:167-168`): a pile of unsorted absurdities
  is as useless as one safe answer. The critic/convergence step must be
  strict, not polite. Our reviewer culture already trends strict; extend that
  instinct to "refuse to pass if the worker produced 20 ideas and didn't
  commit."
- **Reviewer scaling.** Adding P-DIVERGE parallelism means a single task may
  produce 5× the transcript volume. Need to ensure reviewer prompts handle
  long inputs gracefully and that we summarize divergence output before
  feeding it to the score/critic step (ADHD already does this: only the
  one-line ideas go to the scorer, not the full branch traces).
- **Curating frames is a taste problem.** Not every frame suits every task
  type; the codeMode bias + 1-wild rule is a good default, but we should
  expect to tune the frame set over time. Shipping a too-large default set
  dilutes signal; start small (8 frames) and grow from lessons.

---

## 7. Suggested follow-up task ordering

If/when the team picks up adapt work, recommended order (dependency-aware):

1. **A1–A4** (docs/prompt-only adopts) — batch into one reviewed task, low
   risk, no behavior change. Can ship immediately.
2. **B1 + B2** (pre-flight gate + P-DIVERGE/P-CLUSTER/P-DEEPEN primitives) —
   one reviewed task; these are prompt/enum changes and must ship together
   (primitive without gate = wasted cost; gate without primitive = no way to
   act on "yes" answers).
3. **B3** (IDEATION review profile) — after B2 lands, so reviewers can judge
   divergent output properly.
4. **B5a** (skill-only `/adhd` via vendored SKILL.md) — after B2/B3; end-users
   can then invoke it.
5. **B4** (creative-strategy lessons) — after B2 ships and there are real
   ideation runs to learn from; requires examples to tune the lesson-kind
   criteria.
6. **B6** (progress events) — whenever UX complaints about long silent runs
   justify it.
7. **B5b** (first-class `--mode ideate` Hub task mode with Feishu rendering)
   — only if B5a usage shows demand; defer.

No code is changed by this PR. The deliverable is this document.

---

## 8. License and attribution note

ADHD is MIT-licensed (Udit Akhouri, 2025). Lifting the *frames* (paraphrased,
not verbatim) and the *structural patterns* (diverge/focus split, isolation
invariant, three-axis scoring, pre-flight gate, cluster-by-angle, non-obvious
pick, provocation wildcard) into prompts and primitives is straightforwardly
allowed; we should include attribution in the frame asset file and in any
SKILL.md we derive from it. We are not redistributing ADHD's code, so there
is no binary/source redistribution requirement, but crediting upstream is
courteous and aligned with how ADHD itself credits adopters in its README
(section "Early adopters").
