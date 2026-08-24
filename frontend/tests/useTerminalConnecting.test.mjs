import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

// The composable imports `ref` from 'vue'. For the unit test we don't need
// Vue's reactivity — just a plain mutable box. We replace the vue import with
// a local shim before transpiling.
const composableSource = await readFile(
  new URL('../src/composables/useTerminalConnecting.ts', import.meta.url),
  'utf8',
)
const shimmedSource = composableSource.replace(
  `import { ref, type Ref } from 'vue'`,
  `const ref = (initial) => ({ value: initial })`,
)
const { outputText } = ts.transpileModule(shimmedSource, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const {
  CONNECTING_TIMEOUT_MS,
  useTerminalConnecting,
  getConnectingState,
} = mod

// Fake timers so we don't wait 8s per test.
function withFakeTimers(fn) {
  const originalSetTimeout = globalThis.setTimeout
  const originalClearTimeout = globalThis.clearTimeout
  const timers = new Map()
  let nextId = 1
  let now = 0
  globalThis.setTimeout = (cb, ms) => {
    const id = nextId++
    timers.set(id, { cb, at: now + (ms ?? 0) })
    return id
  }
  globalThis.clearTimeout = (id) => {
    timers.delete(id)
  }
  const advance = (ms) => {
    now += ms
    for (const [id, t] of [...timers]) {
      if (t.at <= now) {
        timers.delete(id)
        t.cb()
      }
    }
  }
  try {
    return fn(advance)
  } finally {
    globalThis.setTimeout = originalSetTimeout
    globalThis.clearTimeout = originalClearTimeout
  }
}

function stateOf(api, tabId) {
  return getConnectingState(tabId, api.loadedTabIds.value, api.timeoutTabIds.value, api.errorTabIds.value)
}

test('initial state is connecting (overlay shown, no error)', () => {
  const api = useTerminalConnecting(() => {})
  assert.equal(stateOf(api, 't1'), 'connecting')
})

test('markLoaded clears the overlay and removes any timeout/error state', () => {
  withFakeTimers(() => {
    const api = useTerminalConnecting(() => {})
    api.startConnectingTimer('t1')
    api.markLoaded('t1')
    assert.equal(stateOf(api, 't1'), 'loaded')
    assert.equal(api.errorTabIds.value.has('t1'), false)
    assert.equal(api.timeoutTabIds.value.has('t1'), false)
  })
})

test('timeout transitions to "timeout" state (slow, not proven failure)', () => {
  withFakeTimers((advance) => {
    const api = useTerminalConnecting(() => {})
    api.startConnectingTimer('t1')
    advance(CONNECTING_TIMEOUT_MS + 1)
    assert.equal(stateOf(api, 't1'), 'timeout')
    // Not a hard error
    assert.equal(api.errorTabIds.value.has('t1'), false)
  })
})

test('markError (iframe error event) transitions to hard error state', () => {
  withFakeTimers(() => {
    const api = useTerminalConnecting(() => {})
    api.startConnectingTimer('t1')
    api.markError('t1')
    assert.equal(stateOf(api, 't1'), 'error')
  })
})

test('reload callback fires only for the retried tab', () => {
  withFakeTimers((advance) => {
    const reloaded = []
    const api = useTerminalConnecting((id) => reloaded.push(id))
    // t1 loads fine, t2 times out
    api.startConnectingTimer('t1')
    api.markLoaded('t1')
    api.startConnectingTimer('t2')
    advance(CONNECTING_TIMEOUT_MS + 1)
    // Retry only t2
    api.retryTab('t2')
    assert.deepEqual(reloaded, ['t2'])
    // t1 stays loaded
    assert.equal(stateOf(api, 't1'), 'loaded')
  })
})

test('cached-tab revisit does not flash (loaded tab stays loaded)', () => {
  withFakeTimers(() => {
    const api = useTerminalConnecting(() => {})
    api.markLoaded('t1')
    // Re-arming the timer for an already-loaded tab is a no-op.
    api.startConnectingTimer('t1')
    assert.equal(stateOf(api, 't1'), 'loaded')
  })
})

test('eviction resets state so a future re-add shows connecting again', () => {
  withFakeTimers(() => {
    const api = useTerminalConnecting(() => {})
    api.markLoaded('t1')
    assert.equal(stateOf(api, 't1'), 'loaded')
    api.resetTabConnectingState('t1')
    assert.equal(stateOf(api, 't1'), 'connecting')
  })
})

test('clearAllTimers cancels pending timeouts (no timeout after unmount)', () => {
  withFakeTimers((advance) => {
    const api = useTerminalConnecting(() => {})
    api.startConnectingTimer('t1')
    api.clearAllTimers()
    advance(CONNECTING_TIMEOUT_MS + 1)
    assert.equal(api.timeoutTabIds.value.has('t1'), false)
    assert.equal(api.errorTabIds.value.has('t1'), false)
  })
})

test('retry resets timeout state and re-arms the timer', () => {
  withFakeTimers((advance) => {
    const reloaded = []
    const api = useTerminalConnecting((id) => reloaded.push(id))
    api.startConnectingTimer('t1')
    advance(CONNECTING_TIMEOUT_MS + 1)
    assert.equal(stateOf(api, 't1'), 'timeout')
    api.retryTab('t1')
    // After retry, state is connecting again (not timeout, not error, not loaded).
    assert.equal(stateOf(api, 't1'), 'connecting')
    assert.deepEqual(reloaded, ['t1'])
  })
})

test('markError overrides a pending timeout (hard error wins)', () => {
  withFakeTimers((advance) => {
    const api = useTerminalConnecting(() => {})
    api.startConnectingTimer('t1')
    advance(CONNECTING_TIMEOUT_MS + 1)
    assert.equal(stateOf(api, 't1'), 'timeout')
    // An actual iframe error fires after the timeout — hard error wins.
    api.markError('t1')
    assert.equal(stateOf(api, 't1'), 'error')
  })
})
