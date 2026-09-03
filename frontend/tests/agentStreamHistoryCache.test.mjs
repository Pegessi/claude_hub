import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(
  new URL('../src/utils/agentStreamHistoryCache.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const { createAgentStreamHistoryCache } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

function snapshot(sequence) {
  return {
    capabilities: { structured: true },
    events: [{ stream_sequence: sequence }],
    cursor: sequence,
  }
}

test('stores and restores a Chat history snapshot without copying its event array', () => {
  const cache = createAgentStreamHistoryCache(2)
  const saved = snapshot(42)

  cache.set('/tabs/a/stream', saved)
  const restored = cache.get('/tabs/a/stream')

  assert.equal(restored, saved)
  assert.equal(restored.events, saved.events)
  assert.equal(restored.cursor, 42)
})

test('uses LRU eviction so recently revisited Chat histories stay warm', () => {
  const cache = createAgentStreamHistoryCache(2)
  cache.set('a', snapshot(1))
  cache.set('b', snapshot(2))

  assert.ok(cache.get('a'))
  cache.set('c', snapshot(3))

  assert.equal(cache.get('b'), undefined)
  assert.equal(cache.get('a')?.cursor, 1)
  assert.equal(cache.get('c')?.cursor, 3)
})

test('does not retain snapshots when caching is disabled', () => {
  const cache = createAgentStreamHistoryCache(0)
  cache.set('a', snapshot(1))
  assert.equal(cache.get('a'), undefined)
})
