import assert from 'node:assert/strict'
import test from 'node:test'
import { loadChatFeature } from './chatPresentationTestHarness.mjs'

const { formatReviewMessage, reviewAnchorIsLive, reviewAnchorRowCount, reviewCode, reviewCountLabel, reviewLocation, reviewNote, reviewQuotedCode, reviewShortLocation, REVIEW_CODE_LIMIT } =
  await loadChatFeature('review/reviewComments')

const comment = overrides => ({
  id: 'c1',
  key: 'group-1:tool-1:3',
  turn: 'turn',
  group: 'group-1',
  tool: 'tool-1',
  row: 3,
  file: 'src/app.ts',
  line: 42,
  endLine: null,
  code: 'const value = compute(input)',
  text: 'Handle the empty list here.',
  ...overrides,
})

test('a location names a file and line, a range, or just a file', () => {
  assert.equal(reviewLocation(comment()), 'src/app.ts:42')
  assert.equal(reviewLocation(comment({ endLine: 45 })), 'src/app.ts:42-45')
  assert.equal(reviewLocation(comment({ endLine: 42 })), 'src/app.ts:42', 'a one-row range is not a range')
  assert.equal(reviewLocation(comment({ line: null, endLine: null })), 'src/app.ts', 'an unnumbered edit keeps the path')
  assert.equal(reviewLocation(comment({ file: '', line: 7 })), 'diff:7', 'a nameless diff still says where')
  assert.equal(reviewLocation(comment({ file: '', line: null })), 'diff')
})

test('the review message is one numbered block for the agent', () => {
  const message = formatReviewMessage([
    comment(),
    comment({ id: 'c2', key: 'k2', file: 'src/b.py', line: 9, endLine: 11, code: 'def run(payload)', text: 'Split this helper.\nSecond thought: reuse the existing parser.' }),
    comment({ id: 'c3', key: 'k3', file: 'docs/readme.md', line: null, code: '', text: 'Mention the new flag.' }),
  ])
  assert.equal(message, [
    'Review comments (3):',
    '1. src/app.ts:42 — `const value = compute(input)` — Handle the empty list here.',
    '2. src/b.py:9-11 — `def run(payload)` — Split this helper. Second thought: reuse the existing parser.',
    '3. docs/readme.md — Mention the new flag.',
  ].join('\n'))
})

test('notes stay one line and code never steals the markup', () => {
  assert.equal(reviewNote('  a\n\n   b  '), 'a b')
  assert.equal(formatReviewMessage([comment({ code: 'x = `a` + b', text: 'Rename this.' })])
    .split('\n')[1], '1. src/app.ts:42 — "x = `a` + b" — Rename this.')
  const long = reviewCode('y'.repeat(REVIEW_CODE_LIMIT + 40))
  assert.equal(long.length, REVIEW_CODE_LIMIT)
  assert.match(long, /…$/)
  assert.equal(reviewCode(''), '')
  assert.equal(reviewQuotedCode(''), '')
  assert.equal(reviewCountLabel(1), '1 comment')
  assert.equal(reviewCountLabel(3), '3 comments')
})

test('the tray label keeps the file tail and the line numbers', () => {
  assert.equal(reviewShortLocation(comment()), 'src/app.ts:42')
  assert.equal(reviewShortLocation(comment({ file: 'backend/app/services/status.py', line: 239 })), 'services/status.py:239')
  assert.equal(reviewShortLocation(comment({ file: 'backend/app/services/status.py', line: 9, endLine: 11 })), 'services/status.py:9-11')
  assert.equal(reviewShortLocation(comment({ line: null, endLine: null })), 'src/app.ts')
  assert.equal(reviewShortLocation(comment({ file: '', line: 7 })), 'diff:7')
})

test('an empty tray composes no message at all', () => {
  assert.equal(formatReviewMessage([]), '')
})

// A turn whose tool carries a five-row diff, and the shapes a reload can leave
// behind: a renumbered part key, a dropped turn, a tool without a diff.
const patch = '*** Begin Patch\n*** Update File: src/app.ts\n@@ -1,2 +1,3 @@\n keep\n-old\n+a\n+b\n*** End Patch'
const tool = { key: 'tool-1', name: 'apply_patch', argsText: JSON.stringify({ input: patch }), resultText: '' }
const turns = [{ key: 'turn', parts: [{ kind: 'tool_group', key: 'group-1', tools: [tool] }] }]
const emptyDiffTool = { key: 'tool-1', name: 'Read', argsText: JSON.stringify({ file_path: 'src/app.ts' }), resultText: '' }
const renumbered = [{ key: 'turn', parts: [{ kind: 'tool_group', key: 'group-renumbered', tools: [tool] }] }]

test('a saved anchor stays live only while its row still exists', () => {
  // Header row, hunk row and four content rows: rows 0..5 addressable.
  assert.equal(reviewAnchorRowCount(comment(), turns), 6)
  assert.equal(reviewAnchorIsLive(comment({ row: 5 }), turns), true)
  assert.equal(reviewAnchorIsLive(comment({ row: 6 }), turns), false, 'a row past the end is gone')
  assert.equal(reviewAnchorIsLive(comment({ row: -1 }), turns), false)
})

test('a renumbered turn, part or tool makes the anchor stale', () => {
  assert.equal(reviewAnchorRowCount(comment(), renumbered), null, 'a reload renames the part')
  assert.equal(reviewAnchorIsLive(comment(), renumbered), false)
  assert.equal(reviewAnchorIsLive(comment({ turn: 'turn-gone' }), turns), false)
  assert.equal(reviewAnchorIsLive(comment({ tool: 'tool-gone' }), turns), false)
  const noDiff = [{ key: 'turn', parts: [{ kind: 'tool_group', key: 'group-1', tools: [emptyDiffTool] }] }]
  assert.equal(reviewAnchorRowCount(comment(), noDiff), 0, 'a call without a diff has no rows')
  assert.equal(reviewAnchorIsLive(comment(), noDiff), false)
})
