import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const read = name => readFileSync(
  new URL(`../src/features/chat/components/${name}.vue`, import.meta.url),
  'utf8',
)
const turnParts = read('ChatTurnParts')
const standalone = read('ChatSubagentCard')
const region = read('ChatSubagentRegion')
const subthread = read('ChatSubthread')

test('uncorrelated Agent and Task calls retain a clearly scoped fallback card', () => {
  assert.match(turnParts, /<ChatSubagentCard[\s\S]*?v-else-if="part\.kind === 'subagent'"/)
  assert.match(turnParts, /:render-revision="turn\.renderRevision"/)
  assert.match(standalone, /subagent-card--standalone/)
  assert.match(standalone, /subagentChip\(part\.tool\)/)
  assert.match(standalone, /subagentCallStatusLabel\(part\.tool\.status\)/)
  assert.doesNotMatch(standalone, /subagentStatusLabel\(part\.tool\.status\)/)
  assert.match(standalone, /renderRevision:\s*number/)
})

test('one turn-level region renders one keyed row per complete provider id', () => {
  assert.match(turnParts, /<ChatSubagentRegion[\s\S]*?v-else-if="part\.kind === 'subagents'"/)
  assert.match(region, /data-testid="subagent-region"/)
  assert.match(region, /v-for="thread in part\.threads"/)
  assert.match(region, /:key="thread\.key"/)
  assert.match(subthread, /:data-subagent-thread-id="thread\.threadId"/)
  assert.match(subthread, /\{\{ thread\.threadId \}\}/)
  assert.match(subthread, /:title="thread\.threadId"/)
  assert.doesNotMatch(subthread, /threadId\.slice/)
})

test('dispatch status and child-turn lifecycle are rendered separately', () => {
  assert.match(subthread, /subagentDispatchLabel\(thread\.launchTool\.status\)/)
  assert.match(subthread, /subthreadStatusLabel\(thread\)/)
})

test('expanding a child retains prompt, prose, thinking, tools and images', () => {
  assert.match(subthread, /subthreadInstructionText\(thread\.launchTool\)/)
  assert.match(subthread, /v-for="sub in subthreadPartsFor\(thread\)"/)
  for (const kind of ['instruction', 'thinking', 'reasoning_status', 'text', 'status', 'tool_group', 'agent_image']) {
    assert.match(subthread, new RegExp(`sub\\.kind === '${kind}'`))
  }
  assert.match(subthread, /:complete="subthreadTerminal\(thread\)"/)
})

test('native region uses theme tokens and keeps narrow viewport rules', () => {
  assert.match(region, /\.subagent-region-body \{[\s\S]*?gap: 8px/)
  assert.match(subthread, /\.subagent-thread-row \{[\s\S]*?--ch-color-border-muted/)
  assert.match(subthread, /@media \(max-width: 640px\) \{[\s\S]*?\.subagent-thread-row > summary/)
})
