# Auto Mode — CLI-Native Sub-Agent Orchestration (Design Proposal)

> Task: e8175dff-6fed-4a2e-8d6b-488ec1ba11b8 · auto mode 优化
> Author: cb-agent-2 (Claude) · 2026-06-01
> Status: Draft for human review — 不含代码改动
> Companion doc: `2026-05-27-auto-mode-team-design.md` (Cursor 提的 ManagedSession 团队化方案，V2 视角)

## 1. 背景

### 1.1 当前 Auto mode 实际形态

`task_mode=autonomous` 的执行链路（参见
`backend/claude_hub/services/workspace_manager.py`）目前是：

- 一个 **worker session** 拿到 `_build_task_assignment_prompt`：
  - `_execution_complexity_assignment_block` 给一句软性提示「complex 时可以
    delegate to subagents when your runtime supports them」。
  - `_autonomous_assignment_block` 强调多迭代 + evaluator 路由。
- 一个 **reviewer session** 兼 evaluator，跑 `_autonomous_review_block`。
- 失败时 worker 在**同一个 session 内**收到 `_build_continue_prompt`，自己继续干。

也就是说：长任务下整条迭代闭环都压在「一个 worker agent 的单条对话上下文」里。

### 1.2 用户反馈

> 现在的 Auto mode 不太完全。CLI 现在应该都有比较充分的 sub-agent 能力。
> Auto mode 任务在 Prompt 阶段就应该强烈要求主 Agent 担当 orchestrator，
> 也作为和用户交互的角色，所有具体的开发、验证、Review 都让它按分工去
> spawn sub-agent 完成。多 Agent 在复杂任务下不会那么容易 confuse，
> 单 Agent 在长上下文里很容易忘事。

### 1.3 与 2026-05-27 团队化方案的关系

`2026-05-27-auto-mode-team-design.md` 提的是「在 Hub 里再开一组 ManagedSession
组成 team」——属于**会话层（V2/V3）**：好处是每个角色独立 tab、独立 git 写权限，
代价是要新枚举 / 新 API / 新前端 UI。

本提案聚焦于**Prompt 层（V1）**：

- 不改 schema，不改前端，不开新 ManagedSession。
- 直接强化 `autonomous` 任务的指派 prompt，让单一 worker session 内主 CLI
  Agent 强制以 orchestrator 模式工作，**通过自己的原生 sub-agent 工具**
  （Claude Code 的 `Task` tool、Cursor 的 sub-agent、Codex 的子任务）完成
  实现 / 测试 / 内审。
- 与 V2 团队化方案是**叠加关系**：V1 prompt 层先落地拿到 80% 收益；后续
  V2 把成熟的「角色契约」迁移成跨 session 的 ManagedSession team。

## 2. 设计原则

1. **Single conversational front door**：用户只跟 worker session 的主 Agent
   对话；orchestrator 角色就是这个主 Agent，不暴露 sub-agent 直接给用户。
2. **Prompt-first, schema-stable**：V1 全部通过 prompt 表达，复用
   `AgentReport`、`autonomous_run.iteration`、`changed_files`、
   `validation`、`acceptance_check` 字段。无 DB migration。
3. **Runtime-aware delegation**：每种 CLI（claude / cursor / codex / terminal）
   有不同的 sub-agent 能力，prompt 要按 `session.agent_type` 给出**具体可
   执行的 sub-agent 调用建议**而不是泛泛的「if your runtime supports」。
4. **Bounded subtasks**：每个 sub-agent 调用必须带：目标 / 输入 / 期望产出 /
   acceptance 子集 / 报告回形式，避免「让 sub-agent 自由发挥」导致的失控。
5. **Audit-trail in main report**：orchestrator 必须把每次 sub-agent 调用
   摘要写进自己最终汇报的 `validation` / 自由文本字段；让 reviewer 和人类
   能复盘谁干了什么。
6. **Opt-out gracefully**：simple 复杂度的 autonomous 任务、或者不支持
   sub-agent 的 runtime（terminal / 旧 cursor），自动退化为单 agent 行为，
   不强求伪造 sub-agent 流程。

## 3. Orchestrator 角色契约（Prompt 层）

