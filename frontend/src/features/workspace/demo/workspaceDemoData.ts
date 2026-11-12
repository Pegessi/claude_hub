/**
 * Realistic multi-workspace dataset for three clean, low-noise Agent Workspace
 * design paradigms:
 *  1. linear_zen     — Borderless hairline list + editorial vertical spine
 *  2. focused_inbox  — Centered 740px calm single-column reader (Things 3 / Pulse)
 *  3. precision_grid — Swiss-grid deployment ledger with inline stage blocks (Vercel)
 */

export type DemoStyleId = 'linear_zen' | 'focused_inbox' | 'precision_grid'

export interface DemoStyleOption {
  id: DemoStyleId
  label: string
  reference: string
}

export const DEMO_STYLES: readonly DemoStyleOption[] = [
  {
    id: 'linear_zen',
    label: '1 · Linear Split',
    reference: 'Linear / GitHub PR Spine',
  },
  {
    id: 'focused_inbox',
    label: '2 · Calm Reader',
    reference: 'Things 3 / Superhuman',
  },
  {
    id: 'precision_grid',
    label: '3 · Trace Ledger',
    reference: 'Vercel / Raycast',
  },
]

export interface DemoExternalLink {
  label: string
  url: string
}

export interface DemoTimelineNode {
  id: string
  stage: string
  shortStage: string
  actor: string
  tone: 'neutral' | 'warn' | 'pass' | 'live'
  timeLabel: string
  deltaLabel: string
  summary: string
  receipt?: string
  sessionTabId?: string
  sessionArchived?: boolean
}

export interface DemoTranscriptLine {
  speaker: string
  time: string
  text: string
}

export interface DemoTaskRecord {
  id: string
  title: string
  workspace: string
  branch: string
  bucket: 'needs_you' | 'working' | 'done'
  statusLabel: string
  statusTone: 'attention' | 'working' | 'done'
  decisionLine?: string
  outcomeSummary: string
  liveStep?: string
  elapsed: string
  updatedAgo: string
  diffStat?: { added: number; removed: number }
  checksLabel: string
  links: DemoExternalLink[]
  timeline: DemoTimelineNode[]
  transcriptExcerpt: DemoTranscriptLine[]
  internalMeta: {
    control: string
    epoch: number
    mode: string
    seat: string
  }
}

