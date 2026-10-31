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
function mountStream(configuration) {
  let stream
  const app = renderer.createApp({ setup() { stream = useAgentStream(configuration); return () => null } })
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

test('a provider failure displays its detail without retrying or losing history', async t => {
  const event = { stream_sequence: 0, type: 'text_delta', payload: { text: 'Saved answer' } }
  const detail = 'provider exited with code 1: authentication failed or timed out'
  const requests = []
  t.mock.method(globalThis, 'fetch', async url => {
    requests.push(url)
    if (url.endsWith('/capabilities')) return Response.json({ structured: true })
    if (url.includes('/events?')) return Response.json({ events: [event], next_sequence: 0, has_more: false })
    return Response.json({ detail: `  ${detail}\n` }, { status: 409 })
  })
  const { stream, unmount } = mountStream()
  t.after(unmount)
  await stream.start('provider-auth-failure', 'terminal-tab')
  await nextImmediate()
  assert.equal(stream.connectionState.value, 'failed')
  assert.equal(stream.errorMessage.value, `wait HTTP 409: ${detail}`)
  assert.deepEqual(stream.events.value, [event])
  assert.equal(requests.filter(url => url.endsWith('/wait')).length, 1)
  assert.ok(!requests.some(url => /\/(retry|send)$/.test(url)))
})

for (const [name, body] of [
  ['missing', '{}'],
  ['empty', '{"detail":" "}'],
  ['null', 'null'],
  ['object', '{"detail":{"message":"do not stringify"}}'],
  ['array', '{"detail":[{"msg":"validation issue"}]}'],
  ['number', '{"detail":401}'],
  ['HTML response', '<html>upstream failure</html>'],
]) {
  test(`a ${name} error detail keeps the HTTP fallback`, async t => {
    t.mock.method(globalThis, 'fetch', async url => {
      if (url.endsWith('/capabilities')) return Response.json({ structured: true })
      if (url.includes('/events?')) return Response.json({ events: [], next_sequence: -1, has_more: false })
      return new Response(body, { status: 409 })
    })
    const { stream, unmount } = mountStream()
    t.after(unmount)
    await stream.start(`malformed-detail-${name}`, 'terminal-tab')
    await nextImmediate()
    assert.equal(stream.connectionState.value, 'failed')
    assert.equal(stream.errorMessage.value, 'wait HTTP 409')
  })
}

for (const status of [408, 429, 503]) {
  test(`a ${status} detail preserves observation retry without replacing the provider`, async t => {
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const requests = []
    let waitCalls = 0
    t.mock.method(globalThis, 'fetch', async (url, options) => {
      requests.push(url)
      if (url.endsWith('/capabilities')) return Response.json({ structured: true })
      if (url.includes('/events?')) return Response.json({ events: [], next_sequence: -1, has_more: false })
      waitCalls += 1
      if (waitCalls === 1) return Response.json({ detail: 'temporary upstream failure' }, { status })
      return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))
    })
    const { stream, unmount } = mountStream()
    t.after(unmount)
    await stream.start(`retryable-detail-${status}`, 'terminal-tab')
    await nextImmediate()
    assert.equal(stream.connectionState.value, 'live')
    t.mock.timers.tick(1_000)
    await nextImmediate()
    assert.equal(waitCalls, 2)
    assert.equal(stream.connectionState.value, 'live')
    assert.equal(stream.errorMessage.value, null)
    assert.ok(!requests.some(url => /\/(retry|send)$/.test(url)))
  })
}

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
  let releaseHistory
  let releaseDetail
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    if (url.endsWith('/capabilities')) {
      return new Promise(resolve => { releaseCapabilities = () => resolve({ ok: true, json: async () => ({ structured: true }) }) })
    }
    if (url.includes('/events?')) {
      const parsed = new URL(url, 'http://local')
      if (parsed.searchParams.get('view') === 'conversation') {
        return new Promise(resolve => {
          releaseHistory = () => resolve({ ok: true, json: async () => ({ events: [], next_sequence: 4, has_more: false, history_details: [newDescriptor] }) })
        })
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
  releaseHistory()
  await nextImmediate()
  releaseCapabilities()
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

test('stop flushes pending events even when capabilities are no longer ready', async t => {
  const previous = globalThis.EventSource
  const sources = []
  globalThis.EventSource = class {
    handlers = new Map()
    constructor() { sources.push(this) }
    addEventListener(name, callback) { this.handlers.set(name, callback) }
    close() {}
  }
  t.after(() => { if (previous === undefined) delete globalThis.EventSource; else globalThis.EventSource = previous })
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    if (url.endsWith('/capabilities')) return { ok: true, json: async () => ({ structured: true }) }
    if (url.includes('/events?')) return { ok: true, json: async () => ({ events: [], next_sequence: -1, has_more: false }) }
    return pendingUntilAbort(options)
  })
  const { stream, unmount } = mountStream()
  t.after(unmount)
  await stream.start('stop-before-ready', 'terminal-tab')
  sources[0].handlers.get('agent-stream')({
    data: JSON.stringify({ stream_sequence: 0, type: 'text_delta', payload: { text: 'pending' } }),
  })
  stream.capabilities.value = null

  stream.stop()
  assert.equal(stream.events.value.length, 1, 'stop must synchronously settle the pending batch')
  await delay(60)
  assert.equal(stream.events.value.length, 1, 'a retired batch must not mutate history later')
})