下面这段是要追加进 `_autonomous_assignment_block`（或新增
`_autonomous_orchestrator_block`）的核心契约文本草稿：

```
## Orchestrator Contract (Auto Mode)

You are the *orchestrator* and the *only* voice the user hears for this task.
You must NOT do bulk execution, validation, or judging in your own context.
Instead, decompose the task into bounded subtasks and delegate them to
sub-agents using your runtime's native sub-agent capability:

- claude  → use the Task tool with appropriate subagent_type
            (general-purpose / Explore / Plan / code-reviewer / ...).
- cursor  → use cursor's sub-agent / spawn capability; YOLO is on by default.
- codex   → use codex's subtask / fan-out capability.
- terminal/other → no native sub-agents available; degrade to single-agent
                    execution and document the degradation in your goal
                    packet's assumptions.

This task is NOT assumed to be a coding task. The Hub does not prescribe a
fixed coding-shaped role list. Instead it gives you "role primitives"
(responsibility shapes) and you declare the concrete role schema this task
will use, in your first working report.

Role primitives (domain-agnostic responsibility shapes):

  P-PLAN      — decompose, decide subtask graph, hold the spec.
  P-EXECUTE   — produce the artifact (code, prompt, image, doc, query, ...).
  P-VALIDATE  — mechanical/objective check (tests, schema, hashes, lint).
  P-JUDGE     — qualitative critique vs acceptance (review, aesthetic
                judge, fact check, rubric scoring).
  P-INTEGRATE — combine partial outputs into the final deliverable.
  P-RESEARCH  — fetch external knowledge / docs / references.

In your first working report you MUST declare a `workflow:` block listing
the concrete roles you allocated for this task and the dependency edges
between them. There is no fixed enum of templates; you compose roles from
the primitives above. Include a `notes:` line explaining why this schema
fits the task. The two worked examples below demonstrate the shape — they
are illustrative, NOT a closed list. Any non-trivial workflow MUST contain
at least one P-EXECUTE and one P-JUDGE; P-VALIDATE is required when the
task has any objectively-checkable success criterion.

────────────────────────────────────────────────────────────────────────
Example 1 — Coding task (linear, all-in-context LLM calls)

Task: "Add a soft-delete endpoint to /api/orders. Reject if the order
       has shipped. Cover with tests."

  workflow:
    roles:
      - id: planner
        primitive: P-PLAN     model: opus
        duty: decompose, list acceptance, identify ORM/router blast radius
      - id: implementer
        primitive: P-EXECUTE  model: opus
        duty: edit router + service + ORM, scoped to the orders module
      - id: tester
        primitive: P-VALIDATE model: sonnet
        duty: add unit + integration tests covering shipped vs unshipped
      - id: internal-reviewer
        primitive: P-JUDGE    model: opus
        duty: independent review against acceptance + REVIEW.md
      - id: integrator
        primitive: P-INTEGRATE model: opus
        duty: collect changed_files + ledger, decide ready_for_review
    deps: planner → implementer → tester → internal-reviewer → integrator
    notes: Pure code change, no external API; P-RESEARCH not needed.

────────────────────────────────────────────────────────────────────────
Example 2 — Image generation task (feedback loop + external API)

Task: "Generate a hero image for the launch page: cyberpunk street scene,
       cool neon palette, 1920x1080, no brand logos."

  workflow:
    roles:
      - id: director
        primitive: P-PLAN     model: opus
        duty: translate aesthetic spec into controllable prompt dimensions
      - id: prompt-author
        primitive: P-EXECUTE  model: opus
        duty: produce 3 candidate T2I prompts (with negative prompts)
      - id: image-generator
        primitive: P-EXECUTE  model: external:t2i.v3
        duty: run each candidate, return S3 URIs (no LLM tokens spent)
      - id: safety-validator
        primitive: P-VALIDATE model: sonnet
        duty: call internal logo/NSFW detector; reject failing images
      - id: aesthetic-judge
        primitive: P-JUDGE    model: opus
        duty: score on composition / palette / fit; <7 triggers loop back
      - id: integrator
        primitive: P-INTEGRATE model: opus
        duty: bundle final URI + critique + ledger
    deps: |
      director → prompt-author → image-generator → safety-validator
                                                  → aesthetic-judge
      aesthetic-judge --(score<7)--> prompt-author    # at most 2 retries
      aesthetic-judge --(score≥7)--> integrator
    notes: P-EXECUTE appears twice (prompt authoring vs T2I call) — the
           authoring role uses opus, the API-only role uses external:.
           Loop guards against single-shot misses.
────────────────────────────────────────────────────────────────────────

The two examples are deliberately different shapes (linear vs branching +
external API + repeated primitive). Use them as inspiration; do not pick
either verbatim unless your task genuinely matches.

For each subtask you dispatch, hand the sub-agent a STRUCTURED ENVELOPE
(same schema across claude / cursor / codex):

  [subtask-envelope]
  role.id: <as declared in workflow.roles>
  primitive: <P-PLAN|P-EXECUTE|P-VALIDATE|P-JUDGE|P-INTEGRATE|P-RESEARCH>
  objective: <one sentence — your contract with the sub-agent>
  success_criteria: <bullet list, must map to goal_packet.acceptance>
  inputs: <files / links / prior artifacts>
  output_schema: <what the sub-agent must return — patch summary,
                  prompt string, image URI, lint report, ...>
  tools_allowed: <whitelist; deny everything else>
  context_budget: <approximate token / step budget>
  return_mode: final-only       # default; opt-in to `full-transcript`
                                # only when debugging or auditing.

Sub-agents return summaries to YOU; only YOU post AgentReports to the Hub.
After integrating their outputs, your own validation step confirms the
combined result before you transition to ready_for_review.

P-VALIDATE and P-JUDGE are SEPARATE primitives. Do NOT fold either into
your own context. P-VALIDATE is mechanical/objective (tests, lint, schema,
hashes); P-JUDGE is qualitative (code review, aesthetic critique, fact
check). At least one of each MUST appear in any non-trivial workflow.

You MUST keep an in-context "subagent ledger" that records, for each
subtask: {role.id, primitive, goal, agent_type, model_or_api, decision
(accepted/rejected/retried), evidence}. Include a compact ledger summary
in your final report's validation field so the human reviewer can audit
the delegation.
```

