import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(
  new URL('../src/utils/chatModePolicy.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const { getAvailableChatModes, getCurrentChatModeId } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

const dynamicCaps = {
  supports_dynamic_modes: true,
  available_modes: [
    { id: 'default', label: 'Default', description: 'Standard coding mode' },
    { id: 'plan', label: 'Plan', description: 'Read-only planning mode' },
  ],
  current_mode: 'default',
}

test('dynamic capabilities expose backend-provided modes without provider hard-coding', () => {
  assert.deepEqual(getAvailableChatModes(dynamicCaps), [
    { id: 'default', label: 'Agent', description: 'Standard coding mode' },
    { id: 'plan', label: 'Plan', description: 'Read-only planning mode' },
  ])
  assert.equal(getCurrentChatModeId(dynamicCaps), 'default')
})

test('default mode has the Agent display label without changing its persisted id', () => {
  const modes = getAvailableChatModes(dynamicCaps)
  assert.deepEqual(modes.map(({ id, label }) => ({ id, label })), [
    { id: 'default', label: 'Agent' },
    { id: 'plan', label: 'Plan' },
  ])
  assert.equal(getCurrentChatModeId(dynamicCaps), 'default')
})

test('mode controls stay hidden when the backend does not support dynamic modes', () => {
  assert.deepEqual(getAvailableChatModes({ ...dynamicCaps, supports_dynamic_modes: false }), [])
  assert.deepEqual(getAvailableChatModes({
    supports_dynamic_modes: true,
    available_modes: [],
    current_mode: null,
  }), [])
})

test('invalid or duplicate capability entries do not create misleading controls', () => {
  const capabilities = {
    supports_dynamic_modes: true,
    available_modes: [
      { id: 'default', label: 'Default' },
      { id: '', label: 'Missing id' },
      { id: 'plan', label: '' },
      { id: 'default', label: 'Duplicate' },
      null,
    ],
    current_mode: 'unknown',
  }
  assert.deepEqual(getAvailableChatModes(capabilities), [{ id: 'default', label: 'Agent' }])
  assert.equal(getCurrentChatModeId(capabilities), null)
})
