import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { getEventListeners } from 'node:events'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { computed, createRenderer, nextTick, ref, watch } from 'vue'
import ts from 'typescript'

const transpileOptions = {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}

const moduleUrls = new Map()
async function moduleUrl(path) {
  const rel = path.replace(/\.ts$/, '')
  if (moduleUrls.has(rel)) return moduleUrls.get(rel)
  const source = await readFile(new URL(`../src/${rel}.ts`, import.meta.url), 'utf8')
  let { outputText } = ts.transpileModule(source, transpileOptions)
  for (const [, specifier] of outputText.matchAll(/from ['"]([^'"]+)['"]/g)) {
    const url = specifier.startsWith('@/')
      ? await moduleUrl(specifier.slice(2))
      : import.meta.resolve(specifier)
    outputText = outputText
      .replaceAll(`'${specifier}'`, JSON.stringify(url))
      .replaceAll(`"${specifier}"`, JSON.stringify(url))
  }
  const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
  moduleUrls.set(rel, url)
  return url
}

const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {},
})

async function mountStream() {
  const { useAgentStream } = await import(await moduleUrl('composables/useAgentStream'))
  let stream
  const app = renderer.createApp({
    setup() {
      stream = useAgentStream()
      return () => null
    },
  })
  app.mount({})
  return { stream, unmount: () => app.unmount() }
}

async function waitFor(predicate, message) {
  const deadline = Date.now() + 3000
  while (Date.now() < deadline) {
    if (predicate()) return
    await new Promise(resolve => setTimeout(resolve, 5))
  }
  throw new Error(message)
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

test('a late retry response cannot restart a stream after deactivate and reactivate', async t => {
  const retryResponse = deferred()
  let retryCalls = 0
  let capabilityCalls = 0
  let waitCalls = 0

  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url.endsWith('/retry')) {
      retryCalls += 1
      return retryResponse.promise
    }
    if (url.endsWith('/capabilities')) {
      capabilityCalls += 1
      return { ok: true, json: async () => ({ structured: true }) }
    }
    if (url.includes('/events?')) {
      return {
        ok: true,
        json: async () => ({ events: [], next_sequence: -1, has_more: false }),
      }
    }
    if (url.endsWith('/wait')) {
      waitCalls += 1
      return new Promise((_, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
      })
    }
    throw new Error(`unexpected URL ${url}`)
  })

  const { stream, unmount } = await mountStream()
  t.after(unmount)

  const staleRetry = stream.retry('tab-a', 'terminal-tab')
  await waitFor(() => retryCalls === 1, 'retry request did not start')

  // KeepAlive deactivation stops this instance. A later activation starts a
  // new generation for the same tab before the old retry request returns.
  stream.stop()
  await stream.start('tab-a', 'terminal-tab')
  await waitFor(() => stream.connectionState.value === 'live' && waitCalls === 1, 'reactivated stream did not start')

  retryResponse.resolve({ ok: true, json: async () => ({}) })
  await staleRetry
  await new Promise(resolve => setTimeout(resolve, 0))

  assert.equal(capabilityCalls, 1, 'the stale retry must not start a second hydration')
  assert.equal(waitCalls, 1, 'the stale retry must not replace the active live poll')
  assert.equal(stream.connectionState.value, 'live')
})

test('long-poll removes the parent abort listener after every normal request', async t => {
  const NativeAbortController = globalThis.AbortController
  const controllers = []
  class TrackingAbortController extends NativeAbortController {
    constructor() {
      super()
      controllers.push(this)
    }
  }
  globalThis.AbortController = TrackingAbortController
  t.after(() => { globalThis.AbortController = NativeAbortController })

  let waitCalls = 0
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url.endsWith('/capabilities')) {
      return { ok: true, json: async () => ({ structured: true }) }
    }
    if (url.includes('/events?')) {
      return {
        ok: true,
        json: async () => ({ events: [], next_sequence: -1, has_more: false }),
      }
    }
    if (url.endsWith('/wait')) {
      waitCalls += 1
      if (waitCalls < 3) {
        return { ok: true, json: async () => ({ events: [], next_sequence: -1, has_more: false }) }
      }
      return new Promise((_, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
      })
    }
    throw new Error(`unexpected URL ${url}`)
  })

  const { stream, unmount } = await mountStream()
  t.after(unmount)
  await stream.start('listener-test', 'terminal-tab')
  await waitFor(() => waitCalls === 3, 'three wait requests did not run')

  const abortListenerCounts = controllers
    .filter(controller => !controller.signal.aborted)
    .map(controller => getEventListeners(controller.signal, 'abort').length)
  assert.equal(
    Math.max(...abortListenerCounts),
    1,
    'only the current wait request may remain attached to the generation abort signal',
  )

  stream.stop()
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(
    Math.max(0, ...controllers.map(controller => getEventListeners(controller.signal, 'abort').length)),
    0,
    'stop must remove the remaining abort listener',
  )
})

