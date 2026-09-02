import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const lifecycleSource = readFileSync(
  new URL('../src/utils/chatTurnLifecycle.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(lifecycleSource, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const {
  isChatModeLocked,
  hasChatStatusRefreshBoundary,
} = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

const activeTurn = { completed: false, errors: [] }
const completedTurn = { completed: true, errors: [] }
const failedTurn = { completed: false, errors: [{ message: 'provider failed' }] }

test('mode lock spans optimistic submission and the authoritative active turn', () => {
  assert.equal(isChatModeLocked(true, []), true, 'submission-to-turn_started gap stays locked')
  assert.equal(isChatModeLocked(false, [completedTurn, activeTurn]), true)
  assert.equal(isChatModeLocked(false, [completedTurn]), false)
})

test('turn completion or error terminalizes the authoritative mode lock', () => {
  assert.equal(isChatModeLocked(false, [activeTurn, completedTurn]), false)
  assert.equal(isChatModeLocked(false, [activeTurn, failedTurn]), false)
})

function event(stream_sequence, type, extra = {}) {
  return { stream_sequence, type, payload: {}, ...extra }
}

test('status refresh boundaries ignore streaming deltas and fire only for lifecycle edges', () => {
  const started = [event(0, 'turn_started', { tab_id: 'chat-a' })]
  const streaming = [...started, event(1, 'text_delta', { tab_id: 'chat-a' })]
  const completed = [...streaming, event(2, 'turn_completed', { tab_id: 'chat-a' })]

  assert.equal(hasChatStatusRefreshBoundary([], [event(0, 'text_delta')]), false)
  assert.equal(hasChatStatusRefreshBoundary([], started), true)
  assert.equal(hasChatStatusRefreshBoundary(started, streaming), false)
  assert.equal(hasChatStatusRefreshBoundary(streaming, completed), true)
  assert.equal(
    hasChatStatusRefreshBoundary(streaming, [...streaming, event(2, 'error')]),
    true,
  )
})

test('a replaced event source is re-evaluated instead of inheriting the old cursor', () => {
  const previous = [event(0, 'turn_started', { tab_id: 'chat-a' })]
  const replacement = [event(0, 'turn_completed', { tab_id: 'chat-b' })]
  assert.equal(hasChatStatusRefreshBoundary(previous, replacement), true)
})

const structuredPane = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)

test('StructuredPane wires the lifecycle lock into mode UI and refreshes status once per boundary', () => {
  assert.match(structuredPane, /:disabled="modeInteractionLocked \|\| isUpdatingMode"/)
  assert.match(structuredPane, /if \(modeInteractionLocked\.value \|\| isUpdatingMode\.value/)
  assert.match(structuredPane, /hasChatStatusRefreshBoundary\(previous, latest\)/)
  assert.match(structuredPane, /terminalStore\.fetchAgentStatuses\(\)/)
  assert.match(
    structuredPane,
    /pendingDirectTurns\.value = \[\][\s\S]*?startStream\(\)/,
    'source switch clears the optimistic lock before starting the next stream',
  )
})
