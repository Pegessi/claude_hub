import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

// ── Timer / rAF mocks ───────────────────────────────────────────────────
// Node has no requestAnimationFrame; we install deterministic fakes so we
// can assert on scheduling and flush behaviour without real time.
const scheduledRaf = new Map()
const scheduledTimers = new Map()
let rafIdCounter = 1
let timerIdCounter = 1

globalThis.requestAnimationFrame = (cb) => {
  const id = rafIdCounter++
  scheduledRaf.set(id, cb)
  return id
}
globalThis.cancelAnimationFrame = (id) => {
  scheduledRaf.delete(id)
}
globalThis.setTimeout = (cb, ms) => {
  const id = timerIdCounter++
  scheduledTimers.set(id, { cb, ms })
  return id
}
globalThis.clearTimeout = (id) => {
  scheduledTimers.delete(id)
}

function fireRaf() {
  for (const [, cb] of scheduledRaf) cb()
  scheduledRaf.clear()
}
function fireTimer() {
  for (const [, { cb }] of scheduledTimers) cb()
  scheduledTimers.clear()
}
function clearAllScheduled() {
  scheduledRaf.clear()
  scheduledTimers.clear()
}

// ── Load agentStreamBatcher.ts ──────────────────────────────────────────
const source = await readFile(
  new URL('../src/utils/agentStreamBatcher.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})

// agentStreamBatcher.ts imports type-only from '@/types'. After
// transpilation the type import is removed, so no replacement needed.
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const { AgentStreamBatcher, FLUSH_INTERVAL_MS, IMMEDIATE_FLUSH_TYPES } = mod

function makeEvent(overrides = {}) {
  return {
    stream_sequence: 0,
    type: 'text_delta',
    turn_id: 't1',
    ...overrides,
  }
}

// ── Tests ───────────────────────────────────────────────────────────────

test('FLUSH_INTERVAL_MS is 48', () => {
  assert.equal(FLUSH_INTERVAL_MS, 48)
})

test('IMMEDIATE_FLUSH_TYPES contains the required barrier types', () => {
  const types = [...IMMEDIATE_FLUSH_TYPES]
  for (const required of [
    'turn_completed',
    'error',
    'tool_call_completed',
    'approval_required',
    'approval_resolved',
    'status',
  ]) {
    assert.ok(
      types.includes(required),
      `IMMEDIATE_FLUSH_TYPES must include ${required}`,
    )
  }
})

test('text_delta and thinking_delta are NOT immediate-flush types', () => {
  assert.ok(!IMMEDIATE_FLUSH_TYPES.has('text_delta'))
  assert.ok(!IMMEDIATE_FLUSH_TYPES.has('thinking_delta'))
})

test('non-barrier events are batched and flushed on rAF', () => {
  clearAllScheduled()
  const committed = []
  const batcher = new AgentStreamBatcher((batch) => committed.push(...batch))

  batcher.enqueue([makeEvent({ type: 'text_delta', stream_sequence: 1 })])
  batcher.enqueue([makeEvent({ type: 'thinking_delta', stream_sequence: 2 })])

  // Not committed yet — waiting for rAF/timer.
  assert.equal(committed.length, 0)
  assert.ok(scheduledRaf.size > 0 || scheduledTimers.size > 0, 'flush scheduled')

  fireRaf()
  assert.equal(committed.length, 2)
  assert.deepEqual(
    committed.map((e) => e.stream_sequence),
    [1, 2],
  )
})

test('non-barrier events flush on the 48ms timer if rAF does not fire', () => {
  clearAllScheduled()
  const committed = []
  const batcher = new AgentStreamBatcher((batch) => committed.push(...batch))

  batcher.enqueue([makeEvent({ type: 'text_delta', stream_sequence: 1 })])
  assert.equal(committed.length, 0)

  // Simulate the fallback timer firing (rAF never fires).
  fireTimer()
  assert.equal(committed.length, 1)
})

test('barrier events flush immediately with preceding pending deltas in order', () => {
  clearAllScheduled()
  const committed = []
  const batcher = new AgentStreamBatcher((batch) => committed.push(...batch))

  // Pending text deltas that would normally wait for the flush window.
  batcher.enqueue([makeEvent({ type: 'text_delta', stream_sequence: 1 })])
  batcher.enqueue([makeEvent({ type: 'thinking_delta', stream_sequence: 2 })])
  assert.equal(committed.length, 0, 'deltas are batched')

  // A turn_completed barrier forces immediate flush of everything pending,
  // in arrival order.
  batcher.enqueue([makeEvent({ type: 'turn_completed', stream_sequence: 3 })])

  assert.equal(committed.length, 3)
  assert.deepEqual(
    committed.map((e) => e.stream_sequence),
    [1, 2, 3],
    'pending deltas flush before the barrier, in order',
  )
  assert.equal(scheduledRaf.size, 0, 'no rAF scheduled after barrier flush')
  assert.equal(scheduledTimers.size, 0, 'no timer scheduled after barrier flush')
})

test('each required barrier type flushes preceding pending deltas immediately', () => {
  const barriers = [
    'turn_completed',
    'tool_call_completed',
    'approval_required',
    'approval_resolved',
    'error',
    'status',
  ]

  for (const barrier of barriers) {
    clearAllScheduled()
    const committed = []
    const batcher = new AgentStreamBatcher((batch) => committed.push(...batch))

    batcher.enqueue([makeEvent({ type: 'text_delta', stream_sequence: 1 })])
    batcher.enqueue([makeEvent({ type: 'thinking_delta', stream_sequence: 2 })])
    assert.equal(committed.length, 0, `${barrier}: deltas batched before barrier`)

    batcher.enqueue([makeEvent({ type: barrier, stream_sequence: 3 })])

    assert.equal(
      committed.length,
      3,
      `${barrier}: should flush 2 pending deltas + itself`,
    )
    assert.deepEqual(
      committed.map((e) => e.stream_sequence),
      [1, 2, 3],
      `${barrier}: pending deltas flush in order before the barrier`,
    )
  }
})

test('flushNow commits pending events and cancels scheduled timers', () => {
  clearAllScheduled()
  const committed = []
  const batcher = new AgentStreamBatcher((batch) => committed.push(...batch))

  batcher.enqueue([makeEvent({ stream_sequence: 1 })])
  assert.equal(committed.length, 0)

  batcher.flushNow()
  assert.equal(committed.length, 1)
  assert.equal(scheduledRaf.size, 0)
  assert.equal(scheduledTimers.size, 0)
})

test('flushAndCancel commits pending events and cancels timers', () => {
  clearAllScheduled()
  const committed = []
  const batcher = new AgentStreamBatcher((batch) => committed.push(...batch))

  batcher.enqueue([makeEvent({ stream_sequence: 1 })])
  batcher.flushAndCancel()

  assert.equal(committed.length, 1)
  assert.equal(scheduledRaf.size, 0)
  assert.equal(scheduledTimers.size, 0)
})

test('cancel drops pending events without committing', () => {
  clearAllScheduled()
  const committed = []
  const batcher = new AgentStreamBatcher((batch) => committed.push(...batch))

  batcher.enqueue([makeEvent({ stream_sequence: 1 })])
  batcher.cancel()

  assert.equal(committed.length, 0)
  assert.equal(scheduledRaf.size, 0)
  assert.equal(scheduledTimers.size, 0)
})

test('only one flush is scheduled even with multiple enqueues', () => {
  clearAllScheduled()
  const committed = []
  const batcher = new AgentStreamBatcher((batch) => committed.push(...batch))

  batcher.enqueue([makeEvent({ stream_sequence: 1 })])
  batcher.enqueue([makeEvent({ stream_sequence: 2 })])
  batcher.enqueue([makeEvent({ stream_sequence: 3 })])

  // Only one rAF + one timer should be scheduled (the scheduler guards
  // against double-scheduling).
  assert.ok(scheduledRaf.size <= 1, `expected <= 1 rAF, got ${scheduledRaf.size}`)
  assert.ok(scheduledTimers.size <= 1, `expected <= 1 timer, got ${scheduledTimers.size}`)
})

test('after flush, a new enqueue schedules a fresh flush', () => {
  clearAllScheduled()
  const committed = []
  const batcher = new AgentStreamBatcher((batch) => committed.push(...batch))

  batcher.enqueue([makeEvent({ stream_sequence: 1 })])
  fireRaf()
  assert.equal(committed.length, 1)
  assert.equal(scheduledRaf.size, 0)

  batcher.enqueue([makeEvent({ stream_sequence: 2 })])
  assert.equal(committed.length, 1) // not yet flushed
  assert.ok(scheduledRaf.size > 0 || scheduledTimers.size > 0)

  fireRaf()
  assert.equal(committed.length, 2)
})

test('8577 thinking deltas are batched (not one commit per delta)', () => {
  clearAllScheduled()
  let commitCount = 0
  let totalEvents = 0
  const batcher = new AgentStreamBatcher((batch) => {
    commitCount++
    totalEvents += batch.length
  })

  // Simulate 8577 small thinking text deltas arriving in quick succession.
  for (let i = 0; i < 8577; i++) {
    batcher.enqueue([makeEvent({ type: 'thinking_delta', stream_sequence: i })])
  }

  // Nothing committed until flush fires.
  assert.equal(commitCount, 0)
  assert.equal(totalEvents, 0)

  fireRaf()

  // All 8577 events committed in a single flush batch.
  assert.equal(commitCount, 1, 'all deltas committed in one batch')
  assert.equal(totalEvents, 8577)
})
