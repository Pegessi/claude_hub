import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { loadChatFeature, loadChatUtility } from './chatPresentationTestHarness.mjs'

const { countWorkLog, countWorkLogParts, currentStep, describeRunningTool, describeRunningTools, emptyWorkLogCounts, firstResponseHint, FIRST_RESPONSE_HINT_AFTER_MS, FIRST_RESPONSE_STUCK_AFTER_MS, workLogCountLabel, workLogCountParts } =
  await loadChatFeature('worklog/workLog')
const { groupEventsIntoTurns } = await loadChatUtility('agentStreamTimeline')
const { RUNTIME_STATUS_MAX_AGE_MS, runtimeStatusFor } = await loadChatFeature('worklog/runtimeStatus')

/** Raw view of a component file for structural pins (the repo keeps these in
 *  unit tests rather than byte-asserting rendered HTML). */
const readSrc = path => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')

const event = (sequence, type, payload = {}, extra = {}) => ({
  stream_sequence: sequence, type, payload, session_id: 's', tab_id: 't',
  turn_id: 'turn', agent_type: 'claude', created_at: '2026-11-09T10:00:00Z',
  redacted: false, ...extra,
})

const call = (sequence, id, name, args = {}) => event(sequence, 'tool_call_started', {
  tool_call_id: id,
  name,
  args,
})

const finished = (sequence, id, status = 'completed') => event(sequence, 'tool_call_completed', {
  tool_call_id: id,
  status,
})

const tool = (name, status = 'running', argsText = '', extra = {}) => ({
  key: `tool-${name}-${status}`, callId: null, name, status, argsText, resultText: '', ...extra,
})

/** A live turn: two reads still running after one finished edit and command. */
function liveTurn() {
  return groupEventsIntoTurns([
    event(1, 'turn_started', { summary: 'fix the flaky test' }),
    event(2, 'thinking_delta', { text: 'trace the failure' }),
    call(3, 'b1', 'Bash', { command: 'pnpm test work-log' }),
    finished(4, 'b1'),
    call(5, 'e1', 'Edit', { file_path: '/repo/frontend/src/fes.py' }),
    finished(6, 'e1'),
    call(7, 'r1', 'Read', { file_path: '/repo/frontend/src/a.ts' }),
    call(8, 'r2', 'Read', { file_path: '/repo/frontend/src/b.ts' }),
  ])[0]
}

test('counts describe the work done instead of the event stream', () => {
  const turn = liveTurn()
  const counts = countWorkLog(turn)
  assert.equal(counts.commands, 1)
  assert.equal(counts.edits, 1)
  assert.equal(counts.reads, 2)
  assert.equal(counts.failed, 0, 'a completed call is not a failure')
  assert.equal(counts.subagents, 0)
  assert.equal(workLogCountLabel(counts), '1 edit · 1 command · 2 reads')
})

test('the live step names the newest running call, not the tool name', () => {
  const turn = liveTurn()
  const step = currentStep(turn)
  assert.equal(step.waiting, false)
  assert.equal(step.label, 'Reading 2 files', 'parallel same-kind calls read as one sentence')
})

test('a single running command keeps its target so the reader knows what runs', () => {
  const turn = groupEventsIntoTurns([
    event(1, 'turn_started', { summary: 'q' }),
    call(2, 'b1', 'Bash', { command: 'pytest -q tests/test_fes.py' }),
  ])[0]
  assert.equal(currentStep(turn).label, 'Running `pytest -q tests/test_fes.py`')
  assert.equal(describeRunningTool(tool('Edit', 'running', JSON.stringify({ file_path: '/repo/fes.py' }))), 'Editing /repo/fes.py')
})

test('plan bookkeeping is not counted as work', () => {
  const counts = countWorkLogParts([
    { kind: 'tool', key: 'k1', tool: tool('TodoWrite') },
    { kind: 'tool', key: 'k2', tool: tool('Bash', 'completed', JSON.stringify({ command: 'pwd' }) ) },
  ])
  assert.equal(counts.commands, 1)
  assert.equal(counts.other, 0, 'todo rewrites must not pad the summary')
  assert.equal(workLogCountLabel(counts), '1 command')
})

