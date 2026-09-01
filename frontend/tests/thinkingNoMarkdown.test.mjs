import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const structuredPane = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)

// Extract the thinking <details> block from the template. The thinking part is
// rendered inside a <details v-if="part.kind === 'thinking'"> element. We
// isolate that block to assert it never routes through the Markdown renderer.
function extractThinkingBlock(source) {
  const start = source.indexOf(`v-if="part.kind === 'thinking'"`)
  assert.ok(start !== -1, 'thinking v-if block must exist')

  // Find the matching closing </details> for the thinking card. The thinking
  // block ends before the next v-else-if (text part).
  const nextElseIf = source.indexOf('v-else-if', start)
  assert.ok(nextElseIf !== -1, 'text part must follow thinking part')

  return source.slice(start, nextElseIf)
}

// Strip HTML comments so explanatory comments like "no marked/DOMPurify"
// don't trigger false positives in the renderer-usage checks.
function stripComments(source) {
  return source.replace(/<!--[\s\S]*?-->/g, '')
}

const thinkingBlock = stripComments(extractThinkingBlock(structuredPane))

test('thinking part renders as plain <pre> text, not MarkdownContent', () => {
  // Thinking content is interpolated directly into a <pre> element. This
  // guarantees zero marked.parse / DOMPurify.sanitize calls for thinking
  // deltas, which is the core performance fix for long Thinking bursts.
  assert.match(
    thinkingBlock,
    /<pre[^>]*class="thinking-body"[^>]*>\s*\{\{\s*part\.text\s*\}\}\s*<\/pre>/,
    'thinking must use <pre>{{ part.text }}</pre> (plain text interpolation)',
  )
})

test('thinking block does not import or use MarkdownContent', () => {
  assert.doesNotMatch(
    thinkingBlock,
    /<MarkdownContent/,
    'thinking block must not contain <MarkdownContent>',
  )
  assert.doesNotMatch(
    thinkingBlock,
    /markdown-content/,
    'thinking block must not reference markdown-content class',
  )
})

test('thinking block does not reference marked or DOMPurify', () => {
  assert.doesNotMatch(
    thinkingBlock,
    /\bmarked\b/,
    'thinking block must not reference marked',
  )
  assert.doesNotMatch(
    thinkingBlock,
    /\bDOMPurify\b/,
    'thinking block must not reference DOMPurify',
  )
  assert.doesNotMatch(
    thinkingBlock,
    /\bsanitize\b/,
    'thinking block must not reference sanitize',
  )
})

test('text part DOES use MarkdownContent (contrast check)', () => {
  // Sanity check: the assistant text part still goes through MarkdownContent.
  // This confirms the test is correctly distinguishing thinking from text.
  const textBlockStart = structuredPane.indexOf(`v-else-if="part.kind === 'text'"`)
  assert.ok(textBlockStart !== -1, 'text v-else-if block must exist')
  const textBlock = structuredPane.slice(textBlockStart, textBlockStart + 600)
  assert.match(
    textBlock,
    /<MarkdownContent/,
    'text part must use <MarkdownContent>',
  )
})

test('thinking block uses pre-wrap to preserve whitespace without markdown', () => {
  // The <pre> element with white-space: pre-wrap preserves newlines and
  // spaces in thinking content without needing a markdown parser.
  assert.match(
    thinkingBlock,
    /<pre[^>]*class="thinking-body"/,
    'thinking must use a <pre> element for whitespace preservation',
  )
})
