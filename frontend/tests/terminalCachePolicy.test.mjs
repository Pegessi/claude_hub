import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

// terminalCachePolicy.ts is a pure module with no runtime imports, so
// transpiling with TypeScript and loading via data URL works without shims.
const source = await readFile(
  new URL('../src/utils/terminalCachePolicy.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const { computeCacheUpdate } = mod

const MAX = 4

test('activating an already-cached tab does NOT reorder the render list', () => {
  // A then B cached → render list is [A, B] (insertion order).
  let state = { cachedTabIds: [], tabRecency: [] }
  state = computeCacheUpdate(state, 'A', '1x1', MAX)
  state = computeCacheUpdate(state, 'B', '1x1', MAX)
  assert.deepEqual(state.cachedTabIds, ['A', 'B'])

  // Switching back to A must NOT move A to the front or back of the render
  // list — that would move the iframe in the DOM and reload it.
  state = computeCacheUpdate(state, 'A', '1x1', MAX)
  assert.deepEqual(
    state.cachedTabIds,
    ['A', 'B'],
    'render list must stay in insertion order when re-activating A',
  )

  // Switching to B also must not reorder.
  state = computeCacheUpdate(state, 'B', '1x1', MAX)
  assert.deepEqual(
    state.cachedTabIds,
    ['A', 'B'],
    'render list must stay in insertion order when re-activating B',
  )

  // A full A→B→A→B cycle leaves the render list untouched.
  state = computeCacheUpdate(state, 'A', '1x1', MAX)
  state = computeCacheUpdate(state, 'B', '1x1', MAX)
  assert.deepEqual(state.cachedTabIds, ['A', 'B'])
})

test('LRU recency is updated on each activation (render order untouched)', () => {
  let state = { cachedTabIds: [], tabRecency: [] }
  state = computeCacheUpdate(state, 'A', '1x1', MAX)
  state = computeCacheUpdate(state, 'B', '1x1', MAX)
  // After A then B: recency = [A, B] (B is MRU).
  assert.deepEqual(state.tabRecency, ['A', 'B'])

  // Activate A: A becomes MRU → recency = [B, A].
  state = computeCacheUpdate(state, 'A', '1x1', MAX)
  assert.deepEqual(state.tabRecency, ['B', 'A'])
  // Render list still insertion order.
  assert.deepEqual(state.cachedTabIds, ['A', 'B'])
})

test('adding the 5th tab evicts the LRU tab (not the active one)', () => {
  let state = { cachedTabIds: [], tabRecency: [] }
  // Cache A, B, C, D in that order.
  for (const id of ['A', 'B', 'C', 'D']) {
    state = computeCacheUpdate(state, id, '1x1', MAX)
  }
  assert.deepEqual(state.cachedTabIds, ['A', 'B', 'C', 'D'])
  assert.deepEqual(state.tabRecency, ['A', 'B', 'C', 'D'])

  // Activate A so the LRU order becomes [B, C, D, A] (A is MRU).
  state = computeCacheUpdate(state, 'A', '1x1', MAX)
  assert.deepEqual(state.tabRecency, ['B', 'C', 'D', 'A'])

  // Add E. The LRU tab is B (front of recency, not the active E).
  const result = computeCacheUpdate(state, 'E', '1x1', MAX)
  assert.deepEqual(result.evicted, ['B'], 'B should be evicted as LRU')
  assert.deepEqual(result.cachedTabIds, ['A', 'C', 'D', 'E'])
  assert.deepEqual(result.tabRecency, ['C', 'D', 'A', 'E'])
})

test('the active tab is never evicted even if it is LRU', () => {
  let state = { cachedTabIds: [], tabRecency: [] }
  for (const id of ['A', 'B', 'C', 'D']) {
    state = computeCacheUpdate(state, id, '1x1', MAX)
  }
  // Recency: [A, B, C, D]. A is LRU.
  // Re-activate A (it's already cached, so it becomes MRU).
  state = computeCacheUpdate(state, 'A', '1x1', MAX)
  assert.deepEqual(state.tabRecency, ['B', 'C', 'D', 'A'])

  // Now activate B, C, D in order so A becomes LRU again.
  state = computeCacheUpdate(state, 'B', '1x1', MAX)
  state = computeCacheUpdate(state, 'C', '1x1', MAX)
  state = computeCacheUpdate(state, 'D', '1x1', MAX)
  assert.deepEqual(state.tabRecency, ['A', 'B', 'C', 'D'])

  // Activate A (the LRU). It must NOT be evicted — it's the active tab.
  const result = computeCacheUpdate(state, 'A', '1x1', MAX)
  assert.deepEqual(result.evicted, [], 'active tab must not be evicted')
  assert.ok(result.cachedTabIds.includes('A'))
  // A moves to MRU.
  assert.deepEqual(result.tabRecency, ['B', 'C', 'D', 'A'])
})

test('split (non-1x1) layout keeps only the active tab', () => {
  let state = { cachedTabIds: ['A', 'B', 'C'], tabRecency: ['A', 'B', 'C'] }
  const result = computeCacheUpdate(state, 'B', '2x2', MAX)
  assert.deepEqual(result.cachedTabIds, ['B'])
  assert.deepEqual(result.tabRecency, ['B'])
  assert.deepEqual(result.evicted.sort(), ['A', 'C'])
})

test('empty tabId returns state unchanged with no evictions', () => {
  const state = { cachedTabIds: ['A', 'B'], tabRecency: ['A', 'B'] }
  const result = computeCacheUpdate(state, '', '1x1', MAX)
  assert.deepEqual(result.cachedTabIds, ['A', 'B'])
  assert.deepEqual(result.tabRecency, ['A', 'B'])
  assert.deepEqual(result.evicted, [])
})

test('over-cap cache evicts only the LRU non-active tab down to MAX', () => {
  // cachedTabIds has 5 entries (over MAX=4). Active tab is E (MRU).
  let state = { cachedTabIds: ['A', 'B', 'C', 'D', 'E'], tabRecency: ['A', 'B', 'C', 'D', 'E'] }
  const result = computeCacheUpdate(state, 'E', '1x1', MAX)
  // Only enough evictions to get back to MAX=4.
  assert.equal(result.cachedTabIds.length, MAX)
  assert.ok(result.cachedTabIds.includes('E'), 'active tab must not be evicted')
  // A is the LRU non-active tab → only A is evicted.
  assert.deepEqual(result.evicted, ['A'])
  assert.deepEqual(result.cachedTabIds, ['B', 'C', 'D', 'E'])
})