### 3.1 复杂度联动

`_execution_complexity_assignment_block` 现已经在 complex 时鼓励 delegate；
本方案把它升级为：

| `execution_complexity` | autonomous 行为 |
| --- | --- |
| `simple` | 仍允许直接执行；orchestrator contract 给「精简版」（仅强制走一次 P-JUDGE 子代理做内审，其它可由主 Agent 自己干）。 |
| `auto` (default) | 主 Agent 在第一次 working report 就要声明：将走 orchestrator 模式还是单 agent，并在 goal packet 的 assumptions 里说明理由。Hub 不强行；但如果它自报 orchestrator 模式，则必须满足契约（含 workflow 声明）。 |
| `complex` | **强制** orchestrator 模式：必须声明 workflow + 至少包含一次 P-EXECUTE 与一次 P-JUDGE 的 sub-agent 调用；缺失则视为契约违约（见 §6 风险）。 |

**成本闸门（来自 §9 业界共识）**：sub-agent fan-out 的 token 成本约
**10–15×** 单 agent 基线（Anthropic 多智能体研究系统自报数据；Cognition
亦警告）。`auto` 复杂度做模式自判时，orchestrator 必须按以下三条判据
任选其一站得住才走 orchestrator 模式，否则退化为单 agent：

1. **广度并行**：任务可拆成 ≥3 条互相独立、可并行展开的子线索。
2. **超窗口**：单 agent 单上下文塞不下全部资料 / 历史，需要分包阅读。
3. **可清晰隔离**：子任务边界明确，子代理失误不会污染主对话。

仅靠「让一个 agent 多次自我提问 / 自我纠偏」不构成走 orchestrator 模式的
理由（参见 Cognition *Don't Build Multi-Agents*）。

### 3.2 Primitive → 模型映射（per-CLI 强制，用户不可覆盖）

模型钉死的对象是**角色原语**而非具体角色名，这样图像生成任务里的
`prompt-author` 与编码任务里的 `implementer`（都属于 P-EXECUTE）能共用同
一档质量底线。

