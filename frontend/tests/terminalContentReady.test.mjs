import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

// Regression tests for the tab-switch content-ready boundary in TerminalView.vue.
//
// The boundary must:
//   1. Never reveal terminal content solely because a fixed timeout elapsed
//      (no blind setTimeout that sets contentReady=true).
//   2. Only reveal content when the matching terminal-history-refresh-done
//      event arrives (request-correlated).
//   3. Fail closed: if refresh-done never arrives, keep the pane hidden and
//      surface a Retry overlay (contentError=true), never show stale content.
//   4. Not block input during the pending window (no pointer-events:none on
//      .content-pending).
//
// These tests parse the Vue SFC source to assert structural invariants that
// encode the above rules. They are intentionally source-level so they catch
// regressions even when the runtime DOM/xterm stack is not available.

const vueSource = await readFile(
  new URL('../src/components/TerminalView.vue', import.meta.url),
  'utf8',
)

// Extract the <script setup> block.
const scriptMatch = vueSource.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)
assert.ok(scriptMatch, 'TerminalView.vue must contain a <script setup> block')
const script = scriptMatch[1]

// Extract the <style scoped> block.
const styleMatch = vueSource.match(/<style scoped>([\s\S]*?)<\/style>/)
assert.ok(styleMatch, 'TerminalView.vue must contain a <style scoped> block')
const style = styleMatch[1]

// --- Invariant 1: no blind timeout reveals content -----------------------
//
// The previous implementation had a 350 ms setTimeout that set
// contentReady.value = true even when terminal-history-refresh-done never
// arrived. That blind fallback is forbidden: the pane must only be revealed
// by the correlated refresh-done handler (or the no-refresh-to-wait-on
// branches). We assert that no setTimeout callback sets contentReady=true.

