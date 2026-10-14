import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const source = read('../src/utils/dotApi.ts')
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 } }).outputText
const api = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
const response = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const cases = [
  ['loadDotStatus', [], 'GET', '/api/dot/status', undefined],
  ['setupDot', [], 'POST', '/api/dot/setup', { env: null }],
  ['setupDot', [{ ANTHROPIC_MODEL: 'm' }], 'POST', '/api/dot/setup', { env: { ANTHROPIC_MODEL: 'm' } }],
  ['loadDotHealth', [], 'GET', '/api/dot/health', undefined],
  ['recallDot', ['flash attn', 'knowledge'], 'GET', '/api/dot/recall?q=flash+attn&kind=knowledge&limit=30', undefined],
  ['loadDotCards', [], 'GET', '/api/dot/cards', undefined],
  ['loadDotCard', ['k/1'], 'GET', '/api/dot/cards/k%2F1', undefined],
  ['archiveDotCard', ['k-1'], 'DELETE', '/api/dot/cards/k-1', undefined],
  ['loadDotFocus', [], 'GET', '/api/dot/focus', undefined],
  ['addDotFocus', [{ title: 'Ship', priority: 1 }], 'POST', '/api/dot/focus', { title: 'Ship', priority: 1 }],
  ['updateDotFocus', ['f-1', { status: 'done' }], 'PATCH', '/api/dot/focus/f-1', { status: 'done' }],
  ['loadDotWork', [], 'GET', '/api/dot/work', undefined],
  ['loadDotInbox', [], 'GET', '/api/dot/inbox', undefined],
  ['ackDotInbox', [], 'POST', '/api/dot/inbox/ack', { ids: null }],
]
for (const [name, args, method, url, body] of cases) {
  test(`${name}(${JSON.stringify(args)}) calls ${method} ${url}`, async t => {
    let request
    t.mock.method(globalThis, 'fetch', async (u, o = {}) => { request = { u, o }; return response({ ok: true }) })
    assert.deepEqual(await api[name](...args), { ok: true })
    assert.equal(request.u, url)
    assert.equal(request.o.credentials, 'same-origin')
    assert.equal(request.o.method ?? 'GET', method)
    assert.deepEqual(request.o.body === undefined ? undefined : JSON.parse(request.o.body), body)
  })
}

test('Dot API errors surface the server detail', async t => {
  t.mock.method(globalThis, 'fetch', async () => response({ detail: 'focus is full (7 active)' }, 409))
  await assert.rejects(api.addDotFocus({ title: 'x' }), err => err.status === 409 && err.message === 'focus is full (7 active)')
})

test('Dot launcher sits in the app bar and opens the Dot Chat tab', () => {
  const app = read('../src/App.vue')
  const launcher = read('../src/components/DotLauncher.vue')
  assert.match(app, /<div class="app-mode-tools">\s*<DotLauncher \/>/)
  assert.ok(launcher.includes('await setupDot(providerEnv())'))
  // Borrow a working Claude tab's env (active first), else the default preset.
  assert.match(launcher, /tab\.agent_type === 'claude' && tab\.env/)
  assert.match(launcher, /tab\.id === terminalStore\.activeTabId\) \?\? claudeTabs\[0\]/)
  assert.ok(launcher.includes("parseLaunchEnv(defaultPresetTextForAgent('claude'))"))
  assert.ok(launcher.includes("appStore.setMode('terminal')"))
  assert.ok(launcher.includes('fetchTabs({ force: true })'))
  assert.ok(launcher.includes('terminalStore.setActiveTab(status.tab_id)'))
  assert.match(launcher, /<DotPanel[\s\S]*:open="panelOpen"/)
})

test('Dot panel covers focus, work, memory and health', () => {
  const panel = read('../src/components/DotPanel.vue')
  for (const label of ["label: 'Focus'", "label: 'Work'", "label: 'Memory'", "label: 'Health'"]) {
    assert.ok(panel.includes(label), label)
  }
  for (const call of ['recallDot(', 'archiveDotCard(', 'updateDotFocus(', 'ackDotInbox(', 'loadDotHealth(']) {
    assert.ok(panel.includes(call), call)
  }
  assert.ok(panel.includes("event.key === 'Escape'"))
})