test('failed retry preserves same-source history and cursor for the next recovery', async t => {
  const descriptor = { turn_id: 'turn-1', first_sequence: 0, last_sequence: 7, event_count: 8 }
  const event = { stream_sequence: 7, type: 'text_delta', turn_id: 'turn-1', payload: { text: 'saved answer' } }
  const historySince = []
  let capabilitiesAvailable = false
  let retryAttempts = 0
  let finishSuccessfulRetry
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    if (url.endsWith('/capabilities')) {
      return { ok: true, json: async () => ({ structured: capabilitiesAvailable, unavailable_reason: capabilitiesAvailable ? null : 'offline' }) }
    }
    if (url.includes('/events?')) {
      const since = Number(new URL(url, 'http://test').searchParams.get('since_sequence'))
      historySince.push(since)
      return { ok: true, json: async () => since < 0
        ? { events: [event], next_sequence: 7, has_more: false, history_details: [descriptor] }
        : { events: [], next_sequence: 7, has_more: false, history_details: [descriptor] } }
    }
    if (url.endsWith('/retry')) {
      retryAttempts += 1
      if (retryAttempts === 1) return { ok: false, status: 503, json: async () => ({ detail: 'still offline' }) }
      return new Promise(resolve => {
        finishSuccessfulRetry = () => {
          capabilitiesAvailable = true
          resolve({ ok: true, json: async () => ({}) })
        }
      })
    }
    return pendingUntilAbort(options)
  })
  const { stream, unmount } = mountStream()
  t.after(unmount)

  await stream.start('retry-history', 'terminal-tab')
  assert.equal(stream.connectionState.value, 'failed')
  assert.equal(stream.historyReady.value, true)

  await stream.retry('retry-history', 'terminal-tab')
  assert.equal(stream.connectionState.value, 'failed')
  assert.equal(stream.errorMessage.value, 'still offline')
  assert.equal(stream.historyReady.value, true)
  assert.deepEqual(stream.events.value, [event])
  assert.deepEqual(stream.historyDetails.value['turn-1'].descriptor, descriptor)

  const recovering = stream.retry('retry-history', 'terminal-tab')
  await nextImmediate()
  stream.historyDetails.value = {
    ...stream.historyDetails.value,
    'turn-1': { ...stream.historyDetails.value['turn-1'], status: 'loaded' },
  }
  finishSuccessfulRetry()
  await recovering
  assert.equal(stream.connectionState.value, 'live')
  assert.deepEqual(historySince, [-1, 7])
  assert.deepEqual(stream.events.value, [event])
  assert.equal(stream.historyDetails.value['turn-1'].status, 'loaded')
})

