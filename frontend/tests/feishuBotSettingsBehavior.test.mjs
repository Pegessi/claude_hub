import assert from 'node:assert/strict'
import test from 'node:test'
import { bot, clipboard, deferred, flush, harness, mountSettings, pool, response } from './feishuBotUiTestHarness.mjs'

const emptyCreate = { name: '', app_id: '', app_secret: '', verification_token: '', encrypt_key: '' }
const input = { name: 'Created', app_id: 'app-c', app_secret: 'create-secret', verification_token: 'create-token', encrypt_key: 'create-encrypt' }
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
  state.appSecret = 'secret'; state.verificationToken = 'token'; state.encryptKey = 'encrypt'
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
  state.appSecret = 'keep-secret'; state.verificationToken = 'keep-token'; state.encryptKey = 'keep-encrypt'
  h.setTransport(async (_url, init) => init.method === 'PUT'
    ? response({ detail: 'bot_operation_busy' }, 503) : response(h.server()))
  const beforeReads = reads(h)
  await state.saveSecrets()
  assert.equal(state.appSecret, 'keep-secret')
  assert.equal(state.verificationToken, 'keep-token')
  assert.equal(state.encryptKey, 'keep-encrypt')
  assert.equal(state.busy, false)
  assert.equal(reads(h), beforeReads)
  assert.match(state.error, /progress|wait/i)
})


test('a late clipboard failure cannot overwrite the mutation error or trigger a reload', async t => {
  const h = harness(t)
  const mutation = deferred(), copied = deferred()
  const previousWriter = clipboard.setWriter(() => copied.promise)
  h.setTransport(async (_url, init) => init.method === 'PATCH'
    ? mutation.promise : response(h.server()))
  t.after(async () => {
    mutation.resolve(response({ detail: 'hub_access_required' }, 403))
    copied.resolve()
    await flush()
    clipboard.setWriter(previousWriter)
  })
  const state = await mountSettings(t, h)
  state.select(h.store.botById('a'))
  state.editName = 'Attempted update'
  const saving = state.saveMetadata()
  const copying = state.copyUrl()
  mutation.resolve(response({ detail: 'hub_access_required' }, 403))
  await saving
  const mutationError = state.error
  assert.match(mutationError, /permission/i)
  const requests = h.calls.length

  copied.reject(new Error('clipboard permission denied'))
  await copying
  assert.equal(state.error, mutationError)
  assert.equal(state.success, null)
  assert.match(state.clipboardError, /Could not copy/)
  assert.equal(state.clipboardStatus, null)
  assert.equal(h.calls.length, requests)

  state.select(h.store.botById('b'))
  assert.equal(state.clipboardError, null)
  assert.equal(state.clipboardStatus, null)
  state.close()
  assert.equal(state.clipboardError, null)
  assert.equal(state.clipboardStatus, null)
})
