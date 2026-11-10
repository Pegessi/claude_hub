import assert from 'node:assert/strict'
import test from 'node:test'
import { activeBinding, bot, deferred, flush, harness, mountSettings, pool, response } from './feishuBotUiTestHarness.mjs'

const emptyCreate = { name: '', app_id: '', app_secret: '' }
const input = { name: 'Created', app_id: 'app-c', app_secret: 'create-secret' }
const reads = h => h.calls.filter(call => (call.init.method ?? 'GET') === 'GET').length

test('confirmed create clears its credentials and keeps the success message after selection', async t => {
  const h = harness(t)
  h.setTransport(async (_url, init) => init.method === 'POST'
    ? response(pool(2, [...h.server().bots, bot('c', { name: 'Created' })], 'c'))
    : response(h.server()))
  const state = await mountSettings(t, h)
  state.beginCreate()
  Object.assign(state.create, input)
  await state.createBot()
  assert.deepEqual({ ...state.create }, emptyCreate)
  assert.equal(state.selectedId, 'c')
  assert.equal(state.success, 'Bot added.')
})
test('busy selection and reload are ignored; close does not restore fields on a late successful response', async t => {
  const h = harness(t), waiting = deferred()
  const saved = pool(2, [bot('a', { name: 'Saved', revision: 2 }), bot('b')], 'a')
  h.setTransport(async (_url, init) => init.method === 'PATCH' ? waiting.promise : response(h.server()))
  t.after(async () => { waiting.resolve(response(saved)); await flush() })
  const state = await mountSettings(t, h)
  state.select(h.store.botById('a'))
  state.editName = 'Saved'
  state.appSecret = 'replacement-secret'
  Object.assign(state.create, input)
  const writing = state.saveMetadata()
  assert.equal(state.busy, true)
  const beforeReads = reads(h)
  state.select(h.store.botById('b'))
  state.beginCreate()
  await state.refresh()
  assert.equal(state.selectedId, 'a')
  assert.equal(reads(h), beforeReads)
  state.close()
  assert.equal(state.appSecret, '')
  assert.deepEqual({ ...state.create }, emptyCreate)
  waiting.resolve(response(saved))
  await writing
  assert.equal(h.store.botById('a').name, 'Saved')
  assert.equal(state.success, null)
  assert.equal(state.appSecret, '')
  assert.deepEqual({ ...state.create }, emptyCreate)
})
test('a shared update cannot silently advance a metadata draft revision', async t => {
  const h = harness(t)
  const state = await mountSettings(t, h)
  state.select(h.store.botById('a'))
  state.editName = 'My edit'
  state.appSecret = 'must-clear-on-conflict'
  const changed = pool(2, [bot('a', { name: 'Changed elsewhere', enabled: false, revision: 2 }), bot('b')])
  h.store.applyPool(changed)
  h.setServer(changed)
  h.setTransport(async (_url, init) => init.method === 'PATCH'
    ? response({ detail: 'bot_revision_conflict' }, 409) : response(h.server()))
  await state.saveMetadata()
  const writes = h.calls.filter(call => call.init.method === 'PATCH')
  assert.equal(writes.length, 1)
  assert.equal(JSON.parse(writes[0].init.body).expected_revision, 1)
  assert.equal(state.editName, 'Changed elsewhere')
  assert.equal(state.editEnabled, false)
  assert.equal(state.selectedRevision, 2)
  assert.equal(state.appSecret, '')
  assert.match(state.error, /changed|Reload/i)
})

