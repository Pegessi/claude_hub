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
const source = readFileSync(new URL('../src/stores/terminalStore.ts', import.meta.url), 'utf8')
  .replaceAll("from 'pinia'", `from '${pathToFileURL(require.resolve('pinia/dist/pinia.mjs')).href}'`)
  .replaceAll("from 'vue'", `from '${pathToFileURL(require.resolve('vue/dist/vue.runtime.esm-bundler.js')).href}'`)
  .replaceAll("from '@/utils/chatGroups'", `from '${groupsUrl}'`)
const { useTerminalStore } = await import(moduleUrl(source))

function setup(t) {
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => [] }))
  const storage = new Map()
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
  return useTerminalStore()
}

test('background tab refresh keeps the new-session launcher enabled', async t => {
  const store = setup(t)
  let resolveTabs
  globalThis.fetch.mock.mockImplementation((...args) => {
    const url = typeof args[0] === 'string' ? args[0] : args[0].url
    if (url === '/api/tabs') {
      return new Promise(resolve => {
        resolveTabs = () => resolve({ ok: true, json: async () => [] })
      })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  })

  const pending = store.fetchTabs()
  await Promise.resolve()
  // While the background GET /api/tabs is still in flight the + launcher
  // must stay clickable — global isLoading is reserved for real mutations
  // (create/close/archive), not 5-second polling refreshes.
  assert.equal(store.isLoading, false, 'launcher disabled during background refresh')

  resolveTabs()
  await pending
  assert.equal(store.isLoading, false, 'isLoading reset after refresh settles')
})

test('concurrent background refreshes share a single in-flight request', async t => {
  const store = setup(t)
  store.tabs = [{ id: 'a', session_kind: 'chat' }]
  const first = store.fetchTabs()
  const second = store.fetchTabs()
  assert.equal(
    globalThis.fetch.mock.calls.filter(call => call.arguments[0] === '/api/tabs').length,
    1,
    'overlapping fetchTabs calls must coalesce into one request',
  )
  await Promise.all([first, second])
})

test('a forced refresh bypasses coalescing so mutation callers get fresh data', async t => {
  const store = setup(t)
  const tabsCalls = () =>
    globalThis.fetch.mock.calls.filter(call => call.arguments[0] === '/api/tabs').length
  let resolveBackground
  globalThis.fetch.mock.mockImplementation((...args) => {
    if (args[0] === '/api/tabs') {
      if (tabsCalls() === 0) {
        // A poll-started refresh is still hanging when the mutation lands.
        return new Promise(resolve => {
          resolveBackground = () => resolve({ ok: true, json: async () => [{ id: 'stale' }] })
        })
      }
      return Promise.resolve({ ok: true, json: async () => [{ id: 'fresh' }] })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  })

  const background = store.fetchTabs()
  await store.fetchTabs({ force: true })
  assert.equal(tabsCalls(), 2, 'force bypasses the in-flight coalesced request')
  assert.deepEqual(store.tabs.map(tab => tab.id), ['fresh'])
  resolveBackground()
  await background
})

test('createTab drives isLoading so the launcher shows its pending state', async t => {
  const store = setup(t)
  let resolveCreate
  globalThis.fetch.mock.mockImplementation(() =>
    new Promise(resolve => {
      resolveCreate = () => resolve({
        ok: true,
        json: async () => ({ id: 'new', session_kind: 'chat' }),
      })
    }),
  )
  const creating = store.createTab({ name: 'New session' })
  await Promise.resolve()
  assert.equal(store.isLoading, true, 'real create mutation marks the launcher busy')
  resolveCreate()
  await creating
  assert.equal(store.isLoading, false)
})
