import assert from 'node:assert/strict'
import test from 'node:test'
import { bindingHarness, bot, claim, code, config, deferred, flush, pool, response } from './feishuBotUiTestHarness.mjs'

async function claimed(h) {
  await h.api.resume()
  await h.api.generateCode()
  h.setServer(pool(2, [bot('a', { revision: 2, my_claims: [claim()] }), bot('b')]))
  h.setNow(1500)
  await h.fire()
  assert.equal(h.api.viewState.value, 'claimed')
}

test('resume refreshes without a pending attempt and does not duplicate an in-flight initial read', async t => {
  const h = bindingHarness(t), waiting = deferred()
  h.setTransport(() => waiting.promise)
  const first = h.api.resume()
  await h.api.resume()
  assert.equal(h.calls.length, 1)
  waiting.resolve(response(h.server()))
  await first
})

test('claim polling outlives code TTL and ends at claim expiry', async t => {
  const h = bindingHarness(t)
  await claimed(h)
  assert.equal(h.timers.size, 1)
  h.setNow(2500); await h.fire()
  assert.equal(h.api.viewState.value, 'claimed')
  assert.equal(h.timers.size, 1)
  h.setNow(4500); await h.fire()
  assert.match(h.api.error.value, /claimed pairing expired/)
  assert.equal(h.timers.size, 0)
})

test('poll and manual refresh cannot abort a pending activation or strand the mutation flag', async t => {
  const waiting = deferred()
  let signal
  const result = pool(3, [bot('a', { revision: 3, binding: {
    pairing_id: 'bound', state: 'active', tab_id: 'tab-1', workspace_id: null,
    created_at: new Date(1500).toISOString(), is_mine: true, owner_kind: 'oauth', chat_id: 'chat-a',
  } }), bot('b')])
  const h = bindingHarness(t, { activatePairing: (_id, _input, requestSignal) => { signal = requestSignal; return waiting.promise } })
  t.after(async () => { waiting.resolve(result); await flush() })
  await claimed(h)
  const alreadyQueuedCallback = [...h.timers.values()][0]
  h.api.confirmWord.value = 'ABCDEF'
  const activating = h.api.activate()
  assert.equal(h.api.isMutating.value, true)
  assert.equal(h.timers.size, 0)
  const readCount = h.calls.length
  alreadyQueuedCallback()
  assert.equal(await h.api.refresh(), false)
  await flush()
  assert.equal(signal.aborted, false)
  assert.equal(h.api.isMutating.value, true)
  assert.equal(h.calls.length, readCount)
  waiting.resolve(result)
  assert.equal(await activating, true)
  assert.equal(h.api.isMutating.value, false)
  assert.equal(h.api.viewState.value, 'bound-current')
  assert.equal(h.timers.size, 0)
})

test('a busy activation keeps the confirmation word and resumes polling without a recovery GET', async t => {
  const h = bindingHarness(t, { activatePairing: async () => {
    throw new config.FeishuBotRequestError(503, 'bot_operation_busy', 'Another Bot operation is in progress.')
  } })
  await claimed(h)
  h.api.confirmWord.value = 'ABCDEF'
  const readCount = h.calls.length
  assert.equal(await h.api.activate(), false)
  assert.equal(h.api.confirmWord.value, 'ABCDEF')
  assert.equal(h.api.isMutating.value, false)
  assert.equal(h.timers.size, 1)
  assert.equal(h.calls.length, readCount)
})

test('a mutation cancelling an old read clears loading and schedules the new code poll', async t => {
  const h = bindingHarness(t), waiting = deferred()
  await h.api.resume()
  h.setTransport(() => waiting.promise)
  const reading = h.api.refresh()
  assert.equal(h.api.isLoading.value, true)
  assert.equal(await h.api.generateCode(), true)
  assert.equal(h.api.isLoading.value, false)
  assert.equal(h.api.isMutating.value, false)
  assert.equal(h.timers.size, 1)
  waiting.resolve(response(pool(1)))
  await reading
  assert.equal(h.api.pendingCode.value.code, 'CH-TEST')
  assert.equal(h.timers.size, 1)
})

