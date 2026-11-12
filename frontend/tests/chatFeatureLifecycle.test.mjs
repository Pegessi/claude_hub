import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { setImmediate } from 'node:timers'
import test from 'node:test'
import ts from 'typescript'
import * as vue from 'vue'

const require = createRequire(import.meta.url)
const { parse, compileScript } = require('vue/compiler-sfc')
const root = new URL('../src/', import.meta.url)
const emptyComponent = `import { defineComponent, h } from 'vue'; export default defineComponent({ inheritAttrs: false, setup: () => () => null })`

function moduleLoader(stubs) {
  const urls = new Map()
  function urlFor(path) {
    if (urls.has(path)) return urls.get(path)
    const file = new URL(path, root)
    let source = stubs[path] ?? readFileSync(file, 'utf8')
    if (path.endsWith('.vue') && !stubs[path]) {
      const descriptor = parse(source, { filename: path }).descriptor
      const script = compileScript(descriptor, { id: path, inlineTemplate: true })
      source = script.content
    }
    let { outputText } = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
    })
    for (const [, specifier] of outputText.matchAll(/from\s+['"]([^'"]+)['"]/g)) {
      let target
      if (specifier.startsWith('@/')) target = specifier.slice(2)
      else if (specifier.startsWith('.')) target = new URL(specifier, file).pathname.slice(root.pathname.length)
      const resolved = target
        ? urlFor(/\.(vue|ts)$/.test(target) ? target : `${target}.ts`)
        : import.meta.resolve(specifier)
      outputText = outputText.replaceAll(`'${specifier}'`, JSON.stringify(resolved)).replaceAll(`"${specifier}"`, JSON.stringify(resolved))
    }
    const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
    urls.set(path, url)
    return url
  }
  return async path => import(urlFor(path))
}

const flush = async () => { await vue.nextTick(); await new Promise(resolve => setImmediate(resolve)); await vue.nextTick() }

function runtime(t) {
  const restore = []
  function global(name, value) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name)
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
    restore.push(() => {
      if (previous) Object.defineProperty(globalThis, name, previous)
      else delete globalThis[name]
    })
  }
  const state = {
    calls: [], streams: [], queues: new Map(), controllers: [], imageRead: null,
    app: vue.reactive({ isMobile: false }),
    pool: vue.reactive({ pool: null, bots: [], activeBotForTab: () => null }),
  }
  const store = vue.reactive({
    tabs: [{ id: 'one', agent_type: 'codex', env: {} }, { id: 'two', agent_type: 'codex', env: {} }],
    archivedTabs: [],
    agentStatuses: [],
    getQueuedChatState(paneId, tabId) {
      const key = `${paneId}:${tabId}`
      if (!state.queues.has(key)) state.queues.set(key, vue.reactive({ drafts: [], editing: false, editingDraftId: null, editingMessage: '', sendInProgress: false, paused: false, autoActivationBlock: null }))
      return state.queues.get(key)
    },
    hasQueuedChatActivationRequest: () => false,
    isQueuedChatActivationOwner: () => false,
    releaseQueuedChatActivation: (...args) => state.calls.push(['release', ...args]),
    queuedChatActivationRequestId: () => null,
    settleQueuedChatActivation: (...args) => state.calls.push(['settle', ...args]),
    pruneQueuedChatState: (...args) => state.calls.push(['prune', ...args]),
    fetchAgentStatuses: async () => {},
    switchEnv: async () => {},
    forkTab: async () => {},
  })
  state.store = store
  global('__chatLifecycleTest', state)
  const listeners = { window: new Map(), document: new Map() }
  const listen = scope => ({
    addEventListener(type, fn) { const set = scope.get(type) ?? new Set(); set.add(fn); scope.set(type, set) },
    removeEventListener(type, fn) { scope.get(type)?.delete(fn) },
  })
  global('window', {
    ...listen(listeners.window), innerWidth: 1200, setTimeout, clearTimeout,
    location: { href: 'http://hub/?tab=one' },
    history: {
      state: null,
      pushState(value) { this.state = value },
      back() { state.calls.push(['history.back']) },
    },
  })
  global('Element', class {})
  global('HTMLElement', class {})
  class Document {}
  class ShadowRoot {}
  global('Document', Document)
  global('ShadowRoot', ShadowRoot)
  global('document', Object.assign(new Document(), { ...listen(listeners.document), activeElement: null }))
  let frame = 0
  const frames = new Map()
  global('requestAnimationFrame', fn => { frames.set(++frame, fn); return frame })
  global('cancelAnimationFrame', id => frames.delete(id))
  global('ResizeObserver', class {
    constructor(callback) { this.callback = callback; this.observed = []; this.disconnected = false; state.controllers.push(this) }
    observe(node) { this.observed.push(node) }
    disconnect() { this.disconnected = true }
  })
  const response = new Response(null, { status: 204 })
  t.mock.method(globalThis, 'fetch', async (url, init = {}) => {
    state.calls.push(['fetch', url, init])
    return response.clone()
  })
  return { state, listeners, frames, restore: () => restore.reverse().forEach(fn => fn()) }
}

