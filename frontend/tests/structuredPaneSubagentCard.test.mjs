import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)

const standaloneStart = source.indexOf(`v-else-if="part.kind === 'subagent'"`)
const regionStart = source.indexOf(`v-else-if="part.kind === 'subagents'"`)
const approvalStart = source.indexOf(`v-else-if="part.kind === 'approval'"`, regionStart)
assert.ok(standaloneStart > -1, 'pane keeps the uncorrelated sub-agent fallback')
assert.ok(regionStart > standaloneStart, 'native region follows the standalone fallback')
assert.ok(approvalStart > regionStart, 'approval branch follows the complete native region')
const standaloneBlock = source.slice(standaloneStart, regionStart)
const regionBlock = source.slice(regionStart, approvalStart)

test('uncorrelated Agent and Task calls retain a clearly scoped fallback card', () => {
  assert.match(standaloneBlock, /subagent-card--standalone/)
  assert.match(standaloneBlock, /subagentChip\(part\.tool\)/)
  assert.match(standaloneBlock, /subagentCallStatusLabel\(part\.tool\.status\)/)
  assert.doesNotMatch(standaloneBlock, /subagentStatusLabel\(part\.tool\.status\)/)
  assert.match(standaloneBlock, /子代理调用处理中…/)
})

test('one turn-level region renders one keyed row per complete provider id', () => {
  assert.match(regionBlock, /data-testid="subagent-region"/)
  assert.match(regionBlock, /v-for="thread in part\.threads"/)
  assert.match(regionBlock, /:key="thread\.key"/)
  assert.match(regionBlock, /:data-subagent-thread-id="thread\.threadId"/)
  assert.match(regionBlock, /\{\{ thread\.threadId \}\}/)
  assert.match(regionBlock, /:title="thread\.threadId"/)
  assert.doesNotMatch(regionBlock, /threadId\.slice/)
})

test('dispatch status and child-turn lifecycle are rendered separately', () => {
  assert.match(regionBlock, /subagentDispatchLabel\(thread\.launchTool\.status\)/)
  assert.match(regionBlock, /subthreadStatusLabel\(thread\)/)
  assert.match(source, /completed: '本轮完成'/)
  assert.match(source, /failed: '本轮失败'/)
  assert.match(source, /unknown: '状态未知'/)
  assert.match(source, /completed: '已派发'/)
})

test('expanding a child retains prompt, prose, thinking, tools and images', () => {
  assert.match(regionBlock, /subthreadInstructionText\(thread\.launchTool\)/)
  assert.match(regionBlock, /v-for="sub in thread\.parts"/)
  for (const kind of ['instruction', 'thinking', 'text', 'status', 'tool_group', 'agent_image']) {
    assert.match(regionBlock, new RegExp(`sub\\.kind === '${kind}'`))
  }
  assert.match(regionBlock, /:complete="subthreadTerminal\(thread\)"/)
})

test('native region uses theme tokens and keeps narrow viewport rules', () => {
  assert.match(source, /\.subagent-region-body \{[\s\S]*?gap: 8px/)
  assert.match(source, /\.subagent-thread-row \{[\s\S]*?--ch-color-border-muted/)
  assert.match(source, /@media \(max-width: 640px\) \{[\s\S]*?\.subagent-thread-row > summary/)
})
