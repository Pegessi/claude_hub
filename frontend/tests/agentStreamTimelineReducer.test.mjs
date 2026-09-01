import assert from 'node:assert/strict'
import { hrtime } from 'node:process'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const source = await readFile(
  new URL('../src/utils/agentStreamTimeline.ts', import.meta.url),
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
const { groupEventsIntoTurns, IncrementalTimelineReducer } = mod

function makeEvent(seq, type, payload = {}, overrides = {}) {
  return {
    stream_sequence: seq,
    session_id: 's1',
    tab_id: 't1',
    agent_type: 'claude',
    type,
    payload,
    created_at: '2026-01-01T00:00:00Z',
    redacted: false,
    ...overrides,
  }
}

test('incremental reducer produces identical output to groupEventsIntoTurns', () => {
  const events = [
    makeEvent(0, 'turn_started', { summary: 'first' }, { turn_id: 't1' }),
    makeEvent(1, 'thinking_delta', { text: 'thinking…' }, { turn_id: 't1' }),
    makeEvent(2, 'text_delta', { text: 'Hello ' }, { turn_id: 't1' }),
    makeEvent(3, 'tool_call_started', {
      tool_call_id: 'c1', name: 'Bash', args: { command: 'ls' },
    }, { turn_id: 't1' }),
    makeEvent(4, 'tool_call_completed', {
      tool_call_id: 'c1', status: 'completed', result: 'file1',
    }, { turn_id: 't1' }),
    makeEvent(5, 'text_delta', { text: 'world' }, { turn_id: 't1' }),
    makeEvent(6, 'turn_completed', { status: 'completed' }, { turn_id: 't1' }),
  ]

  const expected = groupEventsIntoTurns(events)

  const reducer = new IncrementalTimelineReducer()
  const actual = reducer.reduce(events)

  assert.deepEqual(actual, expected)
})

test('incremental reducer processes events in batches and accumulates correctly', () => {
  const reducer = new IncrementalTimelineReducer()

  const batch1 = [
    makeEvent(0, 'turn_started', { summary: 'hi' }, { turn_id: 't1' }),
    makeEvent(1, 'text_delta', { text: 'foo' }, { turn_id: 't1' }),
  ]
  reducer.reduce(batch1)

  const batch2 = [
    makeEvent(2, 'text_delta', { text: 'bar' }, { turn_id: 't1' }),
    makeEvent(3, 'turn_completed', { status: 'completed' }, { turn_id: 't1' }),
  ]
  const turns = reducer.reduce([...batch1, ...batch2])

  assert.equal(turns.length, 1)
  assert.equal(turns[0].assistantText, 'foobar')
  assert.equal(turns[0].completed, true)
})

test('incremental reducer resets when event list shrinks', () => {
  const reducer = new IncrementalTimelineReducer()

  const events = [
    makeEvent(0, 'turn_started', { summary: 'a' }, { turn_id: 't1' }),
    makeEvent(1, 'text_delta', { text: 'x' }, { turn_id: 't1' }),
  ]
  reducer.reduce(events)
  assert.equal(reducer.consumed, 2)

  // Simulate stream reset: events array becomes empty.
  const turns = reducer.reduce([])
  assert.equal(turns.length, 0)
  assert.equal(reducer.consumed, 0)
  assert.equal(reducer.appliedCount, 0)
})

test('incremental reducer resets on same-length replacement (session switch)', () => {
  const reducer = new IncrementalTimelineReducer()

  const sessionA = [
    makeEvent(0, 'turn_started', { summary: 'A1' }, { turn_id: 'a1', session_id: 'sess-A' }),
    makeEvent(1, 'text_delta', { text: 'from A' }, { turn_id: 'a1', session_id: 'sess-A' }),
  ]
  reducer.reduce(sessionA)
  assert.equal(reducer.consumed, 2)
  assert.equal(reducer.appliedCount, 2)

  // Same length, but different session identity and content. The reducer must
  // detect the non-prefix replacement and rebuild from scratch.
  const sessionB = [
    makeEvent(0, 'turn_started', { summary: 'B1' }, { turn_id: 'b1', session_id: 'sess-B' }),
    makeEvent(1, 'text_delta', { text: 'from B' }, { turn_id: 'b1', session_id: 'sess-B' }),
  ]
  const turns = reducer.reduce(sessionB)

  assert.equal(reducer.consumed, 2)
  // appliedCount resets to 0 on rebuild, then the 2 new events are applied.
  assert.equal(reducer.appliedCount, 2)
  assert.equal(turns.length, 1)
  assert.equal(turns[0].userText, 'B1')
  assert.equal(turns[0].assistantText, 'from B')
})

test('incremental reducer resets on longer non-prefix replacement', () => {
  const reducer = new IncrementalTimelineReducer()

  const original = [
    makeEvent(0, 'turn_started', { summary: 'orig' }, { turn_id: 'o1', session_id: 'sess-A' }),
    makeEvent(1, 'text_delta', { text: 'original' }, { turn_id: 'o1', session_id: 'sess-A' }),
  ]
  reducer.reduce(original)
  assert.equal(reducer.consumed, 2)

  // Longer list, but the prefix diverges (different session). The reducer must
  // detect that events[processedCount-1] is not the last applied event and
  // rebuild from scratch.
  const replacement = [
    makeEvent(0, 'turn_started', { summary: 'new' }, { turn_id: 'n1', session_id: 'sess-B' }),
    makeEvent(1, 'text_delta', { text: 'new text' }, { turn_id: 'n1', session_id: 'sess-B' }),
    makeEvent(2, 'turn_completed', { status: 'completed' }, { turn_id: 'n1', session_id: 'sess-B' }),
  ]
  const turns = reducer.reduce(replacement)

  assert.equal(reducer.consumed, 3)
  assert.equal(reducer.appliedCount, 3)
  assert.equal(turns.length, 1)
  assert.equal(turns[0].userText, 'new')
  assert.equal(turns[0].assistantText, 'new text')
  assert.equal(turns[0].completed, true)
})

test('incremental reducer does not reset when the same session appends (prefix holds)', () => {
  const reducer = new IncrementalTimelineReducer()

  const first = [
    makeEvent(0, 'turn_started', { summary: 'q' }, { turn_id: 't1', session_id: 'sess-A' }),
    makeEvent(1, 'text_delta', { text: 'hello' }, { turn_id: 't1', session_id: 'sess-A' }),
  ]
  reducer.reduce(first)
  assert.equal(reducer.appliedCount, 2)

  // Append more events from the same session. The prefix (first 2 events) is
  // identical, so the reducer must NOT reset and must only apply the new ones.
  const second = [
    ...first,
    makeEvent(2, 'text_delta', { text: ' world' }, { turn_id: 't1', session_id: 'sess-A' }),
    makeEvent(3, 'turn_completed', { status: 'completed' }, { turn_id: 't1', session_id: 'sess-A' }),
  ]
  const turns = reducer.reduce(second)

  // Only 2 new events applied; appliedCount goes from 2 to 4.
  assert.equal(reducer.appliedCount, 4)
  assert.equal(reducer.consumed, 4)
  assert.equal(turns[0].assistantText, 'hello world')
  assert.equal(turns[0].completed, true)
})

test('incremental reducer only processes the unseen suffix (bounded work, 13.5k history)', () => {
  // Build a large historical event list that matches the real-world scale
  // from the review: ~13.5k events. Each turn has a few deltas.
  const HISTORY_TURNS = 3375 // 3375 turns * 4 events = 13500 events
  const history = []
  let seq = 0
  for (let i = 0; i < HISTORY_TURNS; i++) {
    const turnId = `hist-${i}`
    history.push(makeEvent(seq++, 'turn_started', { summary: `q${i}` }, { turn_id: turnId }))
    history.push(makeEvent(seq++, 'text_delta', { text: `a${i} ` }, { turn_id: turnId }))
    history.push(makeEvent(seq++, 'text_delta', { text: `b${i}` }, { turn_id: turnId }))
    history.push(makeEvent(seq++, 'turn_completed', { status: 'completed' }, { turn_id: turnId }))
  }
  assert.equal(history.length, 13500)

  const reducer = new IncrementalTimelineReducer()
  reducer.reduce(history)
  assert.equal(reducer.consumed, 13500)
  assert.equal(reducer.appliedCount, 13500)

  // Now add 300 live deltas to the current (last) turn.
  const liveTurnId = `hist-${HISTORY_TURNS - 1}`
  const liveDeltas = []
  for (let i = 0; i < 300; i++) {
    liveDeltas.push(makeEvent(seq++, 'text_delta', { text: '.' }, { turn_id: liveTurnId }))
  }

  const all = [...history, ...liveDeltas]

  const start = hrtime.bigint()
  const turns = reducer.reduce(all)
  const elapsedMs = Number(hrtime.bigint() - start) / 1e6

  // Deterministic bounded-work assertion: the second reduce must apply
  // exactly the 300 new deltas, NOT re-scan the 13.5k historical events.
  // appliedCount is cumulative since the last reset, so it goes from 13500
  // to 13800 — a delta of exactly 300.
  assert.equal(reducer.appliedCount, 13500 + 300)
  assert.equal(reducer.consumed, all.length)

  // The last turn's assistant text must include all 300 live dots.
  const lastTurn = turns[turns.length - 1]
  assert.equal(lastTurn.assistantText.split('.').length - 1, 300)

  // Timing is informational: a full O(13800) re-scan would be ~10-50ms; the
  // incremental O(300) path should be well under that. We use a generous
  // bound to avoid CI flakiness; the deterministic appliedCount assertion
  // above is the real proof of bounded work.
  if (elapsedMs > 20) {
    console.warn(`incremental reduce of 300 deltas after 13.5k history took ${elapsedMs.toFixed(2)}ms (informational)`)
  }
})

test('incremental reducer preserves tool ordering across batches', () => {
  const reducer = new IncrementalTimelineReducer()

  const batch1 = [
    makeEvent(0, 'turn_started', { summary: 'tools' }, { turn_id: 't1' }),
    makeEvent(1, 'tool_call_started', {
      tool_call_id: 'c1', name: 'Bash', args: {},
    }, { turn_id: 't1' }),
  ]
  reducer.reduce(batch1)

  const batch2 = [
    makeEvent(2, 'tool_call_completed', {
      tool_call_id: 'c1', status: 'completed', result: 'ok',
    }, { turn_id: 't1' }),
    makeEvent(3, 'tool_call_started', {
      tool_call_id: 'c2', name: 'WebSearch', args: {},
    }, { turn_id: 't1' }),
    makeEvent(4, 'tool_call_completed', {
      tool_call_id: 'c2', status: 'completed', result: 'found',
    }, { turn_id: 't1' }),
  ]
  const turns = reducer.reduce([...batch1, ...batch2])

  assert.equal(turns[0].tools.length, 2)
  assert.equal(turns[0].tools[0].name, 'Bash')
  assert.equal(turns[0].tools[0].status, 'completed')
  assert.equal(turns[0].tools[1].name, 'WebSearch')
  assert.equal(turns[0].tools[1].status, 'completed')

  // Parts order must match emission order.
  const kinds = turns[0].parts.map(p => p.kind)
  assert.deepEqual(kinds, ['tool', 'tool'])
})

test('incremental reducer handles replay dedup across batches', () => {
  const reducer = new IncrementalTimelineReducer()

  // First batch: two chunks.
  const batch1 = [
    makeEvent(0, 'turn_started', { summary: 'replay' }, { turn_id: 't1' }),
    makeEvent(1, 'text_delta', { text: '智涌' }, { turn_id: 't1' }),
    makeEvent(2, 'text_delta', { text: '今朝\n' }, { turn_id: 't1' }),
  ]
  reducer.reduce(batch1)

  // Second batch: a third chunk, then a replay of the previous two.
  const batch2 = [
    makeEvent(3, 'text_delta', { text: '千机灯火' }, { turn_id: 't1' }),
    makeEvent(4, 'text_delta', { text: '智涌今朝\n千机灯火' }, { turn_id: 't1' }),
  ]
  const turns = reducer.reduce([...batch1, ...batch2])

  assert.equal(turns[0].assistantText, '智涌今朝\n千机灯火')
})

test('incremental reducer returns a fresh array reference each call', () => {
  const reducer = new IncrementalTimelineReducer()
  const events = [makeEvent(0, 'turn_started', { summary: 'a' })]

  const first = reducer.reduce(events)
  const second = reducer.reduce(events)

  assert.notEqual(first, second, 'reduce must return a new array reference each call')
  assert.deepEqual(first, second)
})