function extractFunction(source, name) {
  const asyncStart = source.indexOf(`async function ${name}(`)
  const syncStart = source.indexOf(`function ${name}(`)
  const start = asyncStart >= 0 ? asyncStart : syncStart
  assert.ok(start >= 0, `${name} must exist`)
  const brace = source.indexOf('{', start)
  let depth = 0
  for (let index = brace; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1
    else if (source[index] === '}') {
      depth -= 1
      if (depth === 0) return source.slice(start, index + 1)
    }
  }
  throw new Error(`could not extract ${name}`)
}

async function loadComposerEntries() {
  const source = await readFile(
    new URL('../src/components/StructuredPane.vue', import.meta.url),
    'utf8',
  )
  const snippet = [
    extractFunction(source, 'sendToStream'),
    extractFunction(source, 'submit'),
    extractFunction(source, 'suspendStreamForDeactivation'),
    extractFunction(source, 'flushDraftQueue'),
    extractFunction(source, 'requestDraftQueueFlush'),
  ].join('\n')
  const { outputText } = ts.transpileModule(snippet, transpileOptions)
  const { toStreamSendAttachment } = await import(await moduleUrl('utils/agentStreamAttachments'))
  const factory = new Function(
    'deps',
    'toStreamSendAttachment',
    `const { isSending, goalComposerLocked, goalInputBlocked, goalSteerOnly, composerError, goalComposerReason, draftMessage, attachments, turnInFlight, optimisticCancelledTurnId, pendingDirectTurns, requestLatestAnchor, hydrateGoal, terminalStore, nextTick, syncComposerTextareaHeight, draftQueue, connectionState, isSubmissionReady, isDraftQueuePaused, isPaneActivated, isQueueSendInProgress, queueAutoActivationBlock, isWaitingForQueuedChatActivation, canSend, stop, props, streamBasePath, isManagedChat } = deps;\n${outputText}\nreturn { submit, suspendStreamForDeactivation, flushDraftQueue, requestDraftQueueFlush };`,
  )
  return (deps) => factory(deps, toStreamSendAttachment)
}

