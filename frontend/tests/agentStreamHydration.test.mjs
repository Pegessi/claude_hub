import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import { setImmediate as nextImmediate, setTimeout as delay } from 'node:timers/promises'
import test from 'node:test'
import { createRenderer, isProxy, watch } from 'vue'
import ts from 'typescript'

const moduleUrls = new Map()
async function moduleUrl(path) {
  if (moduleUrls.has(path)) return moduleUrls.get(path)
  const source = await readFile(new URL(`../src/${path}.ts`, import.meta.url), 'utf8')
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  })
  for (const [, specifier] of outputText.matchAll(/from ['"]([^'"]+)['"]/g)) {
    const url = specifier.startsWith('@/')
      ? await moduleUrl(specifier.slice(2))
      : import.meta.resolve(specifier)
    outputText = outputText.replaceAll(`'${specifier}'`, JSON.stringify(url))
      .replaceAll(`"${specifier}"`, JSON.stringify(url))
  }
  const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
  moduleUrls.set(path, url)
  return url
}
const { useAgentStream } = await import(await moduleUrl('composables/useAgentStream'))
const { agentStreamHistoryCache } = await import(await moduleUrl('utils/agentStreamHistoryCache'))
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {},
})
function mountStream() {
  let stream
  const app = renderer.createApp({ setup() { stream = useAgentStream(); return () => null } })
  app.mount({})
  return { stream, unmount: () => app.unmount() }
}

test('a permanent observation denial fails promptly without retrying or replacing the provider', async t => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async url => {
    requests.push(url)
    if (url.endsWith('/capabilities')) return { ok: true, json: async () => ({ structured: true }) }
    if (url.includes('/events?')) return { ok: true, json: async () => ({ events: [], next_sequence: -1, has_more: false }) }
    return { ok: false, status: 403 }
  })
  const { stream, unmount } = mountStream()
  t.after(unmount)
  await stream.start('permanent-denial', 'terminal-tab')
  await nextImmediate()
  assert.equal(stream.connectionState.value, 'failed')
  assert.equal(requests.filter(url => url.endsWith('/wait')).length, 1)
  assert.ok(!requests.some(url => url.endsWith('/retry')))
})

test('the final delta-only hydration page is committed before live, without deep proxies', async t => {
  const event = { stream_sequence: 0, type: 'text_delta', payload: { text: 'Latest message' } }
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push(url)
    if (url.endsWith('/capabilities')) return { ok: true, json: async () => ({ structured: true }) }
    if (url.includes('/events?')) return {
      ok: true, json: async () => ({ events: [event], next_sequence: 0, has_more: false }),
    }
    return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))
  })
  const { stream, unmount } = mountStream()
  t.after(unmount)
  let visibleAtLive
  const unwatch = watch(stream.connectionState, state => {
    if (state === 'live') visibleAtLive = stream.events.value.slice()
  }, { flush: 'sync' })
  t.after(unwatch)
  await stream.start('hydration-flush-test', 'terminal-tab')
  assert.deepEqual(visibleAtLive, [event])
  assert.equal(stream.events.value[0], event)
  assert.equal(isProxy(stream.events.value), false)
  assert.equal(isProxy(stream.events.value[0].payload), false)
  assert.ok(requests.some(url => url.endsWith('/wait')))
})

test('conversation hydration records lazy detail descriptors and attributed tool ids', async t => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    requests.push({ url, options })
    if (url.endsWith('/capabilities')) return { ok: true, json: async () => ({ structured: true }) }
    if (url.includes('/events?')) {
      assert.match(url, /view=conversation/)
      return {
        ok: true,
        json: async () => ({
          events: [{ stream_sequence: 0, type: 'turn_started', turn_id: 'turn-1', payload: { summary: 'q' }, session_id: 's', tab_id: 't', agent_type: 'codex', created_at: 'now', redacted: false }],
          next_sequence: 7,
          has_more: false,
          history_details: [{ turn_id: 'turn-1', first_sequence: 0, last_sequence: 6, event_count: 7 }],
          attributed_tool_ids: ['tool-1'],
        }),
      }
    }
    return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))
  })

  const { stream, unmount } = mountStream()
  t.after(unmount)
  await stream.start('summary-metadata', 'terminal-tab')

  assert.deepEqual(stream.historyDetails.value['turn-1'].descriptor, {
    turn_id: 'turn-1', first_sequence: 0, last_sequence: 6, event_count: 7,
  })
  assert.equal(stream.historyDetails.value['turn-1'].status, 'idle')
  assert.deepEqual([...stream.attributedToolIds.value], ['tool-1'])
  assert.ok(requests.some(({ url }) => /view=conversation/.test(url)))
})

