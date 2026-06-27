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

The strongest finding for claude_hub: **we already own the three load-bearing
pieces** — (a) an orchestrator that can fan out parallel sub-agents, (b) a
fully independent reviewer session acting as a mechanical critic, and
(c) **Claude Code's own `Workflow` tool (`parallel()` / `pipeline()` /
`agent()` / `phase()`) which is exactly the fan-out DSL ADHD hand-rolls
in TypeScript** with `Promise.all + p-limit(n)`; our current
`_subagent_capability_hint` doesn't tell the orchestrator this tool exists,
so our primitives read as a rigid linear template instead of a composable
set of shapes. What we do *not* have is the *ideation orchestration shape*
that sits on top: deliberate frame-driven divergence, isolation-as-feature,
scoring rubric, clustering, the `nonObviousPick` heuristic, the pre-flight cost
gate, and a lessons system that captures *successful creative patterns* rather
than only avoidance patterns.

**Top 6 adoptable ideas, ordered by ROI:**

1. **Point orchestrators at the Workflow tool (A5).** ~20 tokens added to
   `_subagent_capability_hint`; unlocks genuine parallel fan-out for
   research/validation today without any new primitives, and is the
   runtime primitive that P-DIVERGE will later ride on.
2. **Cognitive frames as a reusable prompt asset (A1)** — lift a curated
   ~8-frame subset of ADHD's 15 frames as a static, conditionally-injected
   reference. Zero runtime cost, pure prompt text, MIT-friendly with
   attribution.
3. **Pre-flight cost gate (B1)** before fanning out — extend the existing
   "judge complexity first" instruction with ADHD's sharper three-question
   check (open-ended? high-stakes? open-phrased?).
4. **A new `P-DIVERGE` workflow primitive built on Workflow.parallel + an
   `IDEATION` review profile (B2+B3)** so open-ended design/naming/architecture
   tasks get the divergent-then-convergent treatment, with DIVERGE branches
   pinned to Sonnet/Haiku and synthesis (CLUSTER/DEEPEN) pinned to Opus.
5. **Trap-detection rubric extension for reviewers (A4)** — our reviewer
   already hunts defect-traps; add "cognitive traps" (premature convergence,
   echo-chamber ideas, missing contrarian frames).
6. **Creative-strategy lessons (B4)** — extend the feedback harness to
   capture what *worked* (which frames/analogies unlocked the problem)
   rather than only avoidance patterns.

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
| Parallel fan-out of sub-agents | Orchestrator contract lists "breadth-first parallel across ≥3 independent threads" as an orchestrator-mode trigger (`_prompts.py:481-482`), and our `_subagent_capability_hint` (`_prompts.py:494-524`) documents Task-tool invocation per runtime. **But** we only hint at the `Task` tool — we do NOT mention Claude Code's higher-level `Workflow` tool (`parallel()`, `pipeline()`, `agent()`, `phase()`) which is purpose-built for exactly ADHD-style fan-out/converge. ADHD's `Promise.all + p-limit(n)` in `src/engine.ts` is literally the TS equivalent of `Workflow.parallel()`. V1 contract also implicitly serializes P-EXECUTE (per `2026-06-01` log Q4) and pins P-DIVERGE-style work to Opus, ignoring that fan-out branches should run on the cheaper Sonnet/Haiku tier. | 🟡 Runtime can parallelize via Task, but (a) our contract doesn't point the orchestrator at the Workflow tool, (b) the primitive list is a linear code-task template, not a fan-out shape, (c) model pinning doesn't reflect divergence's volume/cheapness. |
| Isolation between branches | Native to Task/Workflow sub-agent calls (each gets a fresh context). | ✅ Already the default; our prompts don't *name* isolation as a deliberate feature or forbid cross-branch reading. |
| A structured critic pass with numeric rubric | `EvaluationReport` + `RubricCriterion` + `CriterionResult` (`schemas.py:267-308`); reviewer produces numeric scores per criterion and an `overall_score` (pass threshold 0.8, `schemas.py:335`). | 🟡 Structure exists but all 7 shipping rubric axes are code-correctness / scope / integration / regression / validation / handoff focused (`_prompts.py:974-981`). No novelty/diversity/trap-detection axes. |
| A convergence / integrate step | `P-INTEGRATE` primitive (`_prompts.py:623`) — "combine partial outputs into the final deliverable." | ✅ Primitive exists, but tuned for code-artifact merge, not idea clustering. |
| A judge / evaluate step | `P-JUDGE` primitive + the external reviewer. | 🟡 Tuned for defect finding; no cognitive-trap lens. |
| Cluster by underlying angle | — | ❌ No equivalent. |
| Deepen top-K with risk + first step + child ideas | `review_failed → _build_continue_prompt` (`_prompts.py:1057-1092`) returns the task to the same worker to fix defects. | 🟡 Loop exists, but is a fix-defects loop, not an explore/expand loop. |
| Non-obvious-pick heuristic | — | ❌ No equivalent. |
| Trap detection (cognitive: premature convergence, anchoring, echo chambers, missed frames) | Adversarial defect hunt (`_prompts.py:961-969`) covers edge cases, error paths, races, regressions, scope leaks, security/permission assumptions. | 🟡 Defect-traps are covered, cognitive-traps are not. |
| Pre-flight cost gate | The orchestrator contract already says "before implementation, judge whether this task is simple or complex. State the chosen execution strategy in your first working report." | 🟡 We have a binary simple/complex gate; ADHD's three-question gate is sharper (open-ended AND high-stakes AND open-phrased) and includes a one-line "don't use this" advertisement when it refuses. |
| A reusable frame library | — | ❌ No equivalent. |
| Workflow-aware sub-agent orchestration (the shape ADHD is written in) | Claude Code's own **Workflow tool** (`parallel()`, `pipeline()`, `agent()`, `phase()`) provides exactly the fan-out / synchronize / deepen primitives ADHD implements manually in TS. Our `_subagent_capability_hint` and primitive contract do not reference it. | ❌ **Blind spot.** Our contract is a rigid linear P-* template that doesn't tell the orchestrator about the native fan-out DSL it already owns. |

