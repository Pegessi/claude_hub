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
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
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

test('approval_required and approval_resolved are ignored for now', () => {
  const events = [
    makeEvent(1, 'turn_started', { summary: 'go' }),
    makeEvent(2, 'approval_required', { tool_call_id: 'c1' }),
    makeEvent(3, 'approval_resolved', { tool_call_id: 'c1', decision: 'approved' }),
  ]
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns[0].tools.length, 0)
  assert.equal(turns[0].errors.length, 0)
  assert.equal(turns[0].statuses.length, 0)
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