const stubs = {
  'stores/terminalStore.ts': `export const useTerminalStore = () => globalThis.__chatLifecycleTest.store`,
  'stores/appStore.ts': `export const useAppStore = () => globalThis.__chatLifecycleTest.app`,
  'stores/feishuBotPoolStore.ts': `export const useFeishuBotPoolStore = () => globalThis.__chatLifecycleTest.pool`,
  'composables/useAgentStream.ts': `
    import { ref } from 'vue'
    export function useAgentStream() {
      const events = ref([]), connectionState = ref('idle'), errorMessage = ref(null), historyReady = ref(false)
      const historyDetails = ref({}), historyDetailRevision = ref(0), attributedToolIds = ref(new Set())
      const capabilities = ref({ available_models: [], current_model: null, current_reasoning_effort: null, available_modes: [], current_mode: null, supports_images: true, supports_goals: true })
      const start = async (id, source) => { globalThis.__chatLifecycleTest.calls.push(['start', id, source]); historyReady.value = true; connectionState.value = 'live' }
      const stop = () => globalThis.__chatLifecycleTest.calls.push(['stop'])
      const loadHistoryDetail = async id => {
        globalThis.__chatLifecycleTest.calls.push(['loadHistoryDetail', id])
        // A read that lands marks the entry loaded in the real stream; the
        // pane's backfill budget is charged on that landing, not on the request.
        const entry = historyDetails.value[id]
        if (entry && entry.status !== 'loaded') {
          historyDetails.value = { ...historyDetails.value, [id]: { ...entry, status: 'loaded' } }
        }
      }
      const stream = { events, historyReady, historyDetails, historyDetailRevision, attributedToolIds, connectionState, errorMessage, capabilities, start, stop, loadHistoryDetail, retry: start, refreshCapabilities: async () => {}, setMode: async () => {}, nudge() {}, reset() {} }
      globalThis.__chatLifecycleTest.streams.push(stream)
      return stream
    }
    export const validateImageAttachment = () => null
    export const fileToDataUrl = () => globalThis.__chatLifecycleTest.imageRead
    export const generatePreviewDataUrl = async () => 'data:image/png;base64,eA=='
  `,
  'components/MarkdownContent.vue': emptyComponent,
  'components/FeishuIdentityMark.vue': emptyComponent,
  'components/ChatDraftQueue.vue': emptyComponent,
  'components/GoalSetupDialog.vue': emptyComponent,
  'components/ComposerAddMenu.vue': emptyComponent,
  'components/GoalStatusBar.vue': emptyComponent,
  'components/ThinkingIndicator.vue': emptyComponent,
}

function renderer() {
  const create = (tag, text) => ({ tag, text, props: {}, children: [], parent: null, style: {}, scrollTop: 0, scrollHeight: 600, clientHeight: 500,
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, removeAttribute() {}, getRootNode() { return globalThis.document },
    focus() {}, querySelectorAll() { return [] }, getBoundingClientRect() { return { top: 0, bottom: 500 } },
    showModal() { this.open = true }, close() { this.open = false } })
  const body = create('body')
  return vue.createRenderer({
    createElement: tag => create(tag), createText: text => create('#text', text), createComment: text => create('#comment', text),
    setText(node, text) { node.text = text }, setElementText(node, text) { node.text = text },
    patchProp(node, name, _old, value) { node.props[name] = value },
    insert(node, parent, anchor = null) {
      if (node.parent) { const i = node.parent.children.indexOf(node); if (i >= 0) node.parent.children.splice(i, 1) }
      node.parent = parent
      const index = anchor ? parent.children.indexOf(anchor) : -1
      if (index >= 0) parent.children.splice(index, 0, node); else parent.children.push(node)
    },
    remove(node) { const i = node.parent?.children.indexOf(node); if (i >= 0) node.parent.children.splice(i, 1); node.parent = null },
    parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    querySelector: () => body,
  })
}