test('retry never carries history to a different source', async t => {
  let rejectOtherRetry
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    if (url.endsWith('/capabilities')) {
      return { ok: true, json: async () => ({ structured: false, unavailable_reason: 'offline' }) }
    }
    if (url.includes('/events?')) {
      return { ok: true, json: async () => ({
        events: [{ stream_sequence: 0, type: 'text_delta', payload: { text: 'source A' } }],
        next_sequence: 0,
        has_more: false,
      }) }
    }
    if (url.includes('/tabs/source-b/stream/retry')) {
      return new Promise(resolve => { rejectOtherRetry = () => resolve({ ok: false, status: 503, json: async () => ({}) }) })
    }
    return pendingUntilAbort(options)
  })
  const { stream, unmount } = mountStream()
  t.after(unmount)

  await stream.start('source-a', 'terminal-tab')
  const retrying = stream.retry('source-b', 'terminal-tab')
  await nextImmediate()
  assert.equal(stream.historyReady.value, false)
  assert.deepEqual(stream.events.value, [])
  rejectOtherRetry()
  await retrying
})

function pendingUntilAbort(options) {
  return new Promise((_, reject) => {
    if (options.signal.aborted) reject(new Error('aborted'))
    else options.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
  })
}

for (const outcome of ['success', 'unavailable', 'failure']) {
  test(`durable history is visible before delayed capabilities: ${outcome}`, async t => {
    let resolveCapabilities
    const pendingCapabilities = new Promise(resolve => { resolveCapabilities = resolve })
    t.mock.method(globalThis, 'fetch', async (url, options) => {
      if (url.endsWith('/capabilities')) return pendingCapabilities
      if (url.includes('/events?')) {
        assert.equal(new URL(url, 'http://test').searchParams.get('persisted_only'), 'true')
        return { ok: true, json: async () => ({ events: [{ stream_sequence: 0, type: 'text_delta', payload: { text: 'saved answer' } }], next_sequence: 0, has_more: false }) }
      }
      return pendingUntilAbort(options)
    })
    const { stream, unmount } = mountStream()
    t.after(unmount)
    const started = stream.start(`history-first-${outcome}`, 'terminal-tab')
    await nextImmediate()
    assert.equal(stream.historyReady.value, true)
    assert.equal(stream.events.value[0].payload.text, 'saved answer')
    assert.equal(stream.connectionState.value, 'reconciling')
    resolveCapabilities(outcome === 'failure'
      ? { ok: false, status: 503 }
      : { ok: true, json: async () => ({ structured: outcome === 'success', unavailable_reason: outcome === 'unavailable' ? 'offline' : null }) })
    await started
    assert.equal(stream.connectionState.value, outcome === 'success' ? 'live' : 'failed')
    assert.equal(stream.historyReady.value, true)
    assert.equal(stream.events.value[0].payload.text, 'saved answer')
  })
}

test('capability GET is reused for30 minutes and refreshed after configuration or explicit refresh', async t => {
  let clock = 10_000
  let configuration = 'original'
  let capabilityGets = 0
  const urls = []
  t.mock.method(Date, 'now', () => clock)
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    urls.push(url)
    if (url.includes('/capabilities')) {
      capabilityGets += 1
      return { ok: true, json: async () => ({ structured: true, current_model: `model-${capabilityGets}` }) }
    }
    if (url.includes('/events?')) return { ok: true, json: async () => ({ events: [], next_sequence: -1, has_more: false }) }
    return pendingUntilAbort(options)
  })
  const { stream, unmount } = mountStream(() => configuration)
  t.after(unmount)
  await stream.start('capability-reuse', 'terminal-tab')
  clock += 29 * 60 * 1000
  await stream.start('capability-reuse', 'terminal-tab')
  assert.equal(capabilityGets, 1)
  clock += 60 * 1000
  await stream.start('capability-reuse', 'terminal-tab')
  assert.equal(capabilityGets, 2)
  configuration = 'different-provider-env'
  await stream.start('capability-reuse', 'terminal-tab')
  assert.equal(capabilityGets, 3)
  await stream.refreshCapabilities()
  assert.equal(capabilityGets, 4)
  assert.ok(urls.at(-1).endsWith('/capabilities?refresh=true'))
  assert.equal(stream.capabilities.value.current_model, 'model-4')
})