test('no setTimeout callback sets contentReady.value = true (no blind reveal)', () => {
  // Find every setTimeout(...) call and check whether its body contains
  // contentReady.value = true. We allow contentReady.value = false (hide)
  // and contentError.value = true (fail-closed overlay).
  const setTimeoutRegex = /setTimeout\s*\(\s*\(\s*\)\s*=>\s*\{([\s\S]*?)\}\s*,/g
  let match
  let foundBlindReveal = false
  while ((match = setTimeoutRegex.exec(script)) !== null) {
    const body = match[1]
    if (/contentReady\.value\s*=\s*true/.test(body)) {
      foundBlindReveal = true
      break
    }
  }
  assert.equal(
    foundBlindReveal,
    false,
    'A setTimeout callback must never set contentReady.value = true. ' +
      'Content must only be revealed by the correlated refresh-done handler ' +
      'or the no-refresh branches, never by elapsed time.',
  )
})

// --- Invariant 2: the fail-closed timeout sets contentError, not contentReady ---

test('the content-refresh timeout sets contentError=true (fail-closed)', () => {
  // The error-path timeout (CONTENT_REFRESH_TIMEOUT_MS) must flip
  // contentError.value = true so the Retry overlay shows. It must NOT set
  // contentReady.value = true.
  const errorTimerMatch = script.match(
    /errorTimer\s*=\s*window\.setTimeout\s*\(\s*\(\s*\)\s*=>\s*\{([\s\S]*?)\}\s*,\s*CONTENT_REFRESH_TIMEOUT_MS\s*\)/,
  )
  assert.ok(errorTimerMatch, 'errorTimer must be set with CONTENT_REFRESH_TIMEOUT_MS')
  const body = errorTimerMatch[1]
  assert.match(
    body,
    /contentError\.value\s*=\s*true/,
    'The fail-closed timeout must set contentError.value = true',
  )
  assert.doesNotMatch(
    body,
    /contentReady\.value\s*=\s*true/,
    'The fail-closed timeout must NOT set contentReady.value = true',
  )
})

// --- Invariant 3: contentReady is only set true inside the onDone handler ---
//
// We check that contentReady.value = true appears only in:
//   - the onDone handler (correlated refresh-done)
//   - the no-refresh branches (iframe not ready, initial mount, same-tab,
//     mobile, deferred working-agent)
// It must NOT appear inside any setTimeout body (covered by invariant 1)
// and it must NOT appear unconditionally outside those contexts.

test('contentReady=true only appears in onDone or no-refresh branches', () => {
  // Count occurrences of contentReady.value = true.
  const revealMatches = script.match(/contentReady\.value\s*=\s*true/g) || []
  // We expect exactly: onDone (1), iframe-not-ready else (1),
  // initial/same-tab/mobile (1), deferred working-agent (1),
  // and retryContentRefresh else (1).
  assert.ok(
    revealMatches.length >= 4,
    `Expected at least 4 contentReady=true assignments (onDone + no-refresh branches), got ${revealMatches.length}`,
  )
  // None of them may be inside a setTimeout body (invariant 1 covers this,
  // but re-assert here for clarity).
  const setTimeoutRegex = /setTimeout\s*\(\s*\(\s*\)\s*=>\s*\{([\s\S]*?)\}\s*,/g
  let m
  while ((m = setTimeoutRegex.exec(script)) !== null) {
    assert.doesNotMatch(
      m[1],
      /contentReady\.value\s*=\s*true/,
      'contentReady=true must not appear inside any setTimeout body',
    )
  }
})

// --- Invariant 4: .content-pending CSS must not block pointer events -------
//
// The pending pane must remain focusable and clickable so the SAB fast-input
// path and direct xterm focus keep working. pointer-events:none would steal
// the ability to focus the iframe during the refresh window.

test('.content-pending CSS does not set pointer-events: none', () => {
  // Find the .terminal-iframe.active.content-pending rule.
  const ruleMatch = style.match(
    /\.terminal-iframe\.active\.content-pending\s*\{([\s\S]*?)\}/,
  )
  assert.ok(ruleMatch, 'Must have a .terminal-iframe.active.content-pending CSS rule')
  const ruleBody = ruleMatch[1]
  assert.doesNotMatch(
    ruleBody,
    /pointer-events\s*:\s*none/,
    '.content-pending must not set pointer-events: none — input must flow during the pending window',
  )
  // It should still hide the canvas via opacity.
  assert.match(
    ruleBody,
    /opacity\s*:\s*0/,
    '.content-pending must set opacity: 0 to hide the incomplete frame',
  )
})

// --- Invariant 5: contentError ref exists and drives a Retry overlay -------

test('contentError ref exists and is wired to a Retry overlay', () => {
  assert.match(
    script,
    /const contentError\s*=\s*ref\(false\)/,
    'contentError ref must be declared and default to false',
  )
  // The template must show a Retry overlay when contentError is true.
  assert.match(
    vueSource,
    /v-if="contentError/,
    'Template must render an overlay conditional on contentError',
  )
  assert.match(
    vueSource,
    /retryContentRefresh/,
    'Template must call retryContentRefresh from the Retry button',
  )
})

// --- Invariant 6: retryContentRefresh re-issues the refresh and stays hidden ---

test('retryContentRefresh keeps contentReady=false until refresh-done', () => {
  const fnMatch = script.match(
    /function retryContentRefresh\s*\([^)]*\)\s*\{([\s\S]*?)\n\}/,
  )
  assert.ok(fnMatch, 'retryContentRefresh function must exist')
  const body = fnMatch[1]
  // It must reset contentError and hide the pane before re-issuing.
  assert.match(body, /contentError\.value\s*=\s*false/)
  assert.match(body, /contentReady\.value\s*=\s*false/)
  // It must re-issue the history refresh and wait on refresh-done.
  assert.match(body, /postTerminalHistoryRefresh/)
  assert.match(body, /resizeOnHistoryRefreshDone/)
  // It must NOT unconditionally set contentReady=true (only in the
  // iframe-not-ready else branch).
  const revealInBody = body.match(/contentReady\.value\s*=\s*true/g) || []
  assert.equal(
    revealInBody.length,
    1,
    'retryContentRefresh may set contentReady=true only in the iframe-not-ready else branch',
  )
})
