import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { setImmediate } from 'node:timers'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vueUrl = import.meta.resolve('vue')
const piniaUrl = import.meta.resolve('pinia')
const vue = await import(vueUrl)
const { createPinia, getActivePinia, setActivePinia } = await import(piniaUrl)
const { parse, compileScript, compileTemplate } = require('vue/compiler-sfc')
const source = path => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')
const moduleUrl = text => {
  const { outputText } = ts.transpileModule(text, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  })
  return `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
}
const imports = { vue: vueUrl, pinia: piniaUrl }
function rewrite(text) {
  return text.replace(/from\s+(['"])([^'"]+)\1/g, (full, _quote, name) =>
    imports[name] ? `from ${JSON.stringify(imports[name])}` : full)
}
function compileSfcUrl(path, id) {
  const filename = path.split('/').at(-1)
  const { descriptor } = parse(source(path), { filename })
  const script = descriptor.script || descriptor.scriptSetup
    ? compileScript(descriptor, { id, genDefaultAs: '__component' })
    : { content: 'const __component = {}', bindings: undefined }
  const template = descriptor.template
    ? compileTemplate({
      source: descriptor.template.content,
      filename,
      id,
      compilerOptions: { bindingMetadata: script.bindings },
    })
    : { code: 'export function render() { return null }' }
  return moduleUrl(rewrite(`${script.content}\n${template.code}\n__component.render = render\nexport default __component`))
}
const configUrl = moduleUrl(source('utils/feishuBotConfig.ts'))
imports['@/utils/feishuBotConfig'] = configUrl
export const config = await import(configUrl)
const identityUrl = moduleUrl(source('utils/feishuIdentity.ts'))
imports['@/utils/feishuIdentity'] = identityUrl
const storeUrl = moduleUrl(rewrite(source('stores/feishuBotPoolStore.ts')))
imports['@/stores/feishuBotPoolStore'] = storeUrl
const { useFeishuBotPoolStore } = await import(storeUrl)
const bindingComposableUrl = moduleUrl(rewrite(source('composables/useFeishuBinding.ts')))
imports['@/composables/useFeishuBinding'] = bindingComposableUrl
const { useFeishuBinding } = await import(bindingComposableUrl)
const clipboardUrl = moduleUrl(`
  let writer = async () => undefined
  export function setWriter(next) {
    const previous = writer
    writer = next
    return previous
  }
  export function writeClipboard(value) { return writer(value) }
