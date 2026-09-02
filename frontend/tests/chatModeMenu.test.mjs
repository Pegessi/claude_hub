import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const structuredPane = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)

test('mode picker lives beside the attachment control and opens an upward menu', () => {
  const composerRow = structuredPane.match(/<div class="composer-row">[\s\S]*?<textarea/)
  assert.ok(composerRow, 'composer row must contain its left-side tools before the textarea')
  assert.match(composerRow[0], /class="composer-tools"/)
  assert.match(composerRow[0], /class="composer-attach-btn"[\s\S]*?class="composer-mode-trigger"/)
  assert.match(composerRow[0], /aria-haspopup="menu"/)
  assert.match(composerRow[0], /:aria-expanded="isModeMenuOpen"/)
  assert.match(composerRow[0], /\{\{ currentModeLabel \}\}/)
  assert.match(composerRow[0], /class="composer-mode-chevron"/)
  assert.doesNotMatch(structuredPane, /class="composer-mode-row"/)

  const menuRule = structuredPane.match(/\.composer-mode-menu\s*\{[\s\S]*?\}/)
  assert.ok(menuRule, 'mode menu CSS must exist')
  assert.match(menuRule[0], /bottom:\s*calc\(100% \+ \d+px\)/)
  assert.match(menuRule[0], /max-width:\s*min\(/)
})

test('mode menu exposes radio semantics and capability-driven options', () => {
  assert.match(structuredPane, /role="menu"/)
  assert.match(structuredPane, /v-for="option in modeOptions"/)
  assert.match(structuredPane, /role="menuitemradio"/)
  assert.match(structuredPane, /:aria-checked="currentModeId === option\.id"/)
  assert.match(structuredPane, /@click="selectMode\(option\.id\)"/)
  assert.doesNotMatch(structuredPane, /agentType.*(?:default|plan)/i)
})

test('mode picker closes on Escape and outside pointer input', () => {
  assert.match(structuredPane, /event\.key === 'Escape' && isModeMenuOpen\.value/)
  assert.match(structuredPane, /modePickerEl\.value\?\.contains\(event\.target as Node\)/)
  assert.match(structuredPane, /document\.addEventListener\('pointerdown', handleModeOutsidePointer\)/)
  assert.match(structuredPane, /document\.removeEventListener\('pointerdown', handleModeOutsidePointer\)/)
})

test('mode trigger locks for the whole turn and mobile targets remain reachable', () => {
  assert.match(structuredPane, /:disabled="modeInteractionLocked \|\| isUpdatingMode"/)
  assert.match(structuredPane, /if \(modeInteractionLocked\.value \|\| isUpdatingMode\.value/)
  assert.match(
    structuredPane,
    /@media \(max-width: 640px\)[\s\S]*?\.composer-mode-trigger\s*\{[\s\S]*?min-height:\s*44px/,
  )
  assert.match(
    structuredPane,
    /@media \(max-width: 640px\)[\s\S]*?\.composer-mode-menu-item\s*\{[\s\S]*?min-height:\s*44px/,
  )
})