claude runtime（`Task` tool 的 `model` 参数 / `.claude/agents/*.md` 的
`model:` frontmatter）：

| Primitive | 默认模型 | 选型理由 |
| --- | --- | --- |
| P-PLAN      | **opus**   | 任务拆解 / workflow 选型 / 风险预判，长链推理。 |
| P-EXECUTE   | **opus**   | 产出物（代码 / prompt / 文稿 / 查询）的质量决定整轮成败。 |
| P-VALIDATE  | sonnet     | 跑测试 / lint / schema 校验，多为机械性产出。 |
| P-JUDGE     | **opus**   | 独立批评要打得动 P-EXECUTE 的产出。 |
| P-INTEGRATE | **opus**   | 合并子产出、解决冲突，需要全局视角。 |
| P-RESEARCH  | sonnet     | 文档摘要、API 调研，有界检索。 |

**P-EXECUTE 的特殊情况**：当一个 P-EXECUTE 子代理只是"调外部 API"
（图像生成、视频生成、TTS、向量检索等），并不消耗 LLM token 做生成，
此时 `model=` 字段填 `external:<api-name>`（例如 `external:t2i.v3`），
不强制 opus。负责把 prompt 喂给 API 的那个 P-EXECUTE 角色（如
prompt-author）才需要 opus。

cursor / codex runtime：sub-agent 模型显式指定能力随版本变化，先要求
orchestrator 把整条 task 跑在父模型下（任务创建时 Hub 会用与 claude
P-EXECUTE 同档的高阶模型），不强行做角色级模型分发；待 spike 验证后
在 V1.1 补齐。

terminal runtime：N/A。

不开 schema、不开前端开关。如果将来需要按任务覆盖，再走 V2
`AutonomyPolicy.model_overrides` 路线，本期不做。

### 3.3 与外部 reviewer / evaluator 的关系

- 外部 AI reviewer（独立 ManagedSession，跑 `_build_review_prompt` /
  `_autonomous_review_block`）**保留不动**。它仍然是「跨 session 的独立质量
  闸」，用来防 orchestrator 自查自评的盲区。
- 内部 P-JUDGE 子代理是 orchestrator 在自己上下文里 spawn 的子 agent，
  作用是 **post 报告之前** 拦截显然不合格的 iteration——降低外部 reviewer
  收到劣质 review-gate 报告的频率，不取代它。对图像类任务，内部 P-JUDGE
  也可以是「美学评审」「prompt 合规检查」这类专业 judge，与外部代码型
  reviewer 并不冲突。
- 这一点和 2026-05-27 设计 §4.2 的「team 内 reviewer 取代外部 reviewer」
  是有意分歧的：V1 提案选择**保留外部 reviewer**，避免改动现有 review 路由。
  是否在 V2 把外部 reviewer 收口到 team，留待团队化方案落地时再决定。

## 4. 命中的代码点（不在本期落地，仅锚定改动面）

下面是要修改的最小点位（V1 全在 backend 的 prompt 构造层）：

| 文件 | 函数 | 改动 |
| --- | --- | --- |
| `backend/claude_hub/services/workspace_manager.py` | `_execution_complexity_assignment_block` | complex 分支文案改为：必须 orchestrate + delegate；提到 sub-agent 工具名按 `agent_type` 给。 |
| 同上 | `_autonomous_assignment_block` | 末尾追加 §3 的「Orchestrator Contract」并按 `session.agent_type` 注入 sub-agent 调用范例。 |
| 同上 | 新增 `_subagent_capability_hint(agent_type)` | 返回 per-CLI 的 sub-agent 调用片段（claude → Task tool 用法；cursor → 其 sub-agent；codex → 其子任务；terminal → 退化说明）。 |
| 同上 | `_build_continue_prompt` | 在 `revising` 阶段插一句：「保持 orchestrator 模式；本轮修复也走 sub-agent；不要把所有修复揉进主对话」。 |
| 同上 | `_autonomous_review_block` | 加一行：「reviewer 应额外检查 orchestrator 是否给出了 subagent ledger，缺失时建议 review_failed 并指出契约违约」。 |
| `backend/tests/test_workspace_*` | 新增 prompt 单测 | 断言新 contract 文案在 autonomous + complex 时存在；simple 时退化版本生效。 |

