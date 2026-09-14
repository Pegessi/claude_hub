import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// Structural guards for the archived-tab restore flow. These pin the contract
// between terminalStore.unarchiveTab (returns a boolean) and App.vue's deep
// link handler (only confirms restore on success), so a future refactor can't
// silently reintroduce the false "Restored archived session" toast.
const storeSource = readFileSync(
  new URL('../src/stores/terminalStore.ts', import.meta.url),
  'utf8',
)
const appSource = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')

test('unarchiveTab declares a boolean return so callers can gate feedback', () => {
  assert.match(
    storeSource,
    /async function unarchiveTab\(tabId: string\): Promise<boolean>/,
  )
})

test('handleDeepLink captures the unarchiveTab result before toasting', () => {
  assert.match(appSource, /const restored = await store\.unarchiveTab\(tabId\)/)
})

test('handleDeepLink guards the success toast behind the restore result', () => {
  // The "Restored archived session" notification must sit inside an
  // `if (restored)` block so a failed restore doesn't show a success toast.
  const guarded = appSource.match(/if \(restored\) \{([\s\S]*?)\n {4}\}/)
  assert.ok(guarded, 'expected an if (restored) block in handleDeepLink')
  assert.match(guarded[1], /Restored archived session/)
})
