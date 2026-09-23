import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const structuredPane = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)

test('each turn renders one whole-turn copy control beside the fork action', () => {
  const turnActions = structuredPane.match(
    /<div class="turn-actions turn-actions--turn">[\s\S]*?<\/div>\s*<\/div>/,
  )
  assert.ok(turnActions, 'the per-turn action row must exist')
  const row = turnActions[0]
  assert.match(row, /class="turn-fork-button turn-copy-button"/,
    'copy control sits in the same row and pill style as Fork from here')
  assert.match(row, /@click="copyTurn\(turn\)"/,
    'the control copies the whole turn, not a single bubble')
  assert.match(row, /Fork from here/, 'the existing fork control remains in the row')

  // One button per turn, not one per assistant bubble: the copy action must
  // not be placed inside the v-for over parts.
  const partsLoop = structuredPane.match(
    /v-for="part in turnPartsFor\(turn\)"[\s\S]*?<\/template>/,
  )
  assert.ok(partsLoop, 'parts loop must be locatable')
  assert.doesNotMatch(partsLoop[0], /copyTurn|message-copy-button/,
    'no per-part/per-bubble copy button')
})

test('copy is hidden until the turn has copyable text', () => {
  assert.match(
    structuredPane,
    /v-if="turnCopyText\(turn\)"[\s\S]{0,300}?turn-copy-button/,
  )
})

test('copy action assembles text via the pure helper and writes via the clipboard helper', () => {
  assert.match(structuredPane, /import \{ buildTurnCopyText \} from '@\/utils\/chatTurnCopy'/)
  assert.match(structuredPane, /import \{ writeClipboard \} from '@\/utils\/clipboard'/)
  assert.match(structuredPane, /await writeClipboard\(buildTurnCopyText\(turn\)\)/)
})

test('copy exposes transient copied and visible error feedback with aria-live', () => {
  assert.match(structuredPane, /state: 'copied'/)
  assert.match(structuredPane, /state: 'error'/)
  assert.match(structuredPane, /Copy failed/)
  assert.match(structuredPane, /aria-live="polite"/)
  assert.match(structuredPane, /turn-copy-result--error/, 'error feedback gets a danger-styled class')
})

test('copy control is hover-revealed but stays available on touch devices', () => {
  // The action row inherits the existing turn hover reveal.
  assert.match(structuredPane, /\.structured-turn:hover \.turn-actions/)
  // Touch forces the row (copy + fork) visible.
  assert.match(structuredPane, /@media \(hover: none\), \(pointer: coarse\)/)
  const touchBlock = structuredPane.match(
    /@media \(hover: none\), \(pointer: coarse\) \{[\s\S]*?\n\}/,
  )
  assert.ok(touchBlock)
  assert.match(touchBlock[0], /\.structured-turn \.turn-actions \{[\s\S]*?opacity: 1/)
})

test('copy feedback re-renders under v-memo and is cleared on teardown', () => {
  assert.match(structuredPane, /copyFeedback\?\.turnKey === turn\.key \? copyFeedback\.state : null/)
  const clears = structuredPane.match(/clearCopyFeedback\(\)/g) ?? []
  assert.ok(clears.length >= 3, 'feedback is cleared in both lifecycle hooks plus before re-copy')
})
