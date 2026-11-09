import assert from 'node:assert/strict'
import test from 'node:test'
import { loadChatFeature, loadChatUtility } from './chatPresentationTestHarness.mjs'

const { countWorkLog, countWorkLogParts, currentStep, describeRunningTool, describeRunningTools, workLogCountLabel } =
  await loadChatFeature('worklog/workLog')
const { groupEventsIntoTurns } = await loadChatUtility('agentStreamTimeline')

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
  assert.deepEqual(currentStep({ parts: [], errors: [] }), { label: 'Starting', waiting: false })
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

test('an empty summary stays empty so the footer keeps one line', () => {
  assert.equal(workLogCountLabel(countWorkLogParts([])), '')
})
