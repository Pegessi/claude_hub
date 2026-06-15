# seedclaw-v1 任务全生命周期 Prompt 构建与状态转换机分析

> 日期：2026-06-12
> 分析对象：`https://code.byted.org/seed/seedclaw-v1`（repo id 997582，默认分支 `main`）
> 分析工具：bytedcli `codebase` 域（认证账号 wangzehua.ict）
> 对照项目：本仓库 claude_hub（Python / FastAPI + tmux/ttyd workspace 会话）
> 范围：本报告只做只读分析与对照，不修改任一项目代码。

本报告聚焦用户提出的三个问题：

1. seedclaw-v1 的 **task 全生命周期相关 prompt 是如何构建的**；
2. 它有没有**规范（约定）**约束这些 prompt；
3. 它的 **task 状态转换机**是怎么做的。

并在每一节末尾与 claude_hub 做重合点 / 差异点对照（"我们的项目和他的主要功能是重合的"）。

---

## 0. 两个项目的总体定位

| 维度 | seedclaw-v1 | claude_hub |
| --- | --- | --- |
| 后端栈 | NestJS 11 + TypeScript + Bun + PostgreSQL/Drizzle + Zod | Python + FastAPI |
| Agent 运行时 | pi-agent-core + Claude Anthropic API + MCP，ByteFaaS Sandbox runner | tmux / ttyd 会话，多 CLI 运行时（CLAUDE/CURSOR/CODEX/terminal） |
| 任务编排单元 | `work_items`（看板卡）+ `worker_task_runs`（运行）+ daemon | `WorkspaceTask` + `ManagedSession`（worker/orchestrator/reviewer/dispatcher 角色） |
| 仓库形态 | monorepo：`packages/`（server、daemon、prompt-engine、protocol、web） | 单体后端 `backend/claude_hub/`，workspace 逻辑集中在 `services/workspace_manager/` |

两者都属于「人给出任务 → AI agent 执行 → 产出经审阅 → 收尾」的 task 编排系统，所以核心功能高度重合。下面逐项拆解。

---

## 1. seedclaw-v1 的 task 全生命周期 prompt 构建

seedclaw-v1 把"对话型 system prompt"和"worker 任务执行 prompt"两套体系分开：

- **对话型分层 system prompt**：`assembleSystemPrompt`（L0–L8 分层），用于交互式聊天会话。
- **worker 任务子系统**（与 claude_hub 真正重合的部分）：
  - `buildWorkerTaskPrompt`——拼装传给 `runAgent()` 的 **user message**；
  - `renderWorkerPrompt`——worker 的 **system-prompt-override profile**；
  - daemon 侧 `buildClaudeWorkerSystemPrompt`（`claude-worker-system-prompt.ts`）——CLI 落地时注入的 system prompt。

### 1.1 `buildWorkerTaskPrompt`：纯函数、固定段落顺序

`packages/server/services/worker-task-context.ts:88` 的 `buildWorkerTaskPrompt` 是 worker 任务 prompt 的核心装配器。它的关键特征：

- **纯函数、零副作用**：文件头注释明确写 "No DB access, no side effects. All data passed as parameters."（`worker-task-context.ts:2-3`）。所有数据由参数传入，prompt 装配与数据获取彻底解耦。
- **固定段落顺序**：通过 `const sections: string[] = []` 然后按确定顺序 `push`，最后 `sections.join('\n\n')`（`worker-task-context.ts:115-171`）。顺序为：
  1. `buildWorkItemSection(workItem)`——任务上下文（标题 / 描述 / 优先级等）
  2. `buildProjectContextSection`（可选）——项目上下文
  3. `buildLinkedWorkItemsSection`（可选，仅当有 linked items）
  4. `buildTriggerSection(triggerType, triggerComment)`——触发原因（`on_comment` / `on_mention` 等）
  5. `buildExecutionGuidanceSection()`——执行指引
  6. `buildIssueDocumentGuidanceSection`——issue 文档指引
  7. recent comments（仅 `on_comment` / `on_mention` 触发且有评论时）
  8. worker agent `## Instructions`（可选）
  9. skills 段（优先 `resolvedSkills` 全文，退化到 `skills` 名称列表）
  10. `buildGitCoAuthorSection`（可选）
- **段落构建器外置**：实际 section 文本由 `worker-task-context/sections.ts` 提供，本文件只做"编排 + barrel（re-export）"，以保持 ≤500 行。

### 1.2 prompt-slim #39：prompt 只承载"运行时无法得知的环境事实"

