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
// (losing draft, attachments, expand state). The pane is keyed by
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

test('switching back rearms latest before cached history reconciles', () => {
  const body = structuredPane.match(/onActivated\(\(\) => \{([\s\S]*?)\n\}\)/)?.[1]
  assert.ok(body)
  const calls = []
  const visibleHistoryStart = { value: 120 }
  const activate = new Function(
    'resetActivation', 'startStream', 'hydrateGoal', 'nextTick', 'observeTimelineGeometry',
    'visibleHistoryStart', 'timelineDisposed', 'timelineVisit', body,
  )
  activate(
    () => calls.push('reset'),
    () => calls.push('start'),
    () => calls.push('hydrate-goal'),
    () => {},
    () => {},
    visibleHistoryStart, true, 0,
  )
  assert.deepEqual(calls, ['reset', 'start', 'hydrate-goal'])
  assert.equal(visibleHistoryStart.value, null, 'a new visit starts with the latest history window')
})

test('only scrolling an active revealed timeline can detach from latest', () => {
  const body = structuredPane.match(/function handleTimelineScroll\(\) \{([\s\S]*?)\n\}/)?.[1]
  assert.ok(body)
  const scroll = new Function(
    'timelineEl', 'timelineDisposed', 'timelinePhase', 'isLoadingEarlier',
    'isTimelineNearBottom', 'isFollowingLatest', 'rearmFollow', 'detachFromTail', body,
  )
  for (const [disposed, phase, loading, shouldDetach] of [
    [true, 'revealed', false, false],
    [false, 'hidden', false, false],
    [false, 'pinning', false, false],
    [false, 'revealed', true, false],
    [false, 'revealed', false, true],
  ]) {
    let detached = false
    scroll(
      { value: {} }, disposed, { value: phase }, { value: loading },
      () => false, { value: true }, () => {}, () => { detached = true },
    )
    assert.equal(detached, shouldDetach, `disposed=${disposed}, phase=${phase}, loading=${loading}`)
  }
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