function collectInstances(vnode, result = []) {
  if (!vnode) return result
  if (vnode.component) { result.push(vnode.component); collectInstances(vnode.component.subTree, result) }
  if (Array.isArray(vnode.children)) vnode.children.forEach(child => collectInstances(child, result))
  return result
}

async function mount(t) {
  const env = runtime(t)
  const load = moduleLoader(stubs)
  const direct = (await load('features/chat/StructuredPane.vue')).default
  const legacy = (await load('components/StructuredPane.vue')).default
  assert.equal(legacy, direct, 'the compatibility SFC exports the same component object')
  const selected = vue.ref('one')
  const app = renderer().createApp({ setup: () => () => vue.h(vue.KeepAlive, { max: 2 }, [vue.h(legacy, { key: selected.value, tabId: selected.value, paneId: 'pane' })]) })
  const warnings = []
  app.config.warnHandler = message => warnings.push(message)
  const vm = app.mount({ children: [], parent: null })
  let unmounted = false
  const unmount = () => { if (!unmounted) { unmounted = true; app.unmount() } }
  t.after(async () => { unmount(); await flush(); env.restore() })
  await flush()
  const keys = await load('features/chat/owners.ts')
  const active = () => collectInstances(vm.$.subTree).find(i => i.type === direct)
  const owner = (key) => active().provides[key]
  return { ...env, keys, load, selected, owner, active, warnings, app, vm, unmount }
}

test('feature facade preserves KeepAlive identity, DOM refs and owner instances across tab visits', async t => {
  const h = await mount(t)
  const first = h.active()
  const timeline = h.owner(h.keys.chatTimelineKey)
  const delivery = h.owner(h.keys.chatDeliveryKey)
  const attachments = h.owner(h.keys.chatAttachmentsKey)
  const queue = h.owner(h.keys.chatQueueKey)
  assert.equal(timeline.timelineEl.value.tag, 'div', 'child template writes into the viewport owner ref')
  assert.equal(h.owner(h.keys.chatComposerKey).composerTextareaEl.value.tag, 'textarea')
  assert.equal(timeline.timelinePhase.value, 'revealed')
  assert.equal(h.state.streams.length, 1)
  assert.equal(queue.queuedChatState, h.state.queues.get('pane:one'))
  delivery.draftMessage.value = 'preserve this draft'
  timeline.visibleHistoryStart.value = 12
  h.selected.value = 'two'
  await flush()
  assert.equal(timeline.timelineDisposed, true)
  assert.equal(h.state.streams.length, 2)
  assert.equal(h.owner(h.keys.chatDeliveryKey).draftMessage.value, '')
  h.selected.value = 'one'
  await flush()
  assert.ok(h.active() === first, 'KeepAlive must restore the existing pane instance')
  assert.ok(h.owner(h.keys.chatTimelineKey) === timeline, 'The pane must retain its timeline owner')
  assert.ok(h.owner(h.keys.chatDeliveryKey) === delivery, 'The pane must retain its delivery owner')
  assert.ok(h.owner(h.keys.chatAttachmentsKey) === attachments, 'The pane must retain its attachment owner')
  assert.equal(delivery.draftMessage.value, 'preserve this draft')
  assert.equal(timeline.visibleHistoryStart.value, null)
  assert.equal(timeline.timelineDisposed, false)
  assert.deepEqual(h.warnings, [])
})

test('a deactivated pane cannot flush its store-owned queue and teardown invalidates attachment work', async t => {
  const h = await mount(t)
  const attachments = h.owner(h.keys.chatAttachmentsKey)
  const timeline = h.owner(h.keys.chatTimelineKey)
  const queue = h.owner(h.keys.chatQueueKey)
  let resolveRead
  h.state.imageRead = new Promise(resolve => { resolveRead = resolve })
  const upload = attachments.addFiles([{ name: 'image.png', type: 'image/png', size: 1 }])
  assert.equal(attachments.isPreparingAttachments.value, true)
  h.selected.value = 'two'
  await flush()
  queue.draftQueue.value.push({ id: 'queued', message: 'must not send while hidden', attachments: [] })
  await flush()
  assert.equal(h.state.calls.filter(c => c[0] === 'fetch' && c[1].endsWith('/send')).length, 0)
  const epoch = attachments.preparationEpoch.value
  h.unmount()
  await flush()
  resolveRead('data:image/png;base64,eA==')
  await upload
  assert.ok(attachments.preparationEpoch.value > epoch)
  assert.equal(attachments.attachments.value.length, 0)
  assert.equal(timeline.timelineDisposed, true)
  assert.ok(h.state.controllers.every(observer => observer.disconnected))
  assert.equal(h.frames.size, 0)
  assert.ok([...h.listeners.window.values(), ...h.listeners.document.values()].every(set => set.size === 0))
})

