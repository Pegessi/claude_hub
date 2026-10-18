import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const viewSource = await readFile(
  new URL('../src/features/workspace/components/WorkspaceBoard.vue', import.meta.url),
  'utf8',
)

test('agent tag badge renders only when task.agent_tag is set', () => {
  assert.match(viewSource, /v-if="task\.agent_tag"/)
  assert.match(viewSource, /class="agent-tag-badge"/)
})

const styleBlock = await readFile(
  new URL('../src/features/workspace/styles/workspace-board-task-cards.css', import.meta.url),
  'utf8',
)

test('agent tag badge styling stays restrained', () => {
  assert.match(styleBlock, /\.agent-tag-badge\s*\{/)
  assert.match(styleBlock, /font-size:\s*10px/)
  assert.match(styleBlock, /text-overflow:\s*ellipsis/)
  assert.doesNotMatch(styleBlock, /\.agent-tag-badge[^{]*\{[^}]*animation/i)
})

test('untagged tasks do not reserve an empty agent tag badge', () => {
  assert.doesNotMatch(viewSource, /v-if="task\.agent_tag \|\| true"/)
  assert.doesNotMatch(viewSource, /class="agent-tag-badge"[^>]*>\s*\{\{\s*\}\}/)
})
