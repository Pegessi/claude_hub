import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import {
  loadWorkspaceFeature,
  mountWorkspaceConsumer,
  vue,
} from './workspaceFeatureTestHarness.mjs'

// The task detail dialog presents evidence before internal bookkeeping. These
// checks pin the two behaviours a reviewer depends on: which report/progress
// text becomes the headline, and how acceptance criteria acquire their verdict.

const evidence = await loadWorkspaceFeature('presentation/evidence.ts')
const board = await loadWorkspaceFeature('presentation/board.ts')

function report(overrides = {}) {
  return {
    id: 'report-1',
    workspace_id: 'ws-1',
    task_id: 'task-1',
    session_id: 'agent-1',
    state: 'ready_for_review',
    message: 'done',
    changed_files: [],
    created_at: '2026-10-24T09:00:00',
    ...overrides,
  }
}

function task(overrides = {}) {
  return {
    id: 'task-1',
    workspace_id: 'ws-1',
    title: 'Task',
    prompt: 'Do the thing',
    attachments: [],
    goal_packet: null,
    agent_type: 'codex',
    session_kind: 'managed',
    task_mode: 'reviewed',
    execution_complexity: 'auto',
    status: 'working',
    dispatch_pending: false,
    review_attempts: 0,
    created_at: '2026-10-24T08:00:00',
    updated_at: '2026-10-24T09:00:00',
    latest_progress: null,
    ...overrides,
  }
}

function progress(overrides = {}) {
  return {
    state: 'working',
    summary: 'Half done',
    validation: null,
    risks: null,
    artifact_refs: [],
    call_id: 'call-1',
    execution_epoch: 1,
    reported_at: '2026-10-24T09:10:00',
    ...overrides,
  }
}

test('the newest substantive report supplies evidence, not the newest report', () => {
  const reports = [
    report({ id: 'substantive', validation: 'pytest -> 12 passed' }),
    report({ id: 'stub', validation: null, state: 'working' }),
  ]
  assert.equal(evidence.evidenceReport(reports).id, 'substantive')
  assert.equal(evidence.evidenceReport([]), null)
})

test('a failed task leads with the failure reason, everything else with progress', () => {
  const failed = evidence.taskEvidence(
    task({
      status: 'failed',
      failure_reason: 'Dispatcher stopped after exit code 2',
      latest_progress: progress({ summary: 'Still regenerating the export' }),
    }),
    [report()],
  )
  assert.equal(failed.outcome, 'Dispatcher stopped after exit code 2')
  assert.equal(failed.outcomeIsFailure, true)

  const working = evidence.taskEvidence(
    task({ latest_progress: progress({ summary: 'Parser rewrite done' }) }),
    [],
  )
  assert.equal(working.outcome, 'Parser rewrite done')
  assert.equal(working.outcomeIsFailure, false)
})

test('progress wins over the report and both feed the outcome when progress is absent', () => {
  const withProgress = evidence.taskEvidence(
    task({ latest_progress: progress({ summary: 'step 3 of 5' }) }),
    [report({ message_en: 'report text' })],
  )
  assert.equal(withProgress.outcome, 'step 3 of 5')

  const reportOnly = evidence.taskEvidence(task({}), [report({ message_en: 'report text' })])
  assert.equal(reportOnly.outcome, 'report text')
})

test('acceptance criteria keep Goal Packet order and take the executor verdict', () => {
  const model = evidence.taskEvidence(
    task({
      goal_packet: {
        objective: 'Rewrite the parser',
        status: 'approved',
        acceptance_criteria: [
          'Existing tests pass',
          'No new dependency',
          'Public API unchanged',
        ],
      },
    }),
    [
      report({
        acceptance_check: [
          // Order and whitespace differ from the packet on purpose.
          { criterion: 'public   api unchanged', status: 'partial', evidence: 'helper widened' },
          { criterion: 'Existing tests pass', status: 'passed', evidence: '118 passed' },
        ],
      }),
    ],
  )
  assert.deepEqual(
    model.acceptance.map(item => [item.criterion, item.status]),
    [
      ['Existing tests pass', 'passed'],
      ['No new dependency', 'unreported'],
      ['Public API unchanged', 'partial'],
    ],
  )
  assert.equal(model.acceptance[0].evidence, '118 passed')
  assert.equal(model.acceptance[1].evidence, null)
})