function renderedText(node) {
  if (!node) return ''
  return `${node.text ?? ''}${(node.children ?? []).map(renderedText).join('')}`
}

test('mutable standalone subagent results invalidate their leaf without remounting the card', async t => {
  const h = await mount(t)
  const stream = h.state.streams[0]
  const event = (sequence, type, payload = {}, extra = {}) => ({
    stream_sequence: sequence, type, payload, session_id: 's', tab_id: 'one',
    turn_id: 'turn', agent_type: 'claude', created_at: '2026-10-16T10:00:00Z', redacted: false, ...extra,
  })
  stream.events.value = [
    event(1, 'turn_started', { summary: 'delegate' }),
    event(2, 'tool_call_started', { tool_call_id: 'child', name: 'Agent', args: { prompt: 'inspect', description: 'Inspect', subagent_type: 'Explore' } }),
  ]
  await flush()
  const cardType = (await h.load('features/chat/components/ChatSubagentCard.vue')).default
  const first = collectInstances(h.vm.$.subTree).find(instance => instance.type === cardType)
  assert.ok(first)
  assert.match(renderedText(first.subTree.el), /Running/)
  stream.events.value = [...stream.events.value, event(3, 'tool_call_completed', { tool_call_id: 'child', status: 'completed', result: 'child finished' })]
  await flush()
  const current = collectInstances(h.vm.$.subTree).find(instance => instance.type === cardType)
  assert.ok(current === first, 'A result update must retain the existing subagent card instance')
  assert.match(renderedText(current.subTree.el), /Returned/)
  assert.match(renderedText(current.subTree.el), /child finished/)
})

test('timeline child, tool and approval updates keep their keyed renderers and viewer state', async t => {
  const h = await mount(t)
  const stream = h.state.streams[0]
  const event = (sequence, type, payload = {}, extra = {}) => ({
    stream_sequence: sequence, type, payload, session_id: 's', tab_id: 'one', turn_id: 'turn',
    agent_type: 'traex', created_at: '2026-10-16T10:00:00Z', redacted: false, ...extra,
  })
  stream.events.value = [
    event(1, 'turn_started', { summary: 'inspect' }),
    event(2, 'tool_call_started', { tool_call_id: 'child', name: 'spawnAgent', args: { prompt: 'inspect child', receiverThreadIds: ['child-id'] } }),
    event(3, 'text_delta', { text: 'first child report', subagent_thread: 'child-id' }),
    event(4, 'tool_call_started', { tool_call_id: 'shell', name: 'Bash', args: { command: 'echo ok' } }),
    event(5, 'approval_required', { tool_call_id: 'question', kind: 'question', title: 'Choose', questions: [{ id: 'answer', question: 'Pick one', options: [{ label: 'yes' }, { label: 'no' }] }] }),
  ]
  await flush()
  const timeline = h.owner(h.keys.chatTimelineKey)
  const turn = timeline.turns.value[0]
  const group = turn.parts.find(part => part.kind === 'tool_group')
  timeline.expandedToolGroups.set(turn.key, new Set([group.key]))
  await flush()
  const toolType = (await h.load('features/chat/components/ChatToolGroup.vue')).default
  const threadType = (await h.load('features/chat/components/ChatSubthread.vue')).default
  const instances = () => collectInstances(h.vm.$.subTree)
  const firstTool = instances().find(instance => instance.type === toolType)
  const firstThread = instances().find(instance => instance.type === threadType)
  assert.ok(firstTool && firstThread)
  assert.match(renderedText(firstThread.subTree.el), /first child report/)
  stream.events.value = [...stream.events.value,
    event(6, 'tool_call_completed', { tool_call_id: 'shell', status: 'completed', result: 'shell done' }),
    event(7, 'text_delta', { text: ' and second child report', subagent_thread: 'child-id' }),
  ]
  await flush()
  assert.ok(instances().find(instance => instance.type === toolType) === firstTool, 'Tool updates must retain the tool group instance')
  assert.ok(instances().find(instance => instance.type === threadType) === firstThread, 'Child updates must retain the subthread instance')
  assert.match(renderedText(firstTool.subTree.el), /shell done/)
  assert.match(renderedText(firstThread.subTree.el), /second child report/)
  assert.ok(timeline.expandedToolGroups.get(turn.key).has(group.key))
  assert.deepEqual(h.warnings, [])
})