function composerDeps(options = {}) {
  const isSending = ref(false)
  const goalComposerLocked = ref(false)
  const goalSteerOnly = ref(false)
  const goalInputBlocked = computed(() => goalComposerLocked.value && !goalSteerOnly.value)
  const composerError = ref(null)
  const goalComposerReason = ref(null)
  const draftMessage = ref(options.draftMessage ?? 'hello')
  const attachments = ref([])
  const pendingDirectTurns = ref([])
  const authoritativeTurnInFlight = ref(false)
  const turnInFlight = computed(() => (
    authoritativeTurnInFlight.value || pendingDirectTurns.value.length > 0
  ))
  const draftQueue = ref([])
  const connectionState = ref(options.connectionState ?? 'live')
  const isPaneActivated = ref(options.isPaneActivated ?? true)
  const activationCalls = []
  let activationPending = false
  let activationOwner = false
  let stopCalls = 0
  const isPreparingAttachments = ref(options.isPreparingAttachments ?? false)
  const isUpdatingMode = ref(options.isUpdatingMode ?? false)
  const isUpdatingModel = ref(options.isUpdatingModel ?? false)
  const isUpdatingReasoningEffort = ref(options.isUpdatingReasoningEffort ?? false)
  const isSubmissionReady = computed(() => connectionState.value === 'live'
    && !isPreparingAttachments.value
    && !isUpdatingMode.value
    && !isUpdatingModel.value
    && !isUpdatingReasoningEffort.value)
  const canSend = computed(() => isSubmissionReady.value
    && !goalComposerLocked.value
    && (draftMessage.value.trim().length > 0 || attachments.value.length > 0))

  return {
    isSending,
    goalComposerLocked,
    goalSteerOnly,
    goalInputBlocked,
    composerError,
    goalComposerReason,
    draftMessage,
    attachments,
    turnInFlight,
    authoritativeTurnInFlight,
    optimisticCancelledTurnId: ref(null),
    pendingDirectTurns,
    requestLatestAnchor() {},
    async hydrateGoal() {},
    terminalStore: {
      async fetchAgentStatuses() {},
      requestQueuedChatActivation(paneId, tabId) {
        activationPending = true
        activationOwner = true
        activationCalls.push(`pane:${paneId}`)
        activationCalls.push(`tab:${tabId}`)
      },
      releaseQueuedChatActivation() {
        activationPending = false
        activationOwner = false
      },
      settleQueuedChatActivation() {
        activationPending = false
        activationOwner = false
      },
      hasQueuedChatActivationRequest() { return activationPending },
      isQueuedChatActivationOwner() { return activationOwner },
      queuedChatActivationRequestId() { return activationPending ? 1 : null },
    },
    nextTick,
    syncComposerTextareaHeight() {},
    draftQueue,
    connectionState,
    isPaneActivated,
    isQueueSendInProgress: ref(false),
    queueAutoActivationBlock: ref(null),
    isWaitingForQueuedChatActivation: computed(() => activationPending && !activationOwner),
    activationCalls,
    stop() { stopCalls += 1 },
    getStopCalls() { return stopCalls },
    isPreparingAttachments,
    isUpdatingMode,
    isUpdatingModel,
    isUpdatingReasoningEffort,
    isSubmissionReady,
    isDraftQueuePaused: ref(false),
    canSend,
    props: { tabId: 'tab-a', paneId: 'pane-a' },
    streamBasePath: ref('/api/workspaces/tabs/tab-a/stream'),
    isManagedChat: ref(false),
  }
}

function watchDraftQueue(deps, requestDraftQueueFlush) {
  return watch(
    [
      deps.turnInFlight,
      () => deps.draftQueue.value.length,
      deps.isSending,
      deps.goalComposerLocked,
      deps.isSubmissionReady,
      deps.isDraftQueuePaused,
      deps.queueAutoActivationBlock,
      deps.isPaneActivated,
      deps.isWaitingForQueuedChatActivation,
    ],
    requestDraftQueueFlush,
  )
}

test('submit entry refuses Enter-driven sends while the composer is unavailable', async t => {
  const makeEntries = await loadComposerEntries()
  let sendCalls = 0
  t.mock.method(globalThis, 'fetch', async () => {
    sendCalls += 1
    return { ok: true, json: async () => ({}) }
  })
  const deps = composerDeps({ connectionState: 'reconciling' })
  const { submit } = makeEntries(deps)

  assert.equal(deps.canSend.value, false)
  assert.equal(await submit('normal'), false)
  assert.equal(sendCalls, 0)
  assert.equal(deps.draftMessage.value, 'hello')
  assert.equal(deps.pendingDirectTurns.value.length, 0)
})

test('queue watcher waits for live recovery before sending', async t => {
  const makeEntries = await loadComposerEntries()
  let sendCalls = 0
  t.mock.method(globalThis, 'fetch', async () => {
    sendCalls += 1
    return { ok: true, json: async () => ({}) }
  })
  const deps = composerDeps({ draftMessage: '', connectionState: 'reconciling' })
  const entries = makeEntries(deps)
  const unwatch = watchDraftQueue(deps, entries.requestDraftQueueFlush)
  t.after(unwatch)

  deps.draftQueue.value = [{ message: 'queued while hidden', attachments: [] }]
  await nextTick()
  assert.equal(sendCalls, 0)
  assert.equal(deps.draftQueue.value.length, 1)
  assert.equal(deps.draftMessage.value, '')

  deps.connectionState.value = 'live'
  await waitFor(() => sendCalls === 1, 'queue did not resume after the stream became live')
  assert.equal(deps.draftQueue.value.length, 0)
  assert.equal(deps.pendingDirectTurns.value.length, 1)
})