> **不需要改动**：`models/schemas.py`（无新枚举/字段）、
> `services/workspace_state_policy.py`（状态机不变）、
> `api/workspaces.py`、前端任何文件。

## 5. 观测与审计

V1 不引入新的 schema 字段，但要求 orchestrator 在以下两个位置留痕：

1. **首次 `state=working` 报告**：`message` 写明「orchestrator 模式 / 单 agent
   模式」选择；以 `workflow:` 块声明本任务挑选的 roles + deps + notes（见
   §3 contract 末尾的两个 worked example）。`workflow:` 没有 `template`
   枚举字段——orchestrator 直接从 6 个 primitive 编排，并在 `notes:` 里说明
   角色拆分理由。
2. **`review-gate` 报告**：`validation` 字段（已存在自由文本）末尾追加一段：

   ```
   subagent-ledger:
     - role.id=implementer        primitive=P-EXECUTE
       agent=claude:Task#general-purpose model=opus
       goal=... decision=accepted evidence=changed_files[a.py,b.py]
     - role.id=internal-reviewer  primitive=P-JUDGE
       agent=claude:Task#code-reviewer model=opus
       goal=... decision=requested_minor_fix evidence=...
     - role.id=image-generator    primitive=P-EXECUTE
       agent=claude:Task#general-purpose model=external:t2i.v3
       goal=... decision=accepted evidence=output_uris[s3://...]
     ...
   ```

3. 外部 reviewer (`_autonomous_review_block`) 在评分时，把以下三件事纳入
   `review_passed` 判据：(a) ledger 是否完整且每条挂得上 workflow.roles；
   (b) 子任务是否真正达成 acceptance；(c) **关键 primitive（P-EXECUTE 中
   非 external 的、P-JUDGE、P-INTEGRATE）是否使用了 §3.2 钉死的 opus 模型**。

这套观测在 V1 是**纯文本约定**；如果 V2 走 ManagedSession team 路线，
ledger 自然升级成 `AgentTeam` 模型（见 2026-05-27 §4.1）。

## 6. 风险与未决问题

| # | 风险 / 问题 | 当前建议 |
| --- | --- | --- |
| R1 | Token / 成本翻倍：sub-agent 调用消耗额外配额 | `simple` 降级、`auto` 自判、`complex` 才强制；Hub 文案明确告知用户。 |
| R2 | CLI runtime 不支持 sub-agent（如 plain terminal、老版 cursor） | `_subagent_capability_hint` 在不支持时输出降级说明，orchestrator 在 goal packet `assumptions` 标注；不强行假装。 |
| R3 | Orchestrator 自报 ledger 但伪造 / 走过场 | 外部 reviewer 兜底 + ledger 含 evidence (changed_files / test names)；伪造 ledger 视为高风险，建议 `review_failed`。 |
| R4 | sub-agent 输出不可见，长链路调试难 | claude `Task` 调用结果会出现在主 conversation 中，Hub terminal 仍可见；并要求 ledger 写明每个 subtask 的关键 evidence 字段。 |
| R5 | Sub-agent 改文件，主 Agent 无法 attribute 给具体 role | V1 接受「同一 git 工作区」，attribution 靠 ledger；V2 才上 per-role worktree。 |
| R6 | 非编码任务（图像 / 视频 / 文档 / 数据分析）的角色拆分不合理 | §3 contract 用 2 个 worked example（编码 + 图像生成）做 few-shot 锚点；orchestrator 必须在 `workflow.notes` 里说明拆分理由；外部 reviewer 同步检查 workflow.roles 与产出物匹配性。 |
| R7 | P-EXECUTE 走外部 API（图像生成等）的 evidence 不可机器校验 | ledger 强制带 `output_uris` / `external_api` 字段；外部 reviewer 至少做存在性与可访问性检查；deeper 的内容质量交给同 workflow 内的 P-JUDGE 做。 |
| Q1 | 是否对 `auto` 复杂度也强制契约？ | 默认不强制，由 orchestrator 自判；仅 `complex` 强制。如果运行一段时间发现 `auto` 任务也频繁 confuse，再升级到强制。 |
| Q2 | 内部 R4 reviewer 是否能取代外部 reviewer？ | V1 不取代；V2 团队化方案再讨论。 |
| Q3 | terminal / cursor 的 sub-agent 调用语法是否稳定？ | 提案落地前需要 spike：在最新 cursor / codex CLI 上验证 sub-agent 命令；prompt 里给出版本依赖的备注。 |
| Q4 | 多个 sub-agent 并行 implementer 时的写冲突？ | V1 串行派工（contract 文案要求一次只激活一个 implementer）；V2 才上并行 + worktree 隔离。 |