`buildWorkerTaskPrompt` 的签名里保留了 `provider?` 和 `htmlReportPolicy?` 两个参数，但注释明确写它们**只为调用点稳定性保留，不再产出 provider 特定的 prompt 段落**：

- `provider?: WorkerTaskProvider;` — "Accepted for call-site stability; no provider-specific prompt sections remain (prompt-slim #39)."（`worker-task-context.ts:91`）
- `htmlReportPolicy?` — "Accepted for call-site stability; the policy gates the hidden report utility, not this prompt."（`worker-task-context.ts:98`）

这就是 seedclaw-v1 的 **prompt-slim #39 约定**：prompt 里只写运行时自己推断不出来的环境事实，凡是运行时能自行知道 / 自行决定的，都不进 prompt。daemon 侧的 `buildClaudeWorkerSystemPrompt` 同理——只注入环境事实（工作目录、运行时身份等），不灌方法论。

### 1.3 worker 任务的"门"通过续跑 prompt 解决

当一个 run 没有跑到 `completed`、而是停在某个"门"上时，seedclaw-v1 不是重新拼一段完整 prompt，而是用 `--resume <priorSessionId>` + 一段 prompt override 把用户的回复喂回原会话：

- **Plan 门**：`handlePlanSubmittedCompletion`（`task-plan-submitted.ts:47`）——CLI 以 `ExitPlanMode` 结束时，run 转 `awaiting_plan_review`，并写一条 `type: 'plan'` 的看板评论；用户在看板回复后通过 resume 续跑。
- **User-input 门**：`handleUserInputSubmittedCompletion`（`task-user-input-submitted.ts:65`）——CLI 以 `AskUserQuestion` 结束时，run 转 `awaiting_user_input`，写 `type: 'question'` 评论（`summarizeQuestionJson` 把问题 JSON 摘要成人类可读文本，`task-user-input-submitted.ts:31`）。

### 1.4 与 claude_hub 的对照（prompt 构建）

claude_hub 的 prompt 构建集中在 `backend/claude_hub/services/workspace_manager/_prompts.py` 的 `_PromptsMixin`：

**重合点：**
- 都用"固定顺序拼接 block"的方式装配任务 prompt。claude_hub 的 `_build_task_assignment_prompt`（`_prompts.py`）也按固定顺序拼接：header → workspace/task 元数据 → 会话环境行 → 状态快照 → 派发原因 → clear_note → 任务描述 → 附件说明 → lesson context → 复杂度块 → 自治块 → Goal Packet 指引 → 上报状态指引 → curl 示例。
- 都有 reviewer 角色的独立 bootstrap prompt 和 review prompt。
- 都把"门 / 续跑"作为一等公民：claude_hub 用 `_build_continue_prompt` 从 review 反馈续跑，seedclaw-v1 用 `--resume` + override。

**差异点（关键）：**
- **prompt 信息密度哲学相反**。seedclaw-v1 遵循 prompt-slim #39，prompt 里只放运行时无法得知的环境事实，方法论尽量不进 prompt；claude_hub 反过来——在 prompt 里携带**大量方法论和契约**：`_execution_complexity_assignment_block`（simple/complex/auto 指引 + orchestrator cost_guard）、`_orchestrator_contract_block`（角色原语 P-PLAN/P-EXECUTE/P-VALIDATE/P-JUDGE/P-INTEGRATE/P-RESEARCH、subtask envelope schema、subagent-ledger schema、两个 worked example）、`_subagent_capability_hint` + `_model_evidence_contract_block`（按运行时区分子 agent 调用方式与模型钉选，Claude 侧 P-PLAN/P-EXECUTE/P-JUDGE/P-INTEGRATE→opus、P-VALIDATE/P-RESEARCH→sonnet）。
- **lesson 注入**：claude_hub 会从 feedback store 注入历史经验（`lesson_context`），seedclaw-v1 worker prompt 无此机制。
- 这条差异是两个项目最本质的取向分歧：seedclaw-v1 把"怎么做"留给运行时本身，claude_hub 把"怎么做"显式编码进 prompt 以获得跨运行时的一致行为。

---

## 2. seedclaw-v1 的规范（约定）

从代码注释和结构能提炼出 seedclaw-v1 几条明确的工程规范：

