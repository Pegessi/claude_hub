import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const modules = await Promise.all([
  'duration', 'chatQuestionResponse', 'subagentTool', 'agentImage',
  'agentStreamTimeline', 'thinkingPresentation',
].map(async name => ts.transpileModule(
  (await readFile(new URL(`../src/utils/${name}.ts`, import.meta.url), 'utf8'))
    .replace(/^import .* from '@\/utils\/.*$/gm, ''),
  { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 } },
).outputText))
const { presentThinkingParts, createSubthreadThinkingPresenter, groupEventsIntoTurns, IncrementalTimelineReducer, foldTurnParts } =
  await import(`data:text/javascript;base64,${Buffer.from(modules.join('\n')).toString('base64')}`)

const status = (key, text = 'Done thinking', extra = {}) => ({
  kind: 'status', key, text, providerStatus: 'reasoning', messageId: `reasoning:${key}`, ...extra,
})
const thinking = (key, text = 'private reasoning') => ({ kind: 'thinking', key, text })
const event = (sequence, type, payload = {}, extra = {}) => ({
  stream_sequence: sequence, type, payload, session_id: 's', tab_id: 't',
  turn_id: 'turn', agent_type: 'codex', created_at: '2026-10-16T10:00:00Z',
  redacted: false, ...extra,
})

// Assertions run against actual reducer output as well as direct projection
// inputs: status snapshots change in place, not necessarily at the list tail.
test('distinct consecutive completed reasoning items become one display row without changing the log', () => {
  const events = [event(1, 'turn_started', { summary: 'question' })]
  for (let n = 0; n < 12; n++) {
    events.push(event(n + 2, 'status', {
      text: 'Done thinking', provider_status: 'reasoning', snapshot: true,
    }, { message_id: `reasoning:${n}` }))
  }
  const turn = groupEventsIntoTurns(events)[0]
  const before = structuredClone(turn.parts)
  const display = presentThinkingParts(turn.parts, null)
  assert.equal(turn.parts.length, 12)
  assert.deepEqual(display, [{ kind: 'reasoning_status', key: turn.parts[0].key, state: 'done' }])
  assert.deepEqual(turn.parts, before)
})

test('opaque start, plaintext deltas and in-place done stay one keyed card', () => {
  const events = [
    event(1, 'turn_started', { summary: 'question' }),
    event(2, 'status', { text: 'Thinking…', provider_status: 'reasoning', snapshot: true },
      { message_id: 'reasoning:r1' }),
  ]
  const display = () => presentThinkingParts(groupEventsIntoTurns(events)[0].parts, null)
  const key = display()[0].key
  assert.deepEqual(display(), [{ kind: 'reasoning_status', key, state: 'active' }])
  events.push(event(3, 'thinking_delta', { text: 'part one' }))
  assert.deepEqual(display(), [{ kind: 'thinking', key, text: 'part one', state: 'active' }])
  events.push(event(4, 'thinking_delta', { text: ' and two' }))
  events.push(event(5, 'status', { text: 'Done thinking', provider_status: 'reasoning', snapshot: true },
    { message_id: 'reasoning:r1' }))
  assert.deepEqual(display(), [{ kind: 'thinking', key, text: 'part one and two', state: 'done' }])
})

test('only trailing plaintext animates; subsequent tool/text/status stops it', () => {
  for (const boundary of [
    { kind: 'text', key: 'answer', text: 'answer', at: '' },
    { kind: 'tool_group', key: 'tool', tools: [] },
    { kind: 'status', key: 'condense', text: 'Condensing context…' },
    { kind: 'approval', key: 'approval', approval: {} },
    { kind: 'subagents', key: 'children', threads: [] },
  ]) {
    const display = presentThinkingParts([thinking('a'), boundary, thinking('b')], null)
    assert.deepEqual(display.map(p => p.state), ['done', undefined, 'active'])
    assert.equal(display[1], boundary)
  }
})

test('adjacent active opaque items and plaintext share the card instead of an extra hint', () => {
  const result = presentThinkingParts([
    status('a'), status('b', 'Thinking…'), thinking('c', 'one'), thinking('d', 'two'),
  ], null)
  assert.deepEqual(result, [{ kind: 'thinking', key: 'a', text: 'one\n\ntwo', state: 'active' }])
})