test('executor checks still render when no Goal Packet recorded criteria', () => {
  const model = evidence.taskEvidence(task({}), [
    report({ acceptance_check: [{ criterion: 'Tests pass', status: 'failed', evidence: '2 failures' }] }),
  ])
  assert.deepEqual(model.acceptance, [
    { criterion: 'Tests pass', status: 'failed', evidence: '2 failures' },
  ])
})

test('validation, files, artifacts and risks come from progress or the report', () => {
  const model = evidence.taskEvidence(
    task({ latest_progress: progress({ artifact_refs: ['reports/diff.md', 'reports/diff.md'] }) }),
    [report({
      changed_files: ['a.ts', 'a.ts', 'b.md'],
      validation: 'unit suite green',
      risks: 'signature widened',
      artifact_refs: ['reports/diff.md', 'docs/notes.md'],
    })],
  )
  assert.equal(model.validation, 'unit suite green')
  assert.equal(model.risks, 'signature widened')
  assert.deepEqual(model.changedFiles, ['a.ts', 'b.md'])
  assert.deepEqual(model.artifactRefs, ['reports/diff.md', 'docs/notes.md'])
  assert.equal(model.hasContent, true)
})

test('an empty task reports no evidence instead of empty sections', () => {
  const model = evidence.taskEvidence(task({}), [])
  assert.equal(model.hasContent, false)
  assert.deepEqual(model.acceptance, [])
  assert.deepEqual(model.changedFiles, [])
  assert.deepEqual(model.links, [])
})

test('bare review and CI URLs become chips with stable derived labels', () => {
  const links = evidence.evidenceLinks([
    'Ready: MR https://code.byted.org/seed/xperf_evo/-/merge_requests/3705.',
    'See the run https://code.byted.org/seed/xperf_evo/-/pipelines/998877.',
    'GitHub PR https://github.com/acme/hub/pull/42',
  ])
  assert.deepEqual(links, [
    { label: 'MR 3705', url: 'https://code.byted.org/seed/xperf_evo/-/merge_requests/3705' },
    {
      label: 'CI pipeline 998877',
      url: 'https://code.byted.org/seed/xperf_evo/-/pipelines/998877',
    },
    { label: 'PR 42', url: 'https://github.com/acme/hub/pull/42' },
  ])
})

test('a named Markdown link keeps the executor label over the derived one', () => {
  const links = evidence.evidenceLinks([
    'Blocked on [Release blocker MR](https://code.byted.org/acme/hub/-/merge_requests/42).',
    'Package: [nightly CI](https://code.byted.org/acme/hub/-/pipelines/998877)',
    'Proof: [**bench numbers**](https://ci.example.com/-/jobs/123)',
  ])
  assert.deepEqual(links, [
    {
      label: 'Release blocker MR',
      url: 'https://code.byted.org/acme/hub/-/merge_requests/42',
    },
    { label: 'nightly CI', url: 'https://code.byted.org/acme/hub/-/pipelines/998877' },
    { label: 'bench numbers', url: 'https://ci.example.com/-/jobs/123' },
  ])
})

test('a generic Markdown label falls back to the derived destination', () => {
  assert.deepEqual(
    evidence.evidenceLinks(['See [here](https://code.byted.org/acme/hub/-/merge_requests/42).']),
    [{ label: 'MR 42', url: 'https://code.byted.org/acme/hub/-/merge_requests/42' }],
  )
})

test('duplicate links are emitted once and non-links are ignored', () => {
  const links = evidence.evidenceLinks([
    'https://ci.example.com/-/jobs/123 https://ci.example.com/-/jobs/123',
    'no url here',
    null,
  ])
  assert.deepEqual(links, [{ label: 'CI job 123', url: 'https://ci.example.com/-/jobs/123' }])
})

