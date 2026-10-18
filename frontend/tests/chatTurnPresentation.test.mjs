import assert from 'node:assert/strict'
import test from 'node:test'
import { loadChatUtility, loadChatFeature } from './chatPresentationTestHarness.mjs'

const { isTurnProcessExpanded, presentTurnParts, toolGroupStatus, turnProcessFoldSignature } =
  await loadChatFeature('presentation/chatTurnPresentation')
const { groupEventsIntoTurns } = await loadChatUtility('agentStreamTimeline')
const event = (sequence, type, payload = {}, extra = {}) => ({
  stream_sequence: sequence, type, payload, session_id: 's', tab_id: 't',
  turn_id: 'turn', agent_type: 'codex', created_at: '2026-10-16T10:00:00Z',
  redacted: false, ...extra,
})
const completedTurn = () => groupEventsIntoTurns([
  event(1, 'turn_started', { summary: 'question' }),
  event(2, 'thinking_delta', { text: 'reasoning' }),
  event(3, 'text_delta', { text: 'answer' }),
  event(4, 'turn_completed', { status: 'completed' }),
])[0]

test('viewer overrides take precedence over latest-turn defaults without being mutated', () => {
  const turn = completedTurn()
  const overrides = new Map()
  assert.equal(isTurnProcessExpanded(turn, turn.key, overrides), true)
  assert.equal(isTurnProcessExpanded(turn, 'newest', overrides), false)
  overrides.set(turn.key, false)
  assert.equal(isTurnProcessExpanded(turn, turn.key, overrides), false)
  overrides.set(turn.key, true)
  assert.equal(isTurnProcessExpanded(turn, 'newest', overrides), true)
  assert.deepEqual([...overrides], [[turn.key, true]])
})

test('newest turn stays fully rendered while historical process folds and preserves the answer', () => {
  const turn = completedTurn()
  const before = structuredClone(turn)
  const overrides = new Map([[turn.key, false]])
  assert.deepEqual(presentTurnParts(turn, turn.key, overrides).map(part => part.kind), ['thinking', 'text'])
  const folded = presentTurnParts(turn, 'newest', overrides)
  assert.deepEqual(folded.map(part => part.kind), ['process', 'text'])
  assert.equal(folded[0].expanded, false)
  assert.equal(folded[1], turn.parts[1])
  const foldedSignature = turnProcessFoldSignature(turn, 'newest', overrides)
  assert.match(foldedSignature, /^folded\|/)
  overrides.set(turn.key, true)
  const expanded = presentTurnParts(turn, 'newest', overrides)
  assert.deepEqual(expanded.map(part => part.kind), ['process', 'thinking', 'text'])
  assert.equal(expanded[0].expanded, true)
  assert.equal(turnProcessFoldSignature(turn, 'newest', overrides), foldedSignature.replace(/^folded/, 'open'))
  assert.deepEqual(turn, before)
})

test('parent completion and errors determine reasoning state without mutating timeline records', () => {
  const base = completedTurn()
  for (const [extra, expected] of [
    [{ completed: false, completionStatus: undefined }, 'active'],
    [{ completed: true, completionStatus: 'completed' }, 'done'],
    [{ completed: false, completionStatus: 'failed' }, 'interrupted'],
    [{ completed: true, completionStatus: 'cancelled' }, 'interrupted'],
    [{ completed: true, completionStatus: 'interrupted' }, 'interrupted'],
    [{ completed: false, errors: ['failure'] }, 'interrupted'],
  ]) {
    const turn = { ...base, parts: [{ kind: 'thinking', key: 'thought', text: 'work' }], ...extra }
    const before = structuredClone(turn)
    assert.equal(presentTurnParts(turn, turn.key, new Map())[0].state, expected)
    assert.deepEqual(turn, before)
  }
})

test('tool group status preserves running, failed and all-cancelled precedence', () => {
  for (const [statuses, expected] of [
    [[], 'completed'],
    [['completed'], 'completed'],
    [['cancelled', 'completed'], 'completed'],
    [['cancelled', 'cancelled'], 'cancelled'],
    [['cancelled', 'failed'], 'failed'],
    [['failed', 'running'], 'running'],
  ]) {
    assert.equal(toolGroupStatus(statuses.map(status => ({ status }))), expected)
  }
})
