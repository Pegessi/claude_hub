/**
 * Realistic multi-workspace dataset for the three Argon Flagship Agent Workspace
 * design candidates:
 *  1. linear_zen     — "1 · Linear Triage Pro" (Refined 2-Tier Triage Rail + Deliverable Spec Bar + Sculptural Review Spine)
 *  2. focused_inbox  — "2 · Command Studio"    (Decision Spotlight + Live Autonomous Swimlanes + Docked Spine Inspector)
 *  3. precision_grid — "3 · Review Blueprint"  (3-Pane Architectural Evidence, Iterative Spine & Consensus Matrix Workbench)
 */

export type DemoStyleId = 'linear_zen' | 'focused_inbox' | 'precision_grid'

export interface DemoStyleOption {
  id: DemoStyleId
  label: string
  shortLabel: string
  reference: string
}

export const DEMO_STYLES: readonly DemoStyleOption[] = [
  {
    id: 'linear_zen',
    label: '1 · Linear Triage Pro',
    shortLabel: '1 · Triage Pro',
    reference: 'Linear Triage × GitButler Spine',
  },
  {
    id: 'focused_inbox',
    label: '2 · Command Studio',
    shortLabel: '2 · Studio',
    reference: 'Antigravity Spotlight × Live River',
  },
  {
    id: 'precision_grid',
    label: '3 · Review Blueprint',
    shortLabel: '3 · Blueprint',
    reference: 'Stripe Workbench × PR Checks',
  },
]

export interface DemoExternalLink {
  label: string
  url: string
  kind: 'mr' | 'doc' | 'arena' | 'infowall' | 'commit'
  meta: string
}

export interface DemoTimelineNode {
  id: string
  stage: string
  shortStage: string
  actor: string
  actorRole: 'worker' | 'reviewer' | 'orchestrator'
  tone: 'neutral' | 'warn' | 'pass' | 'live'
  timeLabel: string
  deltaLabel: string
  summary: string
  badge?: string
  epochLabel?: string
  filesTouched?: string
  receipt?: string
  sessionTabId?: string
  sessionArchived?: boolean
}

export interface DemoTranscriptLine {
  speaker: string
  role: 'gate' | 'worker' | 'reviewer' | 'system'
  time: string
  text: string
}

export interface DemoConsensusSummary {
  implementer: string
  reviewer: string
  roundsLabel: string
  verdictLabel: string
  verdictTone: 'pass' | 'live' | 'auto'
  metricHighlight: string
}

