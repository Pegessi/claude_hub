import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { loadChatFeature, loadChatUtility } from './chatPresentationTestHarness.mjs'

const { groupEventsIntoTurns, IncrementalTimelineReducer } = await loadChatUtility('agentStreamTimeline')
const { nativeSubagentRows } = await loadChatFeature('presentation/nativeSubagentRows')
const { createSubthreadThinkingPresenter } = await loadChatUtility('thinkingPresentation')
const fixture = JSON.parse(await readFile(new URL('./fixtures/maso-subagent-lifecycle-replay.json', import.meta.url)))

function event(sequence, type, payload = {}, turnId = 'first') {
  return {
    stream_sequence: sequence, type, payload, turn_id: turnId,
    agent_type: 'maso', session_id: 'fixture', tab_id: 'fixture',
    created_at: '2026-11-18T00:00:00Z', redacted: false,
  }
}

function lifecycle(sequence, status = 'active', thread = 'child', turnId = 'first') {
  return event(sequence, 'status', {
    subagent_thread: thread, subagent_status: status,
    provider_status: 'subagent_lifecycle', text: `Sub-agent ${status}`,
  }, turnId)
}

const storedEvents = fixture.events.map(row => {
  const payload = row.thread
    ? { subagent_thread: row.thread, text: 'Child activity' } : {}
  if (row.type === 'status') Object.assign(payload, {
    provider_status: 'subagent_lifecycle', subagent_status: row.status,
  })
  if (row.type === 'turn_completed') payload.status = row.status
  return event(row.sequence, row.type, payload, row.turn)
})
const threads = turns => turns.flatMap(turn =>
  turn.parts.flatMap(part => part.kind === 'subagents' ? part.threads : []))
const running = turns => nativeSubagentRows(turns).filter(row => row.state === 'running').map(row => row.threadId)

test('stored 4492-event history projection has no running children after completed turns', () => {
  assert.equal(fixture.sourceEventCount, 4492)
  const turns = groupEventsIntoTurns(storedEvents)
  assert.ok(turns.every(turn => turn.completed))
  assert.deepEqual(running(turns), [], 'completed stored history must not leave running dock rows')
  assert.deepEqual(threads(turns).filter(child => child.lifecycleStatus === 'active'), [],
    'inline/header child lifecycle must also be terminal')
  for (const name of ['perf_collector-33e1', 'monitoring_collector-637a',
    'model_semantics_verifier-c3ae', 'toolkit_agent_auditor-ad3a']) {
    assert.ok(threads(turns).some(child => child.threadId === name))
  }
})

test('stored history prefix retains genuinely active children until their turn completes', () => {
  const prefix = storedEvents.filter(row => row.stream_sequence <= 232)
  assert.deepEqual(running(groupEventsIntoTurns(prefix)).sort(), [
    'model_semantics_verifier-c3ae', 'monitoring_collector-637a', 'perf_collector-33e1',
  ])
  const reducer = new IncrementalTimelineReducer()
  const memo = new Map()
  nativeSubagentRows(reducer.reduce(prefix), memo)
  const closed = reducer.reduce(storedEvents.filter(row => row.stream_sequence <= 281))
  assert.deepEqual(nativeSubagentRows(closed, memo).filter(row => row.state === 'running'), [])
})

test('new turn closes old children without needing a terminal event; late replay cannot reopen them', () => {
  for (const turnId of ['first', null]) {
    const events = [
      event(1, 'turn_started', {}, turnId),
      lifecycle(2, 'active', 'old', turnId),
    ]
    const reducer = new IncrementalTimelineReducer()
    const first = reducer.reduce(events)[0]
    const old = threads([first])[0]
    const revision = first.renderRevision
    events.push(event(3, 'turn_started', {}, turnId === null ? null : 'second'))
    assert.deepEqual(running(reducer.reduce(events)), [])
    assert.equal(old.lifecycleStatus, 'cancelled')
    assert.ok(first.renderRevision > revision)
    if (turnId !== null) {
      events.push(lifecycle(4, 'active', 'old', turnId))
      events.push(event(5, 'thinking_delta', { subagent_thread: 'old', text: 'late' }, turnId))
      events.push(lifecycle(6, 'active', 'new', 'second'))
      assert.deepEqual(running(reducer.reduce(events)), ['new'])
    }
  }
})

test('completion invalidates dock/thinking memos and settles child tools without overwriting terminal outcomes', () => {
  for (const [parentStatus, expected] of [
    ['completed', 'completed'], ['failed', 'failed'], ['cancelled', 'cancelled'], ['interrupted', 'cancelled'],
  ]) {
    const events = [
      event(1, 'turn_started'),
      lifecycle(2),
      lifecycle(3, 'failed', 'failed-child'),
      lifecycle(4, 'completed', 'done-child'),
      event(5, 'thinking_delta', { subagent_thread: 'child', text: 'still thinking' }),
      event(6, 'tool_call_started', { subagent_thread: 'child', tool_call_id: 'work', name: 'Read' }),
      event(7, 'thinking_delta', { subagent_thread: 'child', text: 'more thinking' }),
    ]
    const reducer = new IncrementalTimelineReducer()
    const memo = new Map()
    const presenter = createSubthreadThinkingPresenter()
    let turns = reducer.reduce(events)
    const child = threads(turns)[0]
    const revision = child.renderRevision
    assert.equal(presenter(child).at(-1).state, 'active')
    assert.deepEqual(nativeSubagentRows(turns, memo).filter(row => row.state === 'running').map(row => row.threadId), ['child'])
    events.push(event(8, 'turn_completed', { status: parentStatus }))
    turns = reducer.reduce(events)
    assert.equal(child.lifecycleStatus, expected)
    assert.ok(child.renderRevision > revision)
    assert.equal(presenter(child).at(-1).state, expected === 'completed' ? 'done' : 'interrupted')
    assert.equal(child.parts.find(part => part.kind === 'tool_group').tools[0].status, expected)
    assert.deepEqual(nativeSubagentRows(turns, memo).filter(row => row.state === 'running'), [])
    assert.equal(threads(turns)[1].lifecycleStatus, 'failed')
    assert.equal(threads(turns)[2].lifecycleStatus, 'completed')
    events.push(lifecycle(9, 'active', 'failed-child'))
    events.push(event(10, 'text_delta', { subagent_thread: 'failed-child', text: 'late report' }))
    events.push(lifecycle(11, 'active', 'late-child'))
    turns = reducer.reduce(events)
    assert.equal(threads(turns)[1].lifecycleStatus, 'failed')
    assert.deepEqual(running(turns), [])
    assert.equal(threads(turns).at(-1).lifecycleStatus, expected)
  }
})

test('explicit child wake-up remains possible in the current unfinished turn', () => {
  const events = [event(1, 'turn_started'), lifecycle(2, 'completed'), lifecycle(3)]
  assert.deepEqual(running(groupEventsIntoTurns(events)), ['child'])
})
