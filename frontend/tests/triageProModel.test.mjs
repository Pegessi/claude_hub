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
const triageSource = transpileFile('../src/features/workspace/presentation/triageProModel.ts')
  .replaceAll("from '@/utils/duration'", `from ${JSON.stringify(durationMod)}`)
  .replaceAll("from './evidence'", `from ${JSON.stringify(evidenceMod)}`)

const triage = await import(asModule(triageSource))

const NOW_MS = Date.parse('2026-11-13T10:30:00Z')

function makeTask(overrides = {}) {
  return {
    id: '47e984bf-da2e-4488-bf27-188273d4927f',
    workspace_id: 'ws-evo',
    title: 'MR !3953 lifecycle session_id lineage fix',
    prompt: 'Verify https://code.byted.org/seed/llmserver/merge_requests/3953 and report.',
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
  assert.equal(triage.classifyTriageBucket(makeTask({ status: 'review' })), 'needs_you')
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
        title: 'GPT-6-Astra',
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
      message: 'Implemented hooks and published [Feishu report](https://bytedance.larkoffice.com/docx/TzbmdyYlioujByxeHPUcSSUCnAf)',
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
  assert.equal(vm.bucket, 'needs_you')
  assert.equal(vm.statusLabel, 'R2 Review PASS')
  assert.equal(vm.consensus.implementer, 'Argon High')
  assert.equal(vm.consensus.reviewer, 'GPT-6-Astra')
  assert.equal(vm.consensus.verdictLabel, 'PASS · VERIFIED')
  assert.equal(vm.links.length, 2)
  assert.deepEqual(vm.links.map(l => l.kind).sort(), ['doc', 'mr'])
  assert.equal(vm.spine.length, 4) // created + 3 reports
  assert.equal(vm.spine[2].tone, 'warn')
  assert.equal(vm.spine[3].tone, 'pass')
  assert.equal(vm.spine[3].badge, 'VERDICT PASS')

  const groups = triage.groupTriageTasks([vm])
  assert.equal(groups[0].key, 'needs_you')
  assert.equal(groups[0].items.length, 1)
})