**Key insight from this review pass:** we do NOT need to build a new engine or
a new multi-agent framework to absorb ADHD. The Claude Code Workflow tool is
already ADHD's runtime — `parallel(...agent()...)` IS the Diverge phase, a
single `agent()` after the parallel returns IS the Focus/Score/Cluster pass,
and a second `parallel(...agent()...)` IS the Deepen phase. What we lack is
(a) telling the orchestrator the Workflow tool exists and when to reach for
it, (b) a divergent-ideation *shape* (frames + gate + rubric + output
contract) layered on top of the generic tool, and (c) model pinning that
reflects divergence (fan-out branches = Sonnet/Haiku; synthesis = Opus).
This moves B2 from "add new primitives + orchestration machinery" down to
"document an additional workflow shape in the contract and point the
orchestrator at the Workflow tool for fan-out" — substantially cheaper.

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

**A5. Point orchestrators at Claude Code's Workflow tool. Prompt-only, ~XS.**

Today our `_subagent_capability_hint` (`_prompts.py:494-524`) documents the
`Task` tool per runtime but never mentions the higher-level **Workflow**
tool that Claude Code ships natively — `parallel()`, `pipeline()`,
`agent()`, `phase()`. This is the fan-out/synchronise DSL that ADHD hand-
rolls in TypeScript. Even without adding P-DIVERGE or any ideation
primitives, the orchestrator can benefit from knowing this tool exists
for **parallel research** (multiple independent reads/searches) and
**parallel validation** (multiple independent checks), which are already
named primitives but whose parallel execution the current contract
doesn't encourage.

Add a short paragraph to the Claude-runtime branch of
`_subagent_capability_hint`:

> - The **Workflow** tool provides higher-level orchestration than raw
>   `Task` calls. Use `Workflow.parallel(...)` to fan out N independent
>   sub-agents (research, validation, or ideation branches) with a
>   concurrency cap; each `agent()` inside gets an isolated fresh
>   context by default, which is the right isolation invariant for
>   fan-out work. Use `Workflow.pipeline(...)` when one stage's output
>   feeds the next. Do NOT simulate parallelism by writing sequential
>   Task calls in one context — that shares state between branches and
>   collapses to a wider single thought.
> - Always cap `concurrency` in `parallel()` to ≤4 unless there is a
>   concrete reason to go higher; this mirrors the cost control in
>   ADHD's own `p-limit(4)`.

