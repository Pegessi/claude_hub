import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { h, nextTick, reactive } from 'vue'
import { createSidebarLoader, makeRenderer } from './chatSidebarTestHarness.mjs'

const terminalPane = readFileSync(
  new URL('../src/components/TerminalPane.vue', import.meta.url),
  'utf8',
)
const structuredPane = readFileSync(
  new URL('../src/features/chat/StructuredPane.vue', import.meta.url),
  'utf8',
)
const queuedObserver = readFileSync(
  new URL('../src/components/QueuedChatObserver.vue', import.meta.url),
  'utf8',
)
const timelineController = readFileSync(
  new URL('../src/features/chat/controllers/useChatTimeline.ts', import.meta.url),
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
  assert.match(queuedObserver, /props\.managedChat \? 'managed-session'(?: as const)? : 'terminal-tab'/)
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
    /onUnmounted\(\(\) => \{([\s\S]*?)\n\}\)/,
  )?.[1] ?? ''
  assert.doesNotMatch(unmountedBody, /releaseQueuedChatActivation/)
  assert.match(unmountedBody, /terminalStore\.pruneQueuedChatState/)
})

test('switching back rearms viewport ownership before stream and Goal hydration', () => {
  const body = structuredPane.match(/onActivated\(\(\) => \{([\s\S]*?)\n\}\)/)?.[1]
  assert.ok(body)
  const activate = body.indexOf('timeline.activateViewport()')
  const start = body.indexOf('startStream()')
  const goal = body.indexOf('goalController.hydrateGoal()')
  assert.ok(activate >= 0 && activate < start && start < goal)
  assert.match(body, /timeline\.observeTimelineGeometry\(\)/)
})

test('only scrolling an active revealed timeline can detach from latest', () => {
  assert.match(timelineController, /function handleTimelineScroll\(\)/)
  assert.match(timelineController, /const el = timelineEl\.value[\s\S]*?if \(!el\) return/)
  assert.match(timelineController, /if \(timelineDisposed \|\| timelinePhase\.value !== 'revealed' \|\| isLoadingEarlier\.value\) return/)
  assert.match(timelineController, /if \(isTimelineNearBottom\(el\)\)[\s\S]*?rearmFollow\(\)[\s\S]*?else[\s\S]*?detachFromTail\(\)/)
})

test('queued observer scopes starts to source configuration without presentation restarts', async t => {
  const starts = []
  const stops = []
  const queue = reactive({
    drafts: [{}], paused: false, editing: false, sendInProgress: false, autoActivationBlock: null,
  })
  const store = reactive({
    tabs: [{
      id: 'tab-a', agent_type: 'codex', session_kind: 'chat', agent_session_id: 'session-a',
      env: { MODEL: 'one' }, name: 'Original', is_unread: false,
    }],
    getQueuedChatState: () => queue,
    releaseQueuedChatActivation() {},
    requestQueuedChatActivation() {},
  })
  globalThis.__queuedObserverTest = { starts, stops, store }
  t.after(() => { delete globalThis.__queuedObserverTest })
  const load = createSidebarLoader({
    'stores/terminalStore.ts': 'export const useTerminalStore = () => globalThis.__queuedObserverTest.store',
    'composables/useAgentStream.ts': `
      import { ref } from 'vue'
      export function useAgentStream(configuration) {
        return {
          events: ref([]), connectionState: ref('hydrating'),
          start: async (id, source) => globalThis.__queuedObserverTest.starts.push([id, source, configuration()]),
          stop: () => globalThis.__queuedObserverTest.stops.push('stop'),
        }
      }
    `,
    'utils/agentStreamTimeline.ts': 'export class IncrementalTimelineReducer { reduce() { return [] } }',
    'utils/chatTurnLifecycle.ts': 'export const isComposerHeldByTurn = () => false',
  })
  const { default: Observer } = await load('components/QueuedChatObserver.vue')
  const renderer = makeRenderer()
  const mount = () => {
    const app = renderer.createApp({ render: () => h(Observer, { paneId: 'pane-a', tabId: 'tab-a' }) })
    app.mount({ children: [] })
    return app
  }

  let app = mount()
  await nextTick()
  assert.equal(starts.length, 1)
  const initialConfiguration = starts[0][2]
  store.tabs[0].name = 'Renamed'
  store.tabs[0].is_unread = true
  await nextTick()
  assert.equal(starts.length, 1, 'title and unread presentation changes must not restart observation')

  store.tabs[0].env = { MODEL: 'two' }
  await nextTick()
  assert.equal(starts.length, 2)
  assert.notEqual(starts[1][2], initialConfiguration)

  app.unmount()
  app = mount()
  await nextTick()
  assert.equal(starts.length, 3)
  assert.equal(starts[2][2], starts[1][2], 'a remount must use the latest configuration signature')
  app.unmount()
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
