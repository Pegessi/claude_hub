import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// Structural guards for the archived-tab restore flow. These pin the contract
// between terminalStore.unarchiveTab (returns a boolean) and App.vue's deep
// link handler (only confirms restore on success), so a future refactor can't
// silently reintroduce the false "Restored archived session" toast.
const storeSource = readFileSync(
  new URL('../src/stores/terminal/index.ts', import.meta.url),
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

test('App clears stale ?tab= deep links on failure, not-found, and tab switch without breaking attachment preview', () => {
  assert.match(appSource, /function clearTabQueryParam\(\)/)
  assert.match(appSource, /stripTabDeepLink\(window\.location\)/)
  assert.match(appSource, /window\.history\.replaceState\(window\.history\.state, '', nextUrl\)/)
  assert.match(appSource, /if \(window\.history\.state\?\.attachmentPreview\) return/)
  assert.match(appSource, /if \(linkedTabId && linkedTabId !== tabId\)/)
})

test('App and WorkspaceSummary do not duplicate toast errors in full-width banners', () => {
  const workspaceSummary = readFileSync(
    new URL('../src/features/workspace/components/WorkspaceSummary.vue', import.meta.url),
    'utf8',
  )
  assert.doesNotMatch(appSource, /class="error-banner"/)
  assert.doesNotMatch(workspaceSummary, /class="workspace-error"/)
})

test('ArchivedSessionsPanel gates close on restore result and separates Personal vs Workspace tabs', () => {
  const panelSource = readFileSync(
    new URL('../src/components/ArchivedSessionsPanel.vue', import.meta.url),
    'utf8',
  )
  assert.match(panelSource, /const restored = await unarchiveTab\(tabId\)\s+if \(restored\) emit\('close'\)/)
  assert.match(panelSource, /archivedManualTabs/)
  assert.match(panelSource, /archivedManagedTabs/)
})