test('terminal states stop live animation without changing explicit successful history', () => {
  for (const part of [thinking('p'), status('p', 'Thinking…')]) {
    assert.equal(presentThinkingParts([part], 'done')[0].state, 'done')
    assert.equal(presentThinkingParts([part], 'interrupted')[0].state, 'interrupted')
  }
  assert.equal(presentThinkingParts([status('done')], 'interrupted')[0].state, 'done')
})

test('errors stop the preceding run and remain visible; interruptions do not merge with success', () => {
  for (const error of [
    { kind: 'error', key: 'err', message: 'failure' },
    { kind: 'status', key: 'err', text: 'failure', providerStatus: 'error', isError: true },
  ]) {
    const display = presentThinkingParts([thinking('a'), error], null)
    assert.equal(display[0].state, 'interrupted')
    assert.equal(display[1], error)
  }
  const parts = [status('a'), status('b', 'Thinking interrupted'), status('c')]
  assert.deepEqual(presentThinkingParts(parts, null).map(p => p.state), ['done', 'interrupted', 'done'])
})

test('ordinary status text and unknown reasoning values are never swallowed', () => {
  const parts = [
    { kind: 'status', key: 'generic', text: 'Done thinking' },
    status('warning', 'Done thinking', { providerStatus: 'warning' }),
    status('future', 'Provider-specific warning'),
  ]
  assert.deepEqual(presentThinkingParts(parts, null), parts)
  assert.equal(presentThinkingParts([status('legacy', 'Done thinking', { providerStatus: undefined })], null)[0].kind,
    'reasoning_status')
})

test('main and child thread projections stay independent, including parent completion', () => {
  const events = [event(1, 'turn_started'), event(2, 'thinking_delta', { text: 'main' })]
  for (const [n, child] of ['child-full-a', 'child-full-b'].entries()) {
    events.push(event(n + 3, 'status', {
      text: 'Thinking…', provider_status: 'reasoning', snapshot: true, subagent_thread: child,
    }, { message_id: `reasoning:${child}` }))
  }
  events.push(event(5, 'turn_completed', { status: 'completed' }))
  const turn = groupEventsIntoTurns(events)[0]
  const region = turn.parts.find(p => p.kind === 'subagents')
  assert.equal(region.threads.length, 2)
  for (const child of region.threads) {
    assert.equal(child.lifecycleStatus, 'active')
    assert.equal(child.parts[0].providerStatus, 'reasoning')
    assert.equal(presentThinkingParts(child.parts, null)[0].state, 'active')
  }
  assert.equal(presentThinkingParts(turn.parts, 'done')[0].state, 'done')
})

test('snapshot metadata-only changes invalidate the render revision, exact replays do not', () => {
  for (const child of [null, 'child']) {
    const payload = { text: 'Done thinking', snapshot: true, ...(child ? { subagent_thread: child } : {}) }
    const events = [event(1, 'turn_started'), event(2, 'status', payload, { message_id: 'reasoning:r' })]
    const first = groupEventsIntoTurns(events)[0]
    events.push(event(3, 'status', { ...payload, provider_status: 'reasoning' }, { message_id: 'reasoning:r' }))
    const second = groupEventsIntoTurns(events)[0]
    assert.equal(second.renderRevision, first.renderRevision + 1)
    events.push(event(4, 'status', { ...payload, provider_status: 'reasoning' }, { message_id: 'reasoning:r' }))
    assert.equal(groupEventsIntoTurns(events)[0].renderRevision, second.renderRevision)
  }
})

test('historical process expansion still uses compact reasoning and preserves delivery', () => {
  const events = [event(1, 'turn_started'),
    event(2, 'status', { text: 'Done thinking', provider_status: 'reasoning' }),
    event(3, 'status', { text: 'Done thinking', provider_status: 'reasoning' }),
    event(4, 'text_delta', { text: 'answer' }), event(5, 'turn_completed', { status: 'completed' })]
  const turn = groupEventsIntoTurns(events)[0]
  const expanded = presentThinkingParts(foldTurnParts(turn, true), 'done')
  assert.deepEqual(expanded.map(p => p.kind), ['process', 'reasoning_status', 'text'])
  const folded = presentThinkingParts(foldTurnParts(turn, false), 'done')
  assert.deepEqual(folded.map(p => p.kind), ['process', 'text'])
})