test('selecting another Bot clears the previous seen claim', async t => {
  const h = bindingHarness(t)
  await claimed(h)
  h.api.selectBot('b')
  await h.api.resume()
  assert.equal(h.api.selectedBotId.value, 'b')
  assert.equal(h.api.viewState.value, 'unbound')
  assert.equal(h.api.error.value, null)
  assert.equal(h.timers.size, 0)
})

test('pause discards late code responses and clears confirmation text', async t => {
  const waiting = deferred()
  const h = bindingHarness(t, { startPairing: () => waiting.promise })
  await h.api.resume()
  const creating = h.api.generateCode()
  h.api.confirmWord.value = 'ABCDEF'
  h.api.pause()
  waiting.resolve(code())
  assert.equal(await creating, false)
  assert.equal(h.api.pendingCode.value, null)
  assert.equal(h.api.confirmWord.value, '')
  assert.equal(h.timers.size, 0)
})

test('a changed Chat ref cannot display the previous Chat code response', async t => {
  const waiting = deferred()
  const h = bindingHarness(t, { startPairing: () => waiting.promise })
  await h.api.resume()
  const creating = h.api.generateCode()
  h.tab.value = 'tab-2'
  await flush()
  waiting.resolve(code())
  assert.equal(await creating, false)
  assert.equal(h.api.pendingCode.value, null)
  assert.equal(h.api.confirmWord.value, '')
  assert.equal(h.timers.size, 0)
})

test('removing the selected Bot never attaches its old code to the next Bot', async t => {
  const h = bindingHarness(t)
  await h.api.resume()
  await h.api.generateCode()
  h.setServer(pool(2, [bot('b')]))
  await h.api.refresh()
  assert.equal(h.api.selectedBotId.value, 'b')
  assert.equal(h.api.pendingCode.value, null)
  assert.equal(h.api.confirmWord.value, '')
  assert.equal(h.api.viewState.value, 'unbound')
  assert.equal(h.timers.size, 0)
})

test('a real claim change clears its predecessor word, but a same-claim refresh does not', async t => {
  const h = bindingHarness(t)
  await claimed(h)
  h.api.confirmWord.value = 'ABCDEF'
  h.store.applyPool(pool(3, [bot('a', { revision: 3, my_claims: [claim()] }), bot('b')]))
  assert.equal(h.api.confirmWord.value, 'ABCDEF')
  h.store.applyPool(pool(4, [bot('a', { revision: 4, my_claims: [{ ...claim(), pairing_id: 'claim-new' }] }), bot('b')]))
  assert.equal(h.api.confirmWord.value, '')
})


test('401 hides a stale binding, clears pairing input and blocks disconnect', async t => {
  let disconnects = 0
  const h = bindingHarness(t, { disconnectPairing: async () => { disconnects += 1; return pool(3) } })
  await h.api.resume()
  await h.api.generateCode()
  h.api.confirmWord.value = 'ABCDEF'
  h.store.applyPool(pool(2, [bot('a', { binding: {
    pairing_id: 'bound', state: 'active', tab_id: 'tab-1', workspace_id: null,
    created_at: new Date(1000).toISOString(), is_mine: true, owner_kind: 'oauth', chat_id: 'chat-a',
  } })]))
  h.setTransport(async () => response({ detail: 'hub_access_required' }, 401))
  assert.equal(await h.api.refresh(), false)
  assert.equal(h.api.needsLogin.value, true)
  assert.equal(h.api.viewState.value, 'error')
  assert.equal(h.api.pendingCode.value, null)
  assert.equal(h.api.confirmWord.value, '')
  assert.equal(await h.api.disconnect(), false)
  assert.equal(disconnects, 0)
  assert.equal(h.timers.size, 0)
})
