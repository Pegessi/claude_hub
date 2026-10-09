import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/utils/serviceRestart.ts', import.meta.url), 'utf8')
const dialog = readFileSync(new URL('../src/components/ServiceRestartDialog.vue', import.meta.url), 'utf8')
const actions = readFileSync(new URL('../src/components/AppExtensionActions.vue', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const { restartOutcome, createRestartId } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const pending = { id: 'request', instanceId: 'old' }

test('request IDs also work on LAN HTTP where randomUUID is unavailable', () => {
  assert.match(createRestartId(), /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  assert.notEqual(createRestartId(), createRestartId())
})

test('recovery requires the accepted request and a new backend instance', () => {
  const ready = { instance_id: 'new', operation: { id: 'request', status: 'succeeded' } }
  assert.equal(restartOutcome(ready, pending), 'succeeded')
  assert.equal(restartOutcome({ ...ready, instance_id: 'old' }, pending), 'waiting')
  assert.equal(restartOutcome({ ...ready, operation: { id: 'other', status: 'succeeded' } }, pending), 'waiting')
  assert.equal(restartOutcome({ instance_id: 'old', operation: null }, pending), 'waiting')
})

test('server failures remain failures and in-progress statuses keep waiting', () => {
  for (const status of ['preparing', 'restarting', 'failed']) {
    assert.equal(restartOutcome({ instance_id: 'new', operation: { id: 'request', status } }, pending), status === 'failed' ? 'failed' : 'waiting')
  }
})

test('restart UI explains and names the frontend build', () => {
  assert.match(dialog, /rebuilds the frontend, synchronizes backend dependencies/)
  assert.match(dialog, />\s*Build and restart\s*</)
  assert.match(dialog, /const recoveryTimeoutMs = 300_000/)
  assert.match(dialog, /within 5 minutes/)
  assert.match(actions, />Build and restart</)
})
