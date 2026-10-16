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
const queuedObserver = readFileSync(
  new URL('../src/components/QueuedChatObserver.vue', import.meta.url),
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

test('TerminalPane keeps the full Chat cache bounded and mounts minimal queue observers', () => {
  assert.match(
    terminalPane,
    /<KeepAlive\s+:max="MAX_CACHED_CHAT_PANES"/,
    'the full Chat cache must stay bounded',
  )
  assert.match(
    terminalPane,
    /<QueuedChatObserver[\s\S]*?v-for="tabId in queuedObserverTabIds"/,
    'evicted queues must retain only a minimal hidden observer',
  )
  // The key is what gives each chat tab its own cached instance; without it a
  // tab switch would reuse one instance and wipe state via the prop change.
  assert.match(
    terminalPane,
    /<StructuredPane[\s\S]*?:key="store\.chatPaneCacheKey\(pane\.tabId\)"/,
    'StructuredPane must be keyed by pane.tabId so each chat tab gets a cached instance',
  )
  assert.match(
    terminalPane,
    /<StructuredPane[\s\S]*?:pane-id="pane\.id"/,
    'StructuredPane must know which pane owns its cached queue',
  )
})

test('minimal queued observer requests foreground activation on completion or failure', () => {
  assert.match(queuedObserver, /props\.managedChat \? 'managed-session' : 'terminal-tab'/)
  assert.match(
    queuedObserver,
    /connectionState\.value === 'live' && !turnInFlight\.value/,
    'cold hydration must not mistake empty history for a completed turn',
  )
  assert.match(queuedObserver, /requestQueuedChatActivation\(props\.paneId, props\.tabId\)/)
})

test('StructuredPane resumes the stream and suspends non-queued observers on KeepAlive edges', () => {
  assert.match(structuredPane, /onActivated\(/, 'onActivated must resume the stream')
  assert.match(
    structuredPane,
    /onDeactivated\([\s\S]*?suspendStreamForDeactivation\(\)/,
    'onDeactivated must release ordinary cached streams while an actionable queue keeps observing completion',
  )
})

test('KeepAlive eviction preserves a minimal observer owned activation request', () => {
  const unmountedBody = structuredPane.match(
    /onUnmounted\(\(\) => \{([\s\S]*?)\n\}\)\n\nfunction retry/,
  )?.[1] ?? ''
  assert.doesNotMatch(
    unmountedBody,
    /releaseQueuedChatActivation/,
    'evicting a full cached pane must not drop the hidden observer FIFO request',
  )
  assert.match(
    unmountedBody,
    /pruneQueuedChatState/,
    'ordinary empty queue state should still be pruned on cache eviction',
  )
})

test('switching back rearms latest before cached history reconciles', () => {
  const body = structuredPane.match(/onActivated\(\(\) => \{([\s\S]*?)\n\}\)/)?.[1]
  assert.ok(body)
  const calls = []
  const visibleHistoryStart = { value: 120 }
  const activate = new Function(
    'resetActivation', 'startStream', 'hydrateGoal', 'nextTick', 'observeTimelineGeometry',
    'visibleHistoryStart', 'isPaneActivated', 'timelineDisposed', 'timelineVisit', 'isManagedChat', body,
  )
  activate(
    () => calls.push('reset'),
    () => calls.push('start'),
    () => calls.push('hydrate-goal'),
    () => {},
    () => {},
    visibleHistoryStart, { value: false }, true, 0, { value: false },
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
