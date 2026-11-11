import assert from 'node:assert/strict'
import test from 'node:test'
import { loadChatFeature } from './chatPresentationTestHarness.mjs'

const { buildHubSubagentLookup, stripHubSubagentPreamble } = await loadChatFeature(
  'presentation/hubSubagentThread',
)

function hubChip(overrides = {}) {
  return {
    kind: 'hub',
    key: 'sa-0123456789ab',
    subagentId: 'sa-0123456789ab',
    tabId: 'tab-child',
    name: 'codex subagent: analyze parser',
    shortId: 'sa-…89ab',
    agentType: 'codex',
    state: 'completed',
    stateLabel: 'Completed',
    summary: 'Fallback summary line.',
    archived: true,
    canJump: true,
    canOpen: true,
    group: 'earlier',
    createdAt: '2026-11-11T08:00:00Z',
    ...overrides,
  }
}

function event(seq, turnId, type, payload) {
  return {
    stream_sequence: seq,
    session_id: 'terminal-tab-tab-child',
    tab_id: 'tab-child',
    agent_type: 'codex',
    type,
    turn_id: turnId,
    message_id: null,
    call_id: null,
    payload,
    created_at: '2026-11-11T08:00:00Z',
    redacted: false,
  }
}

test('stripHubSubagentPreamble removes the internal preamble and keeps user prompt', () => {
  const raw = '<claude_hub_subagent>\nYou are a subagent.\n</claude_hub_subagent>\n\nAudit the parser.'
  assert.equal(stripHubSubagentPreamble(raw), 'Audit the parser.')
  assert.equal(stripHubSubagentPreamble('Plain prompt'), 'Plain prompt')
})

test('buildHubSubagentLookup projects child stream turns into a TimelineSubthread', () => {
  const events = [
    event(0, 't-1', 'turn_started', {
      summary: '<claude_hub_subagent>\nPreamble\n</claude_hub_subagent>\n\nInspect parser edge cases',
    }),
    event(1, 't-1', 'thinking_delta', { text: 'Checking AST nodes' }),
    event(2, 't-1', 'tool_call_started', {
      tool_call_id: 'call-1',
      name: 'Read',
      args: { file_path: '/repo/parser.py' },
    }),
    event(3, 't-1', 'tool_call_completed', {
      tool_call_id: 'call-1',
      name: 'Read',
      status: 'completed',
      result: 'line 1',
    }),
    event(4, 't-1', 'text_delta', { text: 'Found 2 issues in parser.py.' }),
    event(5, 't-1', 'turn_completed', { status: 'completed' }),
    event(6, 't-2', 'turn_started', { summary: 'Check line 42 as well' }),
    event(7, 't-2', 'text_delta', { text: 'Line 42 is clean.' }),
    event(8, 't-2', 'turn_completed', { status: 'completed' }),
  ]

  const lookup = buildHubSubagentLookup(hubChip(), events)
  assert.equal(lookup.ordinal, 2)
  assert.equal(lookup.thread.launchTool?.subagent?.prompt, 'Inspect parser edge cases')
  assert.equal(lookup.thread.lifecycleStatus, 'completed')
  assert.deepEqual(
    lookup.thread.parts.map(part => part.kind),
    ['thinking', 'tool_group', 'text', 'instruction', 'text'],
  )
})

test('buildHubSubagentLookup falls back to chip summary when stream events are empty', () => {
  const lookup = buildHubSubagentLookup(hubChip(), [])
  assert.equal(lookup.ordinal, 1)
  assert.equal(lookup.thread.launchTool?.subagent?.prompt, 'analyze parser')
  assert.equal(lookup.thread.parts.length, 1)
  assert.equal(lookup.thread.parts[0].kind, 'text')
  assert.equal(lookup.thread.parts[0].text, 'Fallback summary line.')
})