test('history detail loading replaces a turn range without advancing the live cursor or breaking interleaved order', async t => {
  const requests = []
  const event = (seq, turnId, type, payload = {}) => ({
    stream_sequence: seq,
    type,
    turn_id: turnId,
    payload,
    session_id: 's',
    tab_id: 't',
    agent_type: 'codex',
    created_at: `2026-01-01T00:00:${String(seq).padStart(2, '0')}Z`,
    redacted: false,
  })
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    requests.push({ url, options })
    if (url.endsWith('/capabilities')) return { ok: true, json: async () => ({ structured: true }) }
    if (url.includes('/events?')) {
      const parsed = new URL(url, 'http://local')
      if (parsed.searchParams.get('view') === 'conversation') {
        return {
          ok: true,
          json: async () => ({
            events: [
              event(0, 'target', 'turn_started', { summary: 'target q' }),
              event(1, 'other', 'turn_started', { summary: 'other q' }),
              event(3, 'other', 'text_delta', { text: 'other answer' }),
              event(4, 'target', 'text_delta', { text: 'target answer' }),
            ],
            next_sequence: 10,
            has_more: false,
            history_details: [{ turn_id: 'target', first_sequence: 0, last_sequence: 4, event_count: 3 }],
          }),
        }
      }
      assert.equal(parsed.searchParams.get('turn_id'), 'target')
      assert.equal(parsed.searchParams.get('through_sequence'), '4')
      const since = parsed.searchParams.get('since_sequence')
      return {
        ok: true,
        json: async () => since === '-1'
          ? { events: [event(0, 'target', 'turn_started', { summary: 'target q' }), event(2, 'target', 'thinking_delta', { text: 'work' })], next_sequence: 2, has_more: true }
          : { events: [event(4, 'target', 'text_delta', { text: 'target answer' })], next_sequence: 4, has_more: false },
      }
    }
    return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))
  })

  const { stream, unmount } = mountStream()
  t.after(unmount)
  await stream.start('detail-replace', 'terminal-tab')
  await stream.loadHistoryDetail('target')

  assert.equal(stream.historyDetails.value.target.status, 'loaded')
  assert.deepEqual(stream.events.value.map(e => e.stream_sequence), [0, 1, 2, 3, 4])
  assert.deepEqual(stream.events.value.map(e => `${e.turn_id}:${e.type}`), [
    'target:turn_started',
    'other:turn_started',
    'target:thinking_delta',
    'other:text_delta',
    'target:text_delta',
  ])
  const waitBodies = requests
    .filter(({ url }) => url.endsWith('/wait'))
    .map(({ options }) => JSON.parse(options.body))
  assert.ok(waitBodies.every(body => body.since_sequence === 10), 'detail fetches must not move the live wait cursor backwards')
})

test('stale history detail responses cannot mark a grown descriptor loaded', async t => {
  const streamPath = '/api/workspaces/tabs/descriptor-race/stream'
  const oldDescriptor = { turn_id: 'turn-1', first_sequence: 0, last_sequence: 2, event_count: 3 }
  const newDescriptor = { turn_id: 'turn-1', first_sequence: 0, last_sequence: 4, event_count: 5 }
  agentStreamHistoryCache.set(streamPath, {
    capabilities: { structured: true },
    events: [{ stream_sequence: 0, type: 'turn_started', turn_id: 'turn-1', payload: { summary: 'old' }, session_id: 's', tab_id: 't', agent_type: 'codex', created_at: 'now', redacted: false }],
    cursor: 0,
    historyDetails: { 'turn-1': { descriptor: oldDescriptor, status: 'idle' } },
    detailRevision: 0,
  })
  t.after(() => agentStreamHistoryCache.clear())

  let releaseCapabilities
  let releaseDetail
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    if (url.endsWith('/capabilities')) {
      return new Promise(resolve => { releaseCapabilities = () => resolve({ ok: true, json: async () => ({ structured: true }) }) })
    }
    if (url.includes('/events?')) {
      const parsed = new URL(url, 'http://local')
      if (parsed.searchParams.get('view') === 'conversation') {
        return { ok: true, json: async () => ({ events: [], next_sequence: 4, has_more: false, history_details: [newDescriptor] }) }
      }
      return new Promise(resolve => {
        releaseDetail = () => resolve({
          ok: true,
          json: async () => ({
            events: [{ stream_sequence: 2, type: 'thinking_delta', turn_id: 'turn-1', payload: { text: 'old detail' }, session_id: 's', tab_id: 't', agent_type: 'codex', created_at: 'now', redacted: false }],
            next_sequence: 2,
            has_more: false,
          }),
        })
        options.signal.addEventListener('abort', () => resolve({ ok: false, status: 499, json: async () => ({}) }))
      })
    }
    return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))
  })

  const { stream, unmount } = mountStream()
  t.after(unmount)
  const startPromise = stream.start('descriptor-race', 'terminal-tab')
  await nextImmediate()
  const detailPromise = stream.loadHistoryDetail('turn-1')
  await nextImmediate()
  releaseCapabilities()
  await nextImmediate()
  releaseDetail()
  await Promise.all([startPromise, detailPromise])

  assert.equal(stream.historyDetails.value['turn-1'].status, 'idle')
  assert.deepEqual(stream.historyDetails.value['turn-1'].descriptor, newDescriptor)
  assert.deepEqual(stream.events.value.map(event => event.payload.text).filter(Boolean), [])
})