test('owner subscriptions keep the baseline order and are installed only after every owner exists', () => {
  const source = readFileSync(new URL('features/chat/StructuredPane.vue', root), 'utf8')
  const setup = source.slice(source.indexOf('<script setup'))
  const registration = [...setup.matchAll(/^(\w+)\.(register\w+)\(\)/gm)].map(match => `${match[1]}.${match[2]}`)
  assert.deepEqual(registration, [
    'settings.registerAvailabilityWatcher',
    'delivery.registerPendingWatcher',
    'goalController.registerWatchers',
    'settings.registerMenuWatchers',
    'composer.registerWatchers',
    'queue.registerReconciliationWatcher',
    'delivery.registerGoalRecoveryWatcher',
    'queue.registerAutoFlushWatcher',
    'delivery.registerTerminalWatcher',
    'timeline.registerWatchers',
  ])
  const firstRegistration = setup.indexOf(`${registration[0]}()`)
  for (const owner of ['goalController', 'attachmentsController', 'delivery', 'settings', 'composer', 'timeline', 'actions', 'queue', 'images']) {
    const declaration = setup.indexOf(`const ${owner} = useChat`)
    assert.ok(declaration >= 0 && declaration < firstRegistration, `${owner} must be assembled before any immediate subscription`)
  }
})

function yieldingWakeEvent(sequence = 1) {
  return {
    stream_sequence: sequence, type: 'turn_started', session_id: 's', tab_id: 'one',
    turn_id: `wake-${sequence}`, agent_type: 'codex', created_at: '2026-10-17T10:00:00Z', redacted: false,
    payload: { summary: '[Dot wake] New events', metadata: { origin: 'scheduled_task', yields_to_user: true } },
  }
}

test('a yielding Dot wake locks settings but accepts direct composer input without queueing', async t => {
  const h = await mount(t)
  h.state.streams[0].events.value = [yieldingWakeEvent()]
  const delivery = h.owner(h.keys.chatDeliveryKey)
  const composer = h.owner(h.keys.chatComposerKey)
  const queue = h.owner(h.keys.chatQueueKey)
  delivery.draftMessage.value = 'user takes over'
  await flush()
  assert.equal(delivery.turnInFlight.value, true)
  assert.equal(delivery.modeInteractionLocked.value, true)
  assert.equal(delivery.composerTurnInFlight.value, false)
  const composerType = (await h.load('features/chat/components/ChatComposer.vue')).default
  const instance = collectInstances(h.vm.$.subTree).find(item => item.type === composerType)
  const findSendButton = node => node?.props?.class === 'composer-send-btn'
    ? node : (node?.children ?? []).map(findSendButton).find(Boolean)
  assert.equal(renderedText(findSendButton(instance.subTree.el)).trim(), 'Send')
  composer.handleComposerEnter({ shiftKey: false, metaKey: false, ctrlKey: false, altKey: false, preventDefault() {} })
  await flush()
  const sends = h.state.calls.filter(call => call[0] === 'fetch' && call[1].endsWith('/send'))
  assert.equal(sends.length, 1)
  assert.equal(JSON.parse(sends[0][2].body).delivery, 'normal')
  assert.equal(JSON.parse(sends[0][2].body).text, 'user takes over')
  assert.equal(queue.draftQueue.value.length, 0)
  assert.equal(delivery.composerTurnInFlight.value, true, 'the pending user send holds later drafts')
})

test('foreground queued input can flush past a yielding Dot wake with normal delivery', async t => {
  const h = await mount(t)
  h.state.streams[0].events.value = [yieldingWakeEvent()]
  await flush()
  const queue = h.owner(h.keys.chatQueueKey)
  queue.draftQueue.value.push({ id: 'queued-user', message: 'queued takeover', attachments: [] })
  await flush()
  const sends = h.state.calls.filter(call => call[0] === 'fetch' && call[1].endsWith('/send'))
  assert.equal(sends.length, 1)
  assert.equal(JSON.parse(sends[0][2].body).delivery, 'normal')
  assert.equal(JSON.parse(sends[0][2].body).client_turn_id, 'queued-user')
  assert.equal(queue.draftQueue.value.length, 0)
})