test('unavailable capabilities are retried on revisit rather than cached', async t => {
  let requests = 0
  t.mock.method(globalThis, 'fetch', async url => {
    if (url.includes('/capabilities')) {
      requests += 1
      return { ok: true, json: async () => ({ structured: false, unavailable_reason: 'offline' }) }
    }
    return { ok: true, json: async () => ({ events: [], next_sequence: -1, has_more: false }) }
  })
  const { stream, unmount } = mountStream()
  t.after(unmount)
  await stream.start('no-negative-capability-cache', 'terminal-tab')
  await stream.start('no-negative-capability-cache', 'terminal-tab')
  assert.equal(requests, 2)
  assert.equal(stream.connectionState.value, 'failed')
})

test('late capability refresh cannot overwrite a newer mode change when fetch ignores abort', async t => {
  let resolveRefresh
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url.endsWith('/capabilities?refresh=true')) return new Promise(resolve => { resolveRefresh = resolve })
    if (url.endsWith('/capabilities')) return { ok: true, json: async () => ({ structured: true, current_mode: 'default' }) }
    if (url.endsWith('/mode')) return { ok: true, json: async () => ({ structured: true, current_mode: 'plan' }) }
    if (url.includes('/events?')) return { ok: true, json: async () => ({ events: [], next_sequence: -1, has_more: false }) }
    return pendingUntilAbort(options)
  })
  const { stream, unmount } = mountStream()
  t.after(unmount)
  await stream.start('latest-capabilities-win', 'terminal-tab')
  const refresh = stream.refreshCapabilities()
  await nextImmediate()
  await stream.setMode('plan')
  resolveRefresh({ ok: true, json: async () => ({ structured: true, current_mode: 'default' }) })
  await refresh
  assert.equal(stream.capabilities.value.current_mode, 'plan')
  await stream.start('latest-capabilities-win', 'terminal-tab')
  assert.equal(stream.capabilities.value.current_mode, 'plan')
})

test('an unavailable SSE hello retains readable history and a failed state, and expires capability hints', async t => {
  const previous = globalThis.EventSource
  const sources = []
  globalThis.EventSource = class {
    handlers = new Map()
    constructor() { sources.push(this) }
    addEventListener(name, callback) { this.handlers.set(name, callback) }
    close() {}
  }
  t.after(() => { if (previous === undefined) delete globalThis.EventSource; else globalThis.EventSource = previous })
  let gets = 0
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url.endsWith('/capabilities')) {
      gets += 1
      return { ok: true, json: async () => ({ structured: true }) }
    }
    if (url.includes('/events?')) return { ok: true, json: async () => ({ events: [{ stream_sequence: 0, type: 'text_delta', payload: { text: 'history remains' } }], next_sequence: 0, has_more: false }) }
    return pendingUntilAbort(options)
  })
  const { stream, unmount } = mountStream()
  t.after(unmount)
  await stream.start('failed-live-capability', 'terminal-tab')
  sources[0].handlers.get('hello')({ data: JSON.stringify({ structured: false, unavailable_reason: 'runtime lost' }) })
  assert.equal(stream.connectionState.value, 'failed')
  assert.equal(stream.errorMessage.value, 'runtime lost')
  assert.equal(stream.historyReady.value, true)
  assert.equal(stream.events.value[0].payload.text, 'history remains')
  await stream.start('failed-live-capability', 'terminal-tab')
  assert.equal(gets, 2)
})

test('late capability preparation cannot poison a newer configuration cache', async t => {
  let configuration = 'one'
  let gets = 0
  let resolveOld
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url.endsWith('/capabilities')) {
      gets += 1
      if (gets === 1) return new Promise(resolve => { resolveOld = resolve })
      return { ok: true, json: async () => ({ structured: true, current_model: 'new-model' }) }
    }
    if (url.includes('/events?')) return { ok: true, json: async () => ({ events: [], next_sequence: -1, has_more: false }) }
    return pendingUntilAbort(options)
  })
  const { stream, unmount } = mountStream(() => configuration)
  t.after(unmount)
  const old = stream.start('config-race', 'terminal-tab')
  await nextImmediate()
  configuration = 'two'
  await stream.start('config-race', 'terminal-tab')
  resolveOld({ ok: true, json: async () => ({ structured: true, current_model: 'old-model' }) })
  await old
  await stream.start('config-race', 'terminal-tab')
  assert.equal(gets, 2)
  assert.equal(stream.capabilities.value.current_model, 'new-model')
})

