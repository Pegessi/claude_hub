import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

function transpileFile(relPath) {
  const source = readFileSync(new URL(relPath, import.meta.url), 'utf8')
  return ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
  }).outputText
}

const asModule = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`

const durationMod = asModule(transpileFile('../src/utils/duration.ts'))
const reportsMod = asModule(transpileFile('../src/features/workspace/presentation/reports.ts'))
const evidenceMod = asModule(
  transpileFile('../src/features/workspace/presentation/evidence.ts').replaceAll(
    "from './reports'",
    `from ${JSON.stringify(reportsMod)}`,
  ),
)
const digestMod = asModule(
  transpileFile('../src/features/workspace/presentation/triageDigest.ts')
    .replaceAll("from '@/utils/duration'", `from ${JSON.stringify(durationMod)}`)
    .replaceAll("from './evidence'", `from ${JSON.stringify(evidenceMod)}`)
    .replaceAll("from './reports'", `from ${JSON.stringify(reportsMod)}`),
)
const triageSource = transpileFile('../src/features/workspace/presentation/triageProModel.ts')
  .replaceAll("from '@/utils/duration'", `from ${JSON.stringify(durationMod)}`)
  .replaceAll("from './evidence'", `from ${JSON.stringify(evidenceMod)}`)
  .replaceAll("from './triageDigest'", `from ${JSON.stringify(digestMod)}`)

const triage = await import(asModule(triageSource))

const NOW_MS = Date.parse('2026-11-13T10:30:00Z')

function makeTask(overrides = {}) {
  return {
    id: '47e984bf-da2e-4488-bf27-188273d4927f',
    workspace_id: 'ws-evo',
    title: 'MR !3953 lifecycle session_id lineage fix',
    prompt: 'Verify https://git.example.com/team/project/merge_requests/3953 and report.',
    attachments: [],
    agent_type: 'maso',
    session_kind: 'chat',
    task_mode: 'reviewed',
    execution_complexity: 'complex',
    execution_control: 'workspace',
    execution_epoch: 2,
    status: 'review',
    dispatch_pending: false,
    review_attempts: 2,
    human_acceptance_requested_at: '2026-11-13T10:20:00Z',
    started_at: '2026-11-13T09:50:00Z',
    created_at: '2026-11-13T09:48:00Z',
    updated_at: '2026-11-13T10:25:00Z',
    ...overrides,
  }
}

test('deriveTaskCode extracts MR/PR identifiers or falls back to clean short hash', () => {
  const mrTask = makeTask()
  assert.equal(triage.deriveTaskCode(mrTask, []), 'MR-3953')

  const plainTask = makeTask({ title: 'Investigate GB200 CUPTI overhead', prompt: 'No link yet' })
  assert.equal(triage.deriveTaskCode(plainTask, []), '#47E984')
})

test('classifyTriageBucket routes review, failed, attention, working, and done accurately', () => {
  assert.equal(triage.classifyTriageBucket(makeTask({ status: 'review' })), 'review')
  assert.equal(
    triage.classifyTriageBucket(makeTask({ status: 'working' }), {
      task_id: 'x',
      phase: 'running',
      reason: 'mr_open',
      reason_text: 'MR !4164 open',
      owner: { kind: 'tab', state: 'idle' },
      attention: false,
      holds_session: false,
      delivery_mr_state: 'open',
      delivery_mr_label: 'MR !4164',
      since: '2026-11-13T10:00:00Z',
      legacy_status: 'working',
    }),
    'review',
  )
  assert.equal(triage.classifyTriageBucket(makeTask({ status: 'failed' })), 'needs_you')
  assert.equal(triage.classifyTriageBucket(makeTask({ status: 'working' })), 'working')
  assert.equal(
    triage.classifyTriageBucket(makeTask({ status: 'working' }), {
      task_id: 'x',
      phase: 'needs_user',
      reason: 'question',
      reason_text: 'Waiting on answer',
      owner: { kind: 'session', state: 'idle' },
      attention: true,
      holds_session: true,
      since: '2026-11-13T10:00:00Z',
      legacy_status: 'working',
    }),
    'needs_you',
  )
  assert.equal(triage.classifyTriageBucket(makeTask({ status: 'done' })), 'done')
  const archivedDone = triage.buildTriageTaskViewModel({
    task: makeTask({ status: 'done', closed_reason: 'chat_archived', human_accepted_at: null }),
    workspaceName: 'evo_workspace',
    reports: [],
    events: [],
    sessionsById: new Map(),
    nowMs: NOW_MS,
  })
  assert.equal(archivedDone.bucket, 'done')
  assert.equal(archivedDone.statusLabel, 'Archived')
  assert.equal(archivedDone.checksLabel, 'Closed by archive')
  assert.equal(archivedDone.consensus.verdictLabel, 'CLOSED BY ARCHIVE')

  const mergedDone = triage.buildTriageTaskViewModel({
    task: makeTask({ status: 'done', closed_reason: 'mr_merged', human_accepted_at: null }),
    workspaceName: 'evo_workspace',
    reports: [],
    events: [],
    sessionsById: new Map(),
    nowMs: NOW_MS,
  })
  assert.equal(mergedDone.statusLabel, 'MR Merged')
  assert.equal(mergedDone.checksLabel, 'MR merged')
})

test('buildTriageTaskViewModel constructs consensus, artifact links, and multi-round spine', () => {
  const sessions = new Map([
    [
      'worker-1',
      {
        id: 'worker-1',
        tab_id: 'tab-worker',
        title: 'Argon High',
        branch: 'wzh/lifecycle',
        agent_type: 'maso',
        ephemeral: true,
      },
    ],
    [
      'rev-1',
      {
        id: 'rev-1',
        tab_id: 'tab-rev',
        title: 'GPT-example-model-d050a243e4ed',
        agent_type: 'codex',
        ephemeral: true,
      },
    ],
  ])

  const reports = [
    {
      id: 'r1',
      workspace_id: 'ws-evo',
      task_id: '47e984bf-da2e-4488-bf27-188273d4927f',
      session_id: 'worker-1',
      state: 'ready_for_review',
      message: 'Implemented hooks and published [Feishu report](https://docs.example.com/docx/TzbmdyYlioujByxeHPUcSSUCnAf)',
      changed_files: ['llmhub/session_trace.cc', 'llmserver/events.py'],
      validation: 'pytest 717 passed',
      review_decision: 'none',
      created_at: '2026-11-13T10:05:00Z',
    },
    {
      id: 'r2',
      workspace_id: 'ws-evo',
      task_id: '47e984bf-da2e-4488-bf27-188273d4927f',
      session_id: 'rev-1',
      state: 'review_failed',
      message: '1 P1: missing ForkSession lineage on retry',
      changed_files: [],
      review_decision: 'reject',
      created_at: '2026-11-13T10:12:00Z',
    },
    {
      id: 'r3',
      workspace_id: 'ws-evo',
      task_id: '47e984bf-da2e-4488-bf27-188273d4927f',
      session_id: 'rev-1',
      state: 'review_passed',
      message: 'All lineage checks verified (0 P0/P1).',
      changed_files: ['llmhub/session_trace.cc', 'llmserver/events.py', 'tests/test_lineage.py'],
      validation: 'pytest 729 passed',
      review_decision: 'approve',
      created_at: '2026-11-13T10:22:00Z',
    },
  ]

  const vm = triage.buildTriageTaskViewModel({
    task: makeTask({ session_id: 'worker-1', review_session_id: 'rev-1' }),
    workspaceName: 'evo_workspace',
    defaultBranch: 'main',
    reports,
    sessionsById: sessions,
    nowMs: NOW_MS,
  })

  assert.equal(vm.code, 'MR-3953')
  assert.equal(vm.bucket, 'review')
  assert.equal(vm.statusTone, 'review')
  assert.equal(vm.statusLabel, 'R2 Review PASS')
  assert.equal(vm.consensus.implementer, 'Argon High')
  assert.equal(vm.consensus.reviewer, 'GPT-example-model-d050a243e4ed')
  assert.equal(vm.consensus.verdictLabel, 'PASS · VERIFIED')
  assert.equal(vm.links.length, 2)
  assert.deepEqual(vm.links.map(l => l.kind).sort(), ['doc', 'mr'])
  assert.equal(vm.spine.length, 4) // created + 3 reports
  assert.equal(vm.spine[2].tone, 'warn')
  assert.equal(vm.spine[3].tone, 'pass')
  assert.equal(vm.spine[3].badge, 'VERDICT PASS')

  const groups = triage.groupTriageTasks([vm])
  assert.deepEqual(groups.map(g => g.label), ['NEEDS YOU', 'WORKING', 'REVIEW', 'DONE'])
  assert.equal(groups[2].key, 'review')
  assert.equal(groups[2].items.length, 1)
  assert.equal(vm.hasIndependentReview, true)
  assert.equal(vm.reviewSummaryChip, 'Argon High → GPT-example-model-d050a243e4ed · R2 PASS')
  assert.equal(vm.milestones.at(-1).isCurrentEcho, true)
})

test('buildTriageTaskViewModel denoises dense initiator prose, extracts .md deliverables, and clusters events without duplication', () => {
  const initiatorTask = makeTask({
    id: 'e76ff774-0053-4953-9b89-6a078f8bd90c',
    title: 'UI upgrade: needs-you inbox, decision cards, evidence-first task detail',
    prompt: 'Fan-out implementation of 6 UI upgrades.',
    execution_control: 'initiator',
    progress_revision: 4,
    status: 'working',
    latest_progress: {
      state: 'needs_input',
      summary:
        'A third, cross-model review (Gemini-4 argon via MASO) found 10 items the first two rounds missed: 1 blocking (live work log froze for streaming turn), 2 high (plan_first approval bypass). All were fixed and merged into `feat/ui-upgrade-batch-20261109` @ `2e743b4` (60 commits, 119 files). Remaining decision is the user\'s: merge to main and push, or stop at branch delivery.',
      validation:
        'At 2e743b4: frontend-lint PASS; frontend-types PASS; frontend-tests 1393/0 PASS; browser-mocks 21/21 PASS',
      risks: 'New backend status fields still unverified against a real backend.',
      artifact_refs: [
        '/home/tiger/claude_hub_workspace/ui-fanout-20261109/REVIEW_ARGON.md',
        '/home/tiger/claude_hub_workspace/ui-fanout-20261109/FINAL_STATUS.md',
      ],
      reported_at: '2026-11-10T00:43:00Z',
    },
  })

  const events = [
    {
      sequence: 1,
      type: 'progress',
      action: 'task_progress',
      created_at: '2026-11-09T16:15:00',
      payload: {
        state: 'started',
        summary: 'Created isolated integration worktree and dispatched 6 feature subagents.',
      },
    },
    {
      sequence: 2,
      type: 'progress',
      action: 'task_progress',
      created_at: '2026-11-09T16:43:00',
      payload: {
        state: 'needs_input',
        summary: initiatorTask.latest_progress.summary,
      },
    },
  ]

  const vm = triage.buildTriageTaskViewModel({
    task: initiatorTask,
    workspaceName: 'claude_hub',
    defaultBranch: 'main',
    reports: [],
    events,
    sessionsById: new Map(),
    nowMs: NOW_MS,
  })

  assert.equal(vm.code, '#E76FF7')
  assert.equal(vm.hasIndependentReview, false)
  assert.equal(vm.reviewSummaryChip, null)
  assert.equal(
    vm.digest.actionPrompt,
    'Decision needed: merge to main and push, or stop at branch delivery.',
  )
  assert.match(vm.digest.headline, /A third, cross-model review/)
  assert.ok(vm.digest.bullets.some(b => b.label === 'Breakdown'))
  assert.ok(vm.deliverables.some(d => d.kind === 'markdown' && d.title === 'REVIEW_ARGON.md'))
  assert.ok(vm.deliverables.some(d => d.kind === 'markdown' && d.title === 'FINAL_STATUS.md'))
  assert.ok(
    vm.deliverables.some(
      d => d.kind === 'branch' && d.title === 'feat/ui-upgrade-batch-20261109 @ 2e743b4',
    ),
  )
  assert.equal(vm.validation.passedCount, 4)
  assert.equal(vm.validation.riskBullets.length, 1)
  assert.equal(vm.milestones.length, 3) // start + 2 progress events
  assert.equal(vm.milestones[1].title, 'Workspace & environment initialized')
  assert.equal(vm.milestones[2].isCurrentEcho, true)
  assert.doesNotMatch(vm.milestones[2].oneLiner, /A third, cross-model review/)
})

test('buildKeyMilestones folds 50+ iterative initiator working events into bounded phases', () => {
  const task = makeTask({
    id: 'b1b6c019-625f-4f70-910b-95e2e4ecdfae',
    title: '将 GB200 算子监控开销迭代压至接近零',
    execution_control: 'initiator',
    progress_revision: 56,
    status: 'working',
    latest_progress: {
      state: 'working',
      summary: 'Independent review APPROVE for cpu-callers4d diagnostic conclusions.',
      reported_at: '2026-11-13T02:59:37Z',
    },
  })

  const events = [
    {
      sequence: 1,
      type: 'progress',
      action: 'task_progress',
      created_at: '2026-11-12T16:53:44',
      payload: { state: 'started', summary: 'Initialized isolated GB200 ablation directory.' },
    },
    ...Array.from({ length: 55 }, (_, i) => ({
      sequence: i + 2,
      type: 'progress',
      action: 'task_progress',
      created_at: '2026-11-12T18:00:00',
      payload: {
        state: 'working',
        summary:
          i === 54
            ? 'Independent review APPROVE for cpu-callers4d diagnostic conclusions.'
            : `Ablation iteration #${i + 1} completed and verified.`,
      },
    })),
  ]

  const vm = triage.buildTriageTaskViewModel({
    task,
    workspaceName: 'llmserver-evo-workspace',
    defaultBranch: 'main',
    reports: [],
    events,
    sessionsById: new Map(),
    nowMs: NOW_MS,
  })

  assert.ok(vm.milestones.length <= 8, `expected <= 8 milestones, got ${vm.milestones.length}`)
  assert.ok(vm.milestones.some(m => (m.collapsedCount ?? 0) > 10))
  assert.equal(vm.milestones.at(-1).isCurrentEcho, true)
})