1. **R7：单文件 ≤500 行**。`worker-task-context.ts:8` 注释 "Kept ≤ 500 lines for R7 compliance."；`task-plan-submitted.ts:6-8` 说明该文件是"从 `DaemonApiController#taskCompleted` 抽出来以让 controller 文件保持在 R7（500 行）以下"。这是硬性约束，并直接驱动了"装配器 + 外置 section 构建器"的拆分方式。
2. **prompt-slim #39**：prompt 只承载运行时无法得知的环境事实（见 §1.2）。
3. **纯函数构建器**：prompt 装配（`buildWorkerTaskPrompt`）和生命周期转换（`work-item-lifecycle.ts` 的多数函数）都是纯函数、无 DB 访问，副作用集中在边界。例如 `onWorkerTaskDispatchedSync`（`work-item-lifecycle.ts:174`）注释明确 "Pure function (no DB access)."
4. **单一真相源的展示状态**（charter §3.5）：UI 的展示态由 `deriveTaskDisplay()` 从底层状态投影出来，不让多处各自维护展示态。
5. **receipt 语言 / never-build #5**：面向用户的失败评论要把引擎和 dispatch-protocol 词汇收敛成"回执"语言（`work-item-lifecycle.ts:359-361` "engine and dispatch-protocol vocabulary collapses to receipt language (never-build #5)"），原始文本留在 run 的 `error` 列。
6. **Run-truth #40**：结论的唯一真相源是 `task_runs.result`，唯一渲染面是 run row，退役了 `type='result'` 的镜像评论（`work-item-lifecycle.ts:247-254`）。
7. **charter 文档治理**：规范条目以编号形式（#39、#40、#52、R7、R20、charter §1.4 / §3.5 等）沉淀在 `/docs` charter 里，代码注释直接引用编号，形成"注释 ↔ charter"双向可追溯。

### 2.1 与 claude_hub 的对照（规范）

**重合点：**
- 都强调**纯函数状态策略**：seedclaw-v1 的 `work-item-lifecycle.ts`、claude_hub 的 `workspace_state_policy.py`（"Pure-function module, no I/O"）目标一致——把状态映射 / 判定逻辑抽成无副作用纯函数，便于测试和复用。`workspace_state_policy.py` 里 `compute_reviewer_verdict_task_update` 的注释甚至明说它是为了"replace inline copies that were duplicated across the WorkspaceManager mixins"，与 seedclaw-v1 抽纯函数去重的动机一致。
- 都做**双语 / 用户友好回执**：claude_hub 报告带 `message_en` / `message_zh`（+ 兼容旧 `message`），seedclaw-v1 有 receipt 语言收敛。

**差异点（关键）：**
- **claude_hub 不强制 R7 500 行上限**。实测 workspace_manager 各模块行数：`_reports.py` = 1002、`_dispatch.py` = 817、`_prompts.py` = 1070、`_task_updates.py` = 320、`_review.py` = 297、`_state.py` = 93、`_tasks.py` = 152。其中 `_reports.py`、`_dispatch.py`、`_prompts.py` 都远超 500 行。这是两项目最显眼的约定差异：seedclaw-v1 用 500 行硬上限逼出细粒度文件拆分，claude_hub 用 mixin 按职责拆分但允许单文件很大。
- seedclaw-v1 的规范以编号 charter 条目为锚（#39 / R7 等）；claude_hub 的"规范"更多以代码内长注释块 + dataclass 契约的形式存在，没有等价的编号 charter 索引。

---

## 3. seedclaw-v1 的 task 状态转换机

seedclaw-v1 的状态机分两层，再投影成 UI 展示态：

### 3.1 两层底层状态

- **`worker_task_runs.status`（运行生命周期）**：
  `queued` →（派发，CAS 写）→ `running` → 终态之一：
  - `completed`（正常完成）
  - `failed`（`failServerBrainTask`，`server-brain-task-state.ts:7` 写 `failed` + `completed_at` + `error`）
  - `awaiting_plan_review`（`handlePlanSubmittedCompletion`，遇 ExitPlanMode）
  - `awaiting_user_input`（`handleUserInputSubmittedCompletion`，遇 AskUserQuestion）
  - `cancelled`
  - 运行时不可用时回退：`deferServerBrainTaskUntilRuntimeReconnect`（`server-brain-task-state.ts:35`）把 run 重置回 `queued`（清空 `runtime_device_id` / `dispatched_at` / `started_at` 等），并 `markDispatchAttempt` 记派发次数，等待 daemon 重连后 redispatch。