test('secret replacement and delete confirmation also use the selected draft revision', async t => {
  const h = harness(t)
  const state = await mountSettings(t, h)
  state.select(h.store.botById('a'))
  state.appSecret = 'secret'
  const changed = pool(2, [bot('a', { revision: 2 }), bot('b')])
  h.store.applyPool(changed); h.setServer(changed)
  h.setTransport(async (_url, init) => init.method
    ? response({ detail: 'bot_revision_conflict' }, 409) : response(h.server()))
  await state.saveSecrets()
  const put = h.calls.find(call => call.init.method === 'PUT')
  assert.equal(JSON.parse(put.init.body).expected_revision, 1)
  assert.equal(state.selectedRevision, 2)
  state.confirmDelete = true
  const changedAgain = pool(3, [bot('a', { revision: 3 }), bot('b')])
  h.store.applyPool(changedAgain); h.setServer(changedAgain)
  await state.removeBot()
  const deletion = h.calls.find(call => call.init.method === 'DELETE')
  assert.equal(JSON.parse(deletion.init.body).expected_revision, 2)
  assert.equal(state.confirmDelete, false)
  assert.equal(state.selectedRevision, 3)
})

test('bot_operation_busy keeps the entered credentials and does not re-read the pool', async t => {
  const h = harness(t)
  const state = await mountSettings(t, h)
  state.select(h.store.botById('a'))
  state.appSecret = 'keep-secret'
  h.setTransport(async (_url, init) => init.method === 'PUT'
    ? response({ detail: 'bot_operation_busy' }, 503) : response(h.server()))
  const beforeReads = reads(h)
  await state.saveSecrets()
  assert.equal(state.appSecret, 'keep-secret')
  assert.equal(state.busy, false)
  assert.equal(reads(h), beforeReads)
  assert.match(state.error, /progress|wait/i)
})

test('selecting per_channel mode on an unbound Bot submits mode in PATCH and updates the Bot', async t => {
  const h = harness(t)
  const updated = pool(2, [bot('a', { mode: 'per_channel', revision: 2 }), bot('b')], 'a')
  h.setTransport(async (_url, init) => init.method === 'PATCH'
    ? response(updated) : response(h.server()))
  const state = await mountSettings(t, h)
  state.select(h.store.botById('a'))
  assert.equal(state.editMode, 'single')
  assert.equal(state.modeSwitchDisabled, false)
  assert.equal(state.modeSwitchDisabledReason, null)
  state.selectMode('per_channel')
  assert.equal(state.editMode, 'per_channel')
  assert.equal(await state.saveMetadata(), true)
  const patch = h.calls.find(call => call.init.method === 'PATCH')
  assert.deepEqual(JSON.parse(patch.init.body), {
    name: 'Bot a',
    enabled: true,
    mode: 'per_channel',
    expected_revision: 1,
  })
  assert.equal(h.store.botById('a').mode, 'per_channel')
  assert.equal(state.editMode, 'per_channel')
  assert.equal(state.success, 'Bot details saved.')
})

test('mode switching is disabled and explained when the Bot has active channels, and a 409 conflict reconciles with an explanation', async t => {
  const h = harness(t)
  const boundBot = bot('b', {
    mode: 'single',
    bindings: [activeBinding('tab-active', { bot_id: 'b', app_id: 'app-b' })],
  })
  const initial = pool(1, [bot('a'), boundBot])
  h.store.applyPool(initial)
  h.setServer(initial)
  const state = await mountSettings(t, h)

  state.select(h.store.botById('b'))
  assert.equal(state.modeSwitchDisabled, true)
  assert.match(state.modeSwitchDisabledReason, /no active channels|Disconnect/i)
  state.selectMode('per_channel')
  assert.equal(state.editMode, 'single', 'disabled mode control cannot switch mode')

  // Now select unbound Bot a, switch mode locally, and simulate a 409 because a
  // channel became active on the server before Save was processed.
  state.select(h.store.botById('a'))
  state.selectMode('per_channel')
  const occupiedServer = pool(2, [
    bot('a', { mode: 'single', revision: 2, bindings: [activeBinding('tab-1')] }),
    boundBot,
  ])
  h.setServer(occupiedServer)
  h.setTransport(async (_url, init) => init.method === 'PATCH'
    ? response({ detail: 'bot_has_active_bindings' }, 409)
    : response(h.server()))
  assert.equal(await state.saveMetadata(), false)
  assert.match(state.error, /active.*channel|Disconnect.*before switching/i)
  assert.equal(state.editMode, 'single')
  assert.equal(state.selectedRevision, 2)
  assert.equal(state.modeSwitchDisabled, true)
})
