import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'
import { createPinia, setActivePinia } from 'pinia'

const require = createRequire(import.meta.url)
const moduleUrl = source => {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  })
  return `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
}
const groupsUrl = moduleUrl(readFileSync(new URL('../src/utils/chatGroups.ts', import.meta.url), 'utf8'))
const vueRuntimeUrl = pathToFileURL(require.resolve('vue/dist/vue.runtime.esm-bundler.js')).href
const reconnectUrl = moduleUrl(
  readFileSync(new URL('../src/utils/terminalReconnect.ts', import.meta.url), 'utf8')
    .replaceAll("from 'vue'", `from '${vueRuntimeUrl}'`),
)
const source = readFileSync(new URL('../src/stores/terminalStore.ts', import.meta.url), 'utf8')
  .replaceAll("from 'pinia'", `from '${pathToFileURL(require.resolve('pinia/dist/pinia.mjs')).href}'`)
  .replaceAll("from 'vue'", `from '${vueRuntimeUrl}'`)
  .replaceAll("from '@/utils/chatGroups'", `from '${groupsUrl}'`)
  .replaceAll("from '@/utils/terminalReconnect'", `from '${reconnectUrl}'`)
const { useTerminalStore } = await import(moduleUrl(source))

function setup(t, initial = {}) {
  const storage = new Map(Object.entries(initial))
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => ({}) }))
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
  } })
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { setTimeout: () => 0 } })
  t.after(() => {
    if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage)
    else delete globalThis.localStorage
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow)
    else delete globalThis.window
  })
  setActivePinia(createPinia())
  const store = useTerminalStore()
  store.tabs = [
    { id: 'a', cwd: '/repo', session_kind: 'chat' },
    { id: 'terminal', session_kind: 'terminal' },
    { id: 'b', cwd: '/repo', session_kind: 'chat' },
    { id: 'managed', workspace_id: 'ws', session_kind: 'chat' },
  ]
  return { store, storage }
}

test('pin and unpin preferences survive new store instances and ignore other session surfaces', t => {
  const { store, storage } = setup(t)
  store.setChatPinned('a', true)
  store.setChatPinned('terminal', true)
  store.setChatPinned('managed', true)
  assert.deepEqual(JSON.parse(storage.get('claude_hub_pinned_chat_ids')), ['a'])
  setActivePinia(createPinia())
  const reloaded = useTerminalStore()
  assert.equal(reloaded.pinnedChatIds.has('a'), true)
  reloaded.tabs = store.tabs
  reloaded.setChatPinned('a', false)
  assert.deepEqual(JSON.parse(storage.get('claude_hub_pinned_chat_ids')), [])
})

test('newly created and duplicated sessions are prepended without reordering existing sessions', async t => {
  const { store } = setup(t)
  const responses = [
    { id: 'new', cwd: '/repo', session_kind: 'chat' },
    { id: 'duplicate', cwd: '/repo', session_kind: 'chat' },
  ]
  globalThis.fetch.mock.mockImplementation(async () => ({
    ok: true,
    json: async () => responses.shift(),
  }))

  await store.createTab({ name: 'New session' })
  assert.deepEqual(store.tabs.map(tab => tab.id), ['new', 'a', 'terminal', 'b', 'managed'])

  await store.duplicateTab('a')
  assert.deepEqual(
    store.tabs.map(tab => tab.id),
    ['duplicate', 'new', 'a', 'terminal', 'b', 'managed'],
  )
})

test('ID-based reorder keeps managed and terminal rows and saves the complete order', async t => {
  const { store } = setup(t)
  store.activeTabId = 'b'
  store.reorderTabById('b', 'a', 'before')
  await store.saveTabOrder()
  assert.deepEqual(store.tabs.map(tab => tab.id), ['b', 'a', 'terminal', 'managed'])
  assert.equal(store.activeTabId, 'b')
  const [url, request] = globalThis.fetch.mock.calls[0].arguments
  assert.equal(url, '/api/tabs/order')
  assert.equal(request.method, 'PUT')
  assert.deepEqual(JSON.parse(request.body), { tab_ids: ['b', 'a', 'terminal', 'managed'] })
  assert.deepEqual(store.chatTabsByCwd[0].tabs.map(tab => tab.id), ['b', 'a'])
})

test('rapid reorder writes wait for earlier saves before persisting the latest order', async t => {
  const { store } = setup(t)
  let completeFirst
  const requests = []
  globalThis.fetch.mock.mockImplementation(async (_url, request) => {
    requests.push(JSON.parse(request.body).tab_ids)
    if (requests.length === 1) await new Promise(resolve => { completeFirst = resolve })
    return { ok: true }
  })
  store.reorderTabById('b', 'a', 'before')
  await Promise.resolve()
  store.reorderTabById('a', 'b', 'before')
  const settled = store.saveTabOrder()
  await Promise.resolve()
  assert.equal(requests.length, 1)
  assert.deepEqual(requests[0], ['b', 'a', 'terminal', 'managed'])
  completeFirst()
  await settled
  assert.deepEqual(requests.at(-1), ['a', 'b', 'terminal', 'managed'])
})

test('missing or managed drag IDs do not write order or remove sessions', async t => {
  const { store } = setup(t)
  const previous = store.tabs
  store.reorderTabById('missing', 'a', 'before')
  store.reorderTabById('managed', 'a', 'before')
  store.reorderTabById('a', 'managed', 'after')
  store.reorderTabById('a', 'a', 'after')
  await Promise.resolve()
  assert.equal(store.tabs, previous)
  assert.equal(globalThis.fetch.mock.calls.length, 0)
})

test('mobile drawer open/close/toggle actions update visibility', t => {
  const { store } = setup(t)
  assert.equal(store.mobileDrawerOpen, false)
  store.openMobileDrawer()
  assert.equal(store.mobileDrawerOpen, true)
  // open is idempotent
  store.openMobileDrawer()
  assert.equal(store.mobileDrawerOpen, true)
  store.closeMobileDrawer()
  assert.equal(store.mobileDrawerOpen, false)
  store.toggleMobileDrawer()
  assert.equal(store.mobileDrawerOpen, true)
  store.toggleMobileDrawer()
  assert.equal(store.mobileDrawerOpen, false)
})

test('selecting a session from the mobile drawer switches tab AND dismisses it', t => {
  const { store } = setup(t)
  store.openMobileDrawer()
  store.activeTabId = 'a'
  store.selectMobileTab('b')
  assert.equal(store.activeTabId, 'b')
  assert.equal(store.mobileDrawerOpen, false)
  // An unknown id neither switches nor opens anything.
  store.selectMobileTab('does-not-exist')
  assert.equal(store.activeTabId, 'b')
})

test('pinned group is still built for mobile-shaped chat data (presentation-agnostic)', async t => {
  const { store } = setup(t)
  store.setChatPinned('a', true)
  // Mobile reuses the exact same buildChatSidebarGroups source as desktop;
  // import it through the same transpile harness used for the store above.
  const { buildChatSidebarGroups } = await import(groupsUrl)
  const groups = buildChatSidebarGroups(store.chatTabs, store.pinnedChatIds, {
    query: '',
    activeTabId: 'b',
    collapsed: new Set(),
    expanded: new Set(),
  })
  const pinned = groups.find(group => group.pinned)
  assert.ok(pinned, 'a pinned group is rendered')
  assert.deepEqual(pinned.visibleTabs.map(tab => tab.id), ['a'])
  // The pinned row is absent from every cwd group.
  assert.ok(
    groups.filter(group => !group.pinned).every(group => !group.tabs.some(tab => tab.id === 'a')),
  )
})