export interface DemoTaskRecord {
  id: string
  code: string
  title: string
  workspace: string
  branch: string
  bucket: 'needs_you' | 'working' | 'done'
  statusLabel: string
  statusTone: 'attention' | 'working' | 'done'
  progressPct?: number
  decisionLine?: string
  outcomeSummary: string
  liveStep?: string
  elapsed: string
  updatedAgo: string
  diffStat?: { added: number; removed: number; files: number }
  checksLabel: string
  consensus: DemoConsensusSummary
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
    code: 'MR-3953',
    title: 'MR !3953 细粒度生命周期打点与同 session 串联实测',
    workspace: 'evo_workspace',
    branch: 'wzh/request-lifecycle-events',
    bucket: 'needs_you',
    statusLabel: '2/2 Review PASS',
    statusTone: 'attention',
    decisionLine: 'Astra 第 2 轮复审已通过（0 P0/P1），m13_tiny 700 条多模态与长文事件零丢失。',
    outcomeSummary: '补齐 Hub → Server 全阶段 25 类事件打点及 ForkSession / 粘性重放血缘串联；4.6K Prefix-Cache 与 3 图 ViT AOTI 实测稳态零额外开销。',
    elapsed: '38m',
    updatedAgo: '4m ago',
    diffStat: { added: 498, removed: 31, files: 6 },
    checksLabel: 'pytest 729 passed',
    consensus: {
      implementer: 'Argon High',
      reviewer: 'GPT-6-Astra',
      roundsLabel: 'R1 拦截 1 P1 → R2 修复通过',
      verdictLabel: 'PASS · 0 P0 / 0 P1',
      verdictTone: 'pass',
      metricHighlight: 'TTFT 222ms → 99ms · 700/700 events',
    },
    links: [
      {
        label: 'MR !3953',
        url: 'https://code.byted.org/seed/llmserver/merge_requests/3953',
        kind: 'mr',
        meta: 'seed/llmserver · +498 −31 (6 files)',
      },
      {
        label: '飞书瀑布图实测报告',
        url: 'https://bytedance.larkoffice.com/docx/TzbmdyYlioujByxeHPUcSSUCnAf',
        kind: 'doc',
        meta: 'Lark Docx · 4.6K Prefix + ViT AOTI',
      },
    ],
    timeline: [
      {
        id: 't1',
        stage: '创建隔离工作树并拉起执行会话',
        shortStage: 'Init',
        actor: 'Ted',
        actorRole: 'orchestrator',
        tone: 'neutral',
        timeLabel: '12:21',
        deltaLabel: '0m',
        epochLabel: 'EPOCH 1 · INITIAL IMPLEMENTATION & GATE',
        summary: '基于最新 master 建立 operator-trace-20261111，绑定 ephemeral 会话。',
      },
      {
        id: 't2',
        stage: '首轮实现与本机 m13_tiny 真机压测',
        shortStage: 'Impl R1',
        actor: 'Argon High',
        actorRole: 'worker',
        tone: 'neutral',
        timeLabel: '12:41',
        deltaLabel: '+20m',
        badge: '+441 LOC',
        filesTouched: 'session_trace.cc, event_bus.py (+4 files)',
        summary: '完成核心单行 Hook 收敛，实测正常流、KV 抢占与重放并产出飞书报告。',
        receipt: 'pytest 717 passed · GPU memory cleanly released',
        sessionTabId: '6364744d',
        sessionArchived: true,
      },
      {
        id: 't3',
        stage: '异构交叉审查（第 1 轮）· 拦截 1 项 P1',
        shortStage: 'Astra R1',
        actor: 'GPT-6-Astra',
        actorRole: 'reviewer',
        tone: 'warn',
        timeLabel: '12:52',
        deltaLabel: '+11m',
        badge: '1 P1 BLOCKED',
        filesTouched: 'router_sticky_retry.cc:184',
        summary: '指出粘性路由重试与 ForkSession 未透传父 session_id，跨轮生命周期会断链。',
        receipt: 'verdict: CHANGES_REQUESTED (0 P0, 1 P1 · lineage break on retry)',
        sessionTabId: '67e8ab53',
        sessionArchived: true,
      },
      {
        id: 't4',
        stage: '补齐 Session 血缘并加测 8K 长文与多模态',
        shortStage: 'Fix R2',
        actor: 'Argon High',
        actorRole: 'worker',
        tone: 'neutral',
        timeLabel: '13:02',
        deltaLabel: '+10m',
        epochLabel: 'EPOCH 2 · LINEAGE FIX & FINAL CONSENSUS',
        badge: '+57 LOC',
        filesTouched: 'session_trace.cc, test_session_lineage.py',
        summary: '新增透传 session_id 与 Fork 血缘；验证 Prefix-Cache 命中（TTFT 222ms → 99ms）。',
        receipt: 'pytest 729 passed (+12 lineage tests) · 700/700 events matched',
        sessionTabId: '6364744d',
        sessionArchived: true,
      },
      {
        id: 't5',
        stage: '异构交叉审查（第 2 轮）· 共识通过',
        shortStage: 'Astra R2',
        actor: 'GPT-6-Astra',
        actorRole: 'reviewer',
        tone: 'pass',
        timeLabel: '13:06',
        deltaLabel: '+4m',
        badge: 'VERDICT PASS',
        summary: '确认 C++/Python 契约一致、无遗留阻塞项，准予交付验收。',
        receipt: 'verdict: PASS (0 P0, 0 P1) · verified commit b2c4fe1',
        sessionTabId: '67e8ab53',
        sessionArchived: true,
      },
    ],
    transcriptExcerpt: [
      { speaker: 'Goal Gate', role: 'gate', time: '12:52', text: 'Round 1 blocked: propagate parent session_id across sticky retries and ForkSession.' },
      { speaker: 'Argon High', role: 'worker', time: '12:56', text: 'Patched session_trace.cc (+28 −4) and added 12 lineage regression cases on m13_tiny.' },
      { speaker: 'GPT-6-Astra', role: 'reviewer', time: '13:06', text: 'Verdict PASS (0 P0, 0 P1). Verified 700/700 multimodal & 8K prefix-cache events on b2c4fe1.' },
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
    code: 'MR-1379',
    title: 'EVO 接入 USM Audio Encoder（迁移打平 + 算子融合双 MR）',
    workspace: 'evo_workspace',
    branch: 'codex/evo-usm-encoder',
    bucket: 'needs_you',
    statusLabel: 'Stacked MRs Ready',
    statusTone: 'attention',
    decisionLine: 'AudioProcessor 41 项单测与 xperf_gpt_triton 0.0 误差对齐，H20 端到端提速 1.8x–5.6x。',
    outcomeSummary: '已完成叠栈双 MR：MR !1379 负责权重加载、预处理逐位打平与 AOTI 导出；MR !1385 完成 Attention/Norm 融合加速。',
    elapsed: '54m',
    updatedAgo: '19m ago',
    diffStat: { added: 612, removed: 44, files: 9 },
    checksLabel: '41 parity passed',
    consensus: {
      implementer: 'Argon High + Astra',
      reviewer: 'Argon High',
      roundsLabel: '双 MR 叠栈独立复核通过',
      verdictLabel: 'PASS · 0.0 max diff',
      verdictTone: 'pass',
      metricHighlight: 'H20 B=16: 41.2ms → 7.38ms (5.58x)',
    },
    links: [
      {
        label: 'MR !1379 (Parity)',
        url: 'https://code.byted.org/seed/xperf_evo/merge_requests/1379',
        kind: 'mr',
        meta: 'xperf_evo · 权重迁移与 0.0 对拍',
      },
      {
        label: 'MR !1385 (Fusion)',
        url: 'https://code.byted.org/seed/xperf_evo/merge_requests/1385',
        kind: 'mr',
        meta: 'xperf_evo · H20 Attention/Norm 融合',
      },
    ],
    timeline: [
      {
        id: 'u1',
        stage: 'MR 1 · 权重迁移与 AudioProcessor 0.0 对拍',
        shortStage: 'MR 1 Parity',
        actor: 'Argon High',
        actorRole: 'worker',
        tone: 'neutral',
        timeLabel: '18:53',
        deltaLabel: '+24m',
        epochLabel: 'STACK 1 · BITWISE PARITY & AOTI EXPORT',
        badge: '0.0 DIFF',
        filesTouched: 'usm_audio_encoder.py, aoti_export.cc',
        summary: '对齐 CMVN、Padding 与下采样帧长计算，完成 AOTI 无权重导出。',
        receipt: 'max_abs_diff = 0.000000 across 41 mel/CMVN shapes',
        sessionTabId: '3b3f8742',
        sessionArchived: true,
      },
      {
        id: 'u2',
        stage: 'MR 2 · H20 算子融合与 CUDA Graph 调优',
        shortStage: 'MR 2 Perf',
        actor: 'GPT-6-Astra',
        actorRole: 'worker',
        tone: 'neutral',
        timeLabel: '19:21',
        deltaLabel: '+28m',
        epochLabel: 'STACK 2 · KERNEL FUSION & REVIEW SIGN-OFF',
        badge: '5.58x SPEEDUP',
        filesTouched: 'fused_usm_attn.cu, graph_runner.cc',
        summary: 'p6dense_usm B=16 延迟由 41.2ms 降至 7.38ms（5.58x），同步补充性能基线表。',
        receipt: 'H20 benchmark: 0.6B rope_usm 2.41x · 0.8B p6dense_usm 5.58x',
        sessionTabId: '57ad2f2d',
        sessionArchived: true,
      },
      {
        id: 'u3',
        stage: '独立交叉审查 · 通过',
        shortStage: 'Review Pass',
        actor: 'Argon High',
        actorRole: 'reviewer',
        tone: 'pass',
        timeLabel: '19:32',
        deltaLabel: '+11m',
        badge: 'VERDICT PASS',
        summary: '核实 AOTI 稳态常驻零增量、双分支 rebase 干净。',
        receipt: 'verdict: PASS · zero resident memory leak over 1,000 steps',
        sessionTabId: '8598b9fc',
        sessionArchived: true,
      },
    ],
    transcriptExcerpt: [
      { speaker: 'Argon High', role: 'worker', time: '19:18', text: 'Confirmed 0.000000 max diff against xperf_gpt_triton across all 41 mel/CMVN shapes.' },
      { speaker: 'GPT-6-Astra', role: 'worker', time: '19:26', text: 'H20 benchmark complete: 0.6B rope_usm 2.41x, 0.8B p6dense_usm 5.58x.' },
      { speaker: 'Argon High', role: 'reviewer', time: '19:32', text: 'Both stacked MRs verified clean on rebase; ready for human merge.' },
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
    code: 'EV-4496',
    title: 'LF M15.3 DSA 256×H20 Arena 评估与 Perf 报告',
    workspace: 'evo_workspace',
    branch: 'eval/m15-3-dsa',
    bucket: 'working',
    statusLabel: '73% · 4,820/6,585',
    statusTone: 'working',
    progressPct: 73,
    outcomeSummary: 'Serving 256 卡 H20 稳定运行（GPU 利用率 84.6%，KV 水位 <78%），待尾段跑完自动生成飞书对比报告。',
    liveStep: 'Sampling Grafana QPM & P99 on trial 449604337 (4,820 / 6,585)',
    elapsed: '26m',
    updatedAgo: 'live',
    checksLabel: '84.6% GPU · 0 OOM',
    consensus: {
      implementer: 'Argon High',
      reviewer: 'Auto-Gate',
      roundsLabel: 'Serving 巡检正常 · 预计 9m 完成',
      verdictLabel: 'RUNNING · 73.2%',
      verdictTone: 'live',
      metricHighlight: 'QPM 148.4 · GPU Util 84.6% · KV <78%',
    },
    links: [
      {
        label: 'Seed Arena #449604337',
        url: 'https://seed.bytedance.net/evaluation/arena/zvz2jc18yk67f66043',
        kind: 'arena',
        meta: '256×H20 · Dynamic PD Serving',
      },
    ],
    timeline: [
      {
        id: 'w1',
        stage: '基线继承校验与 256×H20 实例拉起',
        shortStage: 'Launch',
        actor: 'Argon High',
        actorRole: 'worker',
        tone: 'pass',
        timeLabel: '22:18',
        deltaLabel: '0m',
        epochLabel: 'AUTONOMOUS TRIAL EXECUTION',
        badge: '256×H20 READY',
        summary: 'configuration_readback 零非预期差异，推理侧进入 RUNNING。',
        receipt: 'readback diff: only image_tag & model_path changed as expected',
        sessionTabId: '62506836',
      },
      {
        id: 'w2',
        stage: '持续采样吞吐与长尾样本进度',
        shortStage: 'Sampling',
        actor: 'Argon High',
        actorRole: 'worker',
        tone: 'live',
        timeLabel: '22:44',
        deltaLabel: '+26m',
        badge: '73.2% LIVE',
        summary: '已完成 4,820/6,585 题，0 OOM，QPM 148.4。',
        receipt: 'progress=4820/6585 (73.2%) · qpm=148.4 · gpu_util=84.6%',
        sessionTabId: '62506836',
      },
    ],
    transcriptExcerpt: [
      { speaker: 'merlin-cli', role: 'system', time: '22:18', text: 'Trial 449604337 scheduled on 256×H20; configuration readback verified.' },
      { speaker: 'Argon High', role: 'worker', time: '22:44', text: 'progress=4820/6585 (73.2%) qpm=148.4 gpu_util_avg=84.6% oom=0' },
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
    code: 'GB-3961',
    title: 'GB200 SM100 算子级异步指标采集与开销实测',
    workspace: 'llmserver-evo',
    branch: 'feat/gb200-cupti',
    bucket: 'working',
    statusLabel: 'Reviewing · R1/3',
    statusTone: 'working',
    progressPct: 62,
    outcomeSummary: '4×GB200 真机单 Pass CUPTI 采样已跑通（延迟开销 +0.4%），当前异构 Reviewer 正在审查 CUDA Graph 捕获路径。',
    liveStep: 'Argon High reviewing cupti_range_sampler.py:142 (CUDA Graph teardown)',
    elapsed: '19m',
    updatedAgo: '1m ago',
    diffStat: { added: 328, removed: 19, files: 4 },
    checksLabel: '28 passed · +0.4% overhead',
    consensus: {
      implementer: 'Codex Sol',
      reviewer: 'Argon High',
      roundsLabel: 'R1 严格交叉审查进行中',
      verdictLabel: 'IN REVIEW · R1/3',
      verdictTone: 'live',
      metricHighlight: '4×GB200 overhead +0.4% · 28 tests green',
    },
    links: [
      {
        label: 'Draft MR !3961',
        url: 'https://code.byted.org/seed/llmserver/merge_requests/3961',
        kind: 'mr',
        meta: 'seed/llmserver · +328 −19 (4 files)',
      },
    ],
    timeline: [
      {
        id: 'g1',
        stage: '实现单 Pass 采集器并完成 4×GB200 实测',
        shortStage: 'Impl',
        actor: 'Codex Sol',
        actorRole: 'worker',
        tone: 'pass',
        timeLabel: '22:31',
        deltaLabel: '+16m',
        epochLabel: 'EPOCH 1 · IMPLEMENTATION & BENCHMARK',
        badge: '+0.4% OVERHEAD',
        filesTouched: 'cupti_range_sampler.py, cupti_bridge.cu',
        summary: '在 n125-154-036（Driver 580.167）实测开销 0.4%，28 项单测通过。',
        receipt: 'pytest 28 passed on 4×GB200 · p99 latency delta +0.4%',
        sessionTabId: 'a4c8a1ed',
      },
      {
        id: 'g2',
        stage: '严格交叉审查第 1 轮进行中',
        shortStage: 'Review R1',
        actor: 'Argon High',
        actorRole: 'reviewer',
        tone: 'live',
        timeLabel: '22:47',
        deltaLabel: '+3m',
        badge: 'REVIEWING LIVE',
        filesTouched: 'cupti_range_sampler.py:142',
        summary: '核查多流并发串行化与容器权限降级逻辑。',
        sessionTabId: 'sa-91bc04',
      },
    ],
    transcriptExcerpt: [
      { speaker: 'Codex Sol', role: 'worker', time: '22:31', text: 'Completed 4×GB200 single-pass CUPTI range profiler; 28 unit tests passed.' },
      { speaker: 'Argon High', role: 'reviewer', time: '22:47', text: 'Auditing context teardown safety during CUDA Graph capture in cupti_range_sampler.py:142…' },
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
    code: 'FS-10B',
    title: '飞书 @答疑 · 10B 伴生评测 step 1000–4000 失败根因排查',
    workspace: 'evo_workspace',
    branch: 'feishu-triage',
    bucket: 'done',
    statusLabel: 'InfoWall + Auto-closed',
    statusTone: 'done',
    outcomeSummary: '定位 kvheads=1 一维权重挤压断言失败与 v948 模板端口不足双根因；已写入 InfoWall 并将 140 字回复草稿发至飞书私聊。',
    elapsed: '6m',
    updatedAgo: '1h ago',
    checksLabel: 'InfoWall synced · 0 residue',
    consensus: {
      implementer: 'Argon High',
      reviewer: 'Ted (Auto-Close)',
      roundsLabel: '只读取证 → InfoWall 留档 → 自动释放会话',
      verdictLabel: 'AUTO-ARCHIVED',
      verdictTone: 'auto',
      metricHighlight: '6m 闭环 · 飞书私聊草稿已推送',
    },
    links: [
      {
        label: 'InfoWall 需求与进展记录',
        url: 'https://26ya5x44im.merlin-devbox.bytedance.net',
        kind: 'infowall',
        meta: 'InfoWall Workbench · 已归档关联飞书话题',
      },
    ],
    timeline: [
      {
        id: 'd1',
        stage: '飞书巡检捕获未回话题 @提及',
        shortStage: 'Patrol',
        actor: 'Ted',
        actorRole: 'orchestrator',
        tone: 'neutral',
        timeLabel: '18:24',
        deltaLabel: '0m',
        epochLabel: 'AUTONOMOUS FEISHU TRIAGE & INFOWALL SYNC',
        summary: '「10B eval干活群」话题内检测到异常求助，自动拉起只读 ephemeral 排查任务。',
      },
      {
        id: 'd2',
        stage: 'HDFS 权重头与模板参数双根因取证',
        shortStage: 'Diagnose',
        actor: 'Argon High',
        actorRole: 'worker',
        tone: 'pass',
        timeLabel: '18:29',
        deltaLabel: '+5m',
        badge: '2 ROOT CAUSES',
        summary: '核实 safetensors 形状 (128,) 及 v948 模板 16 端口限制，生成群回复草稿。',
        receipt: 'safetensors header: wqkv shape=(128,) when kvheads=1 · port cap=16',
        sessionTabId: '4cd82e90',
        sessionArchived: true,
      },
      {
        id: 'd3',
        stage: 'InfoWall 留档 + 飞书私聊推送 + 自动结项回收',
        shortStage: 'Closed',
        actor: 'Ted',
        actorRole: 'orchestrator',
        tone: 'pass',
        timeLabel: '18:30',
        deltaLabel: '+1m',
        badge: 'CLEANUP DONE',
        summary: '自动执行 task accept --cleanup-session，零残留会话。',
        receipt: 'infowall sync ok · ephemeral session 4cd82e90 released',
      },
    ],
    transcriptExcerpt: [
      { speaker: 'Argon High', role: 'worker', time: '18:29', text: 'Root cause 1: kvheads=1 1D tensor squeeze assert. Root cause 2: v948 port limit.' },
      { speaker: 'Ted', role: 'system', time: '18:30', text: 'Synced to InfoWall & sent reply draft to Feishu DM; ephemeral session released.' },
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
    code: 'CH-65BB',
    title: 'Claude Hub SQLite WAL 存储收敛与 InfoWall 无损切流',
    workspace: 'claude_hub',
    branch: 'codex/sqlite-storage',
    bucket: 'done',
    statusLabel: 'Merged · 65bba11',
    statusTone: 'done',
    outcomeSummary: '活跃数据体积下降 46.1%，单次任务保存提速 4 倍、流尾读取快 191 倍；InfoWall 1,698 条记录 0 误差衔接。',
    elapsed: '42m',
    updatedAgo: '3h ago',
    diffStat: { added: 1120, removed: 684, files: 18 },
    checksLabel: '7,539 tests passed',
    consensus: {
      implementer: 'Codex Sol',
      reviewer: 'Argon High',
      roundsLabel: 'R1 修复 1 P1 → 全量校验快进合入 main',
      verdictLabel: 'MERGED · 65bba11',
      verdictTone: 'pass',
      metricHighlight: 'Save 4x faster · Tail read 191x faster',
    },
    links: [
      {
        label: 'Commit 65bba11',
        url: 'https://github.com/Pegessi/claude_hub',
        kind: 'commit',
        meta: 'main · +1,120 −684 (18 files)',
      },
    ],
    timeline: [
      {
        id: 's1',
        stage: '统一 SQLite WAL 存储实现与 222 万条事件压测',
        shortStage: 'Impl',
        actor: 'Codex Sol',
        actorRole: 'worker',
        tone: 'neutral',
        timeLabel: '00:05',
        deltaLabel: '+26m',
        epochLabel: 'IMPLEMENTATION & FAST-FORWARD MERGE',
        badge: '−46.1% SIZE',
        summary: '收敛 353 条流与 16 份状态文档，移除双写包装层。',
        receipt: '2.22M events migrated · tail read 191x faster',
        sessionTabId: 'fd83a1cb',
        sessionArchived: true,
      },
      {
        id: 's2',
        stage: '异构审查修复 1 P1 + 快进合入 main',
        shortStage: 'Merged',
        actor: 'Argon High',
        actorRole: 'reviewer',
        tone: 'pass',
        timeLabel: '00:31',
        deltaLabel: '+16m',
        badge: '7,539 PASSED',
        summary: '修复顶层导入触碰默认库问题，6,260 后端 + 1,279 前端单测全绿后自动关单。',
        receipt: 'verify.sh all: 6,260 pytest + 1,279 frontend passed · ff-merged 65bba11',
        sessionTabId: 'd092ac57',
        sessionArchived: true,
      },
    ],
    transcriptExcerpt: [
      { speaker: 'Argon High', role: 'reviewer', time: '00:31', text: 'Fast-forwarded main to 65bba11; worker and reviewer sessions cleanly archived.' },
    ],
    internalMeta: {
      control: 'workspace',
      epoch: 1,
      mode: 'reviewed',
      seat: 'cleaned',
    },
  },
]