- **`work_items.status`（看板态）**：`backlog` / `todo` / `in_progress` / `in_review` / `done`。由生命周期 hook 驱动：
  - **派发时**：`onWorkerTaskDispatchedSync`（`work-item-lifecycle.ts:174`）——若当前状态在 `AUTO_PROGRESS_STATUSES = { backlog, todo, in_review, done }`（`work-item-lifecycle.ts:161`）中，则自动转 `in_progress`，让看板不再显示为"等待人工"。
  - **成功完成**：`onTaskCompleted`（`work-item-lifecycle.ts:216`）——直接转 `done`（满足 `shouldAutoTransitionOnTerminal` 时）。**Lifecycle flip（2026-06-10）**：完成不再停在 `in_review`，因为"acceptance 是 tracker 官僚主义"，结论已经通过 stream done-row / 结果评论 / 回执暴露，人不满意就继续评论，评论会触发续跑 run 并经 `onWorkerTaskDispatchedSync` 重新激活卡片（`work-item-lifecycle.ts:201-214` 注释）。
  - **失败**：`onTaskFailed`（`work-item-lifecycle.ts:331`）——转 `in_review`（**不**转 done）。注释明确"失败的 run 是未完成的工作，不是已交付的结果"（`work-item-lifecycle.ts:320-329`）；`in_review` + 最近 run failed 经 `deriveTaskDisplay` 规则 6（优先于规则 7）派生为 `blocked`，留在"需要你"货架上并带修复动作。

### 3.2 转换的护栏

- **`shouldAutoTransitionOnTerminal`**（`work-item-lifecycle.ts:43`）：终态自动转换（completed→done、failed→in_review）只对 `status === 'in_progress'` 的活动卡片生效；且仅当结束的 agent 仍拥有该卡片（或卡片未分配 / 人工分配——@mention 触发的 run 也要交还终态）。这是防止过期 / 抢占写的关键判定。
- **隐藏 utility 任务跳过**：`isHiddenLifecycleTask`（`work-item-lifecycle.ts:26`）让隐藏 utility 触发的 run 跳过状态转换和进度评论。
- **CAS 风格写**：派发与状态写用 compare-and-swap 思路防竞态（运行态层）。

### 3.3 UI 投影：`deriveTaskDisplay()`

底层两层状态（`worker_task_runs.status` + `work_items.status` + 最近 run 状态）由 `deriveTaskDisplay()` 投影成一个 7 态的 UI 模型（含 `blocked` 等派生态，规则有优先级，如"规则 6 beats 规则 7"）。这对应规范 §3.5"展示态单一真相源"——UI 不自己维护状态，只消费投影结果。

### 3.4 与 claude_hub 的对照（状态机）

claude_hub 的状态机策略集中在 `backend/claude_hub/services/workspace_state_policy.py`（纯函数）+ 状态机执行在 `services/workspace_manager/_reports.py`，枚举在 `models/schemas.py`。

claude_hub 的核心状态词汇（`schemas.py`）：
- `WorkspaceTaskStatus`（`schemas.py:42`）：`todo` / `queued` / `working` / `review` / `done`（看板态，对应 seedclaw-v1 的 `work_items.status`）
- `ManagedSessionStatus`（`schemas.py:111`）：`spawning` / `working` / `idle` / `needs_input` / `done` / `stopped` / `error`（会话运行态，对应 seedclaw-v1 的 `worker_task_runs.status`）
- `AgentReportState`（`schemas.py:123`）：`started` / `working` / `blocked` / `needs_input` / `ready_for_review` / `completed` / `review_started` / `review_passed` / `review_failed` / `review_needs_input`（上报态，驱动状态机的输入）
- `AutonomousRunPhase`（`schemas.py:84`）：`intake` / `rubric_research` / `planning` / `dispatching` / `working` / `evaluating` / `revising` / `waiting_for_human` / `passed` / `failed` / `exhausted` / `cancelled`（自治模式相位）

**重合点：**
- **都把状态机判定抽成纯函数策略层**。claude_hub 的 `managed_status_from_runtime` / `managed_status_from_report` / `task_status_from_report` 等映射函数，对应 seedclaw-v1 的 `onWorkerTaskDispatchedSync` / `onTaskCompleted` 决策核心。
- **都有"派发时把非活动卡片拉回活动态"的规则**。seedclaw-v1 是 `onWorkerTaskDispatchedSync`（→ `in_progress`），claude_hub 在派发 / 续跑路径里同样会把任务状态写回 `working`。
- **都有 review 门 + 终态护栏**。claude_hub 的护栏比 seedclaw-v1 更复杂：`_reviewer_verdict_actionable` / `reviewer_verdict_still_authoritative` / `review_verdict_terminal` 等一组 CAS / 幂等判定（`_reports.py:37` 及 `workspace_state_policy.py`），专门防"过期 / 重复的 reviewer verdict 误写"——这与 seedclaw-v1 `shouldAutoTransitionOnTerminal` 的"只让仍拥有卡片的 agent 写终态"动机同源，但 claude_hub 因为引入了独立 reviewer 角色和 Goal Packet 审批门，竞态面更大，护栏也更厚（`_reports.py:152-298` 一大段注释专门讲过期 reviewer verdict / 迟到 orchestrator 报告的抑制）。
- **失败 / 完成走不同终态**与 seedclaw-v1 一致：claude_hub 的 `_mark_task_review_skipped`（→ `REVIEW` + human acceptance）、`_request_task_review`（→ `REVIEW` + 派 reviewer），失败则保留在需人工的态。