export const DEMO_TASKS: readonly DemoTaskRecord[] = [
  {
    id: '47e984bf',
    title: 'MR !3953 细粒度生命周期打点与同 session 串联实测',
    workspace: 'evo_workspace',
    branch: 'wzh/request-lifecycle-events',
    bucket: 'needs_you',
    statusLabel: 'Ready · 2/2 review',
    statusTone: 'attention',
    decisionLine: 'Astra 第 2 轮复审已通过（0 P0/P1），m13_tiny 700 条多模态与长文事件零丢失。',
    outcomeSummary: '补齐 Hub → Server 全阶段 25 类事件打点及 ForkSession / 粘性重放血缘串联；4.6K Prefix-Cache 与 3 图 ViT AOTI 实测稳态零额外开销。',
    elapsed: '38m',
    updatedAgo: '4m',
    diffStat: { added: 498, removed: 31 },
    checksLabel: '729 passed',
    links: [
      { label: 'MR !3953', url: 'https://code.byted.org/seed/llmserver/merge_requests/3953' },
      { label: '飞书瀑布图报告', url: 'https://bytedance.larkoffice.com/docx/TzbmdyYlioujByxeHPUcSSUCnAf' },
    ],
    timeline: [
      {
        id: 't1',
        stage: '创建隔离工作树并拉起执行会话',
        shortStage: 'Init',
        actor: 'Ted',
        tone: 'neutral',
        timeLabel: '12:21',
        deltaLabel: '0m',
        summary: '基于最新 master 建立 operator-trace-20261111，绑定 ephemeral 会话。',
      },
      {
        id: 't2',
        stage: '首轮实现与本机 m13_tiny 真机压测',
        shortStage: 'Impl R1',
        actor: 'Argon High',
        tone: 'neutral',
        timeLabel: '12:41',
        deltaLabel: '+20m',
        summary: '完成核心单行 Hook 收敛（+441 LOC），实测正常流、KV 抢占与重放并产出飞书报告。',
        receipt: 'pytest 717 passed · GPU memory cleanly released',
        sessionTabId: '6364744d',
        sessionArchived: true,
      },
      {
        id: 't3',
        stage: '异构交叉审查（第 1 轮）· 拦截 1 项 P1',
        shortStage: 'Astra R1',
        actor: 'GPT-6-Astra',
        tone: 'warn',
        timeLabel: '12:52',
        deltaLabel: '+11m',
        summary: '指出粘性路由重试与 ForkSession 未透传父 session_id，跨轮生命周期会断链。',
        sessionTabId: '67e8ab53',
        sessionArchived: true,
      },
      {
        id: 't4',
        stage: '补齐 Session 血缘并加测 8K 长文与多模态',
        shortStage: 'Fix R2',
        actor: 'Argon High',
        tone: 'neutral',
        timeLabel: '13:02',
        deltaLabel: '+10m',
        summary: '新增 +57 行透传 session_id 与 Fork 血缘；验证 Prefix-Cache 命中（TTFT 222ms → 99ms）。',
        receipt: 'pytest 729 passed · 700/700 events matched',
        sessionTabId: '6364744d',
        sessionArchived: true,
      },
      {
        id: 't5',
        stage: '异构交叉审查（第 2 轮）· 共识通过',
        shortStage: 'Astra R2',
        actor: 'GPT-6-Astra',
        tone: 'pass',
        timeLabel: '13:06',
        deltaLabel: '+4m',
        summary: '确认 C++/Python 契约一致、无遗留阻塞项，准予交付验收。',
        sessionTabId: '67e8ab53',
        sessionArchived: true,
      },
    ],
    transcriptExcerpt: [
      { speaker: 'Goal Gate', time: '12:52', text: 'Round 1 blocked: propagate session_id across sticky retries and ForkSession.' },
      { speaker: 'Argon High', time: '12:56', text: 'Updated session_trace.cc (+28 -4) and added 12 lineage regression tests.' },
      { speaker: 'GPT-6-Astra', time: '13:06', text: 'Verdict PASS (0 P0, 0 P1). Verified against commit b2c4fe1.' },
    ],
    internalMeta: {
      control: 'workspace',
      epoch: 2,
      mode: 'strict_review',
      seat: 'ephemeral · auto-release',
    },
  },
  {
    id: 'ce4513f8',
    title: 'EVO 接入 USM Audio Encoder（迁移打平 + 算子融合双 MR）',
    workspace: 'evo_workspace',
    branch: 'codex/evo-usm-encoder',
    bucket: 'needs_you',
    statusLabel: 'Ready · Stacked MRs',
    statusTone: 'attention',
    decisionLine: 'AudioProcessor 41 项单测与 xperf_gpt_triton 0.0 误差对齐，H20 端到端提速 1.8x–5.6x。',
    outcomeSummary: '已完成叠栈双 MR：MR !1379 负责权重加载、预处理逐位打平与 AOTI 导出；MR !1385 完成 Attention/Norm 融合加速。',
    elapsed: '54m',
    updatedAgo: '19m',
    diffStat: { added: 612, removed: 44 },
    checksLabel: '41 parity passed',
    links: [
      { label: 'MR !1379', url: 'https://code.byted.org/seed/xperf_evo/merge_requests/1379' },
      { label: 'MR !1385', url: 'https://code.byted.org/seed/xperf_evo/merge_requests/1385' },
    ],
    timeline: [
      {
        id: 'u1',
        stage: 'MR 1 · 权重迁移与 AudioProcessor 0.0 对拍',
        shortStage: 'MR 1 Parity',
        actor: 'Argon High',
        tone: 'neutral',
        timeLabel: '18:53',
        deltaLabel: '+24m',
        summary: '对齐 CMVN、Padding 与下采样帧长计算，完成 AOTI 无权重导出。',
        receipt: 'max_abs_diff = 0.0 across 41 cases',
        sessionTabId: '3b3f8742',
        sessionArchived: true,
      },
      {
        id: 'u2',
        stage: 'MR 2 · H20 算子融合与 CUDA Graph 调优',
        shortStage: 'MR 2 Perf',
        actor: 'GPT-6-Astra',
        tone: 'neutral',
        timeLabel: '19:21',
        deltaLabel: '+28m',
        summary: 'p6dense_usm B=16 延迟由 41.2ms 降至 7.38ms（5.58x），同步补充性能基线表。',
        sessionTabId: '57ad2f2d',
        sessionArchived: true,
      },
      {
        id: 'u3',
        stage: '独立交叉审查 · 通过',
        shortStage: 'Review Pass',
        actor: 'Argon High',
        tone: 'pass',
        timeLabel: '19:32',
        deltaLabel: '+11m',
        summary: '核实 AOTI 稳态常驻零增量、双分支 rebase 干净。',
        sessionTabId: '8598b9fc',
        sessionArchived: true,
      },
    ],
    transcriptExcerpt: [
      { speaker: 'Argon High', time: '19:18', text: 'Confirmed 0.000000 max diff against xperf_gpt_triton across all mel/CMVN shapes.' },
      { speaker: 'GPT-6-Astra', time: '19:26', text: 'H20 benchmark: 0.6B rope_usm 2.41x, 0.8B p6dense_usm 5.58x.' },
    ],
    internalMeta: {
      control: 'workspace',
      epoch: 1,
      mode: 'reviewed',
      seat: 'ephemeral · archived',
    },
  },
  {
    id: '9d6fb26a',
    title: 'LF M15.3 DSA 256×H20 Arena 评估与 Perf 报告',
    workspace: 'evo_workspace',
    branch: 'eval/m15-3-dsa',
    bucket: 'working',
    statusLabel: '73% · 4,820/6,585',
    statusTone: 'working',
    outcomeSummary: 'Serving 256 卡 H20 稳定运行（GPU 利用率 84.6%，KV 水位 <78%），待尾段跑完自动生成飞书对比报告。',
    liveStep: 'Sampling Grafana QPM & P99 on trial 449604337',
    elapsed: '26m',
    updatedAgo: 'now',
    checksLabel: '84.6% GPU util',
    links: [
      { label: 'Arena Task', url: 'https://seed.bytedance.net/evaluation/arena/zvz2jc18yk67f66043' },
    ],
    timeline: [
      {
        id: 'w1',
        stage: '基线继承校验与 256×H20 实例拉起',
        shortStage: 'Launch',
        actor: 'Argon High',
        tone: 'pass',
        timeLabel: '22:18',
        deltaLabel: '0m',
        summary: 'configuration_readback 零非预期差异，推理侧进入 RUNNING。',
        sessionTabId: '62506836',
      },
      {
        id: 'w2',
        stage: '持续采样吞吐与长尾样本进度',
        shortStage: 'Sampling',
        actor: 'Argon High',
        tone: 'live',
        timeLabel: '22:44',
        deltaLabel: '+26m',
        summary: '已完成 4,820/6,585 题，0 OOM，QPM 148.4。',
        sessionTabId: '62506836',
      },
    ],
    transcriptExcerpt: [
      { speaker: 'merlin-cli', time: '22:44', text: 'progress=4820/6585 (73.2%) qpm=148.4 gpu_util_avg=84.6% oom=0' },
    ],
    internalMeta: {
      control: 'initiator',
      epoch: 1,
      mode: 'subagent',
      seat: 'ephemeral chat',
    },
  },
  {
    id: '43cedad4',
    title: 'GB200 SM100 算子级异步指标采集与开销实测',
    workspace: 'llmserver-evo',
    branch: 'feat/gb200-cupti',
    bucket: 'working',
    statusLabel: 'Reviewing · R1/3',
    statusTone: 'working',
    outcomeSummary: '4×GB200 真机单 Pass CUPTI 采样已跑通（延迟开销 +0.4%），当前异构 Reviewer 正在审查 CUDA Graph 捕获路径。',
    liveStep: 'Argon High reviewing cupti_range_sampler.py (328 LOC)',
    elapsed: '19m',
    updatedAgo: '1m',
    diffStat: { added: 328, removed: 19 },
    checksLabel: '28 passed',
    links: [
      { label: 'Draft MR !3961', url: 'https://code.byted.org/seed/llmserver/merge_requests/3961' },
    ],
    timeline: [
      {
        id: 'g1',
        stage: '实现单 Pass 采集器并完成 4×GB200 实测',
        shortStage: 'Impl',
        actor: 'Codex Sol',
        tone: 'pass',
        timeLabel: '22:31',
        deltaLabel: '+16m',
        summary: '在 n125-154-036（Driver 580.167）实测开销 0.4%，28 项单测通过。',
        sessionTabId: 'a4c8a1ed',
      },
      {
        id: 'g2',
        stage: '严格交叉审查第 1 轮进行中',
        shortStage: 'Review R1',
        actor: 'Argon High',
        tone: 'live',
        timeLabel: '22:47',
        deltaLabel: '+3m',
        summary: '核查多流并发串行化与容器权限降级逻辑。',
        sessionTabId: 'sa-91bc04',
      },
    ],
    transcriptExcerpt: [
      { speaker: 'Argon High', time: '22:47', text: 'Checking context teardown safety during CUDA Graph capture in cupti_range_sampler.py:142…' },
    ],
    internalMeta: {
      control: 'workspace',
      epoch: 1,
      mode: 'strict_review',
      seat: 'llmserver-agent-2',
    },
  },
  {
    id: 'd212d01c',
    title: '飞书 @答疑 · 10B 伴生评测 step 1000–4000 失败根因排查',
    workspace: 'evo_workspace',
    branch: 'feishu-triage',
    bucket: 'done',
    statusLabel: 'Auto-archived',
    statusTone: 'done',
    outcomeSummary: '定位 kvheads=1 一维权重挤压断言失败与 v948 模板端口不足双根因；已写入 InfoWall 并将 140 字回复草稿发至飞书私聊。',
    elapsed: '6m',
    updatedAgo: '1h',
    checksLabel: 'InfoWall synced',
    links: [
      { label: 'InfoWall 记录', url: 'https://26ya5x44im.merlin-devbox.bytedance.net' },
    ],
    timeline: [
      {
        id: 'd1',
        stage: '飞书巡检捕获未回话题 @提及',
        shortStage: 'Patrol',
        actor: 'Ted',
        tone: 'neutral',
        timeLabel: '18:24',
        deltaLabel: '0m',
        summary: '「10B eval干活群」话题内检测到异常求助，自动拉起只读 ephemeral 排查任务。',
      },
      {
        id: 'd2',
        stage: 'HDFS 权重头与模板参数双根因取证',
        shortStage: 'Diagnose',
        actor: 'Argon High',
        tone: 'pass',
        timeLabel: '18:29',
        deltaLabel: '+5m',
        summary: '核实 safetensors 形状 (128,) 及 v948 模板 16 端口限制，生成群回复草稿。',
        sessionTabId: '4cd82e90',
        sessionArchived: true,
      },
      {
        id: 'd3',
        stage: 'InfoWall 留档 + 飞书私聊推送 + 自动结项回收',
        shortStage: 'Closed',
        actor: 'Ted',
        tone: 'pass',
        timeLabel: '18:30',
        deltaLabel: '+1m',
        summary: '自动执行 task accept --cleanup-session，零残留会话。',
      },
    ],
    transcriptExcerpt: [
      { speaker: 'Ted', time: '18:30', text: 'Synced to InfoWall & sent reply draft to Feishu DM; ephemeral session released.' },
    ],
    internalMeta: {
      control: 'workspace',
      epoch: 1,
      mode: 'subagent',
      seat: 'cleaned',
    },
  },
  {
    id: '65bba110',
    title: 'Claude Hub SQLite WAL 存储收敛与 InfoWall 无损切流',
    workspace: 'claude_hub',
    branch: 'codex/sqlite-storage',
    bucket: 'done',
    statusLabel: 'Merged · 65bba11',
    statusTone: 'done',
    outcomeSummary: '活跃数据体积下降 46.1%，单次任务保存提速 4 倍、流尾读取快 191 倍；InfoWall 1,698 条记录 0 误差衔接。',
    elapsed: '42m',
    updatedAgo: '3h',
    diffStat: { added: 1120, removed: 684 },
    checksLabel: '7,539 passed',
    links: [
      { label: 'Commit 65bba11', url: 'https://github.com/Pegessi/claude_hub' },
    ],
    timeline: [
      {
        id: 's1',
        stage: '统一 SQLite WAL 存储实现与 222 万条事件压测',
        shortStage: 'Impl',
        actor: 'Codex Sol',
        tone: 'neutral',
        timeLabel: '00:05',
        deltaLabel: '+26m',
        summary: '收敛 353 条流与 16 份状态文档，移除双写包装层。',
        sessionTabId: 'fd83a1cb',
        sessionArchived: true,
      },
      {
        id: 's2',
        stage: '异构审查修复 1 P1 + 快进合入 main',
        shortStage: 'Merged',
        actor: 'Argon High',
        tone: 'pass',
        timeLabel: '00:31',
        deltaLabel: '+16m',
        summary: '修复顶层导入触碰默认库问题，6,260 后端 + 1,279 前端单测全绿后自动关单。',
        sessionTabId: 'd092ac57',
        sessionArchived: true,
      },
    ],
    transcriptExcerpt: [
      { speaker: 'Argon High', time: '00:31', text: 'Fast-forwarded main to 65bba11; worker and reviewer sessions archived.' },
    ],
    internalMeta: {
      control: 'workspace',
      epoch: 1,
      mode: 'reviewed',
      seat: 'cleaned',
    },
  },
]