test('explicit reset clears warm history cache before a renumbered edit reload', async t => {
  const streamPath = '/api/workspaces/tabs/edit-reset/stream'
  agentStreamHistoryCache.set(streamPath, {
    capabilities: { structured: true },
    events: [{ stream_sequence: 99, type: 'text_delta', turn_id: 'future', payload: { text: 'stale future' }, session_id: 's', tab_id: 't', agent_type: 'codex', created_at: 'old', redacted: false }],
    cursor: 99,
  })
  t.after(() => agentStreamHistoryCache.clear())

  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    if (url.endsWith('/capabilities')) return { ok: true, json: async () => ({ structured: true }) }
    if (url.includes('/events?')) {
      const since = new URL(url, 'http://local').searchParams.get('since_sequence')
      return since === '99'
        ? { ok: true, json: async () => ({ events: [], next_sequence: 99, has_more: false }) }
        : {
          ok: true,
          json: async () => ({
            events: [{ stream_sequence: 0, type: 'text_delta', turn_id: 'new', payload: { text: 'new answer' }, session_id: 's', tab_id: 't', agent_type: 'codex', created_at: 'new', redacted: false }],
            next_sequence: 0,
            has_more: false,
          }),
        }
    }
    return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))
  })

  const { stream, unmount } = mountStream()
  t.after(unmount)
  await stream.start('edit-reset', 'terminal-tab')
  assert.deepEqual(stream.events.value.map(event => event.payload.text), ['stale future'])

  stream.reset()
  await stream.start('edit-reset', 'terminal-tab')

  assert.deepEqual(stream.events.value.map(event => event.payload.text), ['new answer'])
  assert.equal(stream.events.value[0].stream_sequence, 0)
})

test('stop aborts in-flight history detail fetches and ignores late responses', async t => {
  const descriptor = { turn_id: 'turn-1', first_sequence: 0, last_sequence: 2, event_count: 3 }
  let detailSignal
  let releaseDetail
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    if (url.endsWith('/capabilities')) return { ok: true, json: async () => ({ structured: true }) }
    if (url.includes('/events?')) {
      const parsed = new URL(url, 'http://local')
      if (parsed.searchParams.get('view') === 'conversation') {
        return {
          ok: true,
          json: async () => ({
            events: [{ stream_sequence: 0, type: 'turn_started', turn_id: 'turn-1', payload: { summary: 'q' }, session_id: 's', tab_id: 't', agent_type: 'codex', created_at: 'now', redacted: false }],
            next_sequence: 2,
            has_more: false,
            history_details: [descriptor],
          }),
        }
      }
      detailSignal = options.signal
      return new Promise(resolve => {
        releaseDetail = () => resolve({
          ok: true,
          json: async () => ({
            events: [{ stream_sequence: 1, type: 'thinking_delta', turn_id: 'turn-1', payload: { text: 'late detail' }, session_id: 's', tab_id: 't', agent_type: 'codex', created_at: 'now', redacted: false }],
            next_sequence: 1,
            has_more: false,
          }),
        })
      })
    }
    return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))
  })

  const { stream, unmount } = mountStream()
  t.after(unmount)
  await stream.start('stop-detail', 'terminal-tab')
  const detailPromise = stream.loadHistoryDetail('turn-1')
  await nextImmediate()
  assert.ok(detailSignal)
  assert.equal(detailSignal.aborted, false)

  stream.stop()
  assert.equal(detailSignal.aborted, true)
  releaseDetail()
  await detailPromise

  assert.notEqual(stream.historyDetails.value['turn-1']?.status, 'loaded')
  assert.deepEqual(stream.events.value.map(event => event.payload.text).filter(Boolean), [])
})