test('failures stay visible: failed calls, failed launches and turn errors all count', () => {
  const launch = { ...tool('spawnAgent', 'failed'), subagent: { provider: 'traex', agentType: null, description: 'child', prompt: '', threadIds: ['T1'], background: false, directive: false } }
  const turn = {
    parts: [
      { kind: 'tool_group', key: 'g', tools: [tool('Bash', 'failed', JSON.stringify({ command: 'pnpm lint' }))] },
      { kind: 'subagents', key: 's', threads: [{ key: 'th', threadId: 'T1', launchTool: launch, lifecycleStatus: 'failed', renderRevision: 0, parts: [] }] },
    ],
    errors: [{ key: 'e', message: 'provider crashed' }],
  }
  const counts = countWorkLog(turn)
  assert.equal(counts.failed, 3, 'one failed call, one failed launch, one turn error')
  assert.match(workLogCountLabel(counts), /3 failed/)
})

test('one launch addressed to several threads is one failure, not one per child', () => {
  // The reducer attaches a single spawn call to every receiver thread, so
  // counting per thread would report "3 failed" for one failed launch.
  const launch = {
    ...tool('spawnAgent', 'failed'),
    subagent: {
      provider: 'traex', agentType: null, description: 'children', prompt: '',
      threadIds: ['T1', 'T2', 'T3'], background: false, directive: false,
    },
  }
  const threads = ['T1', 'T2', 'T3'].map(threadId => ({
    key: `th-${threadId}`, threadId, launchTool: launch, lifecycleStatus: 'failed', renderRevision: 0, parts: [],
  }))
  const counts = countWorkLogParts([{ kind: 'subagents', key: 's', threads }])
  assert.equal(counts.failed, 1, 'the same call is one failure however many threads it started')
  assert.equal(counts.subagents, 3, 'but it really did start three children')
  assert.equal(workLogCountLabel(counts), '1 failed · 3 sub-agents')
})

test('one directive filed in several children is one failure', () => {
  // sendInput is filed as an instruction row inside each addressed child.
  const directive = { ...tool('sendInput', 'failed'), subagent: { provider: 'traex', agentType: null, description: 'x', prompt: '', threadIds: ['T1', 'T2'], background: false, directive: true } }
  const row = { kind: 'instruction', key: 'instr', tool: directive }
  const threads = ['T1', 'T2'].map(threadId => ({
    key: `th-${threadId}`, threadId, launchTool: null, lifecycleStatus: 'active', renderRevision: 0, parts: [row],
  }))
  assert.equal(countWorkLogParts([{ kind: 'subagents', key: 's', threads }]).failed, 1)
})

test('a child that streams its own region and its spawn call count as one sub-agent', () => {
  const spawn = { ...tool('Agent', 'running'), subagent: { provider: 'claude', agentType: 'Explore', description: 'child', prompt: '', threadIds: ['T9'], background: true, directive: false } }
  const counts = countWorkLogParts([
    { kind: 'subagent', key: 'c', tool: spawn },
    { kind: 'subagents', key: 's', threads: [{ key: 'th', threadId: 'T9', launchTool: spawn, lifecycleStatus: 'active', renderRevision: 0, parts: [] }] },
  ])
  assert.equal(counts.subagents, 1, 'the same child must not be counted twice')
})

test('child work is the turn work: a delegated command and its failure count', () => {
  const counts = countWorkLogParts([{
    kind: 'subagents',
    key: 's',
    threads: [{
      key: 'th', threadId: 'T1', launchTool: null, lifecycleStatus: 'active', renderRevision: 0,
      parts: [{ kind: 'tool_group', key: 'cg', tools: [tool('Bash', 'failed', JSON.stringify({ command: 'make' }))] }],
    }],
  }])
  assert.equal(counts.commands, 1)
  assert.equal(counts.failed, 1)
  assert.equal(workLogCountLabel(counts), '1 command · 1 failed · 1 sub-agent')
})

test('waiting states never read as activity', () => {
  const pendingAnswer = {
    parts: [{ kind: 'approval', key: 'a', approval: { key: 'a', callId: null, kind: 'question', title: null, questions: [], resolved: false } }],
    errors: [],
  }
  assert.deepEqual(currentStep(pendingAnswer), { label: 'Waiting for your answer', waiting: true })

  const childRunning = {
    parts: [
      { kind: 'subagent', key: 'c', tool: { ...tool('Agent', 'running'), subagent: { provider: 'claude', agentType: null, description: 'child', prompt: '', threadIds: ['T1'], background: true, directive: false } } },
      { kind: 'subagents', key: 's', threads: [{ key: 'th', threadId: 'T1', launchTool: null, lifecycleStatus: 'active', renderRevision: 0, parts: [] }] },
    ],
    errors: [],
  }
  assert.deepEqual(currentStep(childRunning), { label: 'Waiting on 1 sub-agent', waiting: true }, 'an open spawn is a delegation, not activity')
})

