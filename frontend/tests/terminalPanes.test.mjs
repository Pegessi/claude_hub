import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const helperSource = await readFile(
  new URL('../src/utils/terminalPanes.ts', import.meta.url),
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
const { visiblePaneTabIds, dispatchTerminalReturnResize, scheduleTerminalReturnResize } = helperModule

// ---------------------------------------------------------------------------
// visiblePaneTabIds
// ---------------------------------------------------------------------------

test('visiblePaneTabIds returns empty set for empty panes', () => {
  const result = visiblePaneTabIds([])
  assert.equal(result.size, 0)
})

test('visiblePaneTabIds skips null tabIds', () => {
  const panes = [{ tabId: null }, { tabId: null }]
  const result = visiblePaneTabIds(panes)
  assert.equal(result.size, 0)
})

test('visiblePaneTabIds returns single tab id for 1x1 layout', () => {
  const panes = [{ tabId: 'tab-1' }]
  const result = visiblePaneTabIds(panes)
  assert.equal(result.size, 1)
  assert.ok(result.has('tab-1'))
})

test('visiblePaneTabIds returns all non-null tab ids for split layout', () => {
  const panes = [
    { tabId: 'tab-1' },
    { tabId: 'tab-2' },
    { tabId: null },
    { tabId: 'tab-3' },
  ]
  const result = visiblePaneTabIds(panes)
  assert.equal(result.size, 3)
  assert.ok(result.has('tab-1'))
  assert.ok(result.has('tab-2'))
  assert.ok(result.has('tab-3'))
})

test('visiblePaneTabIds deduplicates tab ids assigned to multiple panes', () => {
  const panes = [{ tabId: 'tab-1' }, { tabId: 'tab-1' }]
  const result = visiblePaneTabIds(panes)
  assert.equal(result.size, 1)
  assert.ok(result.has('tab-1'))
})

// ---------------------------------------------------------------------------
// dispatchTerminalReturnResize — the actual mode-return dispatch logic
// ---------------------------------------------------------------------------

function makeIframe() {
  const calls = []
  return {
    contentWindow: {
      postMessage(message, targetOrigin) {
        calls.push({ message, targetOrigin })
      },
    },
    calls,
  }
}

test('dispatch sends resize + scroll-bottom to the single visible pane in 1x1 layout', () => {
  const iframe1 = makeIframe()
  const iframes = { 'tab-1': iframe1, 'tab-hidden': makeIframe() }
  const panes = [{ tabId: 'tab-1' }]

  const dispatched = dispatchTerminalReturnResize(panes, iframes)

  assert.equal(dispatched.length, 2)
  assert.deepEqual(dispatched[0], { type: 'terminal-resize', tabId: 'tab-1' })
  assert.deepEqual(dispatched[1], { type: 'terminal-scroll-bottom', tabId: 'tab-1' })

  assert.equal(iframe1.calls.length, 2)
  assert.equal(iframe1.calls[0].message.type, 'terminal-resize')
  assert.equal(iframe1.calls[1].message.type, 'terminal-scroll-bottom')
})

test('dispatch sends to every visible pane in a split layout', () => {
  const iframe1 = makeIframe()
  const iframe2 = makeIframe()
  const iframes = {
    'tab-1': iframe1,
    'tab-2': iframe2,
    'tab-hidden': makeIframe(),
  }
  const panes = [{ tabId: 'tab-1' }, { tabId: 'tab-2' }]

  const dispatched = dispatchTerminalReturnResize(panes, iframes)

  assert.equal(dispatched.length, 4)
  assert.equal(iframe1.calls.length, 2)
  assert.equal(iframe2.calls.length, 2)
})

test('dispatch skips iframes whose tab is not in any visible pane (hidden cache)', () => {
  const iframe1 = makeIframe()
  const hidden = makeIframe()
  const iframes = { 'tab-1': iframe1, 'tab-hidden': hidden }
  const panes = [{ tabId: 'tab-1' }]

  dispatchTerminalReturnResize(panes, iframes)

  assert.equal(hidden.calls.length, 0)
  assert.equal(iframe1.calls.length, 2)
})

test('dispatch skips null iframes even if the tab is in a visible pane', () => {
  const iframe2 = makeIframe()
  const iframes = { 'tab-1': null, 'tab-2': iframe2 }
  const panes = [{ tabId: 'tab-1' }, { tabId: 'tab-2' }]

  const dispatched = dispatchTerminalReturnResize(panes, iframes)

  // Only tab-2 gets messages; tab-1's iframe is null.
  assert.equal(dispatched.length, 2)
  assert.equal(dispatched[0].tabId, 'tab-2')
  assert.equal(iframe2.calls.length, 2)
})

test('dispatch skips iframes whose contentWindow is null', () => {
  const noWindow = { contentWindow: null }
  const iframe2 = makeIframe()
  const iframes = { 'tab-1': noWindow, 'tab-2': iframe2 }
  const panes = [{ tabId: 'tab-1' }, { tabId: 'tab-2' }]

  const dispatched = dispatchTerminalReturnResize(panes, iframes)

  assert.equal(dispatched.length, 2)
  assert.equal(dispatched[0].tabId, 'tab-2')
})

test('dispatch does not double-send when the same tab is assigned to multiple panes', () => {
  const iframe1 = makeIframe()
  const iframes = { 'tab-1': iframe1 }
  // Same tab in two panes (e.g. mirrored split).
  const panes = [{ tabId: 'tab-1' }, { tabId: 'tab-1' }]

  const dispatched = dispatchTerminalReturnResize(panes, iframes)

  // visiblePaneTabIds dedups, so only one resize + one scroll-bottom.
  assert.equal(dispatched.length, 2)
  assert.equal(iframe1.calls.length, 2)
})

test('rapid re-toggle: calling dispatch twice does not leak stale state', () => {
  // Simulate the user switching to terminal, away, and back quickly.
  // Each call is independent — there is no shared mutable state in the
  // dispatcher, so the second call produces the same messages as the first.
  const iframe1 = makeIframe()
  const iframes = { 'tab-1': iframe1 }
  const panes = [{ tabId: 'tab-1' }]

  const first = dispatchTerminalReturnResize(panes, iframes)
  const second = dispatchTerminalReturnResize(panes, iframes)

  assert.equal(first.length, 2)
  assert.equal(second.length, 2)
  // Each call sends exactly 2 messages; total = 4 (no cross-call state).
  assert.equal(iframe1.calls.length, 4)
})

// ---------------------------------------------------------------------------
// scheduleTerminalReturnResize — the deferred scheduling + cancellation logic
// ---------------------------------------------------------------------------

// A mock rAF scheduler that records pending callbacks and lets tests fire
// them on demand. This lets us exercise the scheduling/cancellation path
// without a real browser event loop.
function makeMockScheduler() {
  let nextId = 1
  const pending = new Map()
  return {
    requestAnimationFrame(cb) {
      const id = nextId++
      pending.set(id, cb)
      return id
    },
    cancelAnimationFrame(id) {
      pending.delete(id)
    },
    fire(id) {
      const cb = pending.get(id)
      if (cb) {
        pending.delete(id)
        cb()
      }
    },
    fireAll() {
      for (const [id, cb] of pending) {
        pending.delete(id)
        cb()
      }
    },
    pendingCount() {
      return pending.size
    },
  }
}

test('scheduleTerminalReturnResize defers dispatch until the next frame', () => {
  const iframe1 = makeIframe()
  const iframes = { 'tab-1': iframe1 }
  const panes = [{ tabId: 'tab-1' }]
  const scheduler = makeMockScheduler()
  let mode = 'terminal'

  scheduleTerminalReturnResize(() => mode, panes, iframes, scheduler)

  // Before the frame fires, no messages should have been sent.
  assert.equal(iframe1.calls.length, 0)
  assert.equal(scheduler.pendingCount(), 1)

  // Fire the frame — now the dispatch runs.
  scheduler.fireAll()
  assert.equal(iframe1.calls.length, 2)
})

test('scheduleTerminalReturnResize skips dispatch if mode left terminal before frame fires', () => {
  const iframe1 = makeIframe()
  const iframes = { 'tab-1': iframe1 }
  const panes = [{ tabId: 'tab-1' }]
  const scheduler = makeMockScheduler()
  let mode = 'terminal'

  scheduleTerminalReturnResize(() => mode, panes, iframes, scheduler)

  // User switches away from terminal before the frame fires.
  mode = 'workspace'
  scheduler.fireAll()

  // The stale callback must NOT dispatch.
  assert.equal(iframe1.calls.length, 0)
})

test('scheduleTerminalReturnResize cancels the previous pending callback when called again', () => {
  const iframe1 = makeIframe()
  const iframes = { 'tab-1': iframe1 }
  const panes = [{ tabId: 'tab-1' }]
  const scheduler = makeMockScheduler()
  let mode = 'terminal'

  // First schedule — one pending callback.
  scheduleTerminalReturnResize(() => mode, panes, iframes, scheduler)
  assert.equal(scheduler.pendingCount(), 1)

  // Second schedule — the first callback is cancelled internally, so
  // there is still only one pending callback (the second one).
  scheduleTerminalReturnResize(() => mode, panes, iframes, scheduler)
  assert.equal(scheduler.pendingCount(), 1)

  scheduler.fireAll()
  // Only the second callback's dispatch runs — total 2 messages, not 4.
  assert.equal(iframe1.calls.length, 2)
})

test('scheduleTerminalReturnResize cleanup function cancels the pending callback', () => {
  const iframe1 = makeIframe()
  const iframes = { 'tab-1': iframe1 }
  const panes = [{ tabId: 'tab-1' }]
  const scheduler = makeMockScheduler()
  let mode = 'terminal'

  const cancel = scheduleTerminalReturnResize(() => mode, panes, iframes, scheduler)
  assert.equal(scheduler.pendingCount(), 1)

  // Invoke the cleanup (e.g. when the mode watcher fires again).
  cancel()
  assert.equal(scheduler.pendingCount(), 0)

  // Firing does nothing because the callback was cancelled.
  scheduler.fireAll()
  assert.equal(iframe1.calls.length, 0)
})

test('scheduleTerminalReturnResize rapid re-toggle: mode leaves and re-enters terminal', () => {
  // Simulate: workspace -> terminal (schedule A) -> workspace (cancel A)
  // -> terminal (schedule B). Only B's dispatch should run.
  const iframe1 = makeIframe()
  const iframes = { 'tab-1': iframe1 }
  const panes = [{ tabId: 'tab-1' }]
  const scheduler = makeMockScheduler()
  let mode = 'workspace'

  // Enter terminal.
  mode = 'terminal'
  const cancelA = scheduleTerminalReturnResize(() => mode, panes, iframes, scheduler)
  assert.equal(scheduler.pendingCount(), 1)

  // Rapidly leave terminal — cancel A.
  mode = 'workspace'
  cancelA()
  assert.equal(scheduler.pendingCount(), 0)

  // Re-enter terminal — schedule B.
  mode = 'terminal'
  scheduleTerminalReturnResize(() => mode, panes, iframes, scheduler)
  assert.equal(scheduler.pendingCount(), 1)

  // Fire B's frame.
  scheduler.fireAll()
  assert.equal(iframe1.calls.length, 2)
})
