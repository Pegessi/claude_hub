import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import test from 'node:test'
import ts from 'typescript'
import { ref } from 'vue'

const require = createRequire(import.meta.url)
const source = readFileSync(new URL('../src/composables/useFeishuBinding.ts', import.meta.url), 'utf8')
  .replace("from 'vue'", `from '${pathToFileURL(require.resolve('vue')).href}'`)
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}).outputText
const { useFeishuBinding } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)

const bindingSnapshot = (overrides = {}) => ({
  owner_open_id: 'ou-owner',
  sender_open_id: 'ou-owner',
  app_id: 'cli-bot',
  chat_id: 'oc-chat',
  tab_id: 'tab-1',
  workspace_id: null,
  created_at: '2026-10-04T12:00:00Z',
  ...overrides,
})

const codeSnapshot = () => ({
  code: 'CH-ABCDEFGHIJ',
  expires_at: '2099-10-04T12:10:00Z',
  event_url: 'https://hub.example.test/api/feishu/bot/events',
})

const jsonResponse = (body, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})

const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

test('pause prevents a late binding-code JSON body from restoring state or polling', async t => {
  const jsonStarted = deferred()
  const jsonBody = deferred()
  let timerCalls = 0
  t.mock.method(globalThis, 'setTimeout', () => {
    timerCalls++
    return 1
  })
  t.mock.method(globalThis, 'fetch', async () => ({
    ok: true,
    status: 201,
    json: () => {
      jsonStarted.resolve()
      return jsonBody.promise
    },
  }))
  const state = useFeishuBinding(ref('tab-1'))

  const request = state.generateCode()
  await jsonStarted.promise
  state.pause()
  jsonBody.resolve(codeSnapshot())

  assert.equal(await request, false)
  assert.equal(state.pendingCode.value, null)
  assert.equal(state.viewState.value, 'unbound')
  assert.equal(timerCalls, 0)
})

test('401 clears previously loaded binding state and requires the existing login flow', async t => {
  let response = jsonResponse({ binding: bindingSnapshot() })
  t.mock.method(globalThis, 'fetch', async () => response)
  const state = useFeishuBinding(ref('tab-1'))
  t.after(state.pause)

  assert.equal(await state.refresh(), true)
  assert.equal(state.viewState.value, 'bound-current')
  response = jsonResponse({ detail: 'Not authenticated' }, 401)

  assert.equal(await state.refresh(), false)
  assert.equal(state.binding.value, null)
  assert.equal(state.pendingCode.value, null)
  assert.equal(state.needsLogin.value, true)
  assert.equal(state.viewState.value, 'error')
})

test('503 configuration errors remain visible without fabricating a binding', async t => {
  t.mock.method(globalThis, 'fetch', async () => jsonResponse({ detail: 'Feishu Bot is not configured' }, 503))
  const state = useFeishuBinding(ref('tab-1'))
  t.after(state.pause)

  assert.equal(await state.refresh(), false)
  assert.equal(state.binding.value, null)
  assert.equal(state.viewState.value, 'error')
  assert.equal(state.error.value, 'Feishu Bot is not configured')
  assert.equal(state.needsLogin.value, false)
})

test('normal disconnect uses authenticated DELETE and clears binding state', async t => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    requests.push({ url, options })
    if (options.method === 'DELETE') return new Response(null, { status: 204 })
    return jsonResponse({ binding: bindingSnapshot() })
  })
  const state = useFeishuBinding(ref('tab-1'))
  t.after(state.pause)

  assert.equal(await state.refresh(), true)
  assert.equal(state.viewState.value, 'bound-current')
  assert.equal(await state.disconnect(), true)
  assert.equal(state.binding.value, null)
  assert.equal(state.pendingCode.value, null)
  assert.equal(state.viewState.value, 'unbound')
  assert.equal(requests.at(-1).url, '/api/feishu/bot/binding')
  assert.equal(requests.at(-1).options.method, 'DELETE')
  assert.equal(requests.at(-1).options.credentials, 'same-origin')
})

test('normal code generation sends only the concrete Chat tab id', async t => {
  let requestBody
  t.mock.method(globalThis, 'fetch', async (_url, options = {}) => {
    requestBody = JSON.parse(options.body)
    return jsonResponse(codeSnapshot(), 201)
  })
  const state = useFeishuBinding(ref('tab-1'))
  t.after(state.pause)

  assert.equal(await state.generateCode(), true)
  assert.deepEqual(requestBody, { tab_id: 'tab-1' })
  assert.equal(state.pendingCode.value.code, 'CH-ABCDEFGHIJ')
  assert.equal(state.viewState.value, 'pending')
})
