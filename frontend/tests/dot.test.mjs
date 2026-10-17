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
  ['loadDotIdentity', [], 'GET', '/api/dot/identity', undefined],
  ['updateDotIdentity', [{ name: 'Jarvis', avatar: '🤖' }], 'PATCH', '/api/dot/identity', { name: 'Jarvis', avatar: '🤖' }],
  ['uploadDotAvatar', [new Blob([new Uint8Array([137, 80, 78, 71])])], 'POST', '/api/dot/identity/avatar', { data_base64: 'iVBORw==' }],
  ['clearDotAvatar', [], 'DELETE', '/api/dot/identity/avatar', undefined],
  ['watchDotTask', ['t/1', 4], 'POST', '/api/dot/work/t%2F1/watch', { stall_hours: 4 }],
  ['watchDotTask', ['t-1'], 'POST', '/api/dot/work/t-1/watch', { stall_hours: null }],
  ['unwatchDotTask', ['t-1'], 'POST', '/api/dot/work/t-1/unwatch', undefined],
  ['loadDotLessons', [], 'GET', '/api/dot/lessons', undefined],
  ['loadDotLessons', [true], 'GET', '/api/dot/lessons?all=true', undefined],
  ['addDotLesson', ['Name the output path'], 'POST', '/api/dot/lessons', { rule: 'Name the output path' }],
  ['updateDotLesson', ['l-1', 'Say where'], 'PATCH', '/api/dot/lessons/l-1', { rule: 'Say where' }],
  ['dropDotLesson', ['l-1'], 'DELETE', '/api/dot/lessons/l-1', undefined],
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

test('Dot API validation errors show their messages', async t => {
  const body = { detail: [{ loc: ['query', 'q'], msg: 'String should have at most 2000 characters' }] }
  t.mock.method(globalThis, 'fetch', async () => response(body, 422))
  await assert.rejects(api.recallDot('x'), err => err.status === 422 && err.message === 'String should have at most 2000 characters')
})

test('Dot launcher sits in the app bar and opens the Dot Chat tab', () => {
  const app = read('../src/App.vue')
  const launcher = read('../src/components/DotLauncher.vue')
  const tabBar = read('../src/components/TabBar.vue')
  // Desktop: app bar. Phones hide the app bar, so the mobile TabBar carries a compact launcher.
  assert.match(app, /<div class="app-mode-tools">\s*<DotLauncher v-if="!isMobile" \/>/)
  assert.match(tabBar, /<DotLauncher\s+v-if="isMobile"\s+compact\s+\/>/)
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

test('phone tab bars hide status summary markup while retaining its polling owners', () => {
  const tabBar = read('../src/components/TabBar.vue')
  const statusPanel = read('../src/components/AgentStatusFloatingPanel.vue')
  assert.match(tabBar, /v-if="manualTabs\.length > 0"\s+:hide-summary="isMobile"\s+source="manual"/)
  assert.match(tabBar, /v-if="managedTabs\.length > 0"\s+:hide-summary="isMobile"\s+source="managed"/)
  assert.match(statusPanel, /v-if="!hideSummary && availableRows\.length > 0"/)
  assert.match(tabBar, /v-if="tab\.session_kind === 'chat' \|\| tab\.is_active"\s+class="tab-indicator"/)
})

test('first-pass rate and review coverage read plainly', () => {
  assert.equal(api.formatFirstPass({ reviewed: 0, first_pass: 0, rate: null }), 'No reviews yet')
  assert.equal(api.formatFirstPass({ reviewed: 4, first_pass: 3, rate: 0.75 }), 'First pass 3/4 (75%)')
  assert.equal(api.formatReviewCoverage({ judged: 4, returned: 5 }), 'judged 4/5 returned')
  assert.equal(api.formatReviewCoverage({ judged: 0, returned: 0 }), '')
})

test('Dot panel covers focus, work, lessons, memory and health', () => {
  const panel = read('../src/components/DotPanel.vue')
  for (const label of ["label: 'Focus'", "label: 'Work'", "label: 'Lessons'", "label: 'Memory'", "label: 'Health'"]) {
    assert.ok(panel.includes(label), label)
  }
  for (const call of [
    'recallDot(', 'archiveDotCard(', 'updateDotFocus(', 'ackDotInbox(', 'loadDotHealth(',
    'loadDotLessons(', 'addDotLesson(', 'updateDotLesson(', 'dropDotLesson(', 'formatFirstPass(',
    'formatReviewCoverage(', 'lesson.reach',
  ]) {
    assert.ok(panel.includes(call), call)
  }
  assert.ok(panel.includes("event.key === 'Escape'"))
  // No "just now ago"; a double Enter cannot add twice (the draft is cleared first).
  assert.ok(!/\}\} ago|\)\} ago/.test(panel))
  assert.match(panel, /newFocus\.value = ''\n {2}void run\(/)
  assert.match(panel, /newLesson\.value = ''\n {2}void run\(/)
  assert.ok(panel.includes('maxlength="2000"'))
  // A slow card load (or one left with Back) must not replace the card shown now.
  assert.ok(panel.includes('if (request === cardRequest) selectedCard.value = card'))
  assert.ok(panel.includes('@click="closeCard"'))
})

