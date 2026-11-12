/**
 * Realistic multi-workspace dataset and style descriptors for comparing three
 * Agent Workspace UX directions side-by-side in a live Vue/Pinia demo.
 */

export type DemoStyleId = 'triage_timeline' | 'walkthrough_board' | 'command_center'

export interface DemoStyleOption {
  id: DemoStyleId
  code: string
  name: string
  inspiration: string
  tagline: string
}

export const DEMO_STYLES: readonly DemoStyleOption[] = [
  {
    id: 'triage_timeline',
    code: 'Style A',
    name: 'Triage Feed + Run Timeline',
    inspiration: 'Linear + GitHub Agent HQ',
    tagline: 'Cross-workspace triage list (Waiting on you / Working / Done) paired with a zero-clutter 3-section Run Timeline drawer and archived-chat deep links.',
  },
  {
    id: 'walkthrough_board',
    code: 'Style B',
    name: 'Compact Board + Walkthrough Card',
    inspiration: 'Antigravity + Cursor 2.2',
    tagline: '3-column scannable board (Needs Review / In Progress / Done) whose detail opens as a structured Deliverable Walkthrough with consensus review rounds.',
  },
  {
    id: 'command_center',
    code: 'Style C',
    name: 'Live Command Center',
    inspiration: 'Devin 2.0 + Warp',
    tagline: 'Unified Task + Session cards showing live 2-line activity previews, inline milestone dots, and a side-by-side Timeline + Read-only Transcript split pane.',
  },
]

export interface DemoExternalLink {
  label: string
  kind: 'mr' | 'doc' | 'arena' | 'infowall'
  url: string
}

export interface DemoTimelineNode {
  id: string
  stage: string
  actor: string
  modelBadge: string
  tone: 'start' | 'work' | 'review_block' | 'review_pass' | 'done' | 'live'
  timeLabel: string
  deltaLabel: string
  summary: string
  verification?: string
  sessionTabId?: string
  sessionArchived?: boolean
}

export interface DemoTranscriptLine {
  role: 'user' | 'agent' | 'tool' | 'reviewer'
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
  statusBadge: string
  statusTone: 'attention' | 'working' | 'done' | 'failed'
  decisionPrompt?: string
  outcomeHeadline: string
  liveActivityLine?: string
  liveStepCounts?: string
  elapsed: string
  updatedAgo: string
  links: DemoExternalLink[]
  acceptancePassed: number
  acceptanceTotal: number
  changedFiles: string[]
  reviewRoundsSummary: string
  timeline: DemoTimelineNode[]
  transcriptPreview: DemoTranscriptLine[]
  rawInternalMeta: {
    executionControl: string
    epoch: number
    taskMode: string
    sessionSeat: string
    rawPromptPreview: string
  }
}