**差异点（关键）：**
- **门的解决方式不同**。seedclaw-v1 用 `worker_task_runs` 续跑 run（`--resume`）解决 plan / user-input 门；claude_hub 用**上报驱动的状态转换 + continue prompt**（`_build_continue_prompt`）解决，门的种类也不同——claude_hub 的核心门是 **Goal Packet 审批门**（reviewed 模式下第一个 Goal Packet 报告即审批门，须等 review_passed 才能进实现阶段，见 `_reports.py:510` `_is_goal_packet_approval_review`）。
- **claude_hub 多一层自治模式（Autonomous）相位机**。`AutonomousRunPhase` 12 态 + `autonomous_phase_after_worker_report` / `autonomous_decision_from_review_state` / `autonomous_phase_from_evaluation_decision`（`_reports.py:632-747`）构成一个独立的 evaluator 驱动迭代循环（intake→…→evaluating→revising→passed/failed/exhausted），seedclaw-v1 无对应物。
- **claude_hub 有独立 reviewer 角色 + 显式 review verdict 态**（`review_passed` / `review_failed` / `review_needs_input`）；seedclaw-v1 的"review"更轻——成功直接 done（lifecycle flip 后不再停 in_review），人工不满意靠继续评论触发续跑，没有独立 reviewer agent 角色。
- **claude_hub 有 auto-continue 分类器**（`auto_continue_interruption_reason` / `auto_continue_completion_reason` / `auto_continue_output_looks_busy`，基于输出文本模式），用于在 tmux 会话里判断是否自动续跑；seedclaw-v1 因为是 daemon + CLI 结构化回调（ExitPlanMode / AskUserQuestion 是结构化信号），不需要这种基于输出文本的启发式分类。

---

## 4. 结论：重合与差异速览

**高度重合（同类系统的共同骨架）：**
1. 任务 prompt 都用"固定顺序拼接 block / section"装配。
2. 状态机判定都抽成纯函数策略层（`work-item-lifecycle.ts` ↔ `workspace_state_policy.py`）。
3. 派发时都把非活动卡片拉回活动态。
4. 完成 / 失败走不同终态，失败留在"需人工"侧。
5. 都有终态写护栏（防过期 / 抢占写）。
6. 都做用户友好 / 双语回执。

**本质差异（设计取向分歧）：**
1. **Prompt 哲学相反**：seedclaw-v1 prompt-slim #39（只放环境事实，方法论留给运行时）vs claude_hub 在 prompt 里重度编码方法论与契约（orchestrator 契约、角色原语、模型钉选、lesson 注入）。
2. **文件规范**：seedclaw-v1 强制 R7 ≤500 行硬上限并以编号 charter 治理；claude_hub 不设行上限，靠 mixin 职责拆分 + 代码内长注释。
3. **门 / 续跑机制**：seedclaw-v1 用 `--resume` 续跑 run + 结构化 CLI 信号（ExitPlanMode/AskUserQuestion）；claude_hub 用上报驱动状态转换 + continue prompt，核心门是 Goal Packet 审批门，并额外有 auto-continue 文本启发式。
4. **审阅与自治**：claude_hub 有独立 reviewer 角色、显式 review verdict 态、Goal Packet 审批门、以及一整套 Autonomous 相位机；seedclaw-v1 审阅更轻（成功直接 done，靠继续评论迭代），无自治相位机。

**对 claude_hub 的可借鉴点（仅作建议，不在本次改动范围）：**
- seedclaw-v1 的 prompt-slim #39 提示：claude_hub 的 prompt 已偏重，可评估哪些方法论块是运行时本可自知的、可瘦身的。
- seedclaw-v1 的 R7 行上限 + 装配器/section 外置拆分模式，对 claude_hub 已超千行的 `_prompts.py` / `_reports.py` 是一个可参考的拆分范式。

---

## 5. 文件编码规范借鉴（follow-up，2026-06-13）

> 跟进诉求："可以吸收一下文件编码规范方面的经验"。本节把 seedclaw-v1 的文件级约定转成 claude_hub 可直接落地的建议（仅建议，本次不改生产代码）。

### 5.1 seedclaw-v1 值得吸收的四条文件约定

