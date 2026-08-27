import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const vueSource = await readFile(
  new URL('../src/components/TerminalView.vue', import.meta.url),
  'utf8',
)
const policySource = await readFile(
  new URL('../src/utils/terminalSwitchPolicy.ts', import.meta.url),
  'utf8',
)
const recoverySource = await readFile(
  new URL('../src/utils/terminalPaneRecovery.ts', import.meta.url),
  'utf8',
)

const scriptMatch = vueSource.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)
assert.ok(scriptMatch, 'TerminalView.vue must contain a <script setup> block')
const script = scriptMatch[1]

test('terminalSwitchPolicy keeps tab-switch defer policy in decideSwitchReplay', () => {
  assert.match(policySource, /export function decideSwitchReplay/)
  assert.doesNotMatch(policySource, /isStructuralRecoveryTrigger/)
  assert.doesNotMatch(policySource, /decidePaneRecoveryReplay/)
})

test('terminalPaneRecovery exports document-generation correlation helpers', () => {
  assert.match(recoverySource, /documentGeneration/)
  assert.match(recoverySource, /isStalePaneRefreshDone/)
  assert.match(recoverySource, /bumpIframeDocumentGeneration/)
  assert.doesNotMatch(recoverySource, /shouldAcceptPaneRefreshDone/)
})

test('bootstrap document wait replaces forced remount/reload refresh', () => {
  assert.match(script, /function armBootstrapDocumentWait\s*\(/)
  assert.match(script, /terminal-bootstrap-correlation/)
  assert.match(script, /agentStatus:\s*currentAgentStatus\.value/)
  assert.match(script, /function revealCachedTabSwitch\s*\(/)
  assert.doesNotMatch(script, /scheduleTabSwitchHistoryRecovery/)
  assert.doesNotMatch(script, /schedulePaneHistoryRecovery/)
  assert.doesNotMatch(script, /pane-remount/)
  assert.doesNotMatch(script, /iframe-reload/)
})

test('onIframeLoad bumps document generation and arms bootstrap wait', () => {
  assert.match(vueSource, /bumpIframeDocumentGeneration\(iframeDocumentGeneration, tabId\)/)
  assert.match(vueSource, /armBootstrapDocumentWait\(tabId, tabSwitchGeneration, docGen\)/)
})

test('message bridge validates event.source for refresh-done', () => {
  assert.match(script, /event\.source !== expectedSource/)
  assert.match(script, /iframeWindowForTab/)
})

test('retryContentRefresh re-arms bootstrap wait', () => {
  const fnMatch = script.match(/function retryContentRefresh\s*\([^)]*\)\s*\{([\s\S]*?)\n\}/)
  assert.ok(fnMatch)
  assert.match(fnMatch[1], /armBootstrapDocumentWait/)
  assert.match(fnMatch[1], /stopDocumentReadyWait/)
  assert.doesNotMatch(fnMatch[1], /contentReady\.value\s*=\s*true/)
})

test('stale refresh-done guarded by isStalePaneRefreshDone', () => {
  assert.match(script, /isStalePaneRefreshDone/)
  assert.match(script, /waitForDocumentReady/)
})

test('cached tab switch reveals live output without history refresh', () => {
  assert.match(script, /function revealCachedTabSwitch\s*\(/)
  assert.match(script, /revealCachedTabSwitch\(newTabId\)/)
  const fnStart = script.indexOf('function revealCachedTabSwitch')
  const fnEnd = script.indexOf('/** New iframe document', fnStart)
  assert.ok(fnStart >= 0 && fnEnd > fnStart)
  const fnBody = script.slice(fnStart, fnEnd)
  assert.match(fnBody, /contentReady\.value\s*=\s*true/)
  assert.doesNotMatch(fnBody, /postTerminalHistoryRefresh/)
  assert.doesNotMatch(fnBody, /postTerminalScrollBottom/)
})

test('deferred bootstrap ready refreshes immediately when agent already stable', () => {
  assert.match(script, /detail\.deferredRecovery/)
  assert.match(script, /deferred-status-refresh/)
  assert.match(script, /currentAgentStatus\.value === 'idle' \|\| currentAgentStatus\.value === 'attention'/)
})

test('bootstrap wait does not early-reveal for working agent status', () => {
  const fnMatch = script.match(/function armBootstrapDocumentWait\s*\([^)]*\)[^{]*\{([\s\S]*?)\n\}/)
  assert.ok(fnMatch)
  assert.doesNotMatch(fnMatch[1], /applyDeferredDocumentReveal/)
})