export const DEMO_TASKS: readonly DemoTaskRecord[] = [
  {
    id: '47e984bf',
    title: 'MR !3953 细粒度生命周期打点：补齐同 session_id 跨轮次串联与多模态实测',
    workspace: 'evo_workspace',
    branch: 'wzh/request-lifecycle-events',
    bucket: 'needs_you',
    statusBadge: 'Awaiting acceptance · Consensus 2/2',
    statusTone: 'attention',
    decisionPrompt: 'Astra 交叉复审已通过（0 P0/P1）：+57 行补齐 session_id、Duplex 多轮帧与 ForkSession 血缘，H20 真机 700 条事件零丢失。确认是否合入 Draft MR !3953？',
    outcomeHeadline: '已完成 Hub→Server 25 类全阶段事件打点，OMNI 多模态与 6.6K Prefix-Cache 实测开销 ~0%，两轮异构审查全绿。',
    elapsed: '38m',
    updatedAgo: '4m ago',
    links: [
      { label: 'llmserver MR !3953', kind: 'mr', url: 'https://[redacted]/[redacted]/merge_requests/3953' },
      { label: '飞书全链路瀑布图报告', kind: 'doc', url: '[redacted]' },
      { label: 'InfoWall · P0 需求', kind: 'infowall', url: 'https://[redacted]' },
    ],
    acceptancePassed: 5,
    acceptanceTotal: 5,
    changedFiles: [
      'llmhub/router/session_trace.cc',
      'llmserver/core/lifecycle_events.py',
      'llmserver/omni/embed_tracer.py',
      'tests/test_lifecycle_session_lineage.py',
    ],
    reviewRoundsSummary: 'Round 1 ([redacted]): BLOCK (1 P1 missing ForkSession lineage) → Fixed → Round 2: PASS',
    timeline: [
      {
        id: 't1',
        stage: 'Dispatched in isolated worktree',
        actor: 'Ted (Dot)',
        modelBadge: 'orchestrator',
        tone: 'start',
        timeLabel: '12:21',
        deltaLabel: '0m',
        summary: 'Created worktree operator-trace-20261111 from master and bound ephemeral chat worker.',
      },
      {
        id: 't2',
        stage: 'Round 1 · Implementation & m13_tiny live verification',
        actor: 'Worker',
        modelBadge: 'MASO · Argon High',
        tone: 'work',
        timeLabel: '12:41',
        deltaLabel: '+20m',
        summary: 'Implemented +441 net LOC event hooks; ran 160 normal + KV eviction requests and published Feishu report.',
        verification: 'pytest tests/test_lifecycle*.py (717 passed) · local m13_tiny GPU MIX verified',
        sessionTabId: '6364744d',
        sessionArchived: true,
      },
      {
        id: 't3',
        stage: 'Cross-Review Round 1 · Blocking gap found',
        actor: 'Reviewer',
        modelBadge: 'Codex · [redacted]',
        tone: 'review_block',
        timeLabel: '12:52',
        deltaLabel: '+11m',
        summary: 'BLOCK (1 P1): sticky route retries and ForkSession did not propagate parent session_id in envelope.',
        sessionTabId: '67e8ab53',
        sessionArchived: true,
      },
      {
        id: 't4',
        stage: 'Round 2 · Lineage fix + Multimodal/8K scaling',
        actor: 'Worker',
        modelBadge: 'MASO · Argon High',
        tone: 'work',
        timeLabel: '13:02',
        deltaLabel: '+10m',
        summary: 'Added +57 LOC session_id & ForkSession propagation; verified 4.6K prefix-cache hit (222ms -> 99.4ms) and 3-image ViT AOTI.',
        verification: 'pytest 729 passed · 0 lost events across 700 captured frames',
        sessionTabId: '6364744d',
        sessionArchived: true,
      },
      {
        id: 't5',
        stage: 'Cross-Review Round 2 · Consensus PASS',
        actor: 'Reviewer',
        modelBadge: 'Codex · [redacted]',
        tone: 'review_pass',
        timeLabel: '13:06',
        deltaLabel: '+4m',
        summary: 'PASS (0 P0, 0 P1): verified C++/Python envelope parity and clean GPU process teardown.',
        sessionTabId: '67e8ab53',
        sessionArchived: true,
      },
    ],
    transcriptPreview: [
      { role: 'user', speaker: 'Goal Controller', time: '12:52', text: 'Address strict cross-review round 1: propagate session_id across sticky retries and ForkSession.' },
      { role: 'tool', speaker: 'Edit · session_trace.cc', time: '12:55', text: '+28 -4 lines · attach parent_session_id on fork & replay headers' },
      { role: 'tool', speaker: 'Bash · pytest', time: '13:00', text: 'tests/test_lifecycle_session_lineage.py .... 12 passed in 1.84s' },
      { role: 'reviewer', speaker: '[redacted] (R2)', time: '13:06', text: 'Verdict: PASS. All 5 acceptance criteria verified against git diff b2c4fe1.' },
    ],
    rawInternalMeta: {
      executionControl: 'workspace',
      epoch: 2,
      taskMode: 'reviewed',
      sessionSeat: 'ephemeral (auto-release on accept)',
      rawPromptPreview: '# Task brief from Dot\nGoal: Verify MR !3953 on local m13_tiny and ensure full session_id lifecycle...',
    },
  },
  {
    id: 'ce4513f8',
    title: 'EVO 接入 USM Audio Encoder（MR1 迁移打平 + MR2 H20 算子融合加速）',
    workspace: 'evo_workspace',
    branch: 'codex/evo-usm-encoder',
    bucket: 'needs_you',
    statusBadge: 'Needs decision · Stacked MRs ready',
    statusTone: 'attention',
    decisionPrompt: '双叠栈 MR 已就绪且 AudioProcessor 41 项单测 0.0 逐位对齐。是否直接将两个 Draft MR 转为 Ready 并通知群内评审？',
    outcomeHeadline: 'MR !1379 实现 Eager/AOTI 0.0 误差打平；MR !1385 在 H20 上取得 1.81x–5.58x 端到端加速。',
    elapsed: '54m',
    updatedAgo: '19m ago',
    links: [
      { label: 'xperf_evo MR !1379 (Parity)', kind: 'mr', url: 'https://[redacted]/seed/xperf_evo/merge_requests/1379' },
      { label: 'xperf_evo MR !1385 (Perf)', kind: 'mr', url: 'https://[redacted]/seed/xperf_evo/merge_requests/1385' },
    ],
    acceptancePassed: 4,
    acceptanceTotal: 4,
    changedFiles: [
      'xperf_evo/models/audio/usm_encoder.py',
      'xperf_evo/models/audio/audio_processor.py',
      'tests/audio/test_usm_triton_parity.py',
    ],
    reviewRoundsSummary: 'Round 1 (Argon High): PASS (0 P0/P1, added H20 latency table to MR !1379)',
    timeline: [
      {
        id: 'u1',
        stage: 'MR 1 · Migration, weight loader & AudioProcessor parity',
        actor: 'Worker',
        modelBadge: 'MASO · Argon High',
        tone: 'work',
        timeLabel: '18:53',
        deltaLabel: '+24m',
        summary: 'Aligned CMVN, padding and frame downsampling bitwise (max diff 0.0) with xperf_gpt_triton; exported AOTI bundle.',
        verification: '41 parity unit tests passed on H20',
        sessionTabId: '3b3f8742',
        sessionArchived: true,
      },
      {
        id: 'u2',
        stage: 'MR 2 · Fused Attention/Norm & CUDA Graph optimization',
        actor: 'Helper Subagent',
        modelBadge: 'Codex · [redacted]',
        tone: 'work',
        timeLabel: '19:21',
        deltaLabel: '+28m',
        summary: 'Rebased perf branch onto MR !1379; benchmarked 0.6B rope_usm & 0.8B p6dense_usm (1.81x-5.58x speedup).',
        verification: 'H20 benchmark suite + full audio test suite green',
        sessionTabId: '57ad2f2d',
        sessionArchived: true,
      },
      {
        id: 'u3',
        stage: 'Independent Cross-Review · PASS',
        actor: 'Reviewer',
        modelBadge: 'MASO · Argon High',
        tone: 'review_pass',
        timeLabel: '19:32',
        deltaLabel: '+11m',
        summary: 'Verified zero steady-state AOTI memory overhead and exact tensor shapes across batch sizes.',
        sessionTabId: '8598b9fc',
        sessionArchived: true,
      },
    ],
    transcriptPreview: [
      { role: 'agent', speaker: 'Argon High', time: '19:18', text: 'AudioProcessor bitwise parity confirmed: max_abs_diff = 0.000000 across all 41 mel/CMVN edge cases.' },
      { role: 'tool', speaker: 'Bash · bench_usm_h20.py', time: '19:26', text: 'p6dense_usm B=16: Eager 41.2ms -> Fused AOTI 7.38ms (5.58x speedup)' },
      { role: 'reviewer', speaker: 'Reviewer', time: '19:32', text: 'Both stacked MRs are clean, minimal, and ready for human sign-off.' },
    ],
    rawInternalMeta: {
      executionControl: 'workspace',
      epoch: 1,
      taskMode: 'reviewed',
      sessionSeat: 'evo-workspace-agent-4 (archived)',
      rawPromptPreview: 'Integrate USM Audio Encoder into xperf_evo following EVO ViT conventions in two stacked MRs...',
    },
  },
  {
    id: '9d6fb26a',
    title: '拉起 LF M15.3 DSA 256卡 H20 Arena 评估并产出 Perf Toolkit 对比报告',
    workspace: 'evo_workspace',
    branch: 'eval/m15-3-dsa-arena',
    bucket: 'working',
    statusBadge: 'Working · Step 3/4',
    statusTone: 'working',
    outcomeHeadline: 'Serving 256×H20 已就绪并完成 4,820 / 6,585 样本（73.2%），正在并发采集 P/D 阶段 SM 与 KV 水位曲线。',
    liveActivityLine: 'Sampling Grafana QPM/P99 & KV cache utilization on trial 449604337 (73% done)',
    liveStepCounts: '9 commands · 4 reads · 0 failed · 1 sub-agent',
    elapsed: '26m',
    updatedAgo: 'just now',
    links: [
      { label: 'Arena · pfas5zxr1l6af2f5ed', kind: 'arena', url: 'https://[redacted]/evaluation/arena/zvz2jc18yk67f66043' },
    ],
    acceptancePassed: 2,
    acceptanceTotal: 4,
    changedFiles: ['outputs/m15-3-dsa/perf_summary.json'],
    reviewRoundsSummary: 'Pending completion before final verification',
    timeline: [
      {
        id: 'w1',
        stage: 'Configuration readback & 256×H20 launch',
        actor: 'Worker',
        modelBadge: 'MASO · Argon High',
        tone: 'start',
        timeLabel: '22:18',
        deltaLabel: '0m',
        summary: 'Cloned baseline 8bi4ulgf3n6aeec0d4 with zero unintended diff; Serving job a0c9a489e1af587a entered RUNNING.',
        sessionTabId: '62506836',
      },
      {
        id: 'w2',
        stage: 'Mid-run health & tail-latency probe',
        actor: 'Worker',
        modelBadge: 'MASO · Argon High',
        tone: 'live',
        timeLabel: '22:44',
        deltaLabel: '+26m',
        summary: 'GPU utilization steady at 84.6% (above 30% kill line); 0 CUDA OOMs; 4,820/6,585 samples scored.',
        sessionTabId: '62506836',
      },
    ],
    transcriptPreview: [
      { role: 'tool', speaker: 'merlin-cli arena get', time: '22:42', text: 'status=RUNNING progress=4820/6585 (73.2%) qpm=148.4 gpu_util_avg=84.6%' },
      { role: 'agent', speaker: 'Argon High', time: '22:44', text: 'Throughput is healthy and KV slots stay below 78%; waiting for final 26% tail before generating Feishu perf report.' },
    ],
    rawInternalMeta: {
      executionControl: 'initiator',
      epoch: 1,
      taskMode: 'subagent',
      sessionSeat: 'ephemeral chat',
      rawPromptPreview: 'Launch LF M15.3 DSA evaluation against baseline and generate evoserver-perf-toolkit comparison...',
    },
  },
  {
    id: '43cedad4',
    title: 'GB200 SM100 算子级异步指标采集与开销实测（Nsight Perf SDK / CUPTI）',
    workspace: 'llmserver-evo-workspace',
    branch: 'feat/gb200-async-op-metrics',
    bucket: 'working',
    statusBadge: 'Cross-reviewing · Round 1/3',
    statusTone: 'working',
    outcomeHeadline: 'Worker 已完成 4×GB200 实机单 Pass 计数器采集验证（开销 0.4%），当前异构 Reviewer 正在核对 CUDA Graph 兼容性。',
    liveActivityLine: 'Subagent Argon High reviewing git diff (328 LOC) & GB200 benchmark logs…',
    liveStepCounts: '14 edits · 11 commands · 0 failed · Reviewer active',
    elapsed: '19m',
    updatedAgo: '1m ago',
    links: [
      { label: 'llmserver Draft MR !3961', kind: 'mr', url: 'https://[redacted]/[redacted]/merge_requests/3961' },
    ],
    acceptancePassed: 3,
    acceptanceTotal: 4,
    changedFiles: [
      'llmserver/profiler/cupti_range_sampler.py',
      'llmserver/profiler/gb200_counters.yaml',
      'tests/profiler/test_cupti_graph_replay.py',
    ],
    reviewRoundsSummary: 'Round 1 in progress (MASO · Argon High)',
    timeline: [
      {
        id: 'g1',
        stage: 'Implementation & 4×GB200 SSH live probe',
        actor: 'Worker',
        modelBadge: 'Codex · [redacted]',
        tone: 'work',
        timeLabel: '22:31',
        deltaLabel: '+16m',
        summary: 'Implemented single-pass CUPTI periodic sampler on n125-154-036 (driver 580.167); measured +0.4% latency delta.',
        verification: 'ssh tiger@gb200 pytest tests/profiler -> 28 passed',
        sessionTabId: 'a4c8a1ed',
      },
      {
        id: 'g2',
        stage: 'Strict Cross-Review Gate (Round 1/3)',
        actor: 'Reviewer',
        modelBadge: 'MASO · Argon High',
        tone: 'live',
        timeLabel: '22:47',
        deltaLabel: '+3m',
        summary: 'Checking multi-stream serialization and container privilege fallback paths.',
        sessionTabId: 'sa-91bc04',
      },
    ],
    transcriptPreview: [
      { role: 'agent', speaker: 'Codex Worker', time: '22:46', text: 'Emitted <goal-status state="complete"> after verifying 0.4% overhead on 4xGB200.' },
      { role: 'reviewer', speaker: 'Argon High (R1)', time: '22:47', text: 'Inspecting cupti_range_sampler.py:142 for unguarded context teardown during CUDA Graph capture...' },
    ],
    rawInternalMeta: {
      executionControl: 'workspace',
      epoch: 1,
      taskMode: 'reviewed',
      sessionSeat: 'llmserver-agent-2',
      rawPromptPreview: 'Implement async operator metrics on GB200 and measure overhead...',
    },
  },
  {
    id: 'd212d01c',
    title: '[飞书自动答疑] 10B eval干活群 @提及：伴生评测 step 1000-4000 失败双根因排查',
    workspace: 'evo_workspace',
    branch: 'read-only-triage',
    bucket: 'done',
    statusBadge: 'Auto-accepted · Ephemeral cleaned',
    statusTone: 'done',
    outcomeHeadline: '自动定位双根因（kvheads=1 量化压成 1D 致权重加载断言失败 + v948 模板 tp=1 端口仅分配 16 个），已留档 InfoWall 并推送飞书回复草稿。',
    elapsed: '6m',
    updatedAgo: '1h ago',
    links: [
      { label: 'InfoWall · 10B 伴生排障记录', kind: 'infowall', url: 'https://[redacted]' },
      { label: '飞书原话题消息', kind: 'doc', url: 'https://[redacted]' },
    ],
    acceptancePassed: 3,
    acceptanceTotal: 3,
    changedFiles: [],
    reviewRoundsSummary: 'Read-only triage verified by Dot · session auto-archived',
    timeline: [
      {
        id: 'd1',
        stage: 'Feishu @-mention patrol triggered ephemeral task',
        actor: 'Ted (Dot)',
        modelBadge: 'patrol',
        tone: 'start',
        timeLabel: '18:24',
        deltaLabel: '0m',
        summary: 'Detected unanswered @-mention in thread of「10B eval干活群」; spawned read-only ephemeral worker.',
      },
      {
        id: 'd2',
        stage: 'Log + HDFS checkpoint header + template root-cause',
        actor: 'Worker',
        modelBadge: 'MASO · Argon High',
        tone: 'work',
        timeLabel: '18:29',
        deltaLabel: '+5m',
        summary: 'Verified 1D (128,) kv_chunk shape in HDFS safetensors header and 16-vs-20 port gap in EVO_SERVER:v948.',
        sessionTabId: '4cd82e90',
        sessionArchived: true,
      },
      {
        id: 'd3',
        stage: 'InfoWall archived + Feishu draft sent + Session cleaned',
        actor: 'Ted (Dot)',
        modelBadge: 'auto-close',
        tone: 'done',
        timeLabel: '18:30',
        deltaLabel: '+1m',
        summary: 'Recorded progress to InfoWall demand, sent 140-char Chinese reply draft to user DM, and cleaned up ephemeral session.',
      },
    ],
    transcriptPreview: [
      { role: 'tool', speaker: 'merlin-cli job logs', time: '18:26', text: 'AssertionError: expected 2D kv_chunk [num_kv_heads, head_dim], got shape (128,)' },
      { role: 'agent', speaker: 'Dot Summary', time: '18:30', text: 'Draft sent to Feishu DM & task auto-accepted (--cleanup-session).' },
    ],
    rawInternalMeta: {
      executionControl: 'workspace',
      epoch: 1,
      taskMode: 'subagent',
      sessionSeat: 'ephemeral (cleaned up)',
      rawPromptPreview: 'Read-only triage of Feishu @-mention in 10B eval group...',
    },
  },
  {
    id: '65bba110',
    title: 'Claude Hub SQLite WAL 统一存储收敛 + InfoWall 增量水位无缝切换',
    workspace: 'claude_hub',
    branch: 'codex/sqlite-storage',
    bucket: 'done',
    statusBadge: 'Done · Merged 65bba11',
    statusTone: 'done',
    outcomeHeadline: '活跃体积 -46.1%（1997MB→1076MB），单次任务保存快 4 倍、流尾读取快 191 倍，1,698 条 InfoWall 增量记录 0 误差对齐。',
    elapsed: '42m',
    updatedAgo: '3h ago',
    links: [
      { label: 'Working Log · sqlite-storage', kind: 'doc', url: 'https://github.com/Pegessi/claude_hub' },
    ],
    acceptancePassed: 6,
    acceptanceTotal: 6,
    changedFiles: [
      'backend/claude_hub/services/local_storage.py',
      'backend/claude_hub/services/storage_maintenance.py',
    ],
    reviewRoundsSummary: 'Round 1 (Argon High): fixed 1 P1 + 6 P2s → Round 2: MERGE (6,260 backend + 1,279 frontend tests green)',
    timeline: [
      {
        id: 's1',
        stage: 'SQLite WAL implementation & benchmark on 2.22M events',
        actor: 'Worker',
        modelBadge: 'Codex · [redacted]',
        tone: 'work',
        timeLabel: '00:05',
        deltaLabel: '+26m',
        summary: 'Consolidated 353 streams and 16 state documents into storage.sqlite3 with zstd/zlib compression.',
        sessionTabId: 'fd83a1cb',
        sessionArchived: true,
      },
      {
        id: 's2',
        stage: 'Adversarial Cross-Review + Fix + Fast-forward merge',
        actor: 'Reviewer',
        modelBadge: 'MASO · Argon High',
        tone: 'done',
        timeLabel: '00:31',
        deltaLabel: '+16m',
        summary: 'Fixed eager runtime import bug (P1) and SHA-256 row cache; fast-forwarded main and auto-cleaned sessions.',
        sessionTabId: 'd092ac57',
        sessionArchived: true,
      },
    ],
    transcriptPreview: [
      { role: 'reviewer', speaker: 'Argon High', time: '00:28', text: 'Verified 6,260 backend + 1,279 frontend tests pass; cold workspace read improved 165.5ms -> 58.4ms.' },
    ],
    rawInternalMeta: {
      executionControl: 'workspace',
      epoch: 1,
      taskMode: 'reviewed',
      sessionSeat: 'claude-hub-agent-4 (cleaned up)',
      rawPromptPreview: 'Consolidate JSON and JSONL persistence into SQLite WAL...',
    },
  },
]