for (const cleanup of ['deactivate', 'unmount']) {
  test(`pending preview Close followed by ${cleanup} queues only one history traversal`, async t => {
    const h = await mount(t)
    h.owner(h.keys.chatImagesKey).openImageLightbox('data:image/png;base64,eA==', 'preview')
    await flush()
    const dialogType = (await h.load('components/AttachmentPreviewDialog.vue')).default
    const instance = collectInstances(h.vm.$.subTree).find(i => i.type === dialogType)
    const dialog = instance.subTree.children[0].el
    assert.equal(dialog.open, true)
    // Native cancel shares the Close handler; leave popstate pending while the
    // real KeepAlive/unmount lifecycle clears the preview owner.
    dialog.props.onCancel({ preventDefault() {} })
    assert.equal(h.state.calls.filter(([name]) => name === 'history.back').length, 1)
    if (cleanup === 'deactivate') h.selected.value = 'two'
    else h.unmount()
    await flush()
    assert.equal(h.state.calls.filter(([name]) => name === 'history.back').length, 1)
  })
}

test('a cached Chat closes its model popover before leaving the document', async t => {
  const h = await mount(t)
  const settings = h.owner(h.keys.chatSettingsKey)
  settings.isModelMenuOpen.value = true
  await flush()
  h.selected.value = 'two'
  await flush()
  assert.equal(settings.isModelMenuOpen.value, false)
  h.selected.value = 'one'
  await flush()
  assert.equal(h.owner(h.keys.chatSettingsKey), settings)
  assert.equal(settings.isModelMenuOpen.value, false)
})

test('cached settings cannot mutate a Chat while hydration, reconciliation or failure locks readiness', async t => {
  const h = await mount(t)
  const delivery = h.owner(h.keys.chatDeliveryKey)
  const settings = h.owner(h.keys.chatSettingsKey)
  const stream = h.state.streams[0]
  let mutations = 0
  h.state.store.switchEnv = async () => { mutations += 1 }
  const model = { id: 'test-model', label: 'Test', supported_reasoning_efforts: [] }
  for (const state of ['hydrating', 'reconciling', 'failed']) {
    stream.connectionState.value = state
    await flush()
    assert.equal(delivery.modeInteractionLocked.value, true)
    settings.toggleModelMenu()
    settings.toggleModeMenu()
    assert.equal(settings.isModelMenuOpen.value, false)
    assert.equal(settings.isModeMenuOpen.value, false)
    await settings.selectModelAndEffort(model, '')
    assert.equal(mutations, 0)
  }
  stream.connectionState.value = 'live'
  await flush()
  assert.equal(delivery.modeInteractionLocked.value, false)
  await settings.selectModelAndEffort(model, '')
  assert.equal(mutations, 1)
})

test('a pane that joins a turn mid-flight reads its detail once and keeps the fold closed', async t => {
  // The turn started before this pane existed, so its events were never reduced
  // into parts: the transcript would show an empty fold and a footer with no
  // step to name. One detail read restores the parts; opening the fold is the
  // reader's call, not ours.
  const h = await mount(t)
  const stream = h.state.streams[0]
  const timeline = h.owner(h.keys.chatTimelineKey)
  const event = (sequence, type, payload = {}) => ({
    stream_sequence: sequence, type, payload, session_id: 's', tab_id: 'one',
    turn_id: 'joined', agent_type: 'claude', created_at: '2026-10-16T10:00:00Z', redacted: false,
  })
  stream.events.value = [event(1, 'turn_started', { summary: 'long turn already in flight' })]
  await flush()
  const turns = timeline.turns.value
  assert.equal(turns.length, 1)
  assert.deepEqual(turns[0].parts, [], 'the fixture is the mid-flight shape: a turn with no parts')
  const reads = () => h.state.calls.filter(call => call[0] === 'loadHistoryDetail')

  stream.historyDetails.value = {
    joined: {
      descriptor: { turn_id: 'joined', first_sequence: 1, last_sequence: 900, event_count: 900 },
      status: 'idle',
    },
  }
  await flush()
  assert.deepEqual(reads(), [['loadHistoryDetail', 'joined']], 'the descriptor is read once')
  assert.equal(timeline.processExpandedOverrides.value.size, 0, 'the fold stays closed')

  // Leaving the tab and coming back must not re-read a turn this pane already
  // restored: the descriptor keeps growing while the turn streams.
  h.selected.value = 'two'
  await flush()
  h.selected.value = 'one'
  await flush()
  assert.equal(reads().length, 1, 'one read per turn, not one per tab visit')
  assert.equal(timeline.timelineDisposed, false)
})