This is a pure prompt change in the capability hint; no enums, no schema,
no new primitives. It is the cheapest possible absorption of ADHD's
"isolate branches, fan out genuinely in parallel" lesson and gives
immediate value on non-ideation parallel research/validation work.

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
contract, and tell the orchestrator about Claude Code's Workflow tool.
Effort: ~S, prompt + enum updates only — no new engine.**

Critical realisation (raised in review): **Claude Code already ships a
`Workflow` tool** with `parallel()`, `pipeline()`, `agent()`, `phase()` —
this is exactly the fan-out/synchronise/deepen DSL that ADHD hand-rolls
in TypeScript with `Promise.all + p-limit(n)`. We don't need new Hub-side
parallelism machinery. What we need is:

Files:
- `backend/claude_hub/services/workspace_manager/_prompts.py` lines 618-624
  (the primitives list). Add **P-DIVERGE** (fan out N parallel ideation
  sub-agents under distinct frames from the frame asset, critic disabled
  by instruction, strict isolation), **P-CLUSTER** (synthesis — group
  ideas by underlying angle and surface shape), **P-DEEPEN** (take top-K
  clustered ideas, produce sketch + risk + first step + child ideas).
  These are additions to the primitives vocabulary, not a replacement.
  P-INTEGRATE still covers convergence; P-JUDGE still covers scoring.