1. **单文件硬上限（R7 ≤500 行）**：不是软性建议而是硬约束，注释直接标注（`worker-task-context.ts:8`、`task-plan-submitted.ts:6-8`）。硬上限的价值不在"行数"本身，而在于它**强制**触发职责拆分，避免文件无声地长成上千行。
2. **"编排器 + barrel" 拆分范式**：当一个模块逼近上限时，seedclaw-v1 的做法是把主文件降级成"只做编排 + re-export"，把实体逻辑外置到子目录。实测：
   - `worker-task-context.ts` = 194 行（只做装配编排 + barrel re-export）
   - `worker-task-context/sections.ts` = 300 行（真正的 section 文本构建器）
   - `worker-task-context/types.ts` = 308 行（类型 + 纯工具判定）
   - `worker-task-context/utility-prompts.ts` = 125 行（utility 任务 prompt）
   关键：**对外 import 路径不变**——主文件用 `export { ... } from './worker-task-context/sections.js'` 保留旧路径（`worker-task-context.ts:43-80`），所以拆分对调用方零成本。
3. **文件头声明副作用契约**：每个纯函数模块文件头明确写出约束，例如 "No DB access, no side effects. All data passed as parameters."（`worker-task-context.ts:2-3`）。约束写在文件最显眼处，新增代码时一眼可知"这个文件不许碰 DB"。
4. **注释 ↔ 编号 charter 双向可追溯**：规范条目以编号沉淀（R7 / #39 / #40 / #52 / R20 / charter §x.y），代码注释直接引用编号。读到 `// never-build #5` 能回查 charter，读 charter 能 grep 到落地点。

### 5.2 对 claude_hub 的具体建议

| 建议 | 依据 | 落地动作（建议） |
| --- | --- | --- |
| 给 workspace_manager 设一个**软上限 + 拆分触发线**（如 ≤600 行预警、≤800 行必须拆） | claude_hub 现状：`_prompts.py`=1070、`_reports.py`=1002、`_dispatch.py`=817 已远超 seedclaw-v1 的 500 上限 | 在 `docs/` 落一条编号约定，CI 加一个行数检查（warning 级即可） |
| 用 seedclaw-v1 的"编排器 + barrel"范式拆 `_prompts.py` | `_prompts.py` 1070 行里，prompt 块构建器（`_execution_complexity_assignment_block` / `_orchestrator_contract_block` / `_subagent_capability_hint` / `_model_evidence_contract_block` / lesson 块）彼此独立 | 把这些块抽到 `_prompts/blocks.py` 等子模块，`_PromptsMixin` 只保留 `_build_task_assignment_prompt` 的编排骨架；mixin 对外接口不变，调用方零改动 |
| 在纯函数模块文件头补"副作用契约"注释 | seedclaw-v1 文件头声明范式 | `workspace_state_policy.py` 已有 "Pure-function module, no I/O" 头注释，这条 claude_hub **已部分具备**；建议推广到其他声称无副作用的辅助模块 |
| 把分散的代码内长注释收敛成**编号 charter 索引** | seedclaw-v1 的 #39/R7 编号治理 | claude_hub 已有 `docs/working-logs/` 与 lessons-catalog，可增设一个编号规范索引，让长注释块引用编号而非整段重复 |

> 注意取舍：claude_hub 的 mixin 拆分（按 `_reports` / `_dispatch` / `_prompts` / `_review` 职责切）本身是合理的分层，问题只在**单个 mixin 文件过大**。所以建议是"在现有 mixin 分层之上再做一层 barrel 拆分"，而不是推翻 mixin 结构。

---

## 6. multi-agent prompt 瘦身（follow-up，2026-06-13 提出 / 2026-06-14 落地）

> 跟进诉求（原话）："prompt 的话 我本意是想让 agent 自行判断要不要建立多 agent 的工作 是不是这部分 prompt 可以做的轻量一些"；澄清（原话）："如果是 auto mode 的话 预期确实，需要多 agemt 默认"；以及"是不是现在 auto mode 的 prompt 太重了 有可能精简吗"。
> 本节先定位 claude_hub 当前"是否建多 agent"相关 prompt 的实际代码位置和重量（§6.1），更正上一轮的一处误判（§6.2），再记录已实施的体积瘦身（§6.3–§6.4）。

### 6.1 现状：这部分 prompt 现在有多重、由哪些块构成

claude_hub 里"要不要建多 agent"的指引，**散落在两段、且对所有任务模式生效程度不同**：