test('SSE applies the same summary-attributed replay filter as long-poll and advances reconciliation cursor', async t => {
  const requests = []
  const sources = []
  const event = (seq, type, payload = {}, extra = {}) => ({
    stream_sequence: seq,
    type,
    turn_id: null,
    payload,
    session_id: 's',
    tab_id: 't',
    agent_type: 'codex',
    created_at: 'now',
    redacted: false,
    ...extra,
  })

  class FakeEventSource {
    constructor(url) {
      this.url = url
      this.listeners = new Map()
      this.closed = false
      sources.push(this)
    }
    addEventListener(type, listener) {
      const list = this.listeners.get(type) ?? []
      list.push(listener)
      this.listeners.set(type, list)
    }
    emit(type, data) {
      for (const listener of this.listeners.get(type) ?? []) listener({ data })
    }
    close() { this.closed = true }
  }

  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    requests.push({ url, options })
    if (url.endsWith('/capabilities')) return { ok: true, json: async () => ({ structured: true }) }
    if (url.includes('/events?')) {
      return {
        ok: true,
        json: async () => ({
          events: [],
          next_sequence: 10,
          has_more: false,
          attributed_tool_ids: ['tool-1'],
        }),
      }
    }
    if (url.endsWith('/wait')) {
      return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))
    }
    throw new Error(`unexpected ${url}`)
  })
  const previousEventSource = globalThis.EventSource
  globalThis.EventSource = FakeEventSource
  t.after(() => {
    if (previousEventSource === undefined) delete globalThis.EventSource
    else globalThis.EventSource = previousEventSource
  })

  const { stream, unmount } = mountStream()
  t.after(unmount)
  await stream.start('sse-dedupe', 'terminal-tab')
  assert.equal(sources.length, 1)
  sources[0].emit('agent-stream', JSON.stringify(event(11, 'tool_call_completed', { tool_call_id: 'tool-1', result: 'duplicate' }, { call_id: 'tool-1' })))
  sources[0].emit('agent-stream', JSON.stringify(event(12, 'text_delta', { text: 'fresh' })))
  await delay(60)

  assert.deepEqual(stream.events.value.map(e => e.type), ['text_delta'])
  stream.nudge()
  await delay(0)
  const waitBodies = requests
    .filter(({ url }) => url.endsWith('/wait'))
    .map(({ options }) => JSON.parse(options.body))
  assert.deepEqual(waitBodies.slice(0, 2).map(body => body.since_sequence), [10, 12])
})

test('live reconciliation filters null-turn replay identities learned from summaries while advancing the cursor', async t => {
  const requests = []
  let waitCalls = 0
  const event = (seq, type, payload = {}, extra = {}) => ({
    stream_sequence: seq,
    type,
    turn_id: null,
    payload,
    session_id: 's',
    tab_id: 't',
    agent_type: 'codex',
    created_at: 'now',
    redacted: false,
    ...extra,
  })
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    requests.push({ url, options })
    if (url.endsWith('/capabilities')) return { ok: true, json: async () => ({ structured: true }) }
    if (url.includes('/events?')) {
      return {
        ok: true,
        json: async () => ({
          events: [],
          next_sequence: 10,
          has_more: false,
          attributed_tool_ids: ['tool-1'],
        }),
      }
    }
    if (url.endsWith('/wait')) {
      waitCalls += 1
      if (waitCalls === 1) {
        return {
          ok: true,
          json: async () => ({
            events: [
              event(11, 'tool_call_completed', { tool_call_id: 'tool-1', result: 'duplicate' }, { call_id: 'tool-1' }),
              event(12, 'text_delta', { text: 'fresh' }),
            ],
            next_sequence: 12,
            has_more: false,
          }),
        }
      }
      return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))
    }
    throw new Error(`unexpected ${url}`)
  })

  const { stream, unmount } = mountStream()
  t.after(unmount)
  await stream.start('live-dedupe', 'terminal-tab')
  await delay(60)

  assert.deepEqual(stream.events.value.map(e => e.type), ['text_delta'])
  const waitBodies = requests
    .filter(({ url }) => url.endsWith('/wait'))
    .map(({ options }) => JSON.parse(options.body))
  assert.deepEqual(waitBodies.slice(0, 2).map(body => body.since_sequence), [10, 12])
})