test('Dot name and avatar are shared and editable', () => {
  const launcher = read('../src/components/DotLauncher.vue')
  const panel = read('../src/components/DotPanel.vue')
  const avatar = read('../src/components/DotAvatar.vue')
  assert.ok(launcher.includes('{{ identity.name }}') && panel.includes('{{ identity.name }}'))
  assert.ok(launcher.includes('<DotAvatar') && panel.includes('<DotAvatar'))
  for (const call of ['updateDotIdentity(', 'uploadDotAvatar(', 'clearDotAvatar(']) {
    assert.ok(panel.includes(call), call)
  }
  // Raster images only: no SVG (it can carry script).
  assert.ok(panel.includes('accept="image/png,image/jpeg,image/gif,image/webp"'))
  assert.ok(avatar.includes('dotFileUrl(props.identity.avatar_image)'))
  assert.equal(api.dotFileUrl('files/ab c.png'), '/api/dot/files/ab%20c.png')
})

test('Dot panel can watch and stop watching tasks', () => {
  const panel = read('../src/components/DotPanel.vue')
  assert.ok(panel.includes('watchDotTask(taskId, watchStallHours.value)'))
  assert.ok(panel.includes('unwatchDotTask(item.task_id)'))
  // Stopping a task that waits for review drops the review: ask first.
  assert.match(panel, /item\.review_due && !window\.confirm\(/)
  assert.ok(panel.includes("'task_stalled'"))
})

test('Dot panel is a full-screen sheet with touch-sized targets on phones', () => {
  const panel = read('../src/components/DotPanel.vue')
  const mobile = panel.slice(panel.indexOf('@media (max-width: 768px)'))
  assert.ok(mobile.length > 30, 'mobile media query exists')
  assert.match(mobile, /\.dot-panel \{\s*width: 100%;/)
  assert.ok(mobile.includes('env(safe-area-inset-bottom)'))
  assert.ok(mobile.includes('font-size: 16px'), 'no iOS zoom on focus')
  assert.ok(mobile.includes('min-height: 40px'))
})

test('quiet Dot wake and tidy turns are hidden from the chat', () => {
  const turn = (userText, assistantText, completed = true) => ({ userText, assistantText, completed })
  assert.equal(api.DOT_QUIET_REPLY, '[quiet]')
  assert.ok(api.isQuietDotTurn(turn('[Dot wake] New events', ' [quiet]\n')))
  assert.ok(api.isQuietDotTurn(turn('[Dot tidy] Nightly memory tidy', '[quiet]')))
  assert.ok(!api.isQuietDotTurn(turn('[Dot wake] New events', 'Paper summary is ready.')))
  assert.ok(!api.isQuietDotTurn(turn('Say [quiet]', '[quiet]')), 'only Hub turns')
  assert.ok(!api.isQuietDotTurn(turn('[Dot wake] New events', '[quiet]', false)), 'running turns stay visible')
  const pane = read('../src/components/StructuredPane.vue')
  assert.ok(pane.includes('.filter(({ turn }) => !isQuietDotTurn(turn))'))
})
