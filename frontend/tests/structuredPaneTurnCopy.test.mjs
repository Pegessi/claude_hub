import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const structuredPane = readFileSync(
  new URL('../src/features/chat/components/ChatTurn.vue', import.meta.url),
  'utf8',
)
const actionsController = readFileSync(
  new URL('../src/features/chat/controllers/useChatActions.ts', import.meta.url),
  'utf8',
)
const timeline = readFileSync(
  new URL('../src/features/chat/components/ChatTimeline.vue', import.meta.url),
  'utf8',
)
const pane = readFileSync(
  new URL('../src/features/chat/StructuredPane.vue', import.meta.url),
  'utf8',
)

test('each turn renders one whole-turn copy control beside the fork action', () => {
  const actionStart = structuredPane.indexOf('class="turn-actions turn-actions--turn"')
  const actionEnd = structuredPane.indexOf('</div>', actionStart)
  assert.ok(actionStart >= 0 && actionEnd > actionStart, 'the per-turn action row must exist')
  const row = structuredPane.slice(actionStart, actionEnd)
  assert.match(row, /class="turn-fork-button turn-copy-button"/)
  assert.match(row, /@click="copyTurn\(turn\)"/)
  assert.match(structuredPane.slice(actionStart, actionStart + 2500), /Fork from here/)
  assert.match(structuredPane, /<ChatTurnParts/)
  assert.doesNotMatch(
    structuredPane.match(/<ChatTurnParts[\s\S]*?\/>/)?.[0] ?? '',
    /copyTurn|message-copy-button/,
  )
})

test('copy is hidden until the turn has copyable text', () => {
  assert.match(
    structuredPane,
    /v-if="turnCopyText\(turn\)"[\s\S]{0,300}?turn-copy-button/,
  )
})

test('copy action assembles text via the pure helper and writes via the clipboard helper', () => {
  assert.match(actionsController, /import \{ buildTurnCopyText \} from '@\/utils\/chatTurnCopy'/)
  assert.match(actionsController, /import \{ writeClipboard \} from '@\/utils\/clipboard'/)
  assert.match(actionsController, /await writeClipboard\(buildTurnCopyText\(turn\)\)/)
})

test('copy exposes transient copied and visible error feedback with aria-live', () => {
  assert.match(actionsController, /state: 'copied'/)
  assert.match(actionsController, /state: 'error'/)
  assert.match(actionsController, /Copy failed/)
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

test('copy feedback re-renders under v-memo and is cleared on lifecycle teardown', () => {
  assert.match(timeline, /copyFeedback\?\.turnKey === turn\.key \? copyFeedback\.state : null/)
  const lifecycleClears = pane.match(/actions\.clearCopyFeedback\(\)/g) ?? []
  assert.ok(lifecycleClears.length >= 2, 'feedback is cleared on deactivate and unmount')
  assert.match(actionsController, /function clearCopyFeedback\(\)[\s\S]*?window\.clearTimeout\(copyFeedbackTimer\)/)
})
