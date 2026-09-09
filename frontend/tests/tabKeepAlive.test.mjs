import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const terminalPane = readFileSync(
  new URL('../src/components/TerminalPane.vue', import.meta.url),
  'utf8',
)
const structuredPane = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)
const historyCache = readFileSync(
  new URL('../src/utils/agentStreamHistoryCache.ts', import.meta.url),
  'utf8',
)

// ---------------------------------------------------------------------------
// Chat-tab keep-alive. Switching chat tabs must not destroy the StructuredPane
// (losing scroll, draft, attachments, expand state). The pane is keyed by
// tabId inside a bounded <KeepAlive>, so each chat tab owns a cached instance.
// ---------------------------------------------------------------------------

test('TerminalPane caches StructuredPane per tabId inside a bounded KeepAlive', () => {
  assert.match(
    terminalPane,
    /<KeepAlive\s+:max="[^"]+"/,
    'a <KeepAlive> with a bounded :max must wrap the chat pane',
  )
  // The key is what gives each chat tab its own cached instance; without it a
  // tab switch would reuse one instance and wipe state via the prop change.
  assert.match(
    terminalPane,
    /<StructuredPane[\s\S]*?:key="pane\.tabId"/,
    'StructuredPane must be keyed by pane.tabId so each chat tab gets a cached instance',
  )
})

test('StructuredPane resumes/stops the stream on KeepAlive activate/deactivate', () => {
  assert.match(structuredPane, /onActivated\(/, 'onActivated must resume the stream')
  assert.match(
    structuredPane,
    /onDeactivated\(/,
    'onDeactivated must stop the stream (caching history) so cached panes do not hold open connections',
  )
})

test('activation gate skips the force-pin on KeepAlive reactivation', () => {
  // The connectionState watcher must short-circuit when the timeline was
  // already revealed — otherwise re-entering "reconciling" would force-pin to
  // the tail and clobber the preserved scroll position.
  assert.match(
    structuredPane,
    /if \(timelinePhase\.value === 'revealed'\) return/,
    'the activation gate must return early when the timeline was already revealed (reactivation)',
  )
})

test('default history LRU capacity is expanded well beyond the old value of 3', () => {
  const match = historyCache.match(/createAgentStreamHistoryCache\((\d+)\)/)
  assert.ok(match, 'the default agentStreamHistoryCache capacity must be declared')
  const capacity = Number(match[1])
  assert.ok(
    capacity >= 8,
    `expected the history LRU capacity to be expanded to >= 8 (was 3), got ${capacity}`,
  )
})
