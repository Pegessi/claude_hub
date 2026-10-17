import assert from 'node:assert/strict'
import test from 'node:test'
import { loadChatUtility } from './chatPresentationTestHarness.mjs'

const {
  subagentChip, subagentName, subagentHeadline, subagentPrompt,
  subagentCallStatusLabel, subagentDispatchLabel,
  subagentRegionHeadline, subagentRegionStatusLabel, subagentRegionStatusClass,
  subthreadStatusClass, subthreadStatusLabel, subthreadTerminal,
  subthreadHeadline, subthreadInstructionText,
} = await loadChatUtility('chatSubagentPresentation')

const tool = (view = {}, extra = {}) => ({
  key: 'tool', status: 'completed', argsText: 'raw arguments',
  subagent: { provider: 'claude', agentType: null, description: '', prompt: '', threadIds: [], ...view },
  ...extra,
})
const thread = (lifecycleStatus = 'unknown', extra = {}) => ({
  key: 'thread', threadId: 'complete-provider-thread-id', parts: [], launchTool: null, lifecycleStatus, ...extra,
})
const region = (threads) => ({ kind: 'subagents', key: 'region', threads })

test('standalone card handles absent metadata and keeps provider-specific identity fallbacks', () => {
  const absent = tool({}, { subagent: undefined })
  assert.equal(subagentChip(absent), 'Sub-agent')
  assert.equal(subagentName(absent), 'Sub-agent')
  assert.equal(subagentHeadline(absent), '')
  assert.equal(subagentPrompt(absent), '')
  assert.equal(subagentName(tool({ agentType: 'Explorer' })), 'Explorer')
  assert.equal(subagentName(tool({ provider: 'traex', threadIds: ['1234567890abcdef'] })), 'Thread 12345678')
  assert.equal(subagentName(tool({ provider: 'traex' })), 'TraeX sub-agent')
  assert.equal(subagentName(tool({ provider: 'cursor' })), 'Sub-agent')
})

test('standalone headline prioritizes description then first nonempty prompt line with exact truncation', () => {
  assert.equal(subagentHeadline(tool({ description: '  description  ', prompt: 'prompt' })), 'description')
  assert.equal(subagentHeadline(tool({ prompt: '\n  first line  \n second' })), 'first line')
  assert.equal(subagentHeadline(tool({ prompt: 'x'.repeat(140) })), 'x'.repeat(140))
  assert.equal(subagentHeadline(tool({ prompt: 'x'.repeat(141) })), `${'x'.repeat(140)}…`)
  assert.equal(subagentHeadline(tool()), 'Sub-agent task dispatched')
  assert.equal(subagentPrompt(tool({ prompt: '\noriginal\n' })), '\noriginal\n')
})

test('dispatch and standalone result labels stay distinct across all tool statuses', () => {
  for (const [status, call, dispatch] of [
    ['running', 'Running', 'Dispatching'],
    ['completed', 'Returned', 'Dispatched'],
    ['failed', 'Failed', 'Dispatch failed'],
    ['cancelled', 'Cancelled', 'Dispatch cancelled'],
  ]) {
    assert.equal(subagentCallStatusLabel(status), call)
    assert.equal(subagentDispatchLabel(status), dispatch)
  }
})

test('region summary orders lifecycle counts and preserves singular and plural forms', () => {
  assert.equal(subagentRegionHeadline(region([])), '0 sub-agents')
  assert.equal(subagentRegionHeadline(region([thread()])), '1 sub-agent')
  const part = region(['completed', 'unknown', 'active', 'failed', 'cancelled', 'active'].map(status => thread(status)))
  assert.equal(subagentRegionHeadline(part), '6 sub-agents')
  assert.equal(subagentRegionStatusLabel(part), '1 failed · 1 cancelled · 2 active · 1 unknown · 1 completed')
  assert.equal(subagentRegionStatusClass(part), 'failed')
  assert.equal(subagentRegionStatusClass(region([thread('active'), thread('cancelled')])), 'running')
  assert.equal(subagentRegionStatusClass(region([thread('cancelled'), thread()])), 'cancelled')
  assert.equal(subagentRegionStatusClass(region([thread(), thread('completed')])), 'unknown')
  assert.equal(subagentRegionStatusClass(region([thread('completed')])), 'completed')
})

test('dispatch and child errors affect region display without inventing child lifecycle evidence', () => {
  const launch = tool({}, { status: 'failed' })
  const child = thread('unknown', { launchTool: launch })
  const sibling = thread('active', { key: 'sibling', threadId: 'sibling', launchTool: launch })
  const part = region([child, sibling])
  assert.equal(subagentRegionStatusLabel(part), '1 error · 1 active · 1 unknown', 'shared launch errors count once')
  assert.equal(subagentRegionStatusClass(part), 'failed')
  child.parts.push({ kind: 'status', key: 'error', text: 'connection lost', isError: true })
  assert.equal(subagentRegionStatusLabel(part), '2 errors · 1 active · 1 unknown')
  assert.equal(subthreadStatusClass(child), 'unknown')
  assert.equal(subthreadStatusLabel(child), 'Unknown')
  assert.equal(subthreadTerminal(child), false)
})

test('child lifecycle alone controls class, label and terminal display', () => {
  for (const [status, css, label, terminal] of [
    ['unknown', 'unknown', 'Unknown', false],
    ['active', 'running', 'Active', false],
    ['completed', 'completed', 'Completed', true],
    ['failed', 'failed', 'Failed', true],
    ['cancelled', 'cancelled', 'Cancelled', true],
  ]) {
    const child = thread(status, { launchTool: tool({}, { status: 'running' }) })
    assert.equal(subthreadStatusClass(child), css)
    assert.equal(subthreadStatusLabel(child), label)
    assert.equal(subthreadTerminal(child), terminal)
  }
})

test('child headline uses latest report or instruction without modifying nested parts or identities', () => {
  const child = thread('active', { launchTool: tool({ prompt: 'original prompt' }), parts: [
    { kind: 'text', key: 'report', text: 'earlier report' },
    { kind: 'instruction', key: 'instruction', tool: tool({ prompt: '\n latest instruction\nnext' }) },
    { kind: 'thinking', key: 'thinking', text: 'not a headline' },
  ] })
  const before = structuredClone(child)
  assert.equal(subthreadHeadline(child), 'latest instruction')
  assert.deepEqual(child, before)
  child.parts.push({ kind: 'text', key: 'latest', text: '\n' + 'x'.repeat(141) })
  assert.equal(subthreadHeadline(child), `${'x'.repeat(140)}…`)
  child.parts = []
  assert.equal(subthreadHeadline(child), 'original prompt')
  assert.equal(subthreadHeadline(thread()), 'Waiting for sub-agent activity')
  assert.equal(subthreadInstructionText(tool({ prompt: '' })), '')
  assert.equal(subthreadInstructionText(tool({}, { subagent: undefined })), 'raw arguments')
})
