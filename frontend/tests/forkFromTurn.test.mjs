import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'
import { createPinia, setActivePinia } from 'pinia'

// ── Load the real terminalStore in Node ─────────────────────────────────────
// Rather than mount the whole Vue app, transpile terminalStore.ts to ESM,
// rewrite its bare `pinia`/`vue` imports to absolute file URLs (so they
// resolve from a data: URL, which has no base of its own), and import it.
// Type-only imports (@/types) are erased by the transpiler.
const storeSource = await readFile(
  new URL('../src/stores/terminalStore.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(storeSource, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const piniaUrl = await import.meta.resolve('pinia')
const vueUrl = await import.meta.resolve('vue')
const rewritten = outputText
  .replace(/from\s+['"]pinia['"]/g, `from ${JSON.stringify(piniaUrl)}`)
  .replace(/from\s+['"]vue['"]/g, `from ${JSON.stringify(vueUrl)}`)
const storeModule = await import(
  `data:text/javascript;base64,${Buffer.from(rewritten).toString('base64')}`
)
const useTerminalStore = storeModule.useTerminalStore

// ── Browser globals the store touches ───────────────────────────────────────
// Use Reflect.apply wrappers that read globalThis at call time so that
// mock.timers (which swaps globalThis.setTimeout) is honored when enabled.
if (!globalThis.window) {
  globalThis.window = {
    setTimeout: (...args) => Reflect.apply(globalThis.setTimeout, globalThis, args),
    clearTimeout: (...args) => Reflect.apply(globalThis.clearTimeout, globalThis, args),
    setInterval: (...args) => Reflect.apply(globalThis.setInterval, globalThis, args),
    clearInterval: (...args) => Reflect.apply(globalThis.clearInterval, globalThis, args),
  }
}
if (!globalThis.localStorage) {
  globalThis.localStorage = {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  }
}

// ── Helpers ─────────────────────────────────────────────────────────────────
function makeTab(id, name) {
  return {
    id,
    name,
    shell: 'bash',
    agent_type: 'claude',
    session_kind: 'chat',
    port: 0,
    created_at: new Date().toISOString(),
    is_active: false,
  }
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

function freshStore() {
  setActivePinia(createPinia())
  return useTerminalStore()
}

// Seed a 1x1 layout with the source tab in the active pane.
function seedSinglePane(store, sourceTab, otherTab) {
  store.tabs.push(sourceTab)
  if (otherTab) store.tabs.push(otherTab)
  store.initializePanes()
  store.assignTabToPane(sourceTab.id)
}

// ── Fix 1: fork must not override the user's tab switch ─────────────────────

test('fork resolving after a tab switch does not override the switch', async () => {
  const store = freshStore()
  const tabA = makeTab('tab-a', 'A')
  const tabB = makeTab('tab-b', 'B')
  seedSinglePane(store, tabA, tabB)

  const forkData = makeTab('fork-1', 'Fork of A')
  const forkDeferred = deferred()
  globalThis.fetch = (url, opts) => {
    assert.ok(url.includes('/tabs/tab-a/fork'), 'fork endpoint hit')
    assert.equal(opts.method, 'POST')
    return forkDeferred.promise.then(() => ({
      ok: true,
      json: async () => forkData,
    }))
  }

  // Kick off the fork, then switch to B before it resolves.
  const forkPromise = store.forkTab(tabA.id, 0)
  store.setActiveTab(tabB.id)
  assert.equal(store.activeTabId, tabB.id)

  forkDeferred.resolve()
  const result = await forkPromise

  assert.equal(result.id, 'fork-1')
  assert.ok(store.tabs.some(t => t.id === 'fork-1'), 'fork tab is added regardless')
  assert.equal(
    store.activeTabId,
    tabB.id,
    'user switch is preserved — not yanked to the fork',
  )
  assert.ok(
    store.notifications.some(
      n => n.type === 'success' && n.message.includes('Fork created'),
    ),
    'non-intrusive success notification shown',
  )
})

test('fork resolving while still on the source tab auto-switches to the fork', async () => {
  const store = freshStore()
  const tabA = makeTab('tab-a', 'A')
  seedSinglePane(store, tabA)

  const forkData = makeTab('fork-1', 'Fork of A')
  const forkDeferred = deferred()
  globalThis.fetch = () =>
    forkDeferred.promise.then(() => ({ ok: true, json: async () => forkData }))

  // No switch — the user stays on A.
  const forkPromise = store.forkTab(tabA.id, 0)
  forkDeferred.resolve()
  await forkPromise

  assert.equal(store.activeTabId, 'fork-1', 'auto-switched to the fork')
  const pane = store.panes.find(p => p.id === store.activePaneId)
  assert.equal(pane.tabId, 'fork-1', 'fork assigned to the active pane')
})

// ── Fix 2: fork fetch timeout ───────────────────────────────────────────────

test('fork fetch that hangs times out, resets forkingOrdinal, and shows an error', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })

  const store = freshStore()
  const tabA = makeTab('tab-a', 'A')
  seedSinglePane(store, tabA)

  // Hanging fetch that only settles (rejects) when the abort signal fires.
  globalThis.fetch = (url, opts) =>
    new Promise((resolve, reject) => {
      opts.signal.addEventListener('abort', () => {
        const err = new Error('The operation was aborted')
        err.name = 'AbortError'
        reject(err)
      })
    })

  // Mirror StructuredPane.forkFromTurn's forkingOrdinal bookkeeping so we can
  // assert the fork buttons get re-enabled.
  let forkingOrdinal = null
  const forkFromTurn = async ordinal => {
    if (forkingOrdinal !== null) return
    forkingOrdinal = ordinal
    try {
      await store.forkTab(tabA.id, ordinal)
    } finally {
      forkingOrdinal = null
    }
  }

  const p = forkFromTurn(0)
  assert.equal(forkingOrdinal, 0, 'forkingOrdinal set while in flight')

  // Fast-forward past the 30s fork timeout.
  t.mock.timers.tick(30000)
  await p

  assert.equal(forkingOrdinal, null, 'forkingOrdinal reset after timeout')
  assert.equal(store.isLoading, false, 'isLoading reset')
  assert.ok(
    store.notifications.some(
      n => n.type === 'error' && n.message.includes('timed out'),
    ),
    'timeout error shown',
  )
  assert.ok(
    !store.tabs.some(tab => tab.name === 'Fork of A'),
    'no fork tab created on timeout',
  )
})