test('a turn whose newest part is finished work reports what it is doing instead of nothing', () => {
  const thinking = { parts: [{ kind: 'tool_group', key: 'g', tools: [tool('Bash', 'completed')] }], errors: [] }
  assert.deepEqual(currentStep(thinking), { label: 'Thinking', waiting: false })
  assert.deepEqual(currentStep({ parts: [{ kind: 'text', key: 't', text: 'answer…', at: '' }], errors: [] }), { label: 'Writing the reply', waiting: false })
  const errored = { parts: [{ kind: 'error', key: 'e', message: 'boom' }], errors: [{ key: 'e', message: 'boom' }] }
  assert.deepEqual(currentStep(errored), { label: 'Stopped by an error', waiting: true })
  // A turn that exists but has reported nothing yet is live, not idle: it says
  // what it is waiting for and keeps its clock, without claiming a step.
  assert.deepEqual(
    currentStep({ parts: [], errors: [] }),
    { label: 'Waiting for the first response', waiting: false, bare: true },
  )
  assert.equal(currentStep({ parts: [], errors: [] }).waiting, false, 'the turn is not blocked')
})

test('mixed running calls report the newest one and long targets truncate', () => {
  const mixed = [
    tool('Read', 'running', JSON.stringify({ file_path: '/repo/a.ts' })),
    tool('Bash', 'running', JSON.stringify({ command: `echo ${'x'.repeat(120)}` })),
  ]
  const label = describeRunningTools(mixed)
  assert.match(label, /^Running `echo x+/)
  assert.ok(label.length < 80, `live labels must stay one line, got ${label.length}`)
  assert.ok(label.endsWith('…`'), 'a truncated command is marked, not silently cut')
})

test('every live turn gets the footer, and the bare placeholder is gone', () => {
  // The three-dot "Waiting for response…" card used to stand in for the first
  // seconds of a turn, with no clock and no state. One footer covers both.
  const turnSource = readSrc('features/chat/components/ChatTurn.vue')
  assert.ok(!turnSource.includes('agent-waiting-card'), 'the bare placeholder must be removed')
  assert.ok(!turnSource.includes('agent-waiting-pulse'), 'its animation must not linger')
  assert.match(turnSource, /<ChatTurnWorking\n\s+v-if="isLive\(turn\)"/)
  assert.ok(
    !/<ChatTurnWorking[^>]*awaitingAgentActivity/s.test(turnSource),
    'a live turn with no events must not skip the footer',
  )
  const footerSource = readSrc('features/chat/components/ChatTurnWorking.vue')
  assert.match(footerSource, /turn-working-counts-failed/, 'the failure phrase has its own element')
  assert.ok(
    !/counts-text--failed/.test(footerSource),
    'the whole counts line must not be repainted red for one failure',
  )
})

test('an empty summary stays empty so the footer keeps one line', () => {
  assert.equal(workLogCountLabel(countWorkLogParts([])), '')
})

test('a failure is its own phrase so only it takes the warning tone', () => {
  const counts = countWorkLogParts([{ kind: 'tool_group', key: 'g', tools: [tool('Bash', 'completed'), tool('Edit', 'completed'), tool('Bash', 'failed')] }])
  const parts = workLogCountParts(counts)
  assert.equal(parts.prefix, '1 edit · 2 commands')
  assert.equal(parts.failed, '1 failed')
  assert.equal(parts.suffix, '')
  assert.equal(workLogCountLabel(counts), '1 edit · 2 commands · 1 failed', 'the joined line is unchanged')

  // Delegation still reads after the failure, as it always did.
  const withChild = workLogCountParts({ ...emptyWorkLogCounts(), commands: 1, failed: 1, subagents: 1 })
  assert.equal(withChild.prefix, '1 command')
  assert.equal(withChild.failed, '1 failed')
  assert.equal(withChild.suffix, '1 sub-agent')
  assert.equal(workLogCountLabel({ ...emptyWorkLogCounts(), commands: 1, failed: 1, subagents: 1 }), '1 command · 1 failed · 1 sub-agent')

  const failuresOnly = workLogCountParts({ ...emptyWorkLogCounts(), failed: 2 })
  assert.equal(failuresOnly.prefix, '', 'nothing went through, so there is no neutral phrase')
  assert.equal(failuresOnly.failed, '2 failed')
  assert.equal(workLogCountLabel({ ...emptyWorkLogCounts(), failed: 2 }), '2 failed')
})

test('the silent-start sentence reports what the tab is actually doing', () => {
  const running = { status: 'working', activity: null, ageMs: 2000 }
  const facts = (elapsedMs, runtime, connectionState = 'live') => ({ elapsedMs, runtime, connectionState })

  // A running provider is the common, healthy case: say so instead of implying
  // the turn is stalled.
  assert.equal(
    firstResponseHint(facts(5_000, running)),
    'The provider is running; it has not written anything yet.',
  )
  // Past two minutes of silence the same fact becomes a warning worth acting on.
  assert.equal(
    firstResponseHint(facts(FIRST_RESPONSE_STUCK_AFTER_MS, running)),
    'The provider is running but has written nothing for a while — usually a queued or stuck session.',
  )
  // A concrete activity is more specific than the generic sentence.
  assert.equal(
    firstResponseHint(facts(5_000, { status: 'working', activity: 'Running pytest -q', ageMs: 2000 })),
    'The provider is running pytest -q — nothing written yet.',
  )
  // Dead providers never deliver, so the sentence has to say that outright.
  assert.match(firstResponseHint(facts(5_000, { status: 'offline', activity: null, ageMs: 2000 })), /offline, so nothing can arrive/)
  assert.match(firstResponseHint(facts(5_000, { status: 'attention', activity: null, ageMs: 2000 })), /reported a problem/)
  // An open turn against an idle provider is the dropped-request shape.
  assert.match(firstResponseHint(facts(5_000, { status: 'idle', activity: null, ageMs: 2000 })), /may have dropped the request/)

  // No fresh sample: say that the clock is all we have, and only once it matters.
  assert.equal(firstResponseHint(facts(5_000, null)), '', 'half a second of silence needs no copy')
  assert.equal(
    firstResponseHint(facts(FIRST_RESPONSE_HINT_AFTER_MS, null)),
    'Nothing has come back yet, and the Hub has no fresh runtime status for this tab.',
  )

  // The transport outranks the provider: nothing can arrive over a dead stream.
  assert.equal(firstResponseHint(facts(5_000, running, 'reconciling')), 'Reconnecting to the event stream…')
  assert.match(firstResponseHint(facts(5_000, running, 'failed')), /event stream failed/)
})

test('the footer says who is waiting from facts, not from the model label', () => {
  const src = readSrc('features/chat/components/ChatTurnWorking.vue')
  // The composer already shows the model; repeating it here was the noise this
  // replaced. The sentence carries the information instead.
  assert.doesNotMatch(src, /combinedModelLabel/, 'the model label belongs to the composer')
  assert.doesNotMatch(src, /turn-working-context/, 'no second model chip on the state line')
  assert.match(src, /runtimeStatus/, 'the footer reads the tab runtime status')
})

test('a polled tab status is read as server time and dropped once it stales', () => {
  const now = Date.parse('2026-11-10T17:19:00.000Z')
  // The payload carries no offset; read it as UTC, not as the client's zone.
  const fresh = runtimeStatusFor(
    { status: 'working', activity: 'Running pytest -q', sampled_at: '2026-11-10T17:18:49.175424' },
    now,
  )
  assert.deepEqual(fresh, { status: 'working', activity: 'Running pytest -q', ageMs: 10_825 }, 'fresh sample survives')

  // A +8 client would have read that same string as eight hours old.
  const stale = runtimeStatusFor(
    { status: 'working', sampled_at: '2026-11-10T16:00:00.000Z' },
    now,
  )
  assert.equal(stale, null, 'a sample older than a minute is not evidence')
  assert.ok(now - Date.parse('2026-11-10T16:00:00.000Z') > RUNTIME_STATUS_MAX_AGE_MS)

  // A server clock running behind the browser must not yield a negative age.
  const skewed = runtimeStatusFor(
    { status: 'idle', sampled_at: '2026-11-10T17:19:30.000Z' },
    now,
  )
  assert.deepEqual(skewed, { status: 'idle', activity: null, ageMs: 0 })

  // Missing timestamp still means "this poll says so"; missing sample means null.
  assert.deepEqual(
    runtimeStatusFor({ status: 'offline' }, now),
    { status: 'offline', activity: null, ageMs: 0 },
  )
  assert.equal(runtimeStatusFor(undefined, now), null)
})
