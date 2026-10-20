import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/agentStreamCapabilitiesCache.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const { createAgentStreamCapabilitiesCache, streamCapabilityConfiguration, CAPABILITIES_CACHE_TTL_MS } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`,
)
const caps = { structured: true, current_model: 'model-a' }

test('capability hints expire at30 minutes, without extending TTL on reads', () => {
  let now = 0
  const cache = createAgentStreamCapabilitiesCache(2, CAPABILITIES_CACHE_TTL_MS, () => now)
  cache.set('tab-a', 'config-a', caps)
  now = 29 * 60 * 1000
  assert.equal(cache.get('tab-a', 'config-a'), caps)
  now = 30 * 60 * 1000
  assert.equal(cache.get('tab-a', 'config-a'), undefined)
})

test('configuration changes and unavailable results retire prior hints', () => {
  const cache = createAgentStreamCapabilitiesCache()
  cache.set('a', 'old', caps)
  assert.equal(cache.get('a', 'new'), undefined)
  cache.set('a', 'new', caps)
  cache.set('a', 'new', { structured: false })
  assert.equal(cache.get('a', 'new'), undefined)
  cache.set('a', 'new', { structured: true, unavailable_reason: 'offline' })
  assert.equal(cache.get('a', 'new'), undefined)
  cache.set('a', 'new', { structured: true, adapter_id: 'maso_http' })
  assert.equal(cache.get('a', 'new'), undefined)
})

test('capabilities have bounded, source-scoped LRU retention', () => {
  const cache = createAgentStreamCapabilitiesCache(2)
  cache.set('/tabs/a', '', caps)
  cache.set('/sessions/a', '', { ...caps, current_model: 'managed' })
  assert.equal(cache.get('/tabs/a').current_model, 'model-a')
  cache.set('/tabs/b', '', caps)
  assert.equal(cache.get('/sessions/a'), undefined)
  assert.equal(cache.get('/tabs/a').current_model, 'model-a')
  cache.clear()
  assert.equal(cache.get('/tabs/a'), undefined)
})

test('configuration signatures ignore presentation changes but include provider, env and incarnation', () => {
  const tab = { id: 'a', agent_type: 'codex', env: { B: '2', A: '1' }, agent_session_id: 'native-a' }
  const signature = streamCapabilityConfiguration(tab)
  assert.equal(signature, streamCapabilityConfiguration({ ...tab, env: { A: '1', B: '2' }, name: 'renamed', is_unread: true }))
  for (const changed of [{ agent_type: 'claude' }, { env: { A: 'changed' } }, { agent_session_id: 'native-b' }]) {
    assert.notEqual(signature, streamCapabilityConfiguration({ ...tab, ...changed }))
  }
})