test('a foreground queued Chat flushes once after activation ownership arrives', async t => {
  const makeEntries = await loadComposerEntries()
  let sendCalls = 0
  t.mock.method(globalThis, 'fetch', async () => {
    sendCalls += 1
    return { ok: true, json: async () => ({}) }
  })
  const deps = composerDeps({
    draftMessage: '',
    connectionState: 'live',
    isPaneActivated: true,
  })
  const entries = makeEntries(deps)
  const unwatch = watchDraftQueue(deps, entries.requestDraftQueueFlush)
  t.after(unwatch)

  deps.draftQueue.value = [{ message: 'queued for hidden Chat', attachments: [] }]
  await nextTick()

  await waitFor(() => sendCalls === 1, 'queue did not flush after its Chat activated')
  assert.deepEqual(deps.activationCalls, [], 'the foreground pane must not request activation')
  assert.equal(deps.draftQueue.value.length, 0)
  assert.equal(deps.pendingDirectTurns.value.length, 1)
})

test('deactivation always releases the full Chat stream to the minimal queue observer', async () => {
  const makeEntries = await loadComposerEntries()

  const queued = composerDeps({ draftMessage: '' })
  queued.draftQueue.value = [{ message: 'wait for current turn', attachments: [] }]
  makeEntries(queued).suspendStreamForDeactivation()
  assert.equal(queued.getStopCalls(), 1, 'the full hidden pane must release its stream')

  queued.isDraftQueuePaused.value = true
  makeEntries(queued).suspendStreamForDeactivation()
  assert.equal(queued.getStopCalls(), 2, 'a paused queue must also release its full stream')

  const empty = composerDeps({ draftMessage: '' })
  makeEntries(empty).suspendStreamForDeactivation()
  assert.equal(empty.getStopCalls(), 1, 'an empty hidden Chat must release its stream')
})

test('an inactive full pane releases activation ownership without sending', async () => {
  const makeEntries = await loadComposerEntries()
  const deps = composerDeps({
    draftMessage: '',
    connectionState: 'live',
    isPaneActivated: false,
  })
  deps.draftQueue.value = [{ message: 'do not retry implicitly', attachments: [] }]
  const entries = makeEntries(deps)
  const unwatch = watchDraftQueue(deps, entries.requestDraftQueueFlush)

  deps.terminalStore.requestQueuedChatActivation('pane-a', 'tab-a')
  await nextTick()
  unwatch()

  assert.equal(deps.getStopCalls(), 0)
  assert.deepEqual(deps.activationCalls, ['pane:pane-a', 'tab:tab-a'])
  assert.equal(deps.draftQueue.value.length, 1)
})

test('a permanent foreground stream failure yields FIFO ownership without dropping the queue', async () => {
  const makeEntries = await loadComposerEntries()
  const deps = composerDeps({ draftMessage: '', connectionState: 'failed' })
  deps.draftQueue.value = [{ message: 'preserve for retry', attachments: [] }]
  deps.terminalStore.requestQueuedChatActivation('pane-a', 'tab-a')
  const entries = makeEntries(deps)

  entries.requestDraftQueueFlush()

  assert.equal(deps.queueAutoActivationBlock.value, 'stream-failed')
  assert.equal(deps.isWaitingForQueuedChatActivation.value, false)
  assert.equal(deps.draftQueue.value.length, 1)
})