for (const fails of [false, true]) {
  test(`hydration waits for a superseding capability refresh, including failure=${fails}`, async t => {
    let resolveInitial
    let resolveRefresh
    t.mock.method(globalThis, 'fetch', async (url, options) => {
      if (url.endsWith('/capabilities?refresh=true')) return new Promise(resolve => { resolveRefresh = resolve })
      if (url.endsWith('/capabilities')) return new Promise(resolve => { resolveInitial = resolve })
      if (url.includes('/events?')) return { ok: true, json: async () => ({ events: [], next_sequence: -1, has_more: false }) }
      return pendingUntilAbort(options)
    })
    const { stream, unmount } = mountStream()
    t.after(unmount)
    const started = stream.start(`refresh-during-hydration-${fails}`, 'terminal-tab')
    await nextImmediate()
    const refreshed = stream.refreshCapabilities().catch(error => error)
    await nextImmediate()
    resolveInitial({ ok: true, json: async () => ({ structured: true, current_model: 'old' }) })
    await nextImmediate()
    assert.equal(stream.connectionState.value, 'reconciling')
    resolveRefresh(fails ? { ok: false, status: 503 } : { ok: true, json: async () => ({ structured: true, current_model: 'new' }) })
    await Promise.all([started, refreshed])
    assert.equal(stream.connectionState.value, fails ? 'failed' : 'live')
    assert.equal(stream.historyReady.value, true)
    if (!fails) assert.equal(stream.capabilities.value.current_model, 'new')
  })
}

for (const update of ['refresh', 'mode']) {
  test(`a live ${update} returning unavailable disables controls but preserves history`, async t => {
    t.mock.method(globalThis, 'fetch', async (url, options) => {
      if (url.endsWith('/capabilities')) return { ok: true, json: async () => ({ structured: true }) }
      if (url.endsWith('/capabilities?refresh=true') || url.endsWith('/mode')) {
        return { ok: true, json: async () => ({ structured: false, unavailable_reason: 'unavailable after update' }) }
      }
      if (url.includes('/events?')) return { ok: true, json: async () => ({ events: [{ stream_sequence: 0, type: 'text_delta', payload: { text: 'retained answer' } }], next_sequence: 0, has_more: false }) }
      return pendingUntilAbort(options)
    })
    const { stream, unmount } = mountStream()
    t.after(unmount)
    await stream.start(`unavailable-${update}-result`, 'terminal-tab')
    assert.equal(stream.connectionState.value, 'live')
    await assert.rejects(update === 'mode' ? stream.setMode('plan') : stream.refreshCapabilities(), /unavailable after update/)
    assert.equal(stream.connectionState.value, 'failed')
    assert.equal(stream.historyReady.value, true)
    assert.equal(stream.events.value[0].payload.text, 'retained answer')
  })
}

test('history imported by cold capability preparation is caught up in compacted form before live', async t => {
  let finishPreparation
  let imported = false
  let historyReads = 0
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url.endsWith('/capabilities')) return new Promise(resolve => {
      finishPreparation = () => { imported = true; resolve({ ok: true, json: async () => ({ structured: true }) }) }
    })
    if (url.includes('/events?')) {
      historyReads += 1
      assert.match(url, /persisted_only=true/)
      assert.match(url, /view=conversation/)
      return { ok: true, json: async () => imported
        ? { events: [{ stream_sequence: 6000, type: 'text_delta', turn_id: 'imported', payload: { text: 'folded historical answer' } }], next_sequence: 6000, has_more: false, history_details: [{ turn_id: 'imported', first_sequence: 0, last_sequence: 6000, event_count: 6001 }] }
        : { events: [], next_sequence: -1, has_more: false } }
    }
    assert.equal(JSON.parse(options.body).since_sequence, 6000)
    return pendingUntilAbort(options)
  })
  const { stream, unmount } = mountStream()
  t.after(unmount)
  const started = stream.start('cold-transcript-import', 'terminal-tab')
  await nextImmediate()
  assert.equal(stream.historyReady.value, true)
  finishPreparation()
  await started
  assert.equal(historyReads, 2)
  assert.equal(stream.connectionState.value, 'live')
  assert.equal(stream.events.value.length, 1)
  assert.equal(stream.historyDetails.value.imported.status, 'idle')
})
