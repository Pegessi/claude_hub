import assert from 'node:assert/strict'
import test from 'node:test'

import { loadChatUtility } from './chatPresentationTestHarness.mjs'

const mod = await loadChatUtility('agentStreamTimeline')
const { groupEventsIntoTurns, countProcessSteps } = mod

function makeEvent(seq, type, payload = {}, overrides = {}) {
  return {
    stream_sequence: seq,
    session_id: 's1',
    tab_id: 't1',
    agent_type: 'codex',
    type,
    payload,
    created_at: '2026-01-01T00:00:00Z',
    redacted: false,
    ...overrides,
  }
}

const IMG = '/Users/a/work/tasks/run/gpu-utilization.png'

function imageTurnEvents(toolName, args, agentType = 'codex') {
  return [
    makeEvent(1, 'turn_started', { summary: 'show the chart' }),
    makeEvent(
      2,
      'tool_call_started',
      { tool_call_id: 'c1', name: toolName, args },
      { agent_type: agentType },
    ),
    makeEvent(3, 'tool_call_completed', {
      tool_call_id: 'c1',
      status: 'completed',
      result: '',
    }),
  ]
}

test('view_image is split into an agent_image part carrying the path', () => {
  const turns = groupEventsIntoTurns(imageTurnEvents('view_image', { path: IMG }))
  const part = turns[0].parts.find((p) => p.kind === 'agent_image')
  assert.ok(part, 'expected an agent_image part')
  assert.equal(part.path, IMG)
  assert.equal(part.tool.status, 'completed')
  // It must not also land in a generic tool group.
  assert.ok(!turns[0].parts.some((p) => p.kind === 'tool_group'))
})

test('agent_image path is directly usable to build the scoped URL', () => {
  const turns = groupEventsIntoTurns(imageTurnEvents('view_image', { path: IMG }))
  const part = turns[0].parts.find((p) => p.kind === 'agent_image')
  const url = `/api/workspaces/tabs/${encodeURIComponent('t1')}/stream/agent-image?path=${encodeURIComponent(part.path)}`
  assert.ok(url.startsWith('/api/workspaces/tabs/t1/stream/agent-image?path='))
  assert.ok(!url.includes('/Users'), 'raw path must be percent-encoded')
})

test('Codex view_image (codex agent_type) is recognized', () => {
  const turns = groupEventsIntoTurns(imageTurnEvents('view_image', { path: IMG }, 'codex'))
  assert.equal(turns[0].parts.some((p) => p.kind === 'agent_image'), true)
})

test('Claude Read of an image is recognized; Read of code stays a tool group', () => {
  const image = groupEventsIntoTurns(
    imageTurnEvents('Read', { file_path: '/work/a.png' }, 'claude'),
  )
  assert.equal(image[0].parts.some((p) => p.kind === 'agent_image'), true)

  const code = groupEventsIntoTurns(
    imageTurnEvents('Read', { file_path: '/work/main.ts' }, 'claude'),
  )
  assert.equal(code[0].parts.some((p) => p.kind === 'agent_image'), false)
  assert.equal(code[0].parts.some((p) => p.kind === 'tool_group'), true)
})

test('an ordinary tool is unaffected (generic tool group, no image part)', () => {
  const turns = groupEventsIntoTurns(imageTurnEvents('Bash', { command: 'ls' }))
  assert.equal(turns[0].parts.some((p) => p.kind === 'agent_image'), false)
  assert.equal(turns[0].parts.some((p) => p.kind === 'tool_group'), true)
})

test('agent image counts as one process step', () => {
  const turns = groupEventsIntoTurns(imageTurnEvents('view_image', { path: IMG }))
  assert.equal(countProcessSteps(turns[0].parts), 1)
})