test('a turn that already has parts is never re-read behind the reader', async t => {
  const h = await mount(t)
  const stream = h.state.streams[0]
  // This turn began after the pane opened, so nothing of it predates this pane:
  // the live stream owns every part and a read would be work for nothing.
  const startedNow = new Date().toISOString()
  const event = (sequence, type, payload = {}) => ({
    stream_sequence: sequence, type, payload, session_id: 's', tab_id: 'one',
    turn_id: 'live', agent_type: 'claude', created_at: startedNow, redacted: false,
  })
  stream.events.value = [
    event(1, 'turn_started', { summary: 'streaming here' }),
    event(2, 'tool_call_started', { tool_call_id: 'c1', name: 'Bash', args: { command: 'echo hi' } }),
  ]
  await flush()
  stream.historyDetails.value = {
    live: {
      descriptor: { turn_id: 'live', first_sequence: 1, last_sequence: 2, event_count: 2 },
      status: 'idle',
    },
  }
  await flush()
  assert.deepEqual(
    h.state.calls.filter(call => call[0] === 'loadHistoryDetail'),
    [],
    'the live stream owns a turn it is already reducing',
  )
})

test('a turn whose stream dropped earns exactly one repair read when it stops', async t => {
  const h = await mount(t)
  const stream = h.state.streams[0]
  const startedNow = new Date().toISOString()
  const event = (sequence, type, payload = {}) => ({
    stream_sequence: sequence, type, payload, session_id: 's', tab_id: 'one',
    turn_id: 'gapped', agent_type: 'claude', created_at: startedNow, redacted: false,
  })
  const reads = () => h.state.calls.filter(call => call[0] === 'loadHistoryDetail')
  stream.events.value = [
    event(1, 'turn_started', { summary: 'streaming here' }),
    event(2, 'tool_call_started', { tool_call_id: 'c1', name: 'Bash', args: { command: 'echo hi' } }),
  ]
  await flush()
  stream.historyDetails.value = {
    gapped: {
      descriptor: { turn_id: 'gapped', first_sequence: 1, last_sequence: 2, event_count: 2 },
      status: 'idle',
    },
  }
  await flush()
  assert.deepEqual(reads(), [], 'a turn the live stream is reducing is left alone')

  // The connection drops while this turn is live: whatever the backend accepted
  // in the gap never reached the parts.
  stream.connectionState.value = 'reconciling'
  await flush()
  stream.connectionState.value = 'live'
  await flush()
  assert.deepEqual(reads(), [], 'a dropped stream alone does not trigger a read')

  stream.events.value = [...stream.events.value, event(3, 'turn_completed', { status: 'completed' })]
  await flush()
  assert.deepEqual(reads(), [['loadHistoryDetail', 'gapped']], 'its completion earns the repair read')

  // And no more: the descriptor keeps changing while history is written.
  stream.historyDetails.value = {
    gapped: {
      descriptor: { turn_id: 'gapped', first_sequence: 1, last_sequence: 900, event_count: 900 },
      status: 'idle',
    },
  }
  await flush()
  assert.equal(reads().length, 1, 'one repair read, not a polling loop')
})

test('a pane that opens during a live turn still reads the steps it missed', async t => {
  // Second cross-model review, H-2: "nothing reduced yet" was too strict a test
  // for "this pane joined mid-flight". A live turn almost always narrates once
  // after the mount, so parts exist while everything from before the mount is
  // still missing — and that is exactly the case the reader complains about.
  const h = await mount(t)
  const stream = h.state.streams[0]
  const timeline = h.owner(h.keys.chatTimelineKey)
  const event = (sequence, type, payload = {}) => ({
    stream_sequence: sequence, type, payload, session_id: 's', tab_id: 'one',
    turn_id: 'midflight', agent_type: 'claude', created_at: '2026-10-16T10:00:00Z', redacted: false,
  })
  stream.events.value = [
    event(1, 'turn_started', { summary: 'long turn, already in flight' }),
    event(2, 'text_delta', { text: 'Let me check the files...' }),
  ]
  await flush()
  const turn = timeline.turns.value.at(-1)
  assert.equal(turn.parts.length, 1, 'the fixture has narration from after the mount')
  assert.equal(turn.completed, false, 'and the turn is still running')
  stream.historyDetails.value = {
    midflight: {
      descriptor: { turn_id: 'midflight', first_sequence: 1, last_sequence: 900, event_count: 900 },
      status: 'idle',
    },
  }
  await flush()
  const reads = h.state.calls.filter(call => call[0] === 'loadHistoryDetail')
  assert.deepEqual(
    reads,
    [['loadHistoryDetail', 'midflight']],
    'a live turn that began before this pane opened owes one read, even though narration arrived',
  )
  assert.equal(timeline.processExpandedOverrides.value.size, 0, 'and the fold stays closed')

  // Read once, not once per batch: the descriptor keeps growing.
  stream.historyDetails.value = {
    midflight: {
      descriptor: { turn_id: 'midflight', first_sequence: 1, last_sequence: 1500, event_count: 1500 },
      status: 'idle',
    },
  }
  await flush()
  assert.equal(
    h.state.calls.filter(call => call[0] === 'loadHistoryDetail').length,
    1,
    'one join read per turn',
  )
})