1. **`_execution_complexity_assignment_block`（`_prompts.py:427`）—— 对所有模式都注入**（在 `_build_task_assignment_prompt` 里无条件拼接，`_prompts.py:261`）。它按 `task.execution_complexity`（simple/complex/auto）给一段指引，再附一段 **cost_guard**（`_prompts.py:448-454`），列出"只有满足以下之一才选 orchestrator 模式"的三个条件。这段相对轻量（约 30 行），方向也正确——它本身就是"让 agent 判断"。
2. **`_orchestrator_contract_block`（`_prompts.py:550`）—— 约 100+ 行的重型契约块**：角色原语 P-PLAN/P-EXECUTE/P-VALIDATE/P-JUDGE/P-INTEGRATE/P-RESEARCH、subtask-envelope schema、subagent-ledger schema、**两个完整 worked example（coding / image-gen，含 deps 图）**、observability 要求、enforcement 段。但它目前**只在 Autonomous 模式注入**——经由 `_autonomous_assignment_block`（`_prompts.py:525`），该函数对非 autonomous 模式直接 `return ""`（`_prompts.py:530-531`）。

**结论一**：对 reviewed / direct 模式（也就是本次任务所属模式），其实并**没有**注入那段 100 行的重型 orchestrator 契约；agent 看到的只有 §6.1-1 那段约 30 行的复杂度指引 + cost_guard。所以"重"主要发生在 **autonomous 模式**，以及 auto 复杂度下的措辞。

### 6.2 修正：autonomous 模式默认多 agent 是预期行为，不是冲突

> 跟进澄清（原话）："如果是 auto mode 的话 预期确实，需要多 agemt 默认"。

上一轮本节曾把 auto / simple enforcement 里"即便选单 agent 也必须起一个 P-JUDGE 子 agent"判定为"与让 agent 自行判断相矛盾"。**这是误判，现更正**：这段契约只在 **autonomous 模式**注入（`_autonomous_assignment_block` 对非 autonomous 直接 `return ""`），而 autonomous 模式下"默认走多 agent、单 agent 也要有一次独立评审"正是设计意图。因此**行为层面没有冲突**，enforcement 的语义保持不变。

真正可优化的只有一点：**这段契约的体积（每次派发都内联约 100+ 行）偏重**。按 prompt-slim #39 ——"运行时自己能从通用模型知识推导出来的方法论，不必每次派发都重新注入"——worked example 这类"怎么把任务拆成角色"的范式属于通用模型知识，是体积瘦身的首要目标，而不是行为改动。

### 6.3 已实施的瘦身（2026-06-14）

按"只减体积、不改行为"的取向，对 `_orchestrator_contract_block`（`_prompts.py:550`）做了如下精简：

1. **删掉两个完整 worked example（原 `_prompts.py:617-656`，约 40 行）**，替换为一个约 12 行的 **compact skeleton**（只示意角色形状 + deps 一行 + notes 一行，并在 notes 里以一句话覆盖原 image 范例承载的"外部 API 步骤用 `model: external:<api>`、迭代产物加 judge->execute 回路"两个要点）。范例自身原文即标注"inspiration, not a template to copy verbatim"，符合 prompt-slim #39。
2. **压缩 observability 段**（8 行 → 6 行），保留全部语义关键词（working heartbeat、role.id + elapsed time、image/API job、"Bare placeholders" + contract violations）。
3. **完整保留** role 原语清单、subtask-envelope schema、subagent-ledger schema、capability_hint、model_evidence、以及三个分支（simple/complex/auto）的 enforcement——行为零改动。

效果：该块由 **676 行降至 647 行**（块内约 -29 行 / -26%）。配套测试 `tests/test_workspace_orchestrator_contract.py` 同步更新（断言由"两个 example"改为"compact skeleton + `external:<api>`"，函数 `..._and_examples` 重命名为 `..._and_skeleton`），**19/19 通过**。

> 未采纳的更激进选项（留作后续可选项）：把范例外置到 `docs/orchestrator-contract.md` 并在 prompt 里只留一行指针（类似 lessons-catalog 的按需读取，`_prompts.py:351`）；以及"契约重量随 agent 实际声明的模式动态注入（单 agent 路径全程不背多 agent 契约）"。这两条改动面更大、且触及注入时机，本轮保守起见未做。

### 6.4 before / after（已落地，节选）

worked example 段（原 `_prompts.py:617-656`，约 40 行的两个完整范例）现已替换为：

