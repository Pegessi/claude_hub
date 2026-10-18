import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const read = name => readFileSync(
  new URL(`../src/features/chat/components/${name}.vue`, import.meta.url),
  'utf8',
)
const turnParts = read('ChatTurnParts')
const thinkingCard = read('ChatThinkingCard')

test('thinking parts route to the plain-text thinking leaf', () => {
  assert.match(turnParts, /v-if="part\.kind === 'thinking'"/)
  assert.match(turnParts, /<ChatThinkingCard[\s\S]*?:part="part"/)
  assert.match(thinkingCard, /<pre[^>]*class="thinking-body"[^>]*>\s*\{\{\s*part\.text\s*\}\}\s*<\/pre>/)
})

test('thinking leaf does not import or use MarkdownContent', () => {
  const implementation = thinkingCard.replace(/<!--[\s\S]*?-->/g, '')
  assert.doesNotMatch(implementation, /<MarkdownContent|markdown-content/)
  assert.doesNotMatch(implementation, /\bmarked\b|\bDOMPurify\b|\bsanitize\b/)
})

test('assistant text still uses MarkdownContent as a distinct leaf path', () => {
  const textStart = turnParts.indexOf(`v-else-if="part.kind === 'text'"`)
  assert.ok(textStart !== -1, 'text v-else-if block must exist')
  assert.match(turnParts.slice(textStart, textStart + 700), /<MarkdownContent/)
})

test('thinking leaf uses pre-wrap to preserve whitespace without markdown', () => {
  assert.match(thinkingCard, /<pre[^>]*class="thinking-body"/)
  assert.match(thinkingCard, /white-space:\s*pre-wrap/)
})
