import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { setImmediate } from 'node:timers'
import { pathToFileURL } from 'node:url'
import test from 'node:test'
import ts from 'typescript'
import { ref } from 'vue'

const require = createRequire(import.meta.url)
const source = readFileSync(new URL('../src/composables/useChatGoal.ts', import.meta.url), 'utf8')
  .replace('onUnmounted, ', '')
  .replace("from 'vue'", `from '${pathToFileURL(require.resolve('vue')).href}'`)
  .replace('onUnmounted(dispose)', '')
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}).outputText
const { useChatGoal } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
const snapshot = overrides => ({
  id: 'goal-1', tab_id: 'tab-1', status: 'active', dispatch_state: 'dispatched', ...overrides,
})
const response = body => new Response(JSON.stringify(body), { status: 200 })
const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

test('clear preserves unconfirmed cancellation and hides only a stopped Goal', async t => {
  const state = useChatGoal(ref('tab-1'))
  t.after(state.dispose)
  t.mock.method(globalThis, 'fetch', async () => response(snapshot()))
  await state.hydrate()
  globalThis.fetch = async () => response(snapshot({ status: 'cancelled', dispatch_state: 'uncertain' }))
  assert.equal(await state.clear(), true)
  assert.equal(state.goal.value.dispatch_state, 'uncertain')
  globalThis.fetch = async () => response(snapshot({ status: 'cancelled', dispatch_state: 'idle' }))
  await state.clear()
  assert.equal(state.goal.value, null)
})

test('lost mutation response reconciles server state before showing the error', async t => {
  const state = useChatGoal(ref('tab-1'))
  t.after(state.dispose)
  t.mock.method(globalThis, 'fetch', async () => response(snapshot()))
  await state.hydrate()
  globalThis.fetch = async (_url, options) => {
    if (options.method) throw new Error('connection lost')
    return response(snapshot({ status: 'paused', dispatch_state: 'idle' }))
  }
  assert.equal(await state.pause(), false)
  assert.equal(state.goal.value.status, 'paused')
  assert.equal(state.error.value, 'connection lost')
  assert.equal(state.isMutating.value, false)
})

test('stale hydration cannot overwrite a successful mutation', async t => {
  const state = useChatGoal(ref('tab-1'))
  t.after(state.dispose)
  t.mock.method(globalThis, 'fetch', async () => response(snapshot()))
  await state.hydrate()
  const pending = deferred()
  globalThis.fetch = async (_url, options) => options.method
    ? response(snapshot({ status: 'paused', dispatch_state: 'idle' })) : pending.promise
  const hydration = state.hydrate()
  await state.pause()
  pending.resolve(response(snapshot()))
  await hydration
  assert.equal(state.goal.value.status, 'paused')
})

test('changing tabs isolates a pending mutation and loads the new Goal', async t => {
  const tab = ref('tab-1')
  const state = useChatGoal(tab)
  t.after(state.dispose)
  t.mock.method(globalThis, 'fetch', async () => response(snapshot()))
  await state.hydrate()
  const pending = deferred()
  const newTab = deferred()
  globalThis.fetch = async (_url, options) => options.method ? pending.promise : newTab.promise
  const pause = state.pause()
  tab.value = 'tab-2'
  pending.resolve(response(snapshot({ status: 'paused' })))
  assert.equal(await pause, false)
  assert.equal(state.goal.value, null)
  newTab.resolve(response(snapshot({ id: 'goal-2', tab_id: 'tab-2' })))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(state.goal.value.id, 'goal-2')
})

test('Goal creation sends only an objective and idempotency key', async t => {
  const state = useChatGoal(ref('tab-1'))
  t.after(state.dispose)
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/tabs/tab-1/goal')
    assert.equal(options.method, 'POST')
    const body = JSON.parse(options.body)
    assert.deepEqual(Object.keys(body).sort(), ['client_request_id', 'objective'])
    assert.equal(body.objective, 'Ship the feature')
    assert.ok(body.client_request_id)
    return response(snapshot())
  })
  assert.equal(await state.create({ objective: 'Ship the feature' }), true)
  assert.equal(state.goal.value.status, 'active')
})
