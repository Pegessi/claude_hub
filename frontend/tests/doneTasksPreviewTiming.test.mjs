import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const helperSource = await readFile(
  new URL('../src/utils/doneTasksPreview.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(helperSource, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const helperModule = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const { DONE_TASKS_PREVIEW_LIMIT, previewDoneTasks } = helperModule

// Simulate the per-task card rendering cost. Each task card in
// AgentWorkspaceView performs several lookups (latest report, session,
// agent/reviewer title, review status, injected lessons) and mounts a
// complex subtree. We approximate this with a non-trivial amount of work
// per task so the benchmark reflects the relative cost of rendering N
// cards (the actual DOM cost is far higher per card).
function simulateCardRenderCost(task) {
  let acc = 0
  const s = JSON.stringify(task)
  for (let i = 0; i < 2000; i++) {
    acc += (s.charCodeAt(i % s.length) + i) & 0xff
  }
  return acc
}

function makeDoneTasks(count) {
  return Array.from({ length: count }, (_, i) => ({
    id: `task-${i}`,
    status: 'done',
    completed_at: new Date(Date.UTC(2026, 0, i + 1)).toISOString(),
    created_at: new Date(Date.UTC(2026, 0, i + 1)).toISOString(),
  }))
}

test('done preview reduces rendered card count from 272 to 15', () => {
  const tasks = makeDoneTasks(272)
  const preview = previewDoneTasks(tasks, false)
  assert.equal(preview.length, DONE_TASKS_PREVIEW_LIMIT)
  assert.equal(DONE_TASKS_PREVIEW_LIMIT, 15)
  // The preview renders 15 cards instead of 272 — a ~94.5% reduction in
  // the number of task cards Vue has to mount on every mode switch.
  const reductionPct = ((272 - 15) / 272) * 100
  assert.ok(reductionPct > 90, `expected >90% reduction, got ${reductionPct.toFixed(1)}%`)
})

test('done preview processing is faster than processing all tasks', () => {
  const tasks = makeDoneTasks(272)
  const iterations = 200

  // Measure the cost of the preview path (sort + slice to 15) plus
  // simulating render of the 15 returned cards.
  let previewTotal = 0
  for (let i = 0; i < iterations; i++) {
    const start = performance.now()
    const preview = previewDoneTasks(tasks, false)
    for (const t of preview) simulateCardRenderCost(t)
    previewTotal += performance.now() - start
  }

  // Measure the cost of the old path (sort all 272) plus simulating
  // render of all 272 cards.
  let fullTotal = 0
  for (let i = 0; i < iterations; i++) {
    const start = performance.now()
    const all = previewDoneTasks(tasks, true)
    for (const t of all) simulateCardRenderCost(t)
    fullTotal += performance.now() - start
  }

  const previewAvg = previewTotal / iterations
  const fullAvg = fullTotal / iterations

  // The preview path processes 15 cards instead of 272, so it must be
  // faster. We assert only the ordering (preview < full), not a specific
  // multiplier, because the per-card cost here is a simulation, not a
  // real Vue/DOM render measurement. The factual claim is the 272->15
  // card count reduction (~94.5%), asserted in the test above.
  assert.ok(
    previewAvg < fullAvg,
    `expected preview (${previewAvg.toFixed(3)}ms) < full (${fullAvg.toFixed(3)}ms)`,
  )
})
