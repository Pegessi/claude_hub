import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

// agentStreamTimeline.ts imports only from @/types (type-only), so transpiling
// with TypeScript and loading via data URL works without shims.
const source = await readFile(
  new URL('../src/utils/agentStreamTimeline.ts', import.meta.url),
  'utf8',
)
const questionSource = await readFile(
  new URL('../src/utils/chatQuestionResponse.ts', import.meta.url),
  'utf8',
)
const transpileOptions = {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
}
const questionJs = ts.transpileModule(questionSource, transpileOptions).outputText
const timelineJs = ts.transpileModule(
  source.replace(
    "import { parseStructuredQuestions } from '@/utils/chatQuestionResponse'",
    '',
  ),
  transpileOptions,
).outputText
const bundled = `${questionJs}\n${timelineJs}`
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(bundled).toString('base64')}`
)
const { groupEventsIntoTurns } = mod

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

test('empty event list yields no turns', () => {
  assert.deepEqual(groupEventsIntoTurns([]), [])
})

test('turn_started creates a turn with the user message from summary', () => {
  const events = [makeEvent(1, 'turn_started', { summary: 'Hello' })]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns.length, 1)
  assert.equal(turns[0].userText, 'Hello')
})

test('text_delta appends to assistant text within the current turn', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'Hi' }),
    makeEvent(2, 'text_delta', { text: 'Hello ' }),
    makeEvent(3, 'text_delta', { text: 'world' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns.length, 1)
  assert.equal(turns[0].assistantText, 'Hello world')
})

test('historical multi-chunk final snapshot replays are hidden', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'poem' }, { turn_id: 'poem-turn' }),
    makeEvent(2, 'text_delta', { text: '智涌' }, { turn_id: 'poem-turn' }),
    makeEvent(3, 'text_delta', { text: '今朝\n' }, { turn_id: 'poem-turn' }),
    makeEvent(4, 'text_delta', { text: '千机灯火' }, { turn_id: 'poem-turn' }),
    makeEvent(5, 'text_delta', { text: '智涌今朝\n千机灯火' }, { turn_id: 'poem-turn' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns[0].assistantText, '智涌今朝\n千机灯火')
})

test('historical replay repair supports multiple assistant messages in one turn', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'two messages' }, { turn_id: 'multi' }),
    makeEvent(2, 'text_delta', { text: 'po' }, { turn_id: 'multi' }),
    makeEvent(3, 'text_delta', { text: 'em' }, { turn_id: 'multi' }),
    makeEvent(4, 'text_delta', { text: 'poem' }, { turn_id: 'multi' }),
    makeEvent(5, 'text_delta', { text: 'ex' }, { turn_id: 'multi' }),
    makeEvent(6, 'text_delta', { text: 'plain' }, { turn_id: 'multi' }),
    makeEvent(7, 'text_delta', { text: 'explain' }, { turn_id: 'multi' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns[0].assistantText, 'poemexplain')
})

test('a legitimate repeated single delta remains visible', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'repeat' }, { turn_id: 'repeat' }),
    makeEvent(2, 'text_delta', { text: 'ha' }, { turn_id: 'repeat' }),
    makeEvent(3, 'text_delta', { text: 'ha' }, { turn_id: 'repeat' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns[0].assistantText, 'haha')
})

test('thinking_delta appends to thinking text within the current turn', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'Hi' }),
    makeEvent(2, 'thinking_delta', { text: 'Let me think… ' }),
    makeEvent(3, 'thinking_delta', { text: 'done.' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns[0].thinkingText, 'Let me think… done.')
})

test('tool_call_started creates a running tool; tool_call_completed marks it done', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'run ls' }),
    makeEvent(2, 'tool_call_started', {
      tool_call_id: 'c1',
      name: 'Bash',
      args: { command: 'ls' },
    }),
    makeEvent(3, 'tool_call_completed', {
      tool_call_id: 'c1',
      status: 'completed',
      result: 'file1\nfile2',
    }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns[0].tools.length, 1)
  const tool = turns[0].tools[0]
  assert.equal(tool.name, 'Bash')
  assert.equal(tool.status, 'completed')
  assert.equal(tool.resultText, 'file1\nfile2')
  assert.ok(tool.argsText.includes('ls'))
})

test('tool_call_completed with status=failed marks the tool as failed', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'fail' }),
    makeEvent(2, 'tool_call_started', { tool_call_id: 'c1', name: 'Bash', args: {} }),
    makeEvent(3, 'tool_call_completed', { tool_call_id: 'c1', status: 'failed', result: 'boom' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns[0].tools[0].status, 'failed')
})

test('turn_completed flips still-running tools to completed', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'run' }),
    makeEvent(2, 'tool_call_started', { tool_call_id: 'c1', name: 'Bash', args: {} }),
    makeEvent(3, 'turn_completed', { status: 'completed' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns[0].tools[0].status, 'completed')
})

test('multiple turn_started events create separate turns', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'first' }),
    makeEvent(2, 'text_delta', { text: 'a' }),
    makeEvent(3, 'turn_started', { summary: 'second' }),
    makeEvent(4, 'text_delta', { text: 'b' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns.length, 2)
  assert.equal(turns[0].userText, 'first')
  assert.equal(turns[0].assistantText, 'a')
  assert.equal(turns[1].userText, 'second')
  assert.equal(turns[1].assistantText, 'b')
})

test('error events are captured per turn', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'oops' }),
    makeEvent(2, 'error', { message: 'something broke' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns[0].errors.length, 1)
  assert.equal(turns[0].errors[0].message, 'something broke')
})

test('status events are captured per turn', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'status', { text: 'working…' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns[0].statuses.length, 1)
  assert.equal(turns[0].statuses[0].text, 'working…')
})

test('orphan tool_call_completed (no matching start) renders as standalone tool', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'tool_call_completed', {
      tool_call_id: 'ghost',
      status: 'completed',
      result: 'late result',
    }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns[0].tools.length, 1)
  assert.equal(turns[0].tools[0].resultText, 'late result')
  assert.equal(turns[0].tools[0].status, 'completed')
})

test('approval_required renders interactive approval parts', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'approval_required', {
      tool_call_id: 'c1',
      kind: 'ask_question',
      title: 'Choose',
      questions: [{
        id: 'q1',
        prompt: 'Pick one',
        options: [{ id: 'a', label: 'A' }],
      }],
    }),
    makeEvent(3, 'approval_resolved', { tool_call_id: 'c1', decision: 'approved' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns[0].approvals.length, 1)
  assert.equal(turns[0].approvals[0].resolved, true)
  assert.equal(turns[0].parts.some(part => part.kind === 'approval'), true)
})

test('events before the first turn_started are grouped into an implicit turn', () => {
  const events = [
    makeEvent(1, 'text_delta', { text: 'stray text' }),
    makeEvent(2, 'turn_started', { summary: 'real turn' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns.length, 2)
  assert.equal(turns[0].assistantText, 'stray text')
  assert.equal(turns[1].userText, 'real turn')
})

test('stable turn ids reconcile late events into the original turn', () => {
  const events = [
    makeEvent(0, 'turn_started', { summary: 'first' }, { turn_id: 'turn-a' }),
    makeEvent(1, 'turn_started', { summary: 'second' }, { turn_id: 'turn-b' }),
    makeEvent(2, 'text_delta', { text: 'answer-a' }, { turn_id: 'turn-a', message_id: 'turn-a:assistant' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns.length, 2)
  assert.equal(turns[0].key, 'turn-turn-a')
  assert.equal(turns[0].assistantText, 'answer-a')
  assert.equal(turns[1].assistantText, '')
})

test('identical user text remains distinct when client turn ids differ', () => {
  const turns = groupEventsIntoTurns([
    makeEvent(0, 'turn_started', { summary: 'same' }, { turn_id: 'one' }),
    makeEvent(1, 'turn_completed', { status: 'completed' }, { turn_id: 'one' }),
    makeEvent(2, 'turn_started', { summary: 'same' }, { turn_id: 'two' }),
  ])
  assert.deepEqual(turns.map(turn => turn.turnId), ['one', 'two'])
  assert.deepEqual(turns.map(turn => turn.userText), ['same', 'same'])
  assert.equal(turns[0].completed, true)
  assert.equal(turns[1].completed, false)
})

test('interleaved thinking, tool, thinking, text produce ordered parts', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'search' }, { turn_id: 't' }),
    makeEvent(2, 'thinking_delta', { text: 'pre-tool thought' }, { turn_id: 't' }),
    makeEvent(3, 'tool_call_started', {
      tool_call_id: 'c1',
      name: 'WebSearch',
      args: { q: 'poem' },
    }, { turn_id: 't' }),
    makeEvent(4, 'tool_call_completed', {
      tool_call_id: 'c1',
      status: 'completed',
      result: 'found',
    }, { turn_id: 't' }),
    makeEvent(5, 'thinking_delta', { text: 'post-tool thought' }, { turn_id: 't' }),
    makeEvent(6, 'text_delta', { text: 'final answer' }, { turn_id: 't' }),
  ]
  const turns = groupEventsIntoTurns(events)
  const turn = turns[0]
  assert.ok(Array.isArray(turn.parts), 'turn must expose an ordered parts list')
  const kinds = turn.parts.map(p => p.kind)
  assert.deepEqual(kinds, ['thinking', 'tool', 'thinking', 'text'])
  assert.equal(turn.parts[0].text, 'pre-tool thought')
  assert.equal(turn.parts[1].tool.name, 'WebSearch')
  assert.equal(turn.parts[1].tool.status, 'completed')
  assert.equal(turn.parts[2].text, 'post-tool thought')
  assert.equal(turn.parts[3].text, 'final answer')
})
