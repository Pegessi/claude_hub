import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const viewSource = await readFile(
  new URL('../src/components/AgentWorkspaceView.vue', import.meta.url),
  'utf8',
)

test('agent tag badge renders only when task.agent_tag is set', () => {
  assert.match(viewSource, /v-if="task\.agent_tag"/)
  assert.match(viewSource, /class="agent-tag-badge"/)
})

test('agent tag badge styling stays restrained', () => {
  const styleBlock = viewSource.slice(viewSource.lastIndexOf('<style'))
  assert.match(styleBlock, /\.agent-tag-badge\s*\{/)
  assert.match(styleBlock, /font-size:\s*10px/)
  assert.match(styleBlock, /text-overflow:\s*ellipsis/)
  assert.doesNotMatch(styleBlock, /\.agent-tag-badge[^{]*\{[^}]*animation/i)
})

test('untagged tasks do not reserve an empty agent tag badge', () => {
  assert.doesNotMatch(viewSource, /v-if="task\.agent_tag \|\| true"/)
  assert.doesNotMatch(viewSource, /class="agent-tag-badge"[^>]*>\s*\{\{\s*\}\}/)
})
