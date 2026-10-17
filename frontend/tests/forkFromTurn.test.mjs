import assert from 'node:assert/strict'
import test from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { useTerminalStore } from './terminalStoreTestHarness.mjs'

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
  assert.deepEqual(
    store.tabs.map(tab => tab.id),
    ['fork-1', 'tab-a', 'tab-b'],
    'fork is prepended without reordering existing sessions',
  )
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

  assert.deepEqual(store.tabs.map(tab => tab.id), ['fork-1', 'tab-a'])
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