```
Compact skeleton (e.g. add a soft-delete endpoint to /api/orders, reject if shipped,
cover with tests) -- shape only, not a template:
  workflow:
    roles:
      - id: planner       primitive: P-PLAN      duty: decompose, list acceptance, blast radius
      - id: implementer   primitive: P-EXECUTE   duty: make the change, scoped to the module
      - id: tester        primitive: P-VALIDATE  duty: tests covering the success criteria
      - id: reviewer      primitive: P-JUDGE     duty: independent review vs acceptance
      - id: integrator    primitive: P-INTEGRATE duty: collect ledger, decide ready_for_review
    deps: planner -> implementer -> tester -> reviewer -> integrator
    notes: add P-RESEARCH for unknowns; add a judge->execute loop for iterative artifacts;
    use model: external:<api> for non-LLM tool steps (image/render/etc).
```

要点：保留"声明 workflow + 角色形状"这个**运行时无法替代的动作模板**，去掉两个长范例的逐行细节——把"具体怎么编排"交还给运行时的通用模型知识（prompt-slim #39）；enforcement 与默认多 agent 语义原样保留。

---

## 附：引用清单（file:line）

seedclaw-v1（分析时检出至 `/tmp/seedclaw-v1`）：
- `packages/server/services/worker-task-context.ts:2`（纯函数声明）、`:8`（R7）、`:88`（`buildWorkerTaskPrompt`）、`:91` `:98`（prompt-slim #39 参数注释）、`:115-171`（固定段落顺序）、`:180`（`priorityToInt`）
- `packages/server/services/work-item-lifecycle.ts:26`（`isHiddenLifecycleTask`）、`:43`（`shouldAutoTransitionOnTerminal`）、`:161`（`AUTO_PROGRESS_STATUSES`）、`:174`（`onWorkerTaskDispatchedSync`）、`:201-271`（`onTaskCompleted` + lifecycle flip 2026-06-10）、`:331-380`（`onTaskFailed`）
- `packages/server/services/task-plan-submitted.ts:47`（`handlePlanSubmittedCompletion` → awaiting_plan_review）
- `packages/server/services/task-user-input-submitted.ts:31`（`summarizeQuestionJson`）、`:65`（`handleUserInputSubmittedCompletion` → awaiting_user_input）
- `packages/server/services/server-brain-task-state.ts:7`（`failServerBrainTask` → failed）、`:35`（`deferServerBrainTaskUntilRuntimeReconnect` → queued）

claude_hub（本仓库）：
- `backend/claude_hub/services/workspace_manager/_prompts.py`：`_build_task_assignment_prompt`、`_execution_complexity_assignment_block`、`_orchestrator_contract_block`、`_subagent_capability_hint`、`_model_evidence_contract_block`、`_build_continue_prompt`、`_build_review_prompt`
- `backend/claude_hub/services/workspace_manager/_reports.py:37`（`_reviewer_verdict_actionable`）、`:152-298`（过期 reviewer verdict / 迟到 orchestrator 报告抑制）、`:510`（`_is_goal_packet_approval_review`）、`:632-747`（autonomous 相位机）、`:902`（`_request_task_review`）、`:882`（`_mark_task_review_skipped`）
- `backend/claude_hub/services/workspace_state_policy.py`（纯函数状态策略：`managed_status_from_report` / `task_status_from_report` / `reviewer_verdict_*` / `autonomous_phase_*` / `auto_continue_*`）
- `backend/claude_hub/models/schemas.py`（状态枚举：`WorkspaceTaskStatus`:42 / `AutonomousRunPhase`:84 / `ManagedSessionStatus`:111 / `AgentReportState`:123 等）
- 模块行数（`wc -l`，验证 R7 差异）：`_prompts.py`=1070、`_reports.py`=1002、`_dispatch.py`=817、`_task_updates.py`=320、`_review.py`=297、`_tasks.py`=152、`_state.py`=93

follow-up（2026-06-13，§5 / §6）：
- seedclaw-v1 拆分范式实测行数：`worker-task-context.ts`=194（编排器+barrel）、`worker-task-context/sections.ts`=300、`worker-task-context/types.ts`=308、`worker-task-context/utility-prompts.ts`=125；barrel re-export 见 `worker-task-context.ts:43-80`
- claude_hub multi-agent prompt 块：`_prompts.py:261`（无条件拼接 complexity 块）、`:427`（`_execution_complexity_assignment_block`）、`:448-454`（cost_guard）、`:525-531`（`_autonomous_assignment_block` 非 autonomous 即 `return ""`）、`:550`（`_orchestrator_contract_block`）、`:556-561`（simple enforcement 强制 P-JUDGE）、`:568-574`（auto enforcement 强制 P-JUDGE）、`:617-656`（两个 worked example）、`:351`（lessons 按需读取模式，可复用为契约按需引用）