test('the board keeps failed work in Working but sorts it first and labels it', () => {
  const tasks = [
    task({ id: 'active-1', status: 'working' }),
    task({ id: 'failed-old', status: 'failed', failed_at: '2026-10-24T08:00:00', updated_at: '2026-10-24T08:00:00' }),
    task({ id: 'todo-1', status: 'todo' }),
    task({ id: 'failed-new', status: 'failed', failed_at: '2026-10-24T10:00:00', updated_at: '2026-10-24T10:00:00' }),
  ]
  assert.deepEqual(
    board.orderedColumnTasks(tasks, 'working').map(item => item.id),
    ['failed-new', 'failed-old', 'active-1'],
  )
  assert.deepEqual(
    board.orderedColumnTasks(tasks, 'todo').map(item => item.id),
    ['todo-1'],
  )
  assert.equal(board.taskStatusLabel('failed'), 'Failed')
  assert.equal(board.taskStatusLabel('working'), 'Working')
})

test('empty columns name what is missing instead of saying "No tasks"', () => {
  assert.equal(board.emptyColumnText('review'), 'Nothing waiting for your review')
  assert.equal(board.emptyColumnText('todo'), 'Nothing waiting to start')
  assert.equal(board.emptyColumnText('working'), 'No work in progress')
})

test('report states render as sentences and tolerate missing data', () => {
  assert.equal(board.reportStateLabel('ready_for_review'), 'Ready for review')
  assert.equal(board.reportStateLabel('review_needs_input'), 'Review needs input')
  assert.equal(board.reportStateLabel(null), '')
})

// The Evidence section is the surface a reviewer reads before deciding, so its
// DOM must carry the verdict per criterion and never invent an empty block.
const contexts = await loadWorkspaceFeature('context.ts')
const { default: TaskEvidence } = await loadWorkspaceFeature('components/TaskEvidence.vue')

function mountEvidence(t, selectedTask, reports) {
  const opened = []
  const panel = mountWorkspaceConsumer(
    t,
    TaskEvidence,
    () => {
      contexts.provideWorkspaceRuntimeContext({ formatTime: value => value })
      contexts.provideWorkspaceTaskPresentationContext({
        selectedReports: vue.computed(() => reports),
      })
      contexts.provideWorkspaceArtifactsContext({
        openMarkdownPreviewModal: path => opened.push(path),
        openImageLightbox: () => {},
      })
    },
    { selectedTask },
  )
  return { panel, opened }
}

function findAll(node, predicate, out = []) {
  if (!node) return out
  if (predicate(node)) out.push(node)
  for (const child of node.children || []) findAll(child, predicate, out)
  return out
}

function classTokens(node) {
  const value = node?.props?.class
  if (!value) return []
  if (Array.isArray(value)) return value.flatMap(item => classTokens({ props: { class: item } }))
  return String(value).split(/\s+/).filter(Boolean)
}

function hasClass(node, name) {
  return classTokens(node).includes(name)
}

function textOf(node) {
  if (!node) return ''
  return [node.text || '', ...(node.children || []).map(textOf)].join(' ')
}

test('Evidence renders one row per criterion with its reported status', t => {
  const { panel } = mountEvidence(
    t,
    task({
      goal_packet: {
        objective: 'Rewrite the parser',
        status: 'approved',
        acceptance_criteria: ['Tests pass', 'No new dependency'],
      },
    }),
    [report({
      acceptance_check: [{ criterion: 'Tests pass', status: 'passed', evidence: '118 passed' }],
    })],
  )
  const rows = findAll(panel.root, node => hasClass(node, 'evidence-check'))
  assert.equal(rows.length, 2)
  const marks = findAll(panel.root, node => hasClass(node, 'evidence-check-mark'))
  assert.deepEqual(marks.map(mark => mark.props['data-status']), ['passed', 'unreported'])
  assert.match(textOf(panel.root).replace(/\s+/g, ' '), /Passed — 118 passed/)
})

test('Evidence shows a quiet empty state and no sections without data', t => {
  const { panel } = mountEvidence(t, task({}), [])
  assert.match(textOf(panel.root), /No validation, changed files, or artifacts reported yet/)
  assert.deepEqual(findAll(panel.root, node => hasClass(node, 'evidence-block')), [])
})