test('groupTriageTasks previews top 5 DONE tasks by default, supports Show more and section collapse, and keeps selected task visible', () => {
  const doneVms = Array.from({ length: 12 }, (_, idx) =>
    triage.buildTriageTaskViewModel({
      task: makeTask({
        id: `done-${idx}`,
        title: `Done task ${idx}`,
        status: 'done',
        updated_at: `2026-11-${String(22 - idx).padStart(2, '0')}T10:00:00Z`,
      }),
      workspaceName: 'evo_workspace',
      reports: [],
      events: [],
      sessionsById: new Map(),
      nowMs: NOW_MS,
    }),
  )

  const defaultDone = triage.groupTriageTasks(doneVms).find(g => g.key === 'done')
  assert.equal(defaultDone.items.length, 12)
  assert.equal(defaultDone.visibleItems.length, triage.TRIAGE_DONE_PREVIEW_SIZE)
  assert.equal(defaultDone.hiddenCount, 7)
  assert.equal(defaultDone.expanded, false)
  assert.deepEqual(
    defaultDone.visibleItems.map(i => i.id),
    ['done-0', 'done-1', 'done-2', 'done-3', 'done-4'],
  )

  // Selecting an older DONE task keeps it visible in the 5th slot during preview
  const withOlderSelected = triage
    .groupTriageTasks(doneVms, { selectedTaskId: 'done-9' })
    .find(g => g.key === 'done')
  assert.deepEqual(
    withOlderSelected.visibleItems.map(i => i.id),
    ['done-0', 'done-1', 'done-2', 'done-3', 'done-9'],
  )
  assert.equal(withOlderSelected.hiddenCount, 7)

  // Expanding DONE reveals all 12
  const expandedDone = triage
    .groupTriageTasks(doneVms, { doneExpanded: true })
    .find(g => g.key === 'done')
  assert.equal(expandedDone.visibleItems.length, 12)
  assert.equal(expandedDone.hiddenCount, 0)
  assert.equal(expandedDone.expanded, true)

  // Collapsing DONE hides rows (except keeping active item visible if selected)
  const collapsedDone = triage
    .groupTriageTasks(doneVms, { collapsed: new Set(['done']) })
    .find(g => g.key === 'done')
  assert.equal(collapsedDone.collapsed, true)
  assert.deepEqual(collapsedDone.visibleItems, [])
  assert.equal(collapsedDone.hiddenCount, 12)
})
