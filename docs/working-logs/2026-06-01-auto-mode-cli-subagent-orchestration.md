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
You must NOT do bulk implementation, testing, or reviewing in your own
context. Instead, decompose the task into bounded subtasks and delegate them
to sub-agents using your runtime's native sub-agent capability:

- claude  → use the Task tool with appropriate subagent_type
            (general-purpose / Explore / Plan / code-reviewer / ...).
- cursor  → use cursor's sub-agent / spawn capability for implementer and
            reviewer subtasks; YOLO is on by default for cursor.
- codex   → use codex's subtask / fan-out capability.
- terminal/other → no native sub-agents available; degrade to single-agent
                    execution and document the degradation in your goal
                    packet's assumptions.

Mandatory roles for a non-trivial autonomous task (you allocate, you can
re-use one sub-agent across phases, but the role boundaries must hold):

  R1. planner       — derive goal packet, decompose, decide subtask graph.
  R2. implementer   — write/modify code, narrow blast radius per subtask.
  R3. tester        — write/run tests, validate behavior, capture evidence.
  R4. internal      — independent code-review pass against goal packet
       reviewer    acceptance criteria, BEFORE you post review-gate report.
  R5. researcher    — (optional) external docs / API lookup when policy
                     allows web research.

For each subtask you dispatch, hand the sub-agent a brief of the form:

  [subtask]
  role: <R1..R5>
  goal: <one sentence>
  inputs: <files/links/prior artifacts>
  expected_artifacts: <patch summary / test names / review notes / ...>
  acceptance: <which goal_packet.acceptance_criteria items it must satisfy>
  report-back: <return a short structured summary; do NOT post Hub reports>

Sub-agents return summaries to YOU; only YOU post AgentReports to the Hub.
After integrating their outputs, your own validation step confirms the
combined result before you transition to ready_for_review.

You MUST keep an in-context "subagent ledger": for each subtask record
{role, goal, agent_type, decision (accepted/rejected/retried), evidence}.
Include a compact ledger summary in your final report's validation field
or message body so the human reviewer can audit delegation.
```

### 3.1 复杂度联动

`_execution_complexity_assignment_block` 现已经在 complex 时鼓励 delegate；
本方案把它升级为：

| `execution_complexity` | autonomous 行为 |
| --- | --- |
| `simple` | 仍允许直接执行；orchestrator contract 给「精简版」（仅强制 R4 internal reviewer 走一次 sub-agent，其它可由主 Agent 自己干）。 |
| `auto` (default) | 主 Agent 在第一次 working report 就要声明：将走 orchestrator 模式还是单 agent，并在 goal packet 的 assumptions 里说明理由。Hub 不强行；但如果它自报 orchestrator 模式，则必须满足契约。 |
| `complex` | **强制** orchestrator 模式，至少 R2 implementer + R4 internal reviewer 各一次 sub-agent 调用；缺失则视为契约违约（见 §6 风险）。 |

### 3.2 与外部 reviewer / evaluator 的关系

- 外部 AI reviewer（独立 ManagedSession，跑 `_build_review_prompt` /
  `_autonomous_review_block`）**保留不动**。它仍然是「跨 session 的独立质量
  闸」，用来防 orchestrator 自查自评的盲区。
- 内部 reviewer (R4) 是 orchestrator 在自己上下文里 spawn 的子 agent，
  作用是 **post 报告之前** 拦截显然不合格的 iteration——降低外部 reviewer
  收到劣质 review-gate 报告的频率，不取代它。
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
   模式」选择，并预告 subtask 切片（高层 bullet）。
2. **`review-gate` 报告**：`validation` 字段（已存在自由文本）末尾追加一段：

   ```
   subagent-ledger:
     - role=R2 implementer agent=claude:Task#general-purpose
       goal=... decision=accepted evidence=changed_files[a.py,b.py]
     - role=R4 internal-reviewer agent=claude:Task#code-reviewer
       goal=... decision=requested_minor_fix evidence=...
     ...
   ```

3. 外部 reviewer (`_autonomous_review_block`) 在评分时，把「ledger 是否
   完整 + 子任务是否真正达成 acceptance 子集」纳入 review_passed 判据。

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
- [x] §3 给出 5 类 sub-agent 角色 + per-CLI runtime 的调用建议与降级策略
- [x] §5 给出无 schema 变更的观测与审计方案（subagent ledger 文本约定）
- [x] §1.3 / §3.2 / §7 阐明与 2026-05-27 团队化方案的 V1 / V2 / V3 关系
- [x] §6 列出风险与开放问题；§7 给出阶段性路线

## 9. 参考

- `backend/claude_hub/services/workspace_manager.py`
  - `_build_task_assignment_prompt`、`_execution_complexity_assignment_block`、
    `_autonomous_assignment_block`、`_autonomous_review_block`、
    `_build_continue_prompt`
- `backend/claude_hub/services/workspace_state_policy.py`（不动，仅引用）
- `docs/working-logs/2026-05-26-autonomous-mode-v1.md`（autonomous V1 系统综述）
- `docs/working-logs/2026-05-27-auto-mode-team-design.md`（V2 团队化方案，本提案的下一程）
- multi-agent.wiki：Supervisor / Generator-Critic / Refinement Loop /
  Clarification-at-edge / HITL / Blackboard