test('queue watcher preserves every item through attachment and model updates', async t => {
  const makeEntries = await loadComposerEntries()
  const sentMessages = []
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    sentMessages.push(JSON.parse(options.body).text)
    return { ok: true, json: async () => ({}) }
  })
  const deps = composerDeps({
    draftMessage: '',
    isPreparingAttachments: true,
    isUpdatingModel: true,
  })
  const entries = makeEntries(deps)
  const unwatch = watchDraftQueue(deps, entries.requestDraftQueueFlush)
  t.after(unwatch)

  deps.draftQueue.value = [
    { message: 'first queued', attachments: [] },
    { message: 'second queued', attachments: [] },
  ]
  await nextTick()
  assert.deepEqual(sentMessages, [])
  assert.deepEqual(deps.draftQueue.value.map(item => item.message), ['first queued', 'second queued'])
  assert.equal(deps.draftMessage.value, '')

  deps.isPreparingAttachments.value = false
  await nextTick()
  assert.deepEqual(sentMessages, [], 'model update must keep the queue paused')
  assert.deepEqual(deps.draftQueue.value.map(item => item.message), ['first queued', 'second queued'])

  deps.isUpdatingModel.value = false
  await waitFor(() => sentMessages.length === 1, 'queue did not resume after updates finished')
  assert.deepEqual(sentMessages, ['first queued'])
  assert.deepEqual(deps.draftQueue.value.map(item => item.message), ['second queued'])
})

test('attachment work cannot clear the durable pause after an uncertain queued send', async t => {
  const makeEntries = await loadComposerEntries()
  let sendCalls = 0
  t.mock.method(globalThis, 'fetch', async () => {
    sendCalls += 1
    if (sendCalls === 1) throw new Error('response lost after submit')
    return { ok: true, json: async () => ({}) }
  })
  const deps = composerDeps({ draftMessage: '' })
  const entries = makeEntries(deps)
  const unwatch = watchDraftQueue(deps, entries.requestDraftQueueFlush)
  t.after(unwatch)

  deps.draftQueue.value = [
    { message: 'possibly accepted first', attachments: [] },
    { message: 'definitely not attempted second', attachments: [] },
  ]
  await waitFor(
    () => deps.isDraftQueuePaused.value && !deps.isSending.value,
    'failed queued send did not pause the queue',
  )
  assert.equal(sendCalls, 1)
  assert.equal(deps.draftMessage.value, 'possibly accepted first')
  assert.deepEqual(deps.draftQueue.value.map(item => item.message), ['definitely not attempted second'])

  // addFiles clears a generic composer error before preparing an attachment.
  // That unrelated mutation must not be interpreted as resolving uncertain
  // delivery, even after attachment preparation finishes.
  deps.composerError.value = null
  deps.isPreparingAttachments.value = true
  deps.attachments.value.push({ filename: 'evidence.png' })
  await nextTick()
  deps.isPreparingAttachments.value = false
  await nextTick()
  await new Promise(resolve => setTimeout(resolve, 0))

  assert.equal(sendCalls, 1)
  assert.equal(deps.draftMessage.value, 'possibly accepted first')
  assert.deepEqual(deps.draftQueue.value.map(item => item.message), ['definitely not attempted second'])
  assert.equal(deps.isDraftQueuePaused.value, true)

  // Only an explicit successful resend resolves the uncertain item. The next
  // queued item remains behind its newly active turn.
  assert.equal(await entries.submit('normal'), true)
  assert.equal(sendCalls, 2)
  assert.equal(deps.isDraftQueuePaused.value, false)
  assert.deepEqual(deps.draftQueue.value.map(item => item.message), ['definitely not attempted second'])
})

test('a hanging automatic queue POST times out into the delivery-uncertain pause', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const makeEntries = await loadComposerEntries()
  t.mock.method(globalThis, 'fetch', async (_url, options) =>
    new Promise((_, reject) => {
      options.signal.addEventListener('abort', () => {
        const error = new Error('aborted')
        error.name = 'AbortError'
        reject(error)
      }, { once: true })
    })
  )
  const deps = composerDeps({ draftMessage: '' })
  deps.draftQueue.value = [{ message: 'timeout safely', attachments: [] }]
  const entries = makeEntries(deps)

  const flushing = entries.flushDraftQueue()
  t.mock.timers.tick(15_000)
  await flushing

  assert.equal(deps.isDraftQueuePaused.value, true)
  assert.equal(deps.draftMessage.value, 'timeout safely')
  assert.match(deps.composerError.value, /Delivery is uncertain/)
})
