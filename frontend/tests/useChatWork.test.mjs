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
const source = readFileSync(new URL('../src/composables/useChatWork.ts', import.meta.url), 'utf8')
  .replace("from 'vue'", `from '${pathToFileURL(require.resolve('vue')).href}'`)
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}).outputText
const { useChatWork } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
const snapshot = overrides => ({
  id: 'work-1', source_tab_id: 'tab-1', kind: 'monitor', status: 'running', executions: [], ...overrides,
})
const response = body => new Response(JSON.stringify(body), { status: 200 })
const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
const tick = () => new Promise(resolve => setImmediate(resolve))

test('repeated activation does not duplicate readers; inactive work does not fetch', async t => {
  const state = useChatWork(ref('tab-1'))
  t.after(state.dispose)
  let reads = 0
  const pending = deferred()
  t.mock.method(globalThis, 'fetch', async () => { reads++; return pending.promise })
  await state.refresh()
  assert.equal(reads, 0)
  const started = state.start()
  await state.start()
  await state.refresh()
  assert.equal(reads, 1)
  pending.resolve(response([snapshot()]))
  await started
  state.stop()
  await state.refresh()
  assert.equal(reads, 1)
})

test('switching tabs discards stale reads and never displays another Chat work item', async t => {
  const tab = ref('tab-1')
  const state = useChatWork(tab)
  t.after(state.dispose)
  const oldRead = deferred()
  t.mock.method(globalThis, 'fetch', async url => url.includes('tab-1')
    ? oldRead.promise
    : response([snapshot(), snapshot({ id: 'work-2', source_tab_id: 'tab-2' })]))
  const started = state.start()
  tab.value = 'tab-2'
  await tick()
  assert.deepEqual(state.work.value.map(item => item.id), ['work-2'])
  oldRead.resolve(response([snapshot()]))
  await started
  assert.deepEqual(state.work.value.map(item => item.id), ['work-2'])
})

test('a stale poll cannot overwrite a successful stop', async t => {
  const state = useChatWork(ref('tab-1'))
  t.after(state.dispose)
  t.mock.method(globalThis, 'fetch', async () => response([snapshot()]))
  await state.start()
  const oldRead = deferred()
  globalThis.fetch = async (_url, options) => options.method
    ? response(snapshot({ status: 'stopped' })) : oldRead.promise
  const read = state.refresh()
  assert.equal(await state.update('work-1', { action: 'stop' }), true)
  oldRead.resolve(response([snapshot()]))
  await read
  assert.equal(state.work.value[0].status, 'stopped')
})

test('a lost mutation response reconciles applied server state', async t => {
  const state = useChatWork(ref('tab-1'))
  t.after(state.dispose)
  t.mock.method(globalThis, 'fetch', async () => response([snapshot()]))
  await state.start()
  globalThis.fetch = async (_url, options) => {
    if (options.method) throw new Error('connection lost')
    return response([snapshot({ status: 'stopped' })])
  }
  assert.equal(await state.update('work-1', { action: 'stop' }), false)
  assert.equal(state.work.value[0].status, 'stopped')
  assert.equal(state.stale.value, false)
  assert.equal(state.error.value, 'connection lost')
})

test('unreconciled lost response disables further mutations until refresh succeeds', async t => {
  const state = useChatWork(ref('tab-1'))
  t.after(state.dispose)
  t.mock.method(globalThis, 'fetch', async () => response([snapshot()]))
  await state.start()
  let calls = 0
  globalThis.fetch = async () => { calls++; throw new Error('offline') }
  assert.equal(await state.update('work-1', { action: 'stop' }), false)
  assert.equal(state.stale.value, true)
  assert.equal(calls, 2)
  assert.equal(await state.update('work-1', { action: 'stop' }), false)
  assert.equal(calls, 2)
  globalThis.fetch = async () => response([snapshot({ status: 'stopped' })])
  await state.refresh()
  assert.equal(state.stale.value, false)
  assert.equal(state.work.value[0].status, 'stopped')
})

test('changing tabs isolates pending mutations and targets the original request URL', async t => {
  const tab = ref('tab-1')
  const state = useChatWork(tab)
  t.after(state.dispose)
  t.mock.method(globalThis, 'fetch', async () => response([snapshot()]))
  await state.start()
  const pending = deferred()
  globalThis.fetch = async (url, options) => {
    if (options.method) {
      assert.equal(url, '/api/tabs/tab-1/work/work-1')
      assert.deepEqual(JSON.parse(options.body), { action: 'stop' })
      return pending.promise
    }
    return response([snapshot({ id: 'work-2', source_tab_id: 'tab-2' })])
  }
  const update = state.update('work-1', { action: 'stop' })
  tab.value = 'tab-2'
  await tick()
  pending.resolve(response(snapshot({ status: 'stopped' })))
  assert.equal(await update, false)
  assert.equal(state.work.value[0].id, 'work-2')
  assert.equal(state.busyId.value, null)
})

test('only one mutation can be active and unknown work IDs are rejected locally', async t => {
  const state = useChatWork(ref('tab-1'))
  t.after(state.dispose)
  t.mock.method(globalThis, 'fetch', async () => response([snapshot()]))
  await state.start()
  const pending = deferred()
  let writes = 0
  globalThis.fetch = async () => { writes++; return pending.promise }
  assert.equal(await state.update('other', { action: 'stop' }), false)
  const first = state.update('work-1', { action: 'stop' })
  assert.equal(await state.update('work-1', { action: 'stop' }), false)
  assert.equal(writes, 1)
  pending.resolve(response(snapshot({ status: 'stopped' })))
  await first
})

test('deactivation aborts reads and activation restores authoritative persisted results', async t => {
  const state = useChatWork(ref('tab-1'))
  t.after(state.dispose)
  const pending = deferred()
  let signal
  t.mock.method(globalThis, 'fetch', async (_url, options) => { signal = options.signal; return pending.promise })
  const started = state.start()
  state.stop()
  assert.equal(signal.aborted, true)
  pending.resolve(response([snapshot()]))
  await started
  assert.deepEqual(state.work.value, [])
  globalThis.fetch = async () => response([snapshot({ status: 'completed', latest_result: { summary: 'Finished while away' } })])
  await state.start()
  assert.equal(state.work.value[0].latest_result.summary, 'Finished while away')
})


test('retired ChatWork mutations never reach the network',async t=>{
 const state=useChatWork(ref('tab-1'));t.after(state.dispose);let writes=0
 t.mock.method(globalThis,'fetch',async(_url,options={})=>{
  if(options.method)writes++
  return response(options.method?snapshot({status:'stopped'}):[snapshot()])
 })
 await state.start()
 assert.equal(await state.update('work-1',{action:'resume'}),false)
 assert.equal(await state.update('work-1',{action:'pause'}),false)
 assert.equal(await state.update('work-1',{interval_seconds:60}),false)
 assert.equal(await state.update('work-1',{action:'stop',interval_seconds:60}),false)
 assert.equal(writes,0)
 assert.equal(await state.update('work-1',{action:'stop'}),true)
 assert.equal(writes,1)
})