## 7. Roll-out 路线（建议拆 task）

**Phase 1 (本提案落地后单独立 task)** — Prompt-only V1
- 修改 §4 表中的 5 个函数 + 新增 `_subagent_capability_hint`。
- 增量单测断言新文案在 autonomous + complex 下存在。
- 不动前端、不动 schema、不动 state machine。
- 灰度策略：直接默认开启对 `complex` 任务；`auto` 任务保留 self-judgement。

**Phase 2 — Observability polish**
- 在外部 reviewer 评分提示里**显式**要求检查 subagent-ledger 完整性。
- 可选：在 `AgentReport` 自由文本里识别 `subagent-ledger:` 段，在前端
  task detail 简单折叠展示（仅 UI 层；schema 仍不变）。

**Phase 3 — 与 V2 团队化方案的衔接**
- 当 2026-05-27 提案的 ManagedSession team 落地时，本 V1 的「角色契约」
  与「ledger」直接迁移成 team 内子 session 的 prompt 模板与
  `AgentTeam.member_session_ids` 元数据。
- 此时把外部 reviewer / 内部 reviewer 的关系再统一回顾。

## 8. 验收条件（与 Goal Packet 对齐）

- [x] 本文档落到 `docs/working-logs/2026-06-01-auto-mode-cli-subagent-orchestration.md`
- [x] §1 / §3 / §4 明确定位现有 Auto mode prompt 链路与最小改动点位
- [x] §3 给出 6 类 role primitives（P-PLAN/P-EXECUTE/P-VALIDATE/P-JUDGE/P-INTEGRATE/P-RESEARCH）+ 2 个 worked example（编码线性 / 图像生成带反馈环 + 外部 API）+ per-CLI runtime 的调用建议与降级策略；不预设领域模板枚举
- [x] §5 给出无 schema 变更的观测与审计方案（workflow 声明 + subagent ledger 文本约定，含 model / external API 字段）
- [x] §1.3 / §3.3 / §7 阐明与 2026-05-27 团队化方案的 V1 / V2 / V3 关系
- [x] §3.2 给出按 primitive 钉死的模型映射（P-PLAN/P-EXECUTE/P-JUDGE/P-INTEGRATE = opus；P-VALIDATE/P-RESEARCH = sonnet；P-EXECUTE 走外部 API 时填 external:），用户不可覆盖
- [x] §9 给出业界 7 个主流多 Agent 框架的对比 + 八条共识 + 落到本提案的 5 条修正建议（含派工 envelope 标准化、final-only 默认回程、P-VALIDATE/P-JUDGE 不可合并、CrewAI tuple 借鉴）
- [x] §6 列出风险与开放问题；§7 给出阶段性路线

## 9. 业界 Multi-Agent 框架对比

### 9.1 横向扫描

下表是对主流多 Agent 框架就「角色 / 工作流 / 派工 / 模型 / 验证」五个维度
的对比（细节出处见 §10 参考）：