test('a turn that began in this pane still reports how many events it has produced', async t => {
  // Second cross-model review, H-3: hydration is the only place descriptors are
  // recorded, so a turn started while the pane was open had no descriptor and
  // "N events so far" never appeared. The pane watched every one of those
  // events, so it can count them itself.
  const h = await mount(t)
  const stream = h.state.streams[0]
  const timeline = h.owner(h.keys.chatTimelineKey)
  const startedNow = new Date().toISOString()
  const event = (sequence, type, payload = {}) => ({
    stream_sequence: sequence, type, payload, session_id: 's', tab_id: 'one',
    turn_id: 'in-pane', agent_type: 'claude', created_at: startedNow, redacted: false,
  })
  const countFor = () => timeline.turnEventCount(timeline.turns.value.at(-1))
  stream.events.value = [event(1, 'turn_started', { summary: 'starts here' })]
  await flush()
  // The turn's opening marker is punctuation, not output: counting it would
  // make the footer say "Working · 1 event so far" before the provider has
  // produced anything, and the waiting state would never appear.
  assert.equal(countFor(), null, 'a turn that has only started has produced nothing')
  assert.equal(stream.historyDetails.value['in-pane'], undefined, 'and there is no descriptor to count with')

  stream.events.value = [...stream.events.value, event(2, 'tool_call_started', { tool_call_id: 'c1', name: 'Bash', args: {} })]
  await flush()
  assert.equal(countFor(), 1, 'the count grows with the stream, from the pane itself')

  stream.events.value = [...stream.events.value, event(3, 'text_delta', { text: 'writing' })]
  await flush()
  assert.equal(countFor(), 2, 'and keeps growing')

  // A replay/rescan must not double-count what was already seen: sequences
  // already counted are skipped, whether the array was rebuilt or extended.
  stream.events.value = stream.events.value.map(item => ({ ...item }))
  await flush()
  assert.equal(countFor(), 2, 'a rescan of the same events is not a second count')

  // A hydration descriptor is authoritative when it counts more than this pane
  // watched: the pane must not shrink a whole turn to what it happened to see.
  stream.historyDetails.value = {
    'in-pane': {
      descriptor: { turn_id: 'in-pane', first_sequence: 0, last_sequence: 3, event_count: 40 },
      status: 'idle',
    },
  }
  await flush()
  assert.equal(countFor(), 40, 'the larger count wins')
})

test('a turn that began while the tab was hidden is backfilled on return', async t => {
  // H-2.1(a) of the second review: the "joined mid-flight" baseline was fixed at
  // construction, so a turn that started while the pane was deactivated looked
  // like one this pane had watched from its first event — and was never read.
  const h = await mount(t)
  const stream = h.state.streams[0]
  const timeline = h.owner(h.keys.chatTimelineKey)
  const event = (sequence, type, payload = {}) => ({
    stream_sequence: sequence, type, payload, session_id: 's', tab_id: 'one',
    turn_id: 'while-away', agent_type: 'claude', created_at: '2026-10-16T10:00:00Z', redacted: false,
  })
  const reads = () => h.state.calls.filter(call => call[0] === 'loadHistoryDetail')

  h.selected.value = 'two'
  await flush()
  assert.equal(timeline.timelineDisposed, true, 'the pane is hidden')
  // The backend accepts a whole turn while the reader is elsewhere.
  stream.events.value = [
    event(1, 'turn_started', { summary: 'began while hidden' }),
    event(2, 'text_delta', { text: 'narration from the hidden window' }),
  ]
  await flush()
  assert.deepEqual(reads(), [], 'a hidden pane does not read anything')

  h.selected.value = 'one'
  await flush()
  assert.equal(timeline.timelineDisposed, false, 'the pane is visible again')
  stream.historyDetails.value = {
    'while-away': {
      descriptor: { turn_id: 'while-away', first_sequence: 1, last_sequence: 700, event_count: 700 },
      status: 'idle',
    },
  }
  await flush()
  assert.deepEqual(
    reads(),
    [['loadHistoryDetail', 'while-away']],
    'returning to the tab backfills the turn that started while it was hidden',
  )
})
