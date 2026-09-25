import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'
import { createPinia, setActivePinia } from 'pinia'

// ── Load the real registry + terminalStore in Node ──────────────────────────
// Same approach as forkFromTurn.test.mjs: transpile the TS sources to ESM and
// rewrite bare imports to resolved file/data URLs so they load from a data:
// URL (which has no base of its own). Type-only imports are erased.
const vueUrl = await import.meta.resolve('vue')
const piniaUrl = await import.meta.resolve('pinia')

function transpileToDataUrl(source, rewrites = {}) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2020,
    },
  })
  let rewritten = outputText
  for (const [specifier, url] of Object.entries(rewrites)) {
    rewritten = rewritten.replace(
      new RegExp(`from\\s+['"]${specifier.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}['"]`, 'g'),
      `from ${JSON.stringify(url)}`,
    )
  }
  return `data:text/javascript;base64,${Buffer.from(rewritten).toString('base64')}`
}

const reconnectSource = await readFile(
  new URL('../src/utils/terminalReconnect.ts', import.meta.url),
  'utf8',
)
const reconnectUrl = transpileToDataUrl(reconnectSource, { vue: vueUrl })
const reconnectModule = await import(reconnectUrl)
const { PaneReconnectRegistry, createPaneReconnectRegistry } = reconnectModule

const chatGroupsSource = await readFile(
  new URL('../src/utils/chatGroups.ts', import.meta.url),
  'utf8',
)
const chatGroupsUrl = transpileToDataUrl(chatGroupsSource)

const storeSource = await readFile(
  new URL('../src/stores/terminalStore.ts', import.meta.url),
  'utf8',
)
const storeUrl = transpileToDataUrl(storeSource, {
  pinia: piniaUrl,
  vue: vueUrl,
  '@/utils/chatGroups': chatGroupsUrl,
  '@/utils/terminalReconnect': reconnectUrl,
})
const useTerminalStore = (await import(storeUrl)).useTerminalStore

// ── Browser globals the store touches ───────────────────────────────────────
if (!globalThis.window) {
  globalThis.window = {
    setTimeout: (...args) => Reflect.apply(globalThis.setTimeout, globalThis, args),
    clearTimeout: (...args) => Reflect.apply(globalThis.clearTimeout, globalThis, args),
    setInterval: (...args) => Reflect.apply(globalThis.setInterval, globalThis, args),
    clearInterval: (...args) => Reflect.apply(globalThis.clearInterval, globalThis, args),
  }
}
// Assign unconditionally: Node ≥22 may expose a partial built-in localStorage
// without a callable getItem, which breaks the store's preference reads.
globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
}

function freshStore() {
  setActivePinia(createPinia())
  return useTerminalStore()
}

function makeTab(id, name) {
  return {
    id,
    name,
    shell: 'bash',
    agent_type: 'claude',
    session_kind: 'terminal',
    port: 0,
    created_at: new Date().toISOString(),
    is_active: false,
  }
}

// ── PaneReconnectRegistry: pure state machine ───────────────────────────────

test('registry: unknown tab starts idle at nonce 0', () => {
  const registry = new PaneReconnectRegistry()
  assert.deepEqual(registry.get('tab-x'), { state: 'idle', nonce: 0 })
  assert.equal(registry.isConnecting('tab-x'), false)
})

test('registry: request moves tab to connecting and bumps the nonce', () => {
  const registry = new PaneReconnectRegistry()
  const first = registry.request('tab-a')
  assert.deepEqual(first, { nonce: 1, deduped: false })
  assert.equal(registry.isConnecting('tab-a'), true)
})

test('registry: a second request while connecting is coalesced (concurrent-click dedup)', () => {
  const registry = new PaneReconnectRegistry()
  registry.request('tab-a')
  const second = registry.request('tab-a')
  assert.deepEqual(second, { nonce: 1, deduped: true })
  assert.equal(registry.get('tab-a').nonce, 1)
})

test('registry: settle success transitions to success; non-connecting settle is ignored', () => {
  const registry = new PaneReconnectRegistry()
  registry.request('tab-a')
  assert.equal(registry.settle('tab-a', 'success', 1), true)
  assert.equal(registry.get('tab-a').state, 'success')
  // Already settled — a duplicate settle for the same nonce does nothing.
  assert.equal(registry.settle('tab-a', 'error', 1), false)
  assert.equal(registry.get('tab-a').state, 'success')
})

test('registry: stale-nonce settle/clear cannot overwrite a newer attempt', () => {
  const registry = new PaneReconnectRegistry()
  registry.request('tab-a') // nonce 1, connecting
  // An old request (nonce 1) fails late; meanwhile the tab cycled:
  registry.settle('tab-a', 'success', 1)
  registry.clear('tab-a', 1)
  registry.request('tab-a') // nonce 2, connecting
  // Late failure callback carrying the OLD nonce must not flip the new attempt.
  assert.equal(registry.settle('tab-a', 'error', 1), false)
  assert.equal(registry.get('tab-a').state, 'connecting')
  assert.equal(registry.clear('tab-a', 1), undefined)
  assert.equal(registry.get('tab-a').state, 'connecting')
  // The current nonce settles normally.
  assert.equal(registry.settle('tab-a', 'error', 2), true)
  assert.equal(registry.get('tab-a').state, 'error')
})

test('registry: clear returns the tab to idle and the next request is accepted again', () => {
  const registry = new PaneReconnectRegistry()
  registry.request('tab-a')
  registry.settle('tab-a', 'error')
  registry.clear('tab-a')
  assert.deepEqual(registry.get('tab-a'), { state: 'idle', nonce: 1 })
  const retry = registry.request('tab-a')
  assert.deepEqual(retry, { nonce: 2, deduped: false })
})

