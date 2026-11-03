import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// SessionCreator is a pinia/API-heavy component; per repo convention its
// contract is pinned with source-wiring assertions here while real clicks,
// focus and keyboard behavior are validated by the Chromium composer scenario
// (frontend/tests/browser_chat_composer.py).
const read = relativePath => readFileSync(new URL(relativePath, import.meta.url), 'utf8')
const vue = read('../src/features/terminal/components/SessionCreator.vue')
const css = read('../src/features/terminal/styles/session-creator.css')
const history = read('../src/composables/useCwdHistory.ts')

const script = vue.slice(vue.indexOf('<script setup'))

function sliceBetween(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker)
  if (start < 0) throw new Error(`missing marker: ${startMarker}`)
  const end = source.indexOf(endMarker, start)
  if (end < 0) throw new Error(`missing end marker: ${endMarker}`)
  return source.slice(start, end)
}

// The v-for row plus both of its buttons, ending at the remove button close.
const rowStart = vue.indexOf('v-for="(cwd, index) in visibleCwdChoices"')
const optionClass = vue.indexOf('class="cwd-choice"', rowStart)
const removeClass = vue.indexOf('class="cwd-choice-remove"', rowStart)
const rowEnd = vue.indexOf('</button>', removeClass) + '</button>'.length
const rowBlock = vue.slice(rowStart, rowEnd)

const removeHandlerBlock = sliceBetween(vue, 'function removeCwdChoice(', 'function handleCwdKeydown')
const keydownBlock = sliceBetween(vue, 'function handleCwdKeydown', 'function handleCwdFocusOut')
const choicesBlock = sliceBetween(vue, 'const cwdChoices = computed', 'const visibleCwdChoices')

test('AC1: dropdown renders a presentation row with two sibling native buttons', () => {
  assert.ok(rowStart > 0)
  assert.match(rowBlock, /role="presentation"/)
  assert.match(rowBlock, /class="cwd-choice-row"/)
  assert.equal((rowBlock.match(/<button/g) || []).length, 2)
  assert.equal((rowBlock.match(/<\/button>/g) || []).length, 2)
  assert.equal((rowBlock.match(/type="button"/g) || []).length, 2)
  // Buttons are siblings: the option closes before the remove button opens.
  assert.ok(vue.indexOf('</button>', optionClass) < removeClass)
  assert.doesNotMatch(rowBlock, /<button[^>]*>\s*<button/s)

  // The option keeps the established id/class/role contracts so
  // aria-activedescendant and the existing composer scenario stay valid.
  assert.match(rowBlock, /:id="`tab-cwd-choice-\$\{index\}`"/)
  assert.match(rowBlock, /type="button"[\s\S]*role="option"/)
  assert.match(rowBlock, /:class="\{ 'is-active': activeCwdChoice === index \}"/)
  assert.match(rowBlock, /:aria-selected="form\.cwd === cwd"/)
  assert.match(rowBlock, /@click="selectCwdChoice\(cwd\)"/)
})

test('AC1: remove control is labeled, out of tab order and stops propagation', () => {
  const removeButton = vue.slice(removeClass, rowEnd)
  assert.match(removeButton, /class="cwd-choice-remove"\s+tabindex="-1"/)
  assert.match(removeButton, /:aria-label="`Remove \$\{cwd\}`"/)
  assert.match(removeButton, /:title="`Remove \$\{cwd\}`"/)
  assert.match(removeButton, /<span aria-hidden="true">✕<\/span>/)
  assert.match(removeButton, /@click\.stop="removeCwdChoice\(cwd\)"/)
  // Both controls prevent mousedown default so the input keeps focus.
  assert.equal((rowBlock.match(/@mousedown\.prevent/g) || []).length, 2)
})

test('AC2: useCwdHistory exposes removeCwd/dismissedCwds and empty-key cleanup', () => {
  assert.match(history, /function removeCwd\(/)
  assert.match(history, /const dismissedCwds = computed/)
  assert.match(history, /localStorage\.removeItem\(storageKey\(scopeValue\)\)/)
  assert.match(history, /memoryFallback\.delete\(scopeValue\)/)
})

test('AC3: cwdChoices merges open tabs then filters dismissed paths', () => {
  assert.match(script, /dismissedCwds,?[\s\S]*removeCwd/)
  assert.match(choicesBlock, /const dismissed = new Set\(dismissedCwds\.value\)/)
  assert.match(choicesBlock, /\.filter\(path => !dismissed\.has\(path\)\)/)
})

test('AC4: addCwd clears dismissal before prepending', () => {
  assert.match(history, /dismissedFor\(scope\.value\)\.delete\(path\)/)
})

test('AC5: removing a row never closes the dropdown or selects the row', () => {
  assert.doesNotMatch(removeHandlerBlock, /cwdDropdownOpen\.value = false/)
  assert.doesNotMatch(removeHandlerBlock, /selectCwdChoice\(/)
  assert.match(removeHandlerBlock, /removeCwd\(path\)/)
  // Selected path only: clear the input together with the typeahead filter.
  assert.match(
    removeHandlerBlock,
    /if \(form\.cwd\.trim\(\) === path\) \{\s*form\.cwd = ''\s*cwdFilterActive\.value = false\s*\}/,
  )
  // No unconditional form.cwd assignment: other typed text stays intact.
  assert.equal((removeHandlerBlock.match(/form\.cwd =/g) || []).length, 1)
  // Active index clamps to the remaining rows.
  assert.match(removeHandlerBlock, /const remaining = shown\.filter/)
  assert.match(removeHandlerBlock, /Math\.min\(wasActive, remaining - 1\)/)
})

test('AC6: Delete/Backspace act only with open listbox and active option', () => {
  assert.match(
    keydownBlock,
    /\(event\.key === 'Delete' \|\| event\.key === 'Backspace'\)\s*\n?\s*&& cwdDropdownOpen\.value && activeCwdChoice\.value >= 0/,
  )
  const deleteBranch = keydownBlock.slice(keydownBlock.indexOf("event.key === 'Delete'"))
  assert.match(deleteBranch, /event\.preventDefault\(\)/)
  assert.match(
    deleteBranch,
    /removeCwdChoice\(visibleCwdChoices\.value\[activeCwdChoice\.value\]\)/,
  )
  // Backspace is mentioned exactly once, inside the gated condition, so
  // plain text editing with no active option keeps normal input behavior.
  assert.equal((keydownBlock.match(/'Backspace'/g) || []).length, 1)
})

test('AC1 watch-item: keyboard scroll targets the option inside the row wrapper', () => {
  assert.match(keydownBlock, /const row = cwdChoicesRef\.value\?\.children\.item\(index\)/)
  assert.match(keydownBlock, /row\.querySelector\('\.cwd-choice'\)/)
})

test('row styles keep the option flexible and the remove control compact', () => {
  assert.match(css, /\.cwd-choice-row \{[\s\S]*?display: flex/)
  assert.match(css, /\.cwd-choice \{[\s\S]*?flex: 1 1 auto/)
  assert.match(css, /\.cwd-choice-remove \{[\s\S]*?width: 28px/)
})
