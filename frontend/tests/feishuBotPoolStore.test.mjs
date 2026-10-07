import assert from 'node:assert/strict'
import test from 'node:test'
import { deferred, flush, harness, pool, response } from './feishuBotUiTestHarness.mjs'

test('a GET started before a mutation cannot overwrite its result', async t => {
  const h = harness(t), waiting = deferred()
  h.setTransport(() => waiting.promise)
  const reading = h.store.refresh()
  h.store.applyPool(pool(2))
  waiting.resolve(response(pool(1)))
  await reading
  assert.equal(h.store.pool.pool_revision, 2)
  assert.equal(h.calls.length, 1)
})

test('recovery actually rereads when a newer snapshot arrives during its GET', async t => {
  const h = harness(t), first = deferred(), second = deferred()
  h.setTransport(() => h.calls.length === 1 ? first.promise : second.promise)
  t.after(async () => { first.resolve(response(pool(0, []))); second.resolve(response(pool(0, []))); await flush() })
  h.store.applyPool(pool(3))
  h.store.applyPool(pool(2))
  assert.equal(h.calls.length, 1)
  h.store.applyPool(pool(4))
  first.resolve(response(pool(0, [])))
  await flush()
  assert.equal(h.calls.length, 2)
  assert.equal(h.store.pool.pool_revision, 4)
  second.resolve(response(pool(0, [])))
  await flush()
  assert.equal(h.store.pool.pool_revision, 0)
})

test('two superseded recovery reads retain the latest snapshot without an endless loop', async t => {
  const h = harness(t), first = deferred(), second = deferred()
  h.setTransport(() => h.calls.length === 1 ? first.promise : second.promise)
  t.after(async () => { first.resolve(response(pool(0, []))); second.resolve(response(pool(0, []))); await flush() })
  h.store.applyPool(pool(3)); h.store.applyPool(pool(2)); h.store.applyPool(pool(4))
  first.resolve(response(pool(0, [])))
  await flush()
  assert.equal(h.calls.length, 2)
  h.store.applyPool(pool(5))
  second.resolve(response(pool(0, [])))
  await flush()
  assert.equal(h.store.pool.pool_revision, 5)
  assert.match(h.store.error, /Reload/)
  assert.equal(h.calls.length, 2)
})