test('registry: tabs are isolated — one in-flight reconnect never affects another', () => {
  const registry = new PaneReconnectRegistry()
  const a = registry.request('tab-a')
  const b = registry.request('tab-b')
  assert.deepEqual(a, { nonce: 1, deduped: false })
  assert.deepEqual(b, { nonce: 1, deduped: false })
  assert.equal(registry.isConnecting('tab-a'), true)
  assert.equal(registry.isConnecting('tab-b'), true)
  // Dedup is per-tab: a repeat on A doesn't touch B.
  assert.equal(registry.request('tab-a').deduped, true)
  assert.equal(registry.request('tab-b').deduped, true)
  registry.settle('tab-a', 'success')
  assert.equal(registry.get('tab-a').state, 'success')
  assert.equal(registry.get('tab-b').state, 'connecting')
  registry.clear('tab-a')
  assert.equal(registry.get('tab-a').state, 'idle')
  assert.equal(registry.get('tab-b').state, 'connecting')
})

test('registry: reactive factory returns a working registry', () => {
  const registry = createPaneReconnectRegistry()
  assert.equal(registry.request('tab-a').deduped, false)
  assert.equal(registry.isConnecting('tab-a'), true)
})

// ── terminalStore wiring ────────────────────────────────────────────────────

test('store: requestPaneReconnect rejects unknown/empty tab ids', () => {
  const store = freshStore()
  store.tabs.push(makeTab('tab-a', 'A'))
  assert.equal(store.requestPaneReconnect(''), false)
  assert.equal(store.requestPaneReconnect('does-not-exist'), false)
  assert.equal(store.paneReconnectStatus('does-not-exist').state, 'idle')
})

test('store: first request dispatches; in-flight repeat is deduped; status settles and clears', () => {
  const store = freshStore()
  store.tabs.push(makeTab('tab-a', 'A'))

  assert.equal(store.requestPaneReconnect('tab-a'), true)
  assert.equal(store.paneReconnectStatus('tab-a').state, 'connecting')
  assert.equal(store.paneReconnectStatus('tab-a').nonce, 1)

  // Concurrent click: coalesced, no new nonce.
  assert.equal(store.requestPaneReconnect('tab-a'), false)
  assert.equal(store.paneReconnectStatus('tab-a').nonce, 1)

  assert.equal(store.settlePaneReconnect('tab-a', 'success'), true)
  assert.equal(store.paneReconnectStatus('tab-a').state, 'success')
  store.clearPaneReconnect('tab-a')
  assert.equal(store.paneReconnectStatus('tab-a').state, 'idle')

  // After clear a fresh request is dispatched with a new nonce.
  assert.equal(store.requestPaneReconnect('tab-a'), true)
  assert.equal(store.paneReconnectStatus('tab-a').nonce, 2)
  assert.equal(store.settlePaneReconnect('tab-a', 'error'), true)
  assert.equal(store.paneReconnectStatus('tab-a').state, 'error')
})

test('store: reconnect requests are isolated per tab', () => {
  const store = freshStore()
  store.tabs.push(makeTab('tab-a', 'A'))
  store.tabs.push(makeTab('tab-b', 'B'))

  assert.equal(store.requestPaneReconnect('tab-a'), true)
  assert.equal(store.requestPaneReconnect('tab-b'), true)
  assert.equal(store.paneReconnectStatus('tab-a').nonce, 1)
  assert.equal(store.paneReconnectStatus('tab-b').nonce, 1)
  assert.equal(store.requestPaneReconnect('tab-a'), false)
  assert.equal(store.settlePaneReconnect('tab-a', 'success'), true)
  assert.equal(store.paneReconnectStatus('tab-a').state, 'success')
  assert.equal(store.paneReconnectStatus('tab-b').state, 'connecting')
})

// ── Component wiring (source-level contracts) ───────────────────────────────

test('wiring: TerminalPane renders the reconnect button only on Terminal panes and dispatches the store action', async () => {
  const source = await readFile(
    new URL('../src/components/TerminalPane.vue', import.meta.url),
    'utf8',
  )
  assert.match(source, /class="pane-reconnect-button"/)
  // Hidden for Chat panes (StructuredPane owns that surface).
  assert.match(source, /v-if="!isChatSession"/)
  assert.match(source, /store\.requestPaneReconnect\(/)
  // Disabled while in flight, and the click never activates/switches the pane.
  assert.match(source, /:disabled="isReconnecting"/)
  assert.match(source, /@click\.stop="handleReconnect"/)
  // Reachable labels for all three states plus the idle title.
  assert.match(source, /Reconnect terminal/)
  assert.match(source, /Reconnecting…/)
  assert.match(source, /Reconnect failed — click to retry/)
})

test('wiring: TerminalView performs reconnect via the existing retryTab re-attach path and settles on load/error', async () => {
  const source = await readFile(
    new URL('../src/components/TerminalView.vue', import.meta.url),
    'utf8',
  )
  // The nonce watcher drives the SAME path the overlay Retry button uses.
  assert.match(source, /paneReconnectStatus\(props\.tabId\)/)
  assert.match(source, /retryTab\(next\.tabId\)/)
  // Success/error settlement in the iframe lifecycle handlers.
  assert.match(source, /settlePaneReconnect\(tabId, 'success'\)/)
  assert.match(source, /settlePaneReconnect\(tabId, 'error'\)/)
})