test('trailing child activity does not complete its parent reasoning phase', () => {
  const parts = [status('main', 'Thinking…'), { kind: 'subagents', key: 'children', threads: [] }]
  assert.equal(presentThinkingParts(parts, null)[0].state, 'active')
  assert.equal(presentThinkingParts(parts, 'interrupted')[0].state, 'interrupted')
  assert.equal(presentThinkingParts(parts, 'done')[0].state, 'done')
})

test('unchanged large siblings reuse their projection; child snapshots and lifecycle invalidate it', () => {
  const events = [event(1, 'turn_started')]
  const addChildEvent = (child, type, payload, messageId) => events.push(event(events.length + 1, type,
    { ...payload, subagent_thread: child }, { message_id: messageId }))
  for (const child of ['a', 'b']) {
    for (let n = 0; n < 6750; n++) {
      addChildEvent(child, 'status', { text: 'Done thinking', provider_status: 'reasoning' }, `reasoning:${child}-${n}`)
    }
    addChildEvent(child, 'thinking_delta', { text: 'still working' })
  }
  const reducer = new IncrementalTimelineReducer()
  const present = createSubthreadThinkingPresenter()
  const children = () => reducer.reduce(events)[0].parts.find(p => p.kind === 'subagents').threads
  const [a, b] = children()
  const firstA = present(a)
  const firstB = present(b)
  addChildEvent('a', 'thinking_delta', { text: ' more' })
  children()
  const nextA = present(a)
  assert.notEqual(nextA, firstA)
  assert.equal(present(b), firstB)
  addChildEvent('b', 'status', { text: 'Thinking…', provider_status: 'reasoning', snapshot: true }, 'reasoning:b-live')
  children()
  const liveB = present(b)
  assert.notEqual(liveB, firstB)
  assert.equal(liveB[0].state, 'active')
  assert.equal(present(a), nextA)
  addChildEvent('b', 'status', { text: 'Done thinking', provider_status: 'reasoning', snapshot: true }, 'reasoning:b-live')
  children()
  assert.notEqual(present(b), liveB)
  assert.equal(present(b)[0].state, 'done')
  addChildEvent('b', 'status', { text: 'Thinking…', provider_status: 'reasoning' }, 'reasoning:b-next')
  children()
  const restartedB = present(b)
  assert.equal(restartedB[0].state, 'active')
  addChildEvent('b', 'status', { text: 'Sub-agent turn cancelled', provider_status: 'subagent_lifecycle', subagent_status: 'cancelled' })
  children()
  // Lifecycle and its visible status row must both invalidate the cached run.
  assert.equal(b.lifecycleStatus, 'cancelled')
  assert.equal(present(a), nextA)
  const cancelledB = present(b)
  assert.notEqual(cancelledB, restartedB)
  assert.equal(cancelledB[0].state, 'interrupted')
  assert.equal(present(b), cancelledB)
  const oldLength = cancelledB.length
  // A parent instruction is an append outside child routing; it must not stay cached.
  b.parts.push({ kind: 'instruction', key: 'parent-input', tool: {} })
  assert.equal(present(b).length, oldLength + 1)
})

test('incremental reducer and full replay expose the same reasoning presentation', () => {
  const events = [event(1, 'turn_started'),
    event(2, 'status', { text: 'Thinking…', provider_status: 'reasoning', snapshot: true }, { message_id: 'reasoning:r' }),
    event(3, 'thinking_delta', { text: 'reasoning' }),
    event(4, 'status', { text: 'Done thinking', provider_status: 'reasoning', snapshot: true }, { message_id: 'reasoning:r' })]
  const reducer = new IncrementalTimelineReducer()
  for (let n = 1; n <= events.length; n++) {
    const slice = events.slice(0, n)
    const incremental = reducer.reduce(slice)
    assert.deepEqual(presentThinkingParts(incremental[0].parts, null),
      presentThinkingParts(groupEventsIntoTurns(slice)[0].parts, null))
  }
})