| 框架 | 角色形态 | 工作流声明 | 派工协议 | 角色级模型 | 验证 / 评审 |
| --- | --- | --- | --- | --- | --- |
| **Anthropic Building Effective Agents + 多智能体研究系统** | 单一 Agent 原语 + 角色 prompt | "workflows vs agents" 两分；workflow 在代码里固化 | 显式 task spec（objective / format / tools / bounds），子代理只回**最终答案** | **显式分层**：Opus 做 lead，Sonnet 做并行 worker | evaluator-optimizer 是独立 workflow；LLM-as-judge + 人工 |
| **OpenAI Agents SDK / Swarm** | 单一 Agent 原语 | 涌现式（用代码组合 handoff） | 两种：handoff 转控制权；agent-as-tool 返回值 | 每个 Agent 一个 `model`，run 时可 `model_override` | **Guardrails** 一等公民（input/output 校验） |
| **Microsoft AutoGen v0.4** | 单一 Agent + Team 容器 | 部分声明（团队类）+ 涌现选 turn | `AgentTool` 包装子代理；GroupChat 广播 | 每个 Agent 一个 `model_client` | termination conditions + 可选 critic |
| **LangGraph supervisor** | 图节点（路由 = 一类节点） | **完全声明**：图结构在代码里 | `Command(goto=..., graph=parent)`；可选只回最终消息 | 每节点独立 model + role prompt | 通常加一个 critic 节点 + 条件边 |
| **CrewAI** | 固定 tuple：role + goal + backstory + llm + tools | Crew + Tasks + Process（sequential / hierarchical） | Task 对象；hierarchical 时 manager 调子代理并验证 | 每个 Agent 一个 `llm` | hierarchical manager 自带 validate（有争议） |
| **MetaGPT** | **固定** 软件团队角色（PM / Architect / Engineer / QA） | 硬编码 SOP | shared message pool；按消息类型订阅 | 角色配置文件支持每角色 LLM | QaEngineer 是独立验证角色 |
| **Cognition / Devin** | 单条主线 + 偶尔派出**有界**子代理 | 不主张 multi-agent | — | — | "Don't Build Multi-Agents"：上下文碎片化是主要敌人 |

### 9.2 跨框架共识（八条）

1. **可组合的角色原语优于固定 taxonomy**——只有 MetaGPT 锁死编码角色；
   其他全部走「单一 Agent 原语 + role prompt」。我们的 6 个 primitive 与
   主流一致，避免了 MetaGPT 那种**只能做软件项目**的死路。
2. **声明式 workflow 是生产主流**：LangGraph / CrewAI / Anthropic / MetaGPT
   都把结构放在 LLM 之外固化下来；纯涌现式编排（Swarm 风格）只用于开放
   性任务。我们提案要求 orchestrator 在第一条 working report 里**声明
   workflow**，方向正确。
3. **角色级模型 pinning 是行业标准，不是奇技**：Anthropic 研究系统就是
   Opus lead + Sonnet worker；CrewAI / Swarm / AutoGen / LangGraph 全都按
   Agent 配 model。我们按 primitive 钉死的方案与 Anthropic 一致。
4. **派工信封要结构化，不要自由消息**：Anthropic task spec、LangGraph
   `Command`、Swarm `Result`、CrewAI Task 都是结构化对象。自由文本派工
   会漂移。
5. **回程契约比派工契约更关键**：LangGraph 与 Anthropic 都警告——把子代理
   完整 transcript 回吐给 orchestrator 会**烧爆**上下文预算。**默认只回
   最终答案 / 结构化 artifact**，需要时才 opt-in 回完整历史。
6. **验证通常是独立角色**：evaluator-optimizer (Anthropic) / Guardrails
   (OpenAI) / QaEngineer (MetaGPT) / Generator-Critic (multi-agent.wiki)
   三条独立线索都把 critique 与 orchestrator 拆开。CrewAI 的 hierarchical
   manager 把 validate 揉进 manager 是少数派且有争议。
7. **多 Agent 有 10–15× token 成本倍数**（Anthropic 研究系统自报；
   Cognition 也警告）。仅在 (a) 任务可广度并行 / (b) 单窗口装不下 /
   (c) 子任务可清晰隔离 时才用——这是 §3.1 「complexity 联动」要照抄的判据。
8. **工具 / handoff description 是最高杠杆 prompt 表面**——Anthropic
   团队仅靠改 MCP 工具描述就把子代理任务时间砍 40%。映射到我们提案：
   `_subagent_capability_hint` 与 `.claude/agents/*.md` 的
   `description:` 是最值得反复打磨的字段。

### 9.3 对本提案的修正建议（已并入正文）

下面 5 条直接落进了 §3 / §5（如有）：

