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
  global('window', { ...listen(listeners.window), innerWidth: 1200, setTimeout, clearTimeout })
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
      const events = ref([]), connectionState = ref('idle'), errorMessage = ref(null)
      const capabilities = ref({ available_models: [], current_model: null, current_reasoning_effort: null, available_modes: [], current_mode: null, supports_images: true, supports_goals: true })
      const start = async (id, source) => { globalThis.__chatLifecycleTest.calls.push(['start', id, source]); connectionState.value = 'live' }
      const stop = () => globalThis.__chatLifecycleTest.calls.push(['stop'])
      const stream = { events, connectionState, errorMessage, capabilities, start, stop, retry: start, refreshCapabilities: async () => {}, setMode: async () => {}, nudge() {}, reset() {} }
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
    focus() {}, querySelectorAll() { return [] }, getBoundingClientRect() { return { top: 0, bottom: 500 } } })
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
  assert.equal(h.active(), first)
  assert.equal(h.owner(h.keys.chatTimelineKey), timeline)
  assert.equal(h.owner(h.keys.chatDeliveryKey), delivery)
  assert.equal(h.owner(h.keys.chatAttachmentsKey), attachments)
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
  assert.equal(current, first)
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
  assert.equal(instances().find(instance => instance.type === toolType), firstTool)
  assert.equal(instances().find(instance => instance.type === threadType), firstThread)
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
