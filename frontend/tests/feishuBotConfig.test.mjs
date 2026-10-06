import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/utils/feishuBotConfig.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}).outputText
const {
  FeishuBotConfigRequestError,
  deactivateFeishuBotConfiguration,
  loadFeishuBotConfiguration,
  saveFeishuBotConfiguration,
} = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)

const statusSnapshot = (overrides = {}) => ({
  configured: false, source: 'none', can_manage: false, editable: false,
  event_url: null, revision: 0, ...overrides,
})
const adminSnapshot = (overrides = {}) => ({
  ...statusSnapshot({ configured: true, source: 'stored', can_manage: true, editable: true, revision: 7 }),
  app_id: 'cli-bot', app_secret_configured: true, verification_token_configured: true,
  encrypt_key_configured: true, ...overrides,
})
const input = {
  app_id: 'cli-bot', app_secret: 'app-secret-value', verification_token: 'verification-value',
  encrypt_key: 'encrypt-value', expected_revision: 7,
}
const jsonResponse = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

test('ordinary users load only the safe status endpoint', async t => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    requests.push({ url, options })
    return jsonResponse(statusSnapshot())
  })
  const result = await loadFeishuBotConfiguration()
  assert.deepEqual(result, { status: statusSnapshot(), adminConfig: null })
  assert.deepEqual(requests.map(request => request.url), ['/api/feishu/bot/config/status'])
  assert.equal(requests[0].options.credentials, 'same-origin')
})

test('administrators load safe status before the secret-free admin view', async t => {
  const requests = []
  const safe = statusSnapshot({
    configured: true, source: 'environment', can_manage: true, editable: false,
    event_url: 'https://hub.example.test/api/feishu/bot/events', revision: null,
  })
  const admin = adminSnapshot({ ...safe, app_id: 'cli-env' })
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    requests.push({ url, options })
    return jsonResponse(url.endsWith('/status') ? safe : admin)
  })
  const result = await loadFeishuBotConfiguration()
  assert.deepEqual(result, { status: safe, adminConfig: admin })
  assert.deepEqual(requests.map(request => request.url), ['/api/feishu/bot/config/status', '/api/feishu/bot/config'])
  assert.equal('app_secret' in result.adminConfig, false)
  assert.equal(result.adminConfig.app_secret_configured, true)
})

test('save submits all credentials, revision, and explicit App ID change decision', async t => {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    requests.push({ url, options })
    return jsonResponse(adminSnapshot({ revision: 8 }))
  })
  const saved = await saveFeishuBotConfiguration(input, false)
  await saveFeishuBotConfiguration(input, true)
  assert.equal(saved.revision, 8)
  assert.equal(requests.length, 2)
  assert.equal(requests[0].url, '/api/feishu/bot/config')
  assert.equal(requests[0].options.method, 'PUT')
  assert.equal(requests[0].options.credentials, 'same-origin')
  assert.deepEqual(JSON.parse(requests[0].options.body), { ...input, allow_app_id_change: false })
  assert.equal(JSON.parse(requests[1].options.body).allow_app_id_change, true)
})

test('only known conflict codes are retained for explicit UI handling', async t => {
  let detail = 'app_id_change_confirmation_required'
  t.mock.method(globalThis, 'fetch', async () => jsonResponse({ detail }, 409))
  await assert.rejects(() => saveFeishuBotConfiguration(input, false), error => {
    assert.ok(error instanceof FeishuBotConfigRequestError)
    assert.equal(error.status, 409)
    assert.equal(error.code, 'app_id_change_confirmation_required')
    return true
  })
  detail = 'unexpected-secret-app-secret-value'
  await assert.rejects(() => saveFeishuBotConfiguration(input, false), error => {
    assert.ok(error instanceof FeishuBotConfigRequestError)
    assert.equal(error.code, null)
    assert.equal(error.message, 'The Bot configuration changed. Refresh and try again.')
    assert.doesNotMatch(error.message, /app-secret-value/)
    return true
  })
})

test('validation and damaged-state failures use fixed messages without response details', async t => {
  let response = jsonResponse({ detail: 'upstream echoed app-secret-value' }, 502)
  t.mock.method(globalThis, 'fetch', async () => response)
  await assert.rejects(() => saveFeishuBotConfiguration(input, false), error => {
    assert.equal(error.message, 'Feishu could not validate this configuration. Check the credentials and network, then try again.')
    assert.doesNotMatch(error.message, /app-secret-value/)
    return true
  })
  response = jsonResponse({ detail: 'corrupt record contents' }, 503)
  await assert.rejects(() => loadFeishuBotConfiguration(), error => {
    assert.equal(error.message, 'The Bot configuration is unavailable or damaged. Contact an administrator.')
    assert.doesNotMatch(error.message, /corrupt record contents/)
    return true
  })
})

test('deactivation sends only the expected revision and returns the safe admin view', async t => {
  let request
  const disabled = adminSnapshot({
    configured: false, source: 'none', revision: 8, app_id: null,
    app_secret_configured: false, verification_token_configured: false, encrypt_key_configured: false,
  })
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    request = { url, options }
    return jsonResponse(disabled)
  })
  const result = await deactivateFeishuBotConfiguration(7)
  assert.deepEqual(result, disabled)
  assert.equal(request.url, '/api/feishu/bot/config')
  assert.equal(request.options.method, 'DELETE')
  assert.deepEqual(JSON.parse(request.options.body), { expected_revision: 7 })
})

test('post-commit and busy failures retain only whitelisted fixed meanings', async t => {
  let response
  t.mock.method(globalThis, 'fetch', async () => response)
  const cases = [
    [500, 'routing_cleanup_failed', 'The configuration may have changed, but binding cleanup failed. Check the latest configuration before taking another action.'],
    [503, 'public_url_invalid', 'The instance public URL is invalid. Ask an administrator to fix it. A submitted configuration change may already have taken effect.'],
    [503, 'config_operation_busy', 'Another Bot configuration operation is in progress. Wait and try again.'],
    [503, 'bot_config_unavailable', 'The Bot configuration is unavailable or damaged. Contact an administrator.'],
  ]
  for (const [status, code, message] of cases) {
    response = jsonResponse({ detail: code }, status)
    await assert.rejects(() => saveFeishuBotConfiguration(input, false), error => {
      assert.ok(error instanceof FeishuBotConfigRequestError)
      assert.equal(error.status, status)
      assert.equal(error.code, code)
      assert.equal(error.message, message)
      return true
    })
  }
  response = jsonResponse({ detail: 'unexpected-secret-app-secret-value' }, 500)
  await assert.rejects(() => saveFeishuBotConfiguration(input, false), error => {
    assert.equal(error.code, null)
    assert.equal(error.message, 'The configuration may have changed. Reload configuration to check the latest state before trying again.')
    assert.doesNotMatch(error.message, /app-secret-value/)
    return true
  })
})