`)
await import(clipboardUrl)
imports['@/utils/clipboard'] = clipboardUrl
imports['@/components/FeishuBrandMark.vue'] = compileSfcUrl('components/FeishuBrandMark.vue', 'feishu-brand-mark')
imports['@/components/FeishuIdentityMark.vue'] = compileSfcUrl('components/FeishuIdentityMark.vue', 'feishu-identity-mark')
const Settings = (await import(compileSfcUrl('features/feishu-bot/FeishuBotSettingsDialog.vue', 'bot-settings-behavior'))).default
const BindingPanel = (await import(compileSfcUrl('components/FeishuBindingPanel.vue', 'feishu-binding-panel'))).default

export const flush = () => new Promise(resolve => setImmediate(resolve))
export function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
export const response = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
export const activeBinding = (tabId = 'tab-1', overrides = {}) => ({
  pairing_id: `pair-${tabId}`, state: 'active', bot_id: 'a', app_id: 'app-a',
  tab_id: tabId, workspace_id: null, chat_id: `oc-${tabId}`, chat_type: 'p2p',
  chat_name: null, owner_kind: 'oauth', is_mine: true,
  created_at: new Date(1500).toISOString(), expires_at: null, ...overrides,
})
export const bot = (id = 'a', overrides = {}) => {
  const entry = {
    bot_id: id, name: `Bot ${id}`, app_id: `app-${id}`, source: 'stored', enabled: true,
    credentials_editable: true, deletable: true, revision: 1, generation: 1, configured: true,
    app_secret_configured: true, connection_status: 'connected', updated_at: null,
    mode: 'single', binding: null, bindings: [], my_claims: [], ...overrides,
  }
  if (overrides.binding !== undefined && overrides.bindings === undefined) {
    entry.bindings = entry.binding ? [entry.binding] : []
  } else if (overrides.bindings !== undefined && overrides.binding === undefined) {
    entry.binding = entry.bindings[0] ?? null
  }
  return entry
}
export const pool = (revision, bots = [bot(), bot('b')], focus = null) => ({
  pool_revision: revision, bots, focus_bot_id: focus, pool_editable: true, deprecated_env: [],
})
export const claim = () => ({
  pairing_id: 'claim-a', state: 'claimed', bot_id: 'a', app_id: 'app-a', tab_id: 'tab-1',
  workspace_id: null, chat_id: 'chat-a', created_at: new Date(1100).toISOString(),
  expires_at: new Date(4000).toISOString(),
})
export const groupChat = (id = 'oc-group', overrides = {}) => ({
  chat_id: id, name: `Group ${id}`, chat_mode: 'group', user_count: 3, bot_count: 1, ...overrides,
})
export const chatList = (chats = [groupChat()], revision = 1) => ({ chats, revision })
export const code = () => ({
  bot_id: 'a', revision: 2, code: 'CH-TEST', expires_at: new Date(2000).toISOString(),
})
export function harness(t) {
  const originalPinia = getActivePinia()
  t.after(() => setActivePinia(originalPinia))
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useFeishuBotPoolStore()
  let server = pool(1)
  const calls = [], unexpected = []
  let handle = async (_url, init = {}) => {
    if ((init.method ?? 'GET') !== 'GET') {
      unexpected.push(init.method)
      throw new Error('unexpected mutation in test')
    }
    return response(server)
  }
  t.mock.method(globalThis, 'fetch', async (url, init = {}) => {
    calls.push({ url, init })
    return handle(url, init)
  })
  t.after(() => assert.deepEqual(unexpected, []))
  store.applyPool(server)
  return {
    pinia, store, calls, server: () => server,
    setServer(value) { server = value }, setTransport(value) { handle = value },
  }
}
export function bindingHarness(t, dependencies = {}) {
  const h = harness(t)
  let now = 1000, timerId = 0
  const timers = new Map()
  const tab = vue.ref('tab-1')
  const scope = vue.effectScope()
  const api = scope.run(() => useFeishuBinding(tab, undefined, {
    store: h.store, now: () => now,
    setTimer(callback) { const id = timerId++; timers.set(id, callback); return id },
    clearTimer(id) { timers.delete(id) }, startPairing: async () => code(),
    ...dependencies,
  }))
  t.after(() => { api.pause(); scope.stop() })
  return {
    ...h, api, tab, timers, setNow(value) { now = value },
    async fire() {
      const first = timers.entries().next().value
      assert.ok(first, 'expected a scheduled poll')
      timers.delete(first[0]); first[1](); await flush()
    },
  }
}
function createNode(tag, text = '') {
  return {
    tag,
    text,
    props: {},
    children: [],
    parent: null,
    open: false,
    addEventListener() {},
    removeEventListener() {},
    getRootNode() { return globalThis.document ?? { activeElement: null } },
    showModal() { this.open = true },
    close() { this.open = false },
    contains(target) {
      let current = target
      while (current) {
        if (current === this) return true
        current = current.parent
      }
      return false
    },
  }
}
function testRenderer() {
  return vue.createRenderer({
    createElement: tag => createNode(tag),
    createText: text => createNode('#text', text),
    createComment: text => createNode('#comment', text),
    setText(node, text) { node.text = text },
    setElementText(node, text) { node.text = text; node.children = [] },
    patchProp(node, name, _previous, value) {
      node.props[name] = value
      node[name] = value
    },
    insert(node, parent, anchor = null) {
      if (node.parent) {
        const prev = node.parent.children.indexOf(node)
        if (prev >= 0) node.parent.children.splice(prev, 1)
      }
      node.parent = parent
      const list = (parent.children ??= [])
      const idx = anchor ? list.indexOf(anchor) : -1
      if (idx >= 0) list.splice(idx, 0, node)
      else list.push(node)
    },
    remove(node) {
      const list = node.parent?.children
      if (list) {
        const idx = list.indexOf(node)
        if (idx >= 0) list.splice(idx, 1)
      }
      node.parent = null
    },
    parentNode: node => node.parent ?? null,
    nextSibling: node => {
      const list = node.parent?.children
      return list ? list[list.indexOf(node) + 1] ?? null : null
    },
  })
}
export function textOf(node) {
  if (!node || node.tag === '#comment') return ''
  return `${node.text ?? ''}${(node.children ?? []).map(textOf).join('')}`
}
export function findAll(node, predicate, results = []) {
  if (!node) return results
  if (predicate(node)) results.push(node)
  for (const child of node.children ?? []) findAll(child, predicate, results)
  return results
}
export function findByTestId(node, testId) {
  return findAll(node, item => item.props?.['data-testid'] === testId)[0] ?? null
}
export function findAllByTestId(node, testId) {
  return findAll(node, item => item.props?.['data-testid'] === testId)
}
export async function settleUi() {
  await vue.nextTick()
  await flush()
  await vue.nextTick()
  await flush()
}
export async function clickNode(node) {
  assert.ok(node, 'expected a target node to click')
  assert.ok(!node.props?.disabled, `cannot click disabled node (${node.props?.['data-testid'] ?? node.tag})`)
  const handler = node.props?.onClick
  assert.equal(typeof handler, 'function', 'expected an onClick handler')
  await handler({ preventDefault() {}, stopPropagation() {} })
  await settleUi()
}
export async function changeSelect(node, value) {
  assert.ok(node, 'expected a select node')
  assert.ok(!node.props?.disabled, 'cannot change disabled select')
  const handler = node.props?.onChange
  assert.equal(typeof handler, 'function', 'expected an onChange handler')
  await handler({ target: { value } })
  await settleUi()
}
function ensureDomGlobals() {
  const restore = []
  function setGlobal(name, value) {
    if (name in globalThis) return
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
    restore.push(() => { delete globalThis[name] })
  }
  class Document {}
  class ShadowRoot {}
  setGlobal('Document', Document)
  setGlobal('ShadowRoot', ShadowRoot)
  setGlobal('document', Object.assign(new (globalThis.Document ?? Document)(), {
    activeElement: null,
    addEventListener() {},
    removeEventListener() {},
  }))
  return () => restore.reverse().forEach(fn => fn())
}
export async function mountSettings(t, h) {
  const cleanupDom = ensureDomGlobals()
  const root = createNode('root')
  const app = testRenderer().createApp(Settings)
  app.use(h.pinia)
  const vm = app.mount(root)
  t.after(() => {
    app.unmount()
    cleanupDom()
  })
  await settleUi()
  const state = vm.$.setupState
  Object.defineProperty(state, '$root', { value: root, configurable: true })
  return state
}
export async function mountBindingPanel(t, h, initialTabId = 'tab-1') {
  const cleanupDom = ensureDomGlobals()
  const tabId = vue.ref(initialTabId)
  const root = createNode('root')
  let panelVm = null
  const Wrapper = {
    setup() {
      return () => vue.h(BindingPanel, {
        tabId: tabId.value,
        ref: instance => { panelVm = instance },
      })
    },
  }
  const app = testRenderer().createApp(Wrapper)
  app.use(h.pinia)
  app.mount(root)
  t.after(() => {
    app.unmount()
    cleanupDom()
  })
  await settleUi()
  return {
    root,
    tabId,
    get state() { return panelVm?.$.setupState },
  }
}
