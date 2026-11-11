import assert from 'node:assert/strict'
import test from 'node:test'
import { loadChatFeature } from './chatPresentationTestHarness.mjs'

const {
  formatReviewMessage, reviewAnchorIsLive, reviewAnchorRowCount, reviewCode, reviewCommentInRows,
  reviewCountLabel, reviewLocation, reviewNote, reviewQuotedCode, reviewRangeLines, reviewRangeRows,
  reviewRowLine, reviewRowTapOpensEditor, reviewShortLocation, REVIEW_CODE_LIMIT,
} = await loadChatFeature('review/reviewComments')

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

// A two-hunk diff where removals outnumber additions, so the original side runs
// ahead of the new side: row 9 is original 11, row 11 is new 9.
const line = overrides => ({ sign: ' ', text: 'x', file: 'x.ts', ...overrides })
const offsetDiff = [
  line({ sign: '@', text: 'x.ts', fileHeader: true }),
  line({ sign: '@', text: '@@ -1,5 +1,2 @@' }),
  line({ sign: '-', text: 'a', oldLine: 1 }),
  line({ sign: '-', text: 'b', oldLine: 2 }),
  line({ sign: '-', text: 'c', oldLine: 3 }),
  line({ sign: '+', text: 'd', newLine: 1 }),
  line({ sign: '+', text: 'e', newLine: 2 }),
  line({ sign: '@', text: '@@ -10,2 +6,3 @@' }),
  line({ text: 'f', oldLine: 10, newLine: 7 }),
  line({ sign: '-', text: 'g', oldLine: 11 }),
  line({ sign: '+', text: 'h', newLine: 8 }),
  line({ sign: '+', text: 'i', newLine: 9 }),
]

test('a range names its span low to high even when the sides are offset', () => {
  // The endpoints are a removal (original 11) and an addition (new 9).
  const inverted = reviewRangeLines(offsetDiff, [9, 10, 11])
  assert.deepEqual(inverted, { line: 8, endLine: 11 })
  assert.ok(inverted.line <= inverted.endLine, 'the span never reads backwards')

  // Mixed sides in one hunk still report the extremes it touches.
  assert.deepEqual(reviewRangeLines(offsetDiff, [8, 9]), { line: 7, endLine: 11 })
  assert.deepEqual(reviewRangeLines(offsetDiff, [3]), { line: 2, endLine: null }, 'one row is not a range')
  assert.deepEqual(reviewRangeLines([{ sign: '-', text: 'a' }], [0]), { line: null, endLine: null },
    'an unnumbered edit states no span')

  // The new side is preferred, the original side is the fallback.
  assert.equal(reviewRowLine({ sign: '+', text: 'x', oldLine: 3, newLine: 9 }), 9)
  assert.equal(reviewRowLine({ sign: '-', text: 'x', oldLine: 3 }), 3)
  assert.equal(reviewRowLine({ sign: '@', text: '@@ -1 +1 @@' }), null)
  assert.equal(reviewRowLine(undefined), null)
})

test('a range keeps one file and skips hunk rows in either direction', () => {
  assert.deepEqual(reviewRangeRows(offsetDiff, 9, 11), [9, 10, 11])
  assert.deepEqual(reviewRangeRows(offsetDiff, 11, 9), [9, 10, 11], 'shift-clicking upward selects the same rows')
  assert.deepEqual(reviewRangeRows(offsetDiff, 3, 9), [3, 4, 5, 6, 8, 9], 'hunk and file rows drop out')
  const twoFiles = [...offsetDiff, line({ sign: '@', text: 'y.ts', file: 'y.ts', fileHeader: true }), line({ text: 'z', file: 'y.ts', oldLine: 1 })]
  assert.deepEqual(reviewRangeRows(twoFiles, 3, 13), [13],
    "a range never spans two files: only the clicked row's file counts")
})

test('a note anywhere inside the range is the note being edited', () => {
  const keys = ['g:t:6', 'g:t:7', 'g:t:8']
  assert.equal(reviewCommentInRows([comment({ key: 'g:t:7' })], keys).key, 'g:t:7')
  assert.equal(reviewCommentInRows([comment({ key: 'g:t:6' }), comment({ id: 'c2', key: 'g:t:8' })], keys).key, 'g:t:6',
    'the first note in reading order wins')
  assert.equal(reviewCommentInRows([comment({ key: 'g:t:99' })], keys), null)
  assert.equal(reviewCommentInRows([], keys), null)
})

test('a touch tap opens the editor only away from nested controls', () => {
  const button = { closest: () => ({ tagName: 'BUTTON' }) }
  const glyph = { closest: () => button }
  const cell = { closest: () => null }
  assert.equal(reviewRowTapOpensEditor(true, glyph), false, 'the gutter button handles its own taps')
  assert.equal(reviewRowTapOpensEditor(true, button), false)
  assert.equal(reviewRowTapOpensEditor(true, cell), true)
  assert.equal(reviewRowTapOpensEditor(false, cell), false, 'a desktop click selects text, it does not comment')
  assert.equal(reviewRowTapOpensEditor(true, null), true, 'a target without closest() is a bare row tap')
})

test('the gutter button stops its click from also reaching the row handler', async () => {
  const toolGroup = await (await import('node:fs/promises')).readFile(
    new URL('../src/features/chat/components/ChatToolGroup.vue', import.meta.url), 'utf8')
  assert.match(toolGroup, /@click\.stop="startComment\(tool, activity\.diff, index, \$event\)"/)
  assert.match(toolGroup, /reviewRowTapOpensEditor\(coarsePointer\.value, event\.target as Element \| null\)/)
})