// Presentation order is the feature: an evidence-first detail view must not
// regress into internal fields first, and empty sections must disappear.
const dialogSource = await readFile(
  new URL('../src/features/workspace/components/TaskDetailDialog.vue', import.meta.url),
  'utf8',
)
const overviewSource = await readFile(
  new URL('../src/features/workspace/components/TaskOverview.vue', import.meta.url),
  'utf8',
)
const documentsSource = await readFile(
  new URL('../src/features/workspace/components/TaskMarkdownDocuments.vue', import.meta.url),
  'utf8',
)
const panelSource = await readFile(
  new URL('../src/components/TaskExecutionPanel.vue', import.meta.url),
  'utf8',
)

test('detail dialog orders evidence, decision, description, history, execution', () => {
  const order = ['<TaskEvidence', 'decision-section', '<TaskOverview', '<TaskReports', '<TaskExecutionPanel']
  const positions = order.map(token => dialogSource.indexOf(token))
  assert.ok(positions.every(position => position >= 0), `missing: ${order.join(', ')}`)
  assert.deepEqual(
    [...positions].sort((a, b) => a - b),
    positions,
    `unexpected order: ${order.join(' -> ')}`,
  )
})

test('detail dialog keeps the outcome in the header and folds internal fields', () => {
  assert.match(dialogSource, /detail-outcome/)
  assert.match(dialogSource, /:debug-open="false"/)
  assert.match(dialogSource, /:show-progress="false"/)
})

test('an absent Goal Packet renders nothing instead of an empty header', () => {
  assert.match(overviewSource, /<details\s+v-if="selectedTask\.goal_packet"/)
  assert.doesNotMatch(overviewSource, /No goal packet recorded yet/)
})

test('Markdown outputs only render once documents exist', () => {
  assert.match(documentsSource, /v-if="selectedMarkdownDocuments\.length > 0"/)
  assert.doesNotMatch(documentsSource, /No Markdown outputs discovered yet/)
})

test('execution internals live in a collapsed Debug info fold', () => {
  assert.match(panelSource, /class="task-execution-block task-execution-debug"/)
  assert.match(panelSource, /<summary>Debug info<\/summary>/)
  assert.match(panelSource, /:open="debugOpen"/)
  // The old nested evidence fold must not come back; evidence moved up.
  assert.doesNotMatch(panelSource, /Validation, risks, and artifacts/)
})

test('only Markdown evidence opens a control; other paths are plain text', t => {
  const { panel } = mountEvidence(
    t,
    task({}),
    [report({
      changed_files: ['frontend/src/app.ts', 'docs/notes.md'],
      artifact_refs: ['reports/diff.md', 'reports/coverage.json'],
    })],
  )
  // A disabled button is unfocusable and tells a keyboard user nothing, so a
  // non-previewable path must not be a button at all.
  const buttons = findAll(panel.root, node => node.tag === 'button' && hasClass(node, 'evidence-chip'))
  assert.deepEqual(
    buttons.map(button => textOf(button).trim()),
    ['docs/notes.md', 'reports/diff.md'],
  )
  const chips = findAll(panel.root, node => hasClass(node, 'evidence-chip'))
  const plain = chips.filter(chip => chip.tag !== 'button')
  assert.deepEqual(
    plain.map(chip => textOf(chip).trim()),
    ['frontend/src/app.ts', 'reports/coverage.json'],
  )
  for (const chip of chips) {
    assert.equal(
      chip.props?.disabled,
      undefined,
      `no evidence chip may be disabled: ${textOf(chip)}`,
    )
  }
})

const actionsSource = await readFile(
  new URL('../src/features/workspace/components/TaskDetailActions.vue', import.meta.url),
  'utf8',
)

test('the review bar retires for a tall decision, so any visible sliver counts', () => {
  // A ratio threshold is measured against the target; 0.2 can never be reached
  // by a Decision section more than five screens tall.
  assert.match(dialogSource, /threshold: 0\.01/)
  assert.doesNotMatch(dialogSource, /threshold: 0\.2/)
})

test('the actions caret is decorative and points the way the drawer moves', () => {
  assert.match(actionsSource, /class="detail-footer-chevron"\s+aria-hidden="true"/)
  assert.match(actionsSource, /isDetailActionsExpanded \? '▾' : '▸'/)
  assert.doesNotMatch(actionsSource, /isDetailActionsExpanded \? 'v' : '\^'/)
})
