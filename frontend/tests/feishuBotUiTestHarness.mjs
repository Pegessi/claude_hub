import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { setImmediate } from 'node:timers'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vueUrl = import.meta.resolve('vue')
const piniaUrl = import.meta.resolve('pinia')
export const vue = await import(vueUrl)
const { createPinia, getActivePinia, setActivePinia } = await import(piniaUrl)
const { parse, compileScript } = require('vue/compiler-sfc')
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
const configUrl = moduleUrl(source('utils/feishuBotConfig.ts'))
imports['@/utils/feishuBotConfig'] = configUrl
export const config = await import(configUrl)
const storeUrl = moduleUrl(rewrite(source('stores/feishuBotPoolStore.ts')))
imports['@/stores/feishuBotPoolStore'] = storeUrl
const { useFeishuBotPoolStore } = await import(storeUrl)
export const { useFeishuBinding } = await import(moduleUrl(rewrite(source('composables/useFeishuBinding.ts'))))
const clipboardUrl = moduleUrl(`
  let writer = async () => undefined
  export function setWriter(next) {
    const previous = writer
    writer = next
    return previous
  }
  export function writeClipboard(value) { return writer(value) }
`)
export const clipboard = await import(clipboardUrl)
imports['@/utils/clipboard'] = clipboardUrl
const filename = 'FeishuBotSettingsDialog.vue'
const { descriptor } = parse(source(`components/${filename}`), { filename })
const script = compileScript(descriptor, { id: 'bot-settings-behavior', genDefaultAs: '__component' })
const Settings = (await import(moduleUrl(rewrite(`${script.content}\nexport default __component`)))).default

export const flush = () => new Promise(resolve => setImmediate(resolve))
export function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
export const response = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
export const bot = (id = 'a', overrides = {}) => ({
  bot_id: id, name: `Bot ${id}`, app_id: `app-${id}`, source: 'stored', enabled: true,
  credentials_editable: true, deletable: true, revision: 1, generation: 1, configured: true,
  app_secret_configured: true, connection_status: 'connected', updated_at: null,
  binding: null, my_claims: [], ...overrides,
})
export const pool = (revision, bots = [bot(), bot('b')], focus = null) => ({
  pool_revision: revision, bots, focus_bot_id: focus, pool_editable: true, deprecated_env: [],
})
export const claim = () => ({
  pairing_id: 'claim-a', state: 'claimed', bot_id: 'a', app_id: 'app-a', tab_id: 'tab-1',
  workspace_id: null, chat_id: 'chat-a', created_at: new Date(1100).toISOString(),
  expires_at: new Date(4000).toISOString(),
})
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
export async function mountSettings(t, h) {
  const renderer = vue.createRenderer({
    createElement: tag => ({ tag, children: [] }), createText: text => ({ text }), createComment: text => ({ text }),
    setText(node, text) { node.text = text }, setElementText(node, text) { node.text = text },
    patchProp(node, name, _previous, value) { node[name] = value },
    insert(node, parent) { node.parent = parent; (parent.children ??= []).push(node) },
    remove(node) { const list = node.parent?.children; if (list) list.splice(list.indexOf(node), 1) },
    parentNode: node => node.parent ?? null, nextSibling: () => null,
  })
  // Execute the real compiled SFC setup/lifecycle; DOM layout is a separate check.
  const app = renderer.createApp({ ...Settings, render: () => null })
  app.use(h.pinia)
  const vm = app.mount({ children: [] })
  t.after(() => app.unmount())
  await flush()
  return vm.$.setupState
}
