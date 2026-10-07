import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const storeSource = await readFile(
  new URL('../src/stores/workspaceStore.ts', import.meta.url),
  'utf8',
)
const paginationSource = await readFile(
  new URL('../src/utils/boardPagination.ts', import.meta.url),
  'utf8',
)
const vueUrl = await import.meta.resolve('vue')
const piniaUrl = await import.meta.resolve('pinia')
const { createPinia, getActivePinia, setActivePinia } = await import(piniaUrl)
const transpileOptions = {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
}

function dataModule(source) {
  return `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
}

const paginationJs = ts.transpileModule(paginationSource, transpileOptions).outputText
const paginationUrl = dataModule(paginationJs)
const storeJs = ts.transpileModule(storeSource, transpileOptions).outputText
const rewrittenStoreJs = storeJs
  .replace(/from\s+['"]pinia['"]/g, `from ${JSON.stringify(piniaUrl)}`)
  .replace(/from\s+['"]vue['"]/g, `from ${JSON.stringify(vueUrl)}`)
  .replace(
    /from\s+['"]@\/utils\/boardPagination['"]/g,
    `from ${JSON.stringify(paginationUrl)}`,
  )
const { useWorkspaceStore } = await import(dataModule(rewrittenStoreJs))

const VALID_CAPABILITIES = {
  supported_execution_controls: ['workspace', 'initiator'],
  progress_states: ['started', 'working', 'blocked', 'needs_input', 'completed', 'failed', 'released'],
  record_only_requires_reporter_key: true,
  handoff_requires_release: true,
  legacy_chat_work_create: false,
}

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null
    },
    setItem(key, value) {
      values.set(String(key), String(value))
    },
    removeItem(key) {
      values.delete(key)
    },
    clear() {
      values.clear()
    },
    key(index) {
      return [...values.keys()][index] ?? null
    },
    get length() {
      return values.size
    },
  }
}

function replaceGlobal(name, value) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, name)
  Object.defineProperty(globalThis, name, {
    configurable: true,
    writable: true,
    value,
  })
  return () => {
    if (previous) Object.defineProperty(globalThis, name, previous)
    else delete globalThis[name]
  }
}

async function withStore(fetchImpl, run, activeWorkspaceId = 'ws-a') {
  const storage = memoryStorage({
    claude_hub_active_workspace_id: activeWorkspaceId,
  })
  const restoreFetch = replaceGlobal('fetch', fetchImpl)
  const restoreStorage = replaceGlobal('localStorage', storage)
  const timers = []
  const restoreWindow = replaceGlobal('window', {
    setTimeout(callback, milliseconds) {
      const timer = setTimeout(callback, milliseconds)
      timers.push(timer)
      return timer
    },
  })
  const previousPinia = getActivePinia()
  setActivePinia(createPinia())
  try {
    const store = useWorkspaceStore()
    return await run(store, storage)
  } finally {
    setActivePinia(previousPinia)
    for (const timer of timers) clearTimeout(timer)
    restoreWindow()
    restoreStorage()
    restoreFetch()
  }
}

function assertEmptyCapabilities(store, status, workspaceId = 'ws-a') {
  assert.equal(store.taskCapabilities, null)
  assert.equal(store.taskCapabilitiesWorkspaceId, workspaceId)
  assert.equal(store.taskCapabilitiesStatus, status)
}

test('supported capabilities filter unknown future values', async () => {
  const requests = []
  await withStore(
    async (url, options) => {
      requests.push({ url, signal: options?.signal })
      return jsonResponse({
        ...VALID_CAPABILITIES,
        supported_execution_controls: ['workspace', 'future-control', 'initiator'],
        progress_states: ['started', 'future-state', 'working', 'released'],
      })
    },
    async store => {
      const value = await store.fetchTaskCapabilities('ws-a')
      assert.deepEqual(value, {
        ...VALID_CAPABILITIES,
        supported_execution_controls: ['workspace', 'initiator'],
        progress_states: ['started', 'working', 'released'],
      })
      assert.deepEqual(store.taskCapabilities, value)
      assert.equal(store.taskCapabilitiesWorkspaceId, 'ws-a')
      assert.equal(store.taskCapabilitiesStatus, 'supported')
      assert.equal(store.taskCapabilitiesError, null)
    },
  )
  assert.equal(requests.length, 1)
  assert.equal(requests[0].url, '/api/workspaces/ws-a/task-capabilities')
  assert.ok(requests[0].signal instanceof AbortSignal)
  assert.equal(requests[0].signal.aborted, false)
})

test('only 404 is classified as unsupported', async () => {
  await withStore(
    async () => new Response('not json', { status: 404 }),
    async store => {
      assert.equal(await store.fetchTaskCapabilities('ws-a'), null)
      assertEmptyCapabilities(store, 'unsupported')
      assert.equal(store.taskCapabilitiesError, null)
    },
  )
})

test('non-404 HTTP failures are errors, not unsupported', async t => {
  for (const status of [410, 503]) {
    await t.test(String(status), async () => {
      await withStore(
        async () => jsonResponse({ detail: 'must not be exposed' }, status),
        async store => {
          await assert.rejects(
            store.fetchTaskCapabilities('ws-a'),
            /Task execution options could not be loaded\./,
          )
          assertEmptyCapabilities(store, 'error')
          assert.equal(
            store.taskCapabilitiesError,
            'Task execution options could not be loaded.',
          )
        },
      )
    })
  }
})

test('invalid JSON and invalid schemas are errors', async t => {
  const cases = [
    {
      name: 'invalid JSON',
      response: () => new Response('{', {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    },
    {
      name: 'invalid schema',
      response: () => jsonResponse({
        ...VALID_CAPABILITIES,
        record_only_requires_reporter_key: 'yes',
      }),
    },
    {
      name: 'known values required after future-value filtering',
      response: () => jsonResponse({
        ...VALID_CAPABILITIES,
        supported_execution_controls: ['future-control'],
        progress_states: ['future-state'],
      }),
    },
  ]
  for (const scenario of cases) {
    await t.test(scenario.name, async () => {
      await withStore(
        async () => scenario.response(),
        async store => {
          await assert.rejects(
            store.fetchTaskCapabilities('ws-a'),
            /Task execution options returned an invalid response\./,
          )
          assertEmptyCapabilities(store, 'error')
          assert.equal(
            store.taskCapabilitiesError,
            'Task execution options returned an invalid response.',
          )
        },
      )
    })
  }
})

test('a deferred old Workspace fetch cannot overwrite the switched Workspace', async () => {
  const response = deferred()
  let signal
  await withStore(
    async (_url, options) => {
      signal = options?.signal
      return response.promise
    },
    async store => {
      const pending = store.fetchTaskCapabilities('ws-a')
      assert.equal(store.taskCapabilitiesStatus, 'loading')
      assert.equal(store.taskCapabilitiesWorkspaceId, 'ws-a')

      store.setActiveWorkspace('ws-b')
      assert.equal(signal.aborted, true)
      assert.equal(store.activeWorkspaceId, 'ws-b')
      assert.equal(store.taskCapabilities, null)
      assert.equal(store.taskCapabilitiesWorkspaceId, null)
      assert.equal(store.taskCapabilitiesStatus, 'idle')
      assert.equal(store.taskCapabilitiesError, null)

      response.resolve(jsonResponse(VALID_CAPABILITIES))
      assert.equal(await pending, null)
      assert.equal(store.activeWorkspaceId, 'ws-b')
      assert.equal(store.taskCapabilities, null)
      assert.equal(store.taskCapabilitiesWorkspaceId, null)
      assert.equal(store.taskCapabilitiesStatus, 'idle')
      assert.equal(store.taskCapabilitiesError, null)
    },
  )
})

test('deferred JSON from an old generation cannot replace the current result', async () => {
  const oldJsonStarted = deferred()
  const oldJson = deferred()
  const signals = []
  let calls = 0
  const oldValue = {
    ...VALID_CAPABILITIES,
    progress_states: ['failed'],
  }
  const currentValue = {
    ...VALID_CAPABILITIES,
    progress_states: ['working'],
  }

  await withStore(
    async (_url, options) => {
      signals.push(options?.signal)
      calls += 1
      if (calls === 1) {
        return {
          ok: true,
          status: 200,
          async json() {
            oldJsonStarted.resolve()
            return oldJson.promise
          },
        }
      }
      return jsonResponse(currentValue)
    },
    async store => {
      const oldRequest = store.fetchTaskCapabilities('ws-a')
      await oldJsonStarted.promise

      const currentResult = await store.fetchTaskCapabilities('ws-a')
      assert.deepEqual(currentResult, currentValue)
      assert.deepEqual(store.taskCapabilities, currentValue)
      assert.equal(store.taskCapabilitiesStatus, 'supported')
      assert.equal(signals[0].aborted, true)
      assert.equal(signals[1].aborted, false)

      oldJson.resolve(oldValue)
      assert.equal(await oldRequest, null)
      assert.deepEqual(store.taskCapabilities, currentValue)
      assert.equal(store.taskCapabilitiesWorkspaceId, 'ws-a')
      assert.equal(store.taskCapabilitiesStatus, 'supported')
      assert.equal(store.taskCapabilitiesError, null)
    },
  )
})

test('a stale Workspace call does not abort the active capabilities request', async () => {
  const response = deferred()
  let signal
  let fetchCalls = 0

  await withStore(
    async (_url, options) => {
      fetchCalls += 1
      signal = options?.signal
      return response.promise
    },
    async store => {
      const currentRequest = store.fetchTaskCapabilities('ws-a')
      assert.equal(fetchCalls, 1)
      assert.equal(store.taskCapabilitiesStatus, 'loading')

      assert.equal(await store.fetchTaskCapabilities('ws-b'), null)
      assert.equal(fetchCalls, 1)
      assert.equal(signal.aborted, false)
      assert.equal(store.taskCapabilitiesWorkspaceId, 'ws-a')
      assert.equal(store.taskCapabilitiesStatus, 'loading')

      response.resolve(jsonResponse(VALID_CAPABILITIES))
      assert.deepEqual(await currentRequest, VALID_CAPABILITIES)
      assert.deepEqual(store.taskCapabilities, VALID_CAPABILITIES)
      assert.equal(store.taskCapabilitiesWorkspaceId, 'ws-a')
      assert.equal(store.taskCapabilitiesStatus, 'supported')
      assert.equal(store.taskCapabilitiesError, null)
    },
  )
})


const modernCreate = {
  title: 'A focused goal', prompt: 'Verify the receipt',
  request_key: 'create-call', execution_control: 'initiator', reporter_key: 'x'.repeat(43),
}
const modernReceipt = {
  id: 'task-created', workspace_id: 'ws-a', execution_control: 'initiator',
  execution_epoch: 1, progress_revision: 0,
}

test('modern create rejects incomplete execution receipts without another POST', async t => {
  const invalid = [
    ['execution_epoch', undefined], ['execution_epoch', 0], ['execution_epoch', 1.5], ['execution_epoch', '1'],
    ['progress_revision', undefined], ['progress_revision', -1], ['progress_revision', 1.5], ['progress_revision', '0'],
    ['execution_control', undefined], ['execution_control', 'unknown'],
  ]
  for (const [field, value] of invalid) {
    await t.test(`${field}: ${String(value)}`, async () => {
      const requests = []
      const original = JSON.stringify(modernCreate)
      await withStore(async (url, options) => {
        requests.push({ url, options })
        return jsonResponse({ ...modernReceipt, [field]: value }, 201)
      }, async store => {
        await assert.rejects(store.createTask(modernCreate), /may have succeeded/)
        assert.equal(requests.length, 1)
        assert.equal(requests[0].options.method, 'POST')
        assert.equal(requests[0].options.body, original)
        assert.equal(JSON.stringify(modernCreate), original)
      })
    })
  }
})

test('legacy create can still accept the old receipt shape', async () => {
  const receipt = { id: 'task-old', workspace_id: 'ws-a' }
  await withStore(async () => jsonResponse(receipt), async store => {
    assert.deepEqual(await store.createTask({ title: 'Old', prompt: 'Old form' }), receipt)
  })
})

test('modern create accepts both initial and later-epoch replay receipts', async t => {
  for (const epoch of [1, 3]) {
    await t.test(`epoch ${epoch}`, async () => {
      const receipt = { ...modernReceipt, execution_epoch: epoch, progress_revision: epoch === 1 ? 0 : 7 }
      await withStore(async () => jsonResponse(receipt), async store => {
        assert.deepEqual(await store.createTask(modernCreate), receipt)
      })
    })
  }
})
