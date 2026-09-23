import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)

// The v-if/v-else-if kind markers only ever appear in the template, so they
// locate the render branches in the full SFC source directly (the template
// also nests <template v-if> tags, so slicing on the first close tag is
// unreliable).
const subStart = source.indexOf(`v-else-if="part.kind === 'subagent'"`)
const approvalStart = source.indexOf(`v-else-if="part.kind === 'approval'"`)
assert.ok(subStart > -1, 'pane must render a dedicated sub-agent part branch')
assert.ok(approvalStart > subStart, 'approval branch must follow the sub-agent branch')
const subBlock = source.slice(subStart, approvalStart)

test('sub-agent calls render a dedicated card, not the generic tool block', () => {
  assert.match(subBlock, /class="subagent-card"/, 'must render a .subagent-card')
  assert.match(
    subBlock,
    /class="conversation-avatar conversation-avatar--subagent"/,
    'card must carry its own sub-agent identity avatar',
  )
})

test('sub-agent card projects identity, description and lifecycle status', () => {
  assert.match(subBlock, /subagentChip\(part\.tool\)/, 'must show the provider identity chip')
  assert.match(subBlock, /subagentName\(part\.tool\)/, 'must show the sub-agent type/name')
  assert.match(subBlock, /subagentHeadline\(part\.tool\)/, 'must show the description headline')
  assert.match(
    subBlock,
    /subagentStatusLabel\(part\.tool\.status\)/,
    'must show a localized running/done/failed status',
  )
  // Status badge keeps the shared tool-status color modifiers.
  assert.match(subBlock, /class="tool-status"/)
  assert.match(subBlock, /:class="part\.tool\.status"/)
})

test('sub-agent card expands to the delegated prompt and result, not raw args JSON', () => {
  assert.match(subBlock, /subagentPrompt\(part\.tool\)/, 'expanded body shows the prompt')
  assert.match(subBlock, /part\.tool\.resultText/, 'expanded body shows the result')
  // The internal JSON (receiverThreadIds, raw args object) must never be dumped
  // into the card; only the curated prompt/result projections are rendered.
  assert.doesNotMatch(subBlock, /argsText/, 'card must not render the raw args blob')
  assert.doesNotMatch(subBlock, /receiverThreadIds/, 'card must not leak provider-internal fields')
})

test('ordinary tools still render through the grouped tool-card', () => {
  const toolGroupIdx = source.indexOf(`v-else-if="part.kind === 'tool_group'"`)
  assert.ok(toolGroupIdx > -1, 'ordinary tool_group branch must remain')
  const toolGroupBlock = source.slice(toolGroupIdx, subStart)
  assert.match(toolGroupBlock, /class="tool-card tool-card--group"/)
  assert.match(toolGroupBlock, /v-for="tool in part\.tools"/)
})

test('script wires the shared sub-agent classifier util and projections', () => {
  assert.match(
    source,
    /from '@\/utils\/subagentTool'/,
    'must import labels from the central classifier util',
  )
  for (const fn of ['subagentChip', 'subagentName', 'subagentHeadline', 'subagentPrompt']) {
    assert.match(source, new RegExp(`function ${fn}\\(`), `must define projection helper ${fn}`)
  }
})

test('sub-agent card reuses existing theme tokens rather than introducing colors', () => {
  assert.match(source, /\.subagent-card \{[\s\S]*?--ch-color-surface/)
  assert.match(source, /\.subagent-card \{[\s\S]*?--ch-radius-md/)
  assert.match(source, /\.subagent-card \{[\s\S]*?border-left: 2px solid var\(--ch-color-accent\)/)
})