1. **借 CrewAI 的 role/goal/backstory tuple 作为 primitive 的 canonical 形态**，
   并映射到 Claude Code 的 `.claude/agents/*.md` frontmatter（`name` /
   `description` / `tools` / `model`）。这样用户写自定义 primitive 不需要
   学新格式，框架原生承载。→ 落入 §3 contract 的 sub-agent 描述。
2. **standardize 派工信封**：每个 subtask brief 用同一结构 `{objective,
   success_criteria, inputs, output_schema, tools_allowed, context_budget}`，
   无论 Claude `Task` 还是 cursor sub-agent。→ 修订 §3 contract 文本。
3. **回程默认 final-only**：sub-agent 默认只回「最终答案 + artifact 路径」，
   要完整 transcript 必须显式 opt-in。→ §3 contract 写明默认。
4. **保留 P-VALIDATE 与 P-JUDGE 各自独立、不可合并**：呼应三条独立工业
   传统；明确写「禁止把 validate/judge 合并到 orchestrator 自身」。→ §3 与 §5。
5. **§3.1 cost-gating 文案显式标注 10–15× 成本倍数**，并把「广度并行 /
   超窗口 / 子任务可隔离」三条判据抄进 simple/auto 分支的提示文本。→ §3.1。

下面是把 (1)(2)(3)(4) 落到 §3 contract 文本里的修订片段（粘贴用，作为
未来 Phase 1 实现的一部分）：

```
For each subtask you dispatch, hand the sub-agent a structured envelope
(SAME schema across claude / cursor / codex):

  [subtask-envelope]
  role.id: <as declared in workflow.roles>
  primitive: <P-PLAN|P-EXECUTE|P-VALIDATE|P-JUDGE|P-INTEGRATE|P-RESEARCH>
  objective: <one sentence; this is your contract with the sub-agent>
  success_criteria: <bullet list, must map onto goal_packet.acceptance>
  inputs: <files / links / prior artifacts>
  output_schema: <what the sub-agent must return — patch summary,
                  prompt string, image URI, lint report, ...>
  tools_allowed: <whitelist; deny everything else>
  context_budget: <approximate token / step budget>
  return_mode: final-only        # default; ask for `full-transcript`
                                 # only when debugging or auditing.

Validation (P-VALIDATE) and Judgment (P-JUDGE) are SEPARATE primitives.
Do NOT fold either into the orchestrator's own context. P-VALIDATE is
mechanical/objective (tests, lint, schema, hashes); P-JUDGE is qualitative
(code review, aesthetic critique, fact check). At least one of each must
appear in any non-trivial workflow.
```

这块在 Phase 1 落地时应直接覆盖 §3 contract 中的 `[subtask]` 段；为避免
本期反复修订，目前先以 §9.3 的形式锚定。

## 10. 参考

### 内部
- `backend/claude_hub/services/workspace_manager.py`
  - `_build_task_assignment_prompt`、`_execution_complexity_assignment_block`、
    `_autonomous_assignment_block`、`_autonomous_review_block`、
    `_build_continue_prompt`
- `backend/claude_hub/services/workspace_state_policy.py`（不动，仅引用）
- `docs/working-logs/2026-05-26-autonomous-mode-v1.md`（autonomous V1 系统综述）
- `docs/working-logs/2026-05-27-auto-mode-team-design.md`（V2 团队化方案，本提案的下一程）

### 外部 Multi-Agent 框架（§9 调研来源）
- Anthropic — *Building Effective Agents* / 多智能体研究系统
  （via <https://simonwillison.net/2025/Jun/14/multi-agent-research-system/>）
- OpenAI Agents Python SDK — <https://openai.github.io/openai-agents-python/>
- OpenAI Swarm — <https://github.com/openai/swarm>
- Microsoft AutoGen — <https://github.com/microsoft/autogen>
- LangGraph supervisor — <https://github.com/langchain-ai/langgraph-supervisor-py>
- CrewAI — <https://github.com/crewAIInc/crewAI>
- MetaGPT — <https://github.com/geekan/MetaGPT>
- Cognition / Devin — *Don't Build Multi-Agents* (via Latent Space)
  <https://www.latent.space/p/cognition>
- multi-agent.wiki — <https://multi-agent.wiki/>
  （Supervisor / Generator-Critic / Refinement Loop / Clarification-at-edge /
   HITL / Blackboard）