- Same file, `_subagent_capability_hint` (~lines 494-524): for the Claude
  runtime, document the **Workflow tool** as the preferred mechanism for
  fan-out patterns (P-DIVERGE and other breadth-first parallel work):
  > - When a workflow calls for parallel isolated branches (P-DIVERGE,
  >   parallel research, parallel validation), prefer the Workflow tool
  >   with `parallel(...)` over serial `Task(...)` calls. Use
  >   `pipeline(...)` when one stage's output feeds the next
  >   (CLUSTER→JUDGE→DEEPEN). Each `agent()` inside a parallel block
  >   receives an isolated context — use this to enforce the ADHD
  >   isolation invariant (branches must not see each other's output).
  >   Cap `concurrency` in Workflow.parallel to ≤4 (matching ADHD's
  >   `p-limit(4)` default) to avoid runaway sub-agent creation.
  Cursor/codex fallbacks remain as they are today.
- Same file, `_model_evidence_contract_block` (~lines 527-555): pin models
  per new primitive. Divergent branches are volume work on short inputs
  and benefit more from speed/cost than peak reasoning — pin them to
  **Sonnet** (Haiku acceptable when the problem is cheap). CLUSTER and
  DEEPEN are synthesis/focus passes that reward reasoning — pin to
  **Opus**. Existing pins (Opus for PLAN/EXECUTE/JUDGE/INTEGRATE, Sonnet
  for VALIDATE/RESEARCH) stay.
- Same file, cost-guard (~lines 481-486): explicitly allow parallel fan-out
  for P-DIVERGE (V1 currently implicitly serialises P-EXECUTE; DIVERGE is
  parallel *by construction* and isolation is load-bearing). Cap via the
  Workflow tool concurrency limit above, not via a Hub-side semaphore.
- Same file, in the workflow-shape section around lines 632-648: add a
  short list of recognised workflow *shapes* alongside the existing
  linear shape, so the orchestrator knows when to reach for which tool:
  > - **linear implement**: P-PLAN → P-EXECUTE → (P-VALIDATE) → P-JUDGE → P-INTEGRATE.
  >   Default for bug fixes, features, refactors with one answer.
  > - **divergent ideate**: P-DIVERGE (parallel via Workflow.parallel) →
  >   P-CLUSTER → P-JUDGE (score/trap) → P-DEEPEN (parallel via Workflow.parallel)
  >   → P-INTEGRATE. Use for open-ended design, naming, strategy, fuzzy
  >   debugging when the pre-flight gate passes.
  > - **parallel research / parallel validate**: P-RESEARCH or P-VALIDATE
  >   fan-out via Workflow.parallel when sources/checks are independent;
  >   P-INTEGRATE merges.

The three new primitives plus Workflow-tool awareness let the orchestrator
compose an explicit DIVERGE→CLUSTER→JUDGE(score/trap)→DEEPEN→INTEGRATE
shape for open-ended tasks, using the runtime's own native parallelism
instead of inventing a new execution engine. ADHD's `engine.ts` maps
line-for-line onto Workflow calls:
`run()` = orchestrator script using Workflow, `Promise.all` with p-limit =
`parallel(..., { concurrency: 4 })`, `scoreIdeas/clusterIdeas` = one
`agent()` after the parallel returns, `deepenIdea` top-K = a second
`parallel()`.


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

### 4.4 Prompt-token cost: always-on vs triggered

A question that came up in review: *"How much prompt overhead does this
add, and when does the agent decide to use the ideation workflow?"*
Concrete numbers below so we can size the change honestly.

**Always-on overhead (every task, once A1–A5 are in): ~200 tokens.**

| Item | Tokens (approx.) | Injected on every task? |
|---|---:|---|
| A2 SKILL.md ≤600-char rule | 0 (comment in our SKILL.md, not a model prompt) | no |
| A3 critic-split citation sentence | ~20 tokens, added to reviewer bootstrap (`_prompts.py:~153`) | yes |
| A4 reviewer cognitive-trap bullets (anchoring, echo-chamber) | ~80 tokens, appended to the existing adversarial defect-hunt list (`_prompts.py:963-968`) | yes |
| A5 Workflow-tool mention in `_subagent_capability_hint` | ~100 tokens, added to Claude-runtime branch | yes (Claude runtime only) |
| A1 curated frame library (~8 frames × 2–3 sentences) | ~350 tokens if injected unconditionally — **must be conditional** | only on ideation-shaped tasks |

That is, the default prompt growth from A1–A5 is **~200 tokens**
against orchestrator/reviewer system prompts that are already ~3–4k tokens
(and only ~100 tokens on non-Claude runtimes that don't receive the A5
hint). The frame library must not be unconditionally appended to every
prompt; it should be gated behind the pre-flight check (B1) or an
explicit opt-in, otherwise it is pure noise on the 90%+ of tasks that are
convergent bug fixes / small features.

**Triggered overhead (when the DIVERGE workflow actually fires):**

- System-prompt side: 5 DIVERGE branches × ~200 tokens (generator stance +
  one frame) + P-CLUSTER ~150 + P-JUDGE(scoring) ~200 + 3 DEEPEN calls ×
  ~200 ≈ **~1,950 system-prompt tokens spread across ~10 LLM calls**.
- User-prompt side: ~30 short idea-phrases collected from the branches and
  forwarded into scoring/clustering/deepening ≈ **500–800 tokens**.
- Total ≈ **2,500–3,000 extra prompt tokens** on top of a normal answer,
  and **9 extra LLM calls** per run. This matches ADHD's own claim of
  5–10× a single-shot answer and 30–90s wall clock. Cost is linear in
  branches `O(N × per_branch)`, not quadratic, because branches are
  isolated.

The pre-flight gate is therefore not optional nice-to-have — it is the
cost-control mechanism. Ship it together with the primitive (B1+B2 in one
PR), not after.

### 4.5 When does the agent decide to use this workflow? (rollout plan)

We should NOT start with auto-detection. Recommended three-stage rollout,
from safest to most autonomous:

1. **Stage 1 — cheap adopts only (this PR + one follow-up docs/prompt PR).**
   Ship A1–A4. No new workflow primitive, no new task mode, no auto-trigger.
   The frame library exists as a reference file but is not injected by
   default; human agents (or the orchestrator on a specific contract) can
   still reference it manually. Overhead: ~100 tokens always-on as above.
2. **Stage 2 — explicit opt-in only (one reviewed task, ~S–M effort).**
   Implement B1 (pre-flight gate as a *contract* the orchestrator must
   ask but not auto-fire), B2 (P-DIVERGE / P-CLUSTER / P-DEEPEN
   primitives with concurrency cap), B3 (IDEATION review profile), and
   B5a (vendored `/adhd`-equivalent skill or `claude-hub task create
   --mode ideate`). The workflow fires **only** on explicit invocation:
   user types `/diverge "<problem>"`, or passes `--mode ideate` at task
   creation. There is zero auto-judgment; zero risk of burning 10 calls
   on a typo fix. The pre-flight gate in this stage is used *within*
   an invoked ideation run as a self-check (e.g. "did the user phrase
   this openly enough for fan-out to be worth it?" → if no, answer
   directly with a one-line suggestion to rephrase), not as a trigger.
3. **Stage 3 — gated auto-trigger (only after real usage validates Stages
   1–2).** Wire the three-question pre-flight gate (open-ended? high-
   stakes? open phrasing?) into the orchestrator's existing "judge
   simple vs complex first" step (`_prompts.py:~481`). All three must be
   "yes" to pick DIVERGE; any "no" falls back to the normal
   P-PLAN→P-EXECUTE path. Ship behind a feature flag or workspace-level
   config knob, and collect metrics (trigger rate, false-positive rate
   measured by human override) before enabling by default.

**Hard rules at every stage:**

- If the user's prompt contains `quick / just / standard / canonical /
  textbook / one-line`, never auto-trigger.
- Syntax fixes, typo fixes, known-root-cause bugs, lookups, and pure
  refactors with no design dimension never trigger.
- If the workflow fires but produces shortlists that all share one
  underlying assumption, the IDEATION reviewer must call "convergence
  disguised as divergence" and fail review back for another frame (see
  anti-patterns in SKILL.md:164-166).

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
- **(Added in review pass — Workflow tool blind spot)** Our
  `_subagent_capability_hint` (`_prompts.py:494-524`) documents per-runtime
  `Task`-tool calls but does NOT mention Claude Code's native **Workflow**
  tool (`parallel() / pipeline() / agent() / phase()`), which is exactly the
  fan-out/synchronise DSL that ADHD implements by hand in TypeScript with
  `Promise.all + p-limit(n)`. ADHD's `engine.ts` maps line-for-line onto
  Workflow calls. This drops B2's cost from "build new orchestration
  machinery" to "prompt the orchestrator to use the DSL it already owns",
  and lets parallel research/validation benefit immediately from A5 before
  DIVERGE exists.
- **(Added in review pass — token cost + rollout policy)** §4.4 concrete
  prompt-token budget (always-on ~200 tokens once A1–A5 land; ~2.5–3k
  tokens across ~10 LLM calls when DIVERGE fires) and §4.5 conservative
  three-stage rollout (cheap adopts → explicit opt-in only → gated auto
  behind a flag after usage data), with hard never-trigger rules.

The high-level verdict ("we already own the mechanical critic split; add
frames, a pre-flight gate, and an ideation primitive/profile") is unchanged
from v1; this v2 makes it implementable without another research pass, and
adds the Workflow-tool insight that materially shrinks B2's scope.

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
- **The orchestrator prompt is currently blind to Claude Code's native
  Workflow tool.** Our `_subagent_capability_hint` documents the `Task` tool
  per runtime but never mentions `Workflow.parallel/pipeline/agent/phase`.
  Until A5/B2 land, orchestrators default to linear serial decomposition
  even when the runtime offers a native fan-out DSL. This is a prompt gap,
  not a missing feature — and the cheapest finding in this entire analysis
  to fix.
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

If/when the team picks up adapt work, recommended order (dependency-aware),
mapped to the three-stage rollout in §4.5. The Workflow-tool insight
(A5/B2) shrinks the work considerably: fan-out is a prompt-level change,
not new Hub machinery.

**Stage 1 — cheap adopts only (this PR + one follow-up docs/prompt PR).**

1. **A1–A5** (docs/prompt-only adopts) — one reviewed task, low risk, no
   behavior change beyond the orchestrator now knowing it can call
   `Workflow.parallel` for independent sub-work. Always-on prompt overhead
   stays ≲120 tokens (A2:0, A3:~20, A4:~80, A5:~100, A1 frames:~350
   conditional-injected). Can ship immediately.

**Stage 2 — explicit opt-in only (no auto-trigger).**

2. **B1 + B2** (pre-flight gate contract + P-DIVERGE/P-CLUSTER/P-DEEPEN
   primitives + Workflow-tool parallel shape, with P-DIVERGE pinned to
   Sonnet and CLUSTER/DEEPEN pinned to Opus, concurrency cap ≤4 in
   `Workflow.parallel`) — one reviewed task. Primitive without gate =
   wasted cost; gate without primitive = no way to act on "yes" answers.
   The gate is a *self-check inside an invoked ideation run*, not an
   auto-trigger yet. Critically, B2 is prompt/enum-only: it tells the
   orchestrator to compose Workflow-tool calls, not to build new Hub
   parallelism.
3. **B3** (IDEATION review profile + novelty/diversity/trap axes) — after
   B2 lands, so reviewers can judge divergent output against a suitable
   rubric instead of the code-correctness rubric.
4. **B5a** (vendored `/adhd` or `claude-hub task create --mode ideate`
   as an *explicit* entry point) — after B2/B3; end-users can invoke the
   workflow, but it never fires without an explicit signal.

**Stage 3 — gated auto-trigger (only after Stage 2 ships and usage data
justifies it).**

5. **B4** (creative-strategy lessons) — after Stage 2 has real ideation
   runs to learn from; needs examples to tune the positive-pattern signal.
6. **Wire pre-flight gate into auto-judgment** at `_prompts.py:~481`
   (the existing simple/complex choice) — only auto-fire DIVERGE when all
   three gate questions are "yes"; ship behind a workspace-level flag;
   monitor trigger rate and false-positive rate (human override) before
   enabling by default.
7. **B6** (onEvent-style progress events: "diverging (2/5) … scoring …")
   — whenever UX complaints about 30–90s silent runs justify it.
8. **B5b** (first-class `--mode ideate` Hub task mode with structured
   Feishu rendering, score chips, ★ non-obvious pick, ⚠ traps) — only if
   Stage 2 usage shows real demand; defer.

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

---

## 9. Appendix — current prompt architecture snapshot

> **Canonical format reference for implementers has moved to**
> **[`2026-06-28-prompt-format-reference.md`](./2026-06-28-prompt-format-reference.md).**
> That document is structured as a per-block, per-cell reference with
> clean tables and template shapes. This appendix is retained for the
> narrative tying each rigidity cause to a specific ADHD recommendation
> (A5 / B2 / B3).

For implementers picking up A5 / B1–B3, this section records how prompts are
currently assembled in `backend/claude_hub/services/workspace_manager/_prompts.py`
as of this PR (built on `main@70921a7`). All line references are in that file.

### 9.1 Worker-side assembly

Every worker session receives three **common** layers regardless of mode:

1. **Bootstrap** (`_build_workspace_agent_prompt`, 89–121) — resident-worker
   posture, wait-for-task, expect independent reviewer, report via
   `POST /api/workspaces/sessions/{id}/reports`, bilingual `message_en`+`message_zh`,
   `review_decision ∈ {auto, request, skip}` + `review_reason` required on
   completed reports.
2. **Per-task header** (`_build_task_assignment_prompt`, 255–320) — Workspace/Task
   IDs, title, mode, complexity, env block, state-snapshot path, dispatch reason,
   optional clear_note / attachment_note, lesson-context block.
3. **Per-task footer** (320–343) — read snapshot → derive Goal Packet → report
   state machine (`started / working / blocked / needs_input / ready_for_review /
   completed`) → `curl` examples for `started` and the Goal Packet report. On
   reviewed tasks a gating sentence tells the worker to stop and wait for
   reviewer approval after the first Goal Packet report.

Between the header and footer, blocks are injected based on
**execution_complexity** and **task_mode**.

**Complexity block** (`_execution_complexity_assignment_block`, 459–492) always
emits one of three variants:

- `simple` → "Execute directly in this session, keep plan compact, avoid
  spawning subagents unless blocker."
- `complex` → "Act as task orchestrator: decompose, delegate to subagents, keep
  ownership explicit, personally integrate/validate/accept final result."
- `auto` → "Judge simple/complex first, state strategy in first report; if
  complex orchestrate, if simple execute directly."

All three variants are followed by the same **cost guard** (487–492): orchestrator
mode is expensive; use it only when (1) breadth-first parallel ≥3 threads, (2)
context cannot hold the material, or (3) clean isolation is needed; otherwise
single linear agent.

After the complexity block, two mode-agnostic "hints" always fire:

- **Subagent capability hint** (`_subagent_capability_hint`, 494–525) — per
  runtime. Claude branch currently documents *only* the `Task` tool
  (`Task(subagent_type=..., model=...)`); it does **not** mention the
  `Workflow` tool (`parallel / pipeline / agent / phase`) that natively
  implements fan-out. This is the blind spot A5 fixes.
- **Model evidence contract** (`_model_evidence_contract_block`, 527–555) —
  pins P-PLAN / P-EXECUTE / P-JUDGE / P-INTEGRATE to opus and P-VALIDATE /
  P-RESEARCH to sonnet on the Claude runtime; other runtimes get softer
  fallbacks; requires subagent-ledger evidence.

**Mode block** after the hints:

- `direct` → `_autonomous_assignment_block` returns `""`. No orchestrator
  contract, no P-* primitive list. The worker runs straight through and
  reports `completed` with no reviewer cycle.
- `reviewed` → `_autonomous_assignment_block` also returns `""`. No P-* list,
  no ledger schema, no model pinning enforcement. Only the Goal Packet gate
  and the soft "act as orchestrator" line in the complexity block push
  reviewed/complex toward orchestration; in practice this is weak, which is
  why reviewed/complex often collapses into single-agent-with-reviewer.
- `autonomous` → `_autonomous_assignment_block` (557–580) emits the
  Autonomous Mode V1 header (`max_iterations / evaluation_strictness /
  allow_web_research / require_artifact_review / human_checkpoint_policy /
  current phase`) and worker rules, then `_orchestrator_contract_block`
  (582–677) which enforces complexity-dependent contracts:
  - autonomous/simple: execute directly but MUST spawn one P-JUDGE pre-flight
    before the review-gate report.
  - autonomous/complex: orchestrator mode REQUIRED; workflow MUST include
    ≥1 P-EXECUTE and ≥1 P-JUDGE; posting without a complete subagent ledger
    is a contract violation.
  - autonomous/auto: declare orchestrator vs single-agent in
    `goal_packet.assumptions` in the first report; if orchestrator, the
    complex contract applies; if single-agent, one P-JUDGE is still required.

  The contract block also enumerates the six existing primitives
  (P-PLAN / P-EXECUTE / P-VALIDATE / P-JUDGE / P-INTEGRATE / P-RESEARCH),
  repeats model pinning, specifies a `workflow: {roles, deps, notes}`
  block, gives one linear skeleton example (planner → implementer → tester
  → reviewer → integrator), defines the subtask envelope schema and the
  subagent-ledger schema, and ends with "there is NO fixed enum of templates;
  compose roles freely." The linear example plus execution-only primitives is
  what makes the prompt feel rigid — B2 adds DIVERGE/CLUSTER/DEEPEN and a
  divergent ideate shape so the agent has a non-linear template to imitate.

### 9.2 Reviewer-side assembly

Every reviewer session receives a common bootstrap
(`_build_reviewer_bootstrap_prompt`, 141–202): "Your primary job is to FIND
defects and risks, not to confirm success"; "Approval is the exception, not
the default"; do not defer to implementer confidence; emit `review_started`
early; finish with exactly one of `review_passed / review_failed /
review_needs_input`; keep the message ~12 lines and put details in structured
fields (`validation / risks / acceptance_check / profile_results /
artifact_refs`); bilingual `message_en` + `message_zh`.

Per-review assignment (`_build_review_prompt`, 795–906) always contains:

1. Header (Workspace/Task IDs, title, mode, complexity, impl session, reviewer
   session, env lines, snapshot path) + task description.
2. **Execution-complexity review context** (993–1002) — verify the
   implementation strategy matched the declared complexity; unnecessary
   delegation on simple tasks is a scope risk; missing decomposition or
   integrator validation on complex tasks is blocking; auto tasks must have
   explicitly declared and followed their chosen strategy.
3. Stored Goal Packet JSON.
4. **Autonomous evaluation context** (`_autonomous_review_block`, 1004–1055) —
   empty for direct/reviewed. For autonomous: run JSON, runtime,
   max_iterations, evaluation_strictness, require_artifact_review; "act as
   the evaluator for this iteration" and score against Goal Packet + rubric +
   validation + artifacts + prior evaluations; `review_passed` moves the run
   to passed awaiting human acceptance; `review_failed` triggers targeted
   revision within budget; `review_needs_input` for product judgment /
   missing credentials / unavailable environment. Subagent-ledger
   verification is enforced (complex autonomous MUST embed a ledger in
   `validation`, missing/empty = contract violation → review_failed; each
   entry needs role.id / primitive / agent / model_or_api / decision /
   evidence). Model pinning is verified with runtime-dependent strictness:
   hard opus/sonnet tier check on Claude, softer `runtime-default /
   unsupported / external:<api>` acceptance on Cursor/Codex, honest
   degradation check on terminal. Workflow consistency check:
   `workflow.roles` must match the ledger; ≥1 P-EXECUTE and ≥1 P-JUDGE must
   have actually run; P-VALIDATE is required when objectively checkable
   criteria exist.
5. **Enabled review profiles** (711–717) — profile list JSON + per-profile
   checklist lines produced by `state_policy.review_profile_prompt_lines`.
   Profiles are merged from the task, trigger report, and (autonomous only)
   autonomy policy, then inferred via `state_policy.infer_review_profiles`
   (679–709). Current six: general / code / ui / artifact / delivery /
   boundary. B3 adds `ideation`.
6. **Repository review guidance** (719–793) — up to six `REVIEW.md` files
   discovered by walking up from each changed file (truncated at 4000 chars
   each).
7. lesson_context_block.
8. **Review workflow** (`_review_workflow_block`, 908–991) — two shapes:
   - *Goal Packet approval review* (913–945): read-only; do not judge
     implementation (there is none); check goal fidelity, boundary quality,
     reviewability, handoff quality; fail on narrowed/distorted scope,
     missing editable/non-editable boundaries, or vague validation.
   - *Full review* (946–991): 7 steps — stay read-only; check Goal Packet
     fidelity; derive a task-specific acceptance checklist from 7 sources
     (task description, Goal Packet, explicit user requirements, reports,
     profiles + REVIEW.md, repository conventions, blocked/needs_input
     context); inspect changed files and related paths; **adversarial defect
     hunt across six failure-mode categories BEFORE deciding the verdict**
     (edge/Null/large values, error paths/partial failures/retries,
     concurrency/ordering/shared-state races, regressions to existing flows/
     persistence/migrations, scope leakage / untouched-area side effects,
     security/permission/input-trust assumptions) — anything not ruled out by
     reading code is a candidate defect; independently spot-check the
     highest-risk claimed validation; produce final verdict. Acceptance
     standards cover goal fidelity, functional correctness, scope control,
     integration fit, regression safety, validation quality, handoff quality.
     `review_passed` requires "actively attempted to break the change and
     found no blocking defect" — explicitly forbidden to pass on absence of
     attempt or on implementer-report confidence.
9. Final report format (866–905): ~12 line message with Verdict / Summary /
   Acceptance / Required fixes / Notes; bilingual; detailed evidence in
   structured fields; trigger report JSON; recent 12 task reports JSON; curl
   example for `review_passed`; exit-criteria reminders.

When the reviewer returns `review_failed`, the worker receives
`_build_continue_prompt` (1057–1082): Task ID / title / follow-up instructions
(the reviewer's Required fixes), plus `_autonomous_continue_orchestrator_reminder`
(1083–1092) for autonomous tasks: "if you ran in orchestrator mode, stay in
orchestrator mode for this revision; address blocking issues by dispatching
new sub-agents (P-EXECUTE for fixes, P-VALIDATE for re-tests, P-JUDGE for
re-review) rather than folding work into your own context; append new ledger
entries — do not restart the ledger."

### 9.3 Why the current prompt feels rigid

Cross-referencing 9.1 and 9.2 surfaces the three rigidity causes A5 / B2 / B3
are designed to relieve:

1. **Capability hint blind spot** (494–525): the Claude-runtime hint tells the
   agent only about `Task`; the `Workflow` tool that natively provides
   `parallel / pipeline / agent / phase` fan-out is never mentioned, so the
   agent does not know non-linear orchestration is even available.
2. **Execution-only primitives + linear-only example** (632–662): the
   primitive list has no creative/exploratory roles, and the only worked
   example is a linear chain. The "compose freely" disclaimer at line 674 is
   too weak to overcome the example.
3. **Reviewed-mode has no orchestration contract**: orchestrator_contract_block
   only fires for autonomous mode (line 558); reviewed/complex relies on the
   soft COMPLEX complexity block, with no primitive list, no ledger
   requirement, no model pinning enforcement, and (after A5 lands) no
   Workflow-tool pointer, so reviewed/complex effectively degenerates to
   "single agent does the work, reviewer catches errors."
