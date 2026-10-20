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

for (const clearResponse of ['null', '204']) {
  test(`native Goal clear accepts authoritative ${clearResponse} absence`, async t => {
    const state = useChatGoal(ref('tab-1'))
    t.after(state.dispose)
    t.mock.method(globalThis, 'fetch', async () => response(snapshot({ execution_owner: 'provider_native' })))
    await state.hydrate()
    globalThis.fetch = async () => clearResponse === '204' ? new Response(null, { status: 204 }) : response(null)
    assert.equal(await state.clear(), true)
    assert.equal(state.goal.value, null)
    assert.equal(state.error.value, null)
    assert.equal(state.isHydrated.value, true)
  })
}

test('native limited states and ownership fields survive hydration unchanged', async t => {
  const state = useChatGoal(ref('tab-1'))
  t.after(state.dispose)
  const native = snapshot({
    execution_owner: 'provider_native', status: 'usageLimited', provider_status: 'usageLimited',
    provider_thread_id: 'thread-1', provider_goal_created_at: 100, provider_updated_at: 200,
  })
  t.mock.method(globalThis, 'fetch', async () => response(native))
  await state.hydrate()
  assert.deepEqual(state.goal.value, native)
  globalThis.fetch = async () => response(null)
  await state.hydrate()
  assert.equal(state.hasGoal.value, false)
  assert.equal(state.error.value, null)
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

test('objective edits retain request identity after a lost response and clear it after success', async t => {
  const state = useChatGoal(ref('tab-1'))
  t.after(state.dispose)
  const mutations = []
  let fail = true
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (!options.method) return response(snapshot({ objective: 'Old objective' }))
    assert.equal(options.method, 'PATCH')
    assert.equal(url, '/api/goals/goal-1/objective')
    const body = JSON.parse(options.body)
    mutations.push(body)
    if (fail) throw new Error('response lost')
    return response(snapshot({ objective: body.objective }))
  })
  await state.hydrate()
  assert.equal(await state.updateObjective('New objective'), false)
  fail = false
  assert.equal(await state.updateObjective('New objective'), true)
  assert.equal(mutations[0].client_request_id, mutations[1].client_request_id)
  assert.equal(state.goal.value.objective, 'New objective')
  await state.updateObjective('New objective')
  assert.notEqual(mutations[1].client_request_id, mutations[2].client_request_id)
})
