import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { setImmediate } from 'node:timers'
import test from 'node:test'
import ts from 'typescript'

// The repo has no DOM test runner, so the display logic is exercised as a real
// module and the component is mounted with Vue's custom renderer (the pattern
// used by feishuBotUiTestHarness.mjs): the compiled setup/lifecycle really
// runs, only painting is skipped.
const require = createRequire(import.meta.url)
const { parse, compileScript, compileTemplate } = require('vue/compiler-sfc')
const vueUrl = import.meta.resolve('vue')
const vue = await import(vueUrl)

const readSource = path => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')
const moduleUrl = text => {
  const { outputText } = ts.transpileModule(text, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  })
  return `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
}

const displaySource = readSource('utils/quotaDisplay.ts')
const displayUrl = moduleUrl(displaySource)
const display = await import(displayUrl)
const imports = { vue: vueUrl, '@/utils/quotaDisplay': displayUrl }
const rewrite = text => text.replace(/from\s+(['"])([^'"]+)\1/g, (full, _quote, name) =>
  imports[name] ? `from ${JSON.stringify(imports[name])}` : full)

const menuSource = readSource('components/QuotaMenu.vue')
const { descriptor } = parse(menuSource, { filename: 'QuotaMenu.vue' })
const compiled = compileScript(descriptor, { id: 'quota-menu', genDefaultAs: '__component' })
const QuotaMenu = (await import(moduleUrl(rewrite(`${compiled.content}\nexport default __component`)))).default
const templateCode = compileTemplate({
  source: descriptor.template.content,
  filename: 'QuotaMenu.vue',
  id: 'quota-menu',
  compilerOptions: { bindingMetadata: compiled.bindings },
}).code
const { render: renderQuotaMenu } = await import(moduleUrl(rewrite(templateCode)))

const flush = async () => {
  for (let step = 0; step < 4; step += 1) await new Promise(resolve => setImmediate(resolve))
}

const NOW = Date.parse('2026-05-20T12:00:00Z')
const at = offsetMs => new Date(NOW + offsetMs).toISOString()
const MINUTE = 60_000
const HOUR = 3_600_000
const DAY = 86_400_000

const limit = (overrides = {}) => ({
  scope: 'weekly',
  label: 'Weekly limit',
  state: 'within',
  used_percent: 58,
  remaining_percent: 42,
  resets_at: null,
  ...overrides,
})
const provider = (overrides = {}) => ({
  provider: 'codex',
  available: true,
  state: 'within',
  message: '',
  as_of: at(-5 * MINUTE),
  stale: false,
  limits: [],
  plan: null,
  credit_balance: null,
  enforcing: null,
  ...overrides,
})

// Shaped after a real reading: Codex reports one weekly window plus plan and
// prepaid credit; MASO reports one row per model, two of them uncapped and one
// with nothing to report yet.
const codexProvider = provider({
  limits: [limit({ resets_at: at(2 * DAY) })],
  plan: 'pro',
  credit_balance: '62500.0000000000',
})
const masoProvider = provider({
  provider: 'maso',
  as_of: at(-30_000),
  enforcing: true,
  limits: [
    limit({ scope: 'gpt-6-astra', label: 'gpt-6-astra', used_percent: 14, remaining_percent: 86 }),
    limit({ scope: 'es1_orange_o50', label: 'es1_orange_o50', used_percent: 2, remaining_percent: 98 }),
    limit({ scope: 'es1_lime', label: 'es1_lime', state: 'no_limit', used_percent: null, remaining_percent: null }),
    limit({ scope: 'es1_plum', label: 'es1_plum', state: 'no_limit', used_percent: null, remaining_percent: null }),
    limit({ scope: 'es1_slate', label: 'es1_slate', state: 'no_data', used_percent: null, remaining_percent: null }),
  ],
})
const fullPayload = { providers: [codexProvider, masoProvider] }

const viewOf = (payload, now = NOW) => display.buildQuotaView(payload, now)
const rowsOf = view => Object.fromEntries(view.rows.map(row => [row.key, row]))

test('a reported window shows remaining first, with used and a reset countdown', () => {
  const [codex] = viewOf(fullPayload)
  const weekly = codex.rows[0]
  assert.equal(codex.title, 'Codex')
  assert.equal(codex.statusText, 'Within limit')
  assert.equal(weekly.label, 'Weekly limit')
  assert.equal(weekly.value, '42% left')
  assert.equal(weekly.usedText, '58% used')
  assert.equal(weekly.resetText, 'Resets in 2d')
  assert.equal(weekly.resetTitle, at(2 * DAY))
  assert.equal(weekly.barPercent, 42)
})

test('a missing percentage never renders as zero and keeps its reason', () => {
  const [, maso] = viewOf(fullPayload)
  const rows = rowsOf(maso)
  assert.equal(rows.es1_lime.value, 'No limit')
  assert.equal(rows.es1_slate.value, 'No data yet')
  for (const key of ['es1_lime', 'es1_plum', 'es1_slate']) {
    assert.equal(rows[key].remainingPercent, null)
    assert.equal(rows[key].barPercent, null, `${key} must not draw a bar`)
    assert.equal(rows[key].usedText, '')
    assert.doesNotMatch(rows[key].value, /\d/, `${key} must not show a number`)
  }
})

test('an unrecognized state degrades to Unknown instead of a number or a crash', () => {
  const payload = { providers: [provider({
    state: 'teapot',
    limits: [limit({ scope: 'odd', state: 'teapot', used_percent: null, remaining_percent: null })],
  })] }
  const [view] = viewOf(payload)
  assert.equal(view.state, 'unknown')
  assert.equal(view.statusText, 'Unknown')
  assert.equal(view.rows[0].state, 'unknown')
  assert.equal(view.rows[0].value, 'Unknown')
  assert.equal(view.rows[0].barPercent, null)
})

test('no_limit and no_data win over stray percentages the provider still sent', () => {
  const payload = { providers: [provider({ limits: [
    limit({ scope: 'uncapped', state: 'no_limit', used_percent: 0, remaining_percent: 0 }),
    limit({ scope: 'silent', state: 'no_data', used_percent: 0, remaining_percent: 0 }),
  ] })] }
  const [view] = viewOf(payload)
  assert.deepEqual(view.rows.map(row => row.value), ['No limit', 'No data yet'])
  assert.deepEqual(view.rows.map(row => row.barPercent), [null, null])
})

test('a used-only window reports used instead of inventing a remaining share', () => {
  const payload = { providers: [provider({ limits: [
    limit({ scope: 'partial', used_percent: 58, remaining_percent: null }),
  ] })] }
  const [view] = viewOf(payload)
  assert.equal(view.rows[0].value, '58% used')
  assert.equal(view.rows[0].remainingPercent, null)
  assert.equal(view.rows[0].barPercent, null)
})

test('a window with no numbers at all admits the percentage is unavailable', () => {
  const payload = { providers: [provider({ limits: [
    limit({ scope: 'blank', state: 'near', used_percent: null, remaining_percent: null }),
  ] })] }
  const [view] = viewOf(payload)
  assert.equal(view.rows[0].value, 'Percentage unavailable')
  assert.equal(view.rows[0].barPercent, null)
})

test('percentages keep one decimal and the bar stays inside 0-100', () => {
  const payload = { providers: [provider({ limits: [
    limit({ scope: 'fraction', used_percent: 14.26, remaining_percent: 85.74 }),
    limit({ scope: 'overdrawn', state: 'over', used_percent: 140, remaining_percent: -40 }),
  ] })] }
  const rows = viewOf(payload)[0].rows
  assert.equal(rows[0].value, '85.7% left')
  assert.equal(rows[0].usedText, '14.3% used')
  assert.equal(rows[1].value, '-40% left')
  assert.equal(rows[1].barPercent, 0)
})

test('reading age is always stated, including when the provider gave no timestamp', () => {
  assert.equal(viewOf(fullPayload)[0].asOfText, 'Updated 5m ago')
  assert.equal(viewOf(fullPayload)[1].asOfText, 'Updated just now')
  assert.equal(display.formatAsOf(null, NOW), 'Reading time unknown')
  assert.equal(display.formatAsOf(at(-3 * DAY), NOW), 'Updated 3d ago')
  assert.equal(display.formatAsOf('not-a-date', NOW), 'Reading time unknown')
  assert.equal(display.formatAsOf(at(10 * MINUTE), NOW), 'Reading is timestamped in the future')
})

test('a reading just short of a day boundary is not rounded down into looking fresh', () => {
  assert.equal(display.formatAsOf(at(-47 * HOUR), NOW), 'Updated 47h ago')
  assert.equal(display.formatAsOf(at(-89 * DAY), NOW), 'Updated 89d ago')
})

test('a passed or missing reset is not presented as a live countdown', () => {
  assert.equal(display.formatReset(null, NOW), '')
  assert.equal(display.formatReset(at(-MINUTE), NOW), 'Reset time has passed')
  assert.equal(display.formatReset(at(30_000), NOW), 'Resets in under a minute')
  assert.equal(display.formatReset(at(5 * MINUTE), NOW), 'Resets in 5m')
})

test('a stale reading is called out in the provider block and in the headline', () => {
  const [view] = viewOf({ providers: [provider({ stale: true, limits: [limit()] })] })
  assert.match(view.staleNote, /out of date/)
  assert.equal(view.stale, true)
  assert.equal(display.summarizeQuota([view]), 'Lowest 42% left · stale')
})

test('a reading older than the aging threshold is presented as a last known value', () => {
  const old = viewOf({ providers: [provider({ as_of: at(-3 * HOUR), limits: [limit()] })] })[0]
  assert.equal(old.aging, true)
  assert.match(old.agingNote, /last known values, not a live reading/)
  assert.equal(old.staleNote, '', 'an old reading is not the same claim as a stale one')
  assert.equal(display.summarizeQuota([old]), 'Lowest 42% left · last known')

  const fresh = viewOf({ providers: [provider({ as_of: at(-5 * MINUTE), limits: [limit()] })] })[0]
  assert.equal(fresh.aging, false)
  assert.equal(fresh.agingNote, '')
  assert.equal(display.summarizeQuota([fresh]), 'Lowest 42% left')
})

test('the aging threshold is an hour, measured from the reading itself', () => {
  assert.equal(display.AGING_READING_MS, 3_600_000)
  const justUnder = viewOf({ providers: [provider({ as_of: at(-display.AGING_READING_MS + 1000) })] })[0]
  const exactly = viewOf({ providers: [provider({ as_of: at(-display.AGING_READING_MS) })] })[0]
  assert.equal(justUnder.aging, false)
  assert.equal(exactly.aging, true)
})

test('aging never doubles up on a stale reading or invents an age', () => {
  const staleAndOld = viewOf({ providers: [provider({ stale: true, as_of: at(-3 * DAY), limits: [limit()] })] })[0]
  assert.equal(staleAndOld.aging, false, 'stale already makes the stronger claim')
  assert.match(staleAndOld.staleNote, /out of date/)
  assert.equal(staleAndOld.agingNote, '')
  assert.equal(display.summarizeQuota([staleAndOld]), 'Lowest 42% left · stale')

  const undated = viewOf({ providers: [provider({ as_of: null, limits: [limit()] })] })[0]
  assert.equal(undated.aging, false)
  assert.equal(undated.asOfText, 'Reading time unknown')
})

test('only a provider that reported the same timestamp twice counts as unchanged', () => {
  const before = { providers: [
    provider({ as_of: at(-2 * HOUR) }),
    provider({ provider: 'maso', as_of: at(-2 * MINUTE) }),
  ] }
  const after = { providers: [
    provider({ as_of: at(-2 * HOUR) }),
    provider({ provider: 'maso', as_of: at(-1 * MINUTE) }),
  ] }
  assert.deepEqual(display.findUnchangedProviders(before, after), ['codex'])
})

test('an unprovable comparison never claims a provider is unchanged', () => {
  const undated = { providers: [provider({ as_of: null })] }
  assert.deepEqual(display.findUnchangedProviders(undated, undated), [],
    'a missing timestamp means unknown, not unmoved')

  const down = { providers: [provider({ available: false, state: 'unavailable', as_of: null, message: 'Down.' })] }
  assert.deepEqual(display.findUnchangedProviders(down, down), [])

  const first = { providers: [provider({ as_of: at(-2 * HOUR) })] }
  assert.deepEqual(display.findUnchangedProviders(null, first), [], 'a first load has nothing to compare')
  assert.deepEqual(display.findUnchangedProviders(first, null), [])
})

test('an unchanged provider explains why its refresh produced nothing new', () => {
  const views = display.buildQuotaView(fullPayload, NOW, { unchangedProviders: ['codex'] })
  assert.match(views[0].unchangedNote, /^Refresh returned the same reading\./)
  assert.match(views[0].unchangedNote, /Codex only publishes usage after a conversation turn finishes/)
  assert.equal(views[1].unchangedNote, '', 'MASO really does advance, so it must stay unmarked')
})

test('a provider with no known publishing rule gets an honest generic explanation', () => {
  const payload = { providers: [provider({ provider: 'maso', limits: [limit()] })] }
  const [view] = display.buildQuotaView(payload, NOW, { unchangedProviders: ['maso'] })
  assert.match(view.unchangedNote, /has not published a newer one yet/)
  assert.doesNotMatch(view.unchangedNote, /Codex/)
})

test('no refresh comparison means no notice at all', () => {
  assert.equal(viewOf(fullPayload)[0].unchangedNote, '')
  assert.equal(display.buildQuotaView(fullPayload, NOW, { unchangedProviders: [] })[0].unchangedNote, '')
})

test('an unavailable provider shows the backend reason instead of empty rows', () => {
  const [view] = viewOf({ providers: [provider({
    provider: 'maso',
    available: false,
    state: 'unavailable',
    message: 'MASO service is not configured.',
    as_of: null,
    limits: [],
  })] })
  assert.equal(view.state, 'unavailable')
  assert.equal(view.statusText, 'Unavailable')
  assert.equal(view.message, 'MASO service is not configured.')
  assert.equal(view.emptyText, '')
  assert.deepEqual(view.rows, [])
  assert.equal(view.asOfText, 'Reading time unknown')
})

test('an unavailable provider without a reason still explains itself', () => {
  const [view] = viewOf({ providers: [provider({ available: false, state: 'unavailable', message: '' })] })
  assert.equal(view.message, 'This provider did not report any quota.')
})

const notConfigured = (name = 'maso', message = 'This Hub has no MASO service configured.') => provider({
  provider: name,
  available: false,
  state: 'not_configured',
  message,
  as_of: null,
  limits: [],
})

test('a provider this deployment never configured is reported as a fact, not a fault', () => {
  const [view] = viewOf({ providers: [notConfigured()] })
  assert.equal(view.state, 'not_configured')
  assert.equal(view.statusText, 'Not configured')
  assert.equal(view.messageTone, 'neutral')
  assert.equal(view.message, 'This Hub has no MASO service configured.')
  assert.deepEqual(view.rows, [])
  assert.equal(view.emptyText, '')
  assert.equal(view.aging, false)
  assert.equal(view.staleNote, '')
  assert.equal(view.asOfText, '', 'there is no reading to date, so no age line')
})

test('a missing configuration message falls back to a neutral sentence', () => {
  const [view] = viewOf({ providers: [notConfigured('maso', '')] })
  assert.equal(view.message, 'This Hub is not configured for this provider.')
  assert.equal(view.messageTone, 'neutral')
  assert.doesNotMatch(view.message, /fail|error|unavailable|start|set /i,
    'an unconfigured provider must not be described as broken or tell anyone to fix it')
})

test('not configured and unavailable are different states, not two words for one', () => {
  const [quiet] = viewOf({ providers: [notConfigured()] })
  const [broken] = viewOf({ providers: [provider({
    provider: 'maso',
    available: false,
    state: 'unavailable',
    message: 'MASO service did not respond.',
    as_of: null,
  })] })
  assert.notEqual(quiet.state, broken.state)
  assert.notEqual(quiet.statusText, broken.statusText)
  assert.equal(broken.statusText, 'Unavailable')
  assert.equal(broken.messageTone, 'error')
  assert.equal(quiet.messageTone, 'neutral')
})

test('an available provider with no limits says so rather than looking healthy', () => {
  const [view] = viewOf({ providers: [provider({ state: 'no_data', limits: [] })] })
  assert.equal(view.emptyText, 'This provider reported no limits.')
  assert.equal(view.statusText, 'No data')
})

test('enforcement is only claimed when the provider reported it', () => {
  const advisory = viewOf({ providers: [provider({ provider: 'maso', enforcing: false })] })[0]
  const enforced = viewOf({ providers: [provider({ provider: 'maso', enforcing: true })] })[0]
  const silent = viewOf({ providers: [provider({ provider: 'maso', enforcing: null })] })[0]
  assert.ok(advisory.notes.some(note => /does not refuse requests/.test(note)))
  assert.ok(enforced.notes.some(note => /refused once a limit is reached/.test(note)))
  assert.deepEqual(silent.notes, [])
})

test('plan and prepaid credit are shown, and the credit unit is not invented', () => {
  const [codex] = viewOf(fullPayload)
  assert.ok(codex.notes.includes('Plan: pro'))
  assert.ok(codex.notes.some(note => note.includes('62500.0000000000') && /unit not reported/.test(note)))
})

test('the headline reports the lowest remaining share across providers', () => {
  assert.equal(display.summarizeQuota(viewOf(fullPayload)), 'Lowest 42% left')
})

test('the headline never hides an unavailable provider behind a healthy number', () => {
  const views = viewOf({ providers: [
    codexProvider,
    provider({ provider: 'maso', available: false, state: 'unavailable', message: 'Unreachable.' }),
  ] })
  assert.equal(display.summarizeQuota(views), 'Lowest 42% left · 1 provider unavailable')
})

test('a deployment without MASO reads as healthy, not as one provider down', () => {
  const views = viewOf({ providers: [codexProvider, notConfigured()] })
  assert.equal(display.summarizeQuota(views), 'Lowest 42% left')
})

test('an unconfigured provider is never counted among the failed ones', () => {
  const views = viewOf({ providers: [
    codexProvider,
    notConfigured('maso'),
    provider({ provider: 'other', available: false, state: 'unavailable', message: 'Unreachable.' }),
  ] })
  assert.equal(display.summarizeQuota(views), 'Lowest 42% left · 1 provider unavailable')
})

test('a headline with only unconfigured providers says so instead of raising an alarm', () => {
  assert.equal(display.summarizeQuota(viewOf({ providers: [notConfigured()] })), 'Not configured')
  assert.equal(display.summarizeQuota(viewOf({ providers: [
    notConfigured('codex'), notConfigured('maso'),
  ] })), 'Not configured')
  assert.equal(display.summarizeQuota(viewOf({ providers: [
    notConfigured('maso'),
    provider({ available: false, state: 'unavailable', message: 'Down.' }),
  ] })), 'Unavailable', 'a real outage still shows even next to an unconfigured provider')
})

test('a headline with nothing to measure says so instead of showing a share', () => {
  assert.equal(display.summarizeQuota([]), 'No data')
  assert.equal(display.summarizeQuota(viewOf({ providers: [
    provider({ available: false, state: 'unavailable', message: 'Down.' }),
    provider({ provider: 'maso', available: false, state: 'unavailable', message: 'Down.' }),
  ] })), 'Unavailable')
  assert.equal(display.summarizeQuota(viewOf({ providers: [
    provider({ state: 'no_data', limits: [limit({ state: 'no_data', used_percent: null, remaining_percent: null })] }),
  ] })), 'No data')
})

test('an empty or missing payload renders no provider blocks', () => {
  assert.deepEqual(viewOf({ providers: [] }), [])
  assert.deepEqual(viewOf(null), [])
  assert.deepEqual(viewOf(undefined), [])
})

test('the quota endpoint only asks for a fresh reading when requested', () => {
  assert.equal(display.quotaRequestUrl(false), '/api/quota')
  assert.equal(display.quotaRequestUrl(true), '/api/quota?refresh=true')
})

function mountMenu(t, { withTemplate = false } = {}) {
  const tree = { tag: 'root', attrs: {}, children: [] }
  const renderer = vue.createRenderer({
    createElement: tag => ({ tag, attrs: {}, children: [] }),
    createText: text => ({ text }),
    createComment: text => ({ comment: text }),
    setText(node, text) { node.text = text },
    setElementText(node, text) { node.text = text },
    patchProp(node, name, _previous, value) { node.attrs[name] = value },
    insert(node, parent, anchor) {
      node.parent = parent
      parent.children ??= []
      const index = anchor ? parent.children.indexOf(anchor) : -1
      if (index >= 0) parent.children.splice(index, 0, node)
      else parent.children.push(node)
    },
    remove(node) { const list = node.parent?.children; if (list) list.splice(list.indexOf(node), 1) },
    parentNode: node => node.parent ?? null,
    nextSibling: node => {
      const siblings = node.parent?.children ?? []
      return siblings[siblings.indexOf(node) + 1] ?? null
    },
  })
  // Behavior cases skip painting; the template cases render the real compiled
  // template into the node tree above, so what a user would read is asserted.
  const app = renderer.createApp({ ...QuotaMenu, render: withTemplate ? renderQuotaMenu : () => null })
  const vm = app.mount(tree)
  let live = true
  const unmount = () => { if (live) { live = false; app.unmount() } }
  t.after(unmount)
  return { state: vm.$.setupState, tree, unmount }
}

/** Every piece of text the rendered tree would put on screen. */
function renderedText(node) {
  const own = typeof node.text === 'string' ? [node.text.trim()] : []
  const children = (node.children ?? []).flatMap(renderedText)
  return [...own, ...children].filter(Boolean)
}

/** Every class the rendered tree would apply, so tone can be asserted. */
function renderedClasses(node) {
  const own = typeof node.attrs?.class === 'string' ? node.attrs.class.split(/\s+/) : []
  const children = (node.children ?? []).flatMap(renderedClasses)
  return [...own, ...children].filter(Boolean)
}

// Component cases run against the real clock, so their payloads are timed
// from it; `fullPayload` is fixed to NOW and is for the pure functions.
const aged = (base, ageMs) => ({ ...base, as_of: new Date(Date.now() - ageMs).toISOString() })
const livePayload = () => ({
  providers: [aged(codexProvider, 4 * MINUTE), aged(masoProvider, MINUTE)],
})

function fetchHarness(t, initial = livePayload()) {
  const calls = []
  let handler = async () => new Response(JSON.stringify(initial), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  })
  t.mock.method(globalThis, 'fetch', async (url, init = {}) => {
    calls.push({ url, init })
    return handler(url, init)
  })
  return { calls, set(next) { handler = next } }
}

const open = state => state.handleToggle({ currentTarget: { open: true } })
const close = state => state.handleToggle({ currentTarget: { open: false } })

test('the panel loads once when it is opened and renders both providers', async t => {
  const http = fetchHarness(t)
  const { state } = mountMenu(t)
  assert.equal(http.calls.length, 0, 'a closed panel must not call the provider API')
  assert.equal(state.summaryText, 'Not loaded')

  open(state)
  assert.equal(state.isLoading, true)
  await flush()

  assert.deepEqual(http.calls.map(call => call.url), ['/api/quota'])
  assert.equal(state.isLoading, false)
  assert.equal(state.loadError, null)
  assert.deepEqual(state.providerViews.map(view => view.title), ['Codex', 'MASO'])
  assert.equal(state.providerViews[0].rows[0].value, '42% left')
  assert.equal(state.summaryText, 'Lowest 42% left')

  close(state)
  open(state)
  await flush()
  assert.equal(http.calls.length, 1, 'reopening must reuse the reading already loaded')
})

test('Refresh asks the backend for a new reading', async t => {
  const http = fetchHarness(t)
  const { state } = mountMenu(t)
  open(state)
  await flush()
  state.refreshQuota()
  await flush()
  assert.deepEqual(http.calls.map(call => call.url), ['/api/quota', '/api/quota?refresh=true'])
  assert.equal(state.loadError, null)
})

test('a refresh that returns the identical reading says so, per provider', async t => {
  // Codex republishes only when a turn finishes, so its timestamp stays put
  // while MASO's advances on the very same refresh.
  const frozen = '2026-05-20T09:47:05Z'
  const payload = moved => ({ providers: [
    { ...codexProvider, as_of: frozen },
    { ...masoProvider, as_of: new Date(NOW - moved).toISOString() },
  ] })
  const http = fetchHarness(t, payload(4 * MINUTE))
  const { state } = mountMenu(t)
  open(state)
  await flush()
  assert.deepEqual(state.providerViews.map(view => view.unchangedNote), ['', ''],
    'a first load has nothing to compare against')

  http.set(async () => new Response(JSON.stringify(payload(MINUTE)), { status: 200 }))
  state.refreshQuota()
  await flush()
  assert.match(state.providerViews[0].unchangedNote, /Codex only publishes usage after a conversation turn/)
  assert.equal(state.providerViews[1].unchangedNote, '', 'MASO moved, so it must not be marked')

  // The notice is transient: a reading that finally moves clears it.
  http.set(async () => new Response(JSON.stringify({ providers: [
    { ...codexProvider, as_of: new Date(NOW).toISOString() },
    { ...masoProvider, as_of: new Date(NOW).toISOString() },
  ] }), { status: 200 }))
  state.refreshQuota()
  await flush()
  assert.deepEqual(state.providerViews.map(view => view.unchangedNote), ['', ''])
})

test('a failed refresh keeps the explanation the previous one produced', async t => {
  const frozen = '2026-05-20T09:47:05Z'
  const payload = { providers: [{ ...codexProvider, as_of: frozen }] }
  const http = fetchHarness(t, payload)
  const { state } = mountMenu(t)
  open(state)
  await flush()
  state.refreshQuota()
  await flush()
  assert.match(state.providerViews[0].unchangedNote, /same reading/)

  http.set(async () => new Response(JSON.stringify({ detail: 'Quota service is unavailable' }), { status: 503 }))
  state.refreshQuota()
  await flush()
  assert.equal(state.loadError, 'Could not load quota: Quota service is unavailable')
  assert.match(state.providerViews[0].unchangedNote, /same reading/,
    'a failed refresh does not make the previous reading newer')
})

test('a failed refresh keeps the previous reading, explains itself, and Retry repeats it', async t => {
  const http = fetchHarness(t)
  const { state } = mountMenu(t)
  open(state)
  await flush()

  http.set(async () => new Response(JSON.stringify({ detail: 'Quota service is unavailable' }), {
    status: 503, headers: { 'Content-Type': 'application/json' },
  }))
  state.refreshQuota()
  await flush()
  assert.equal(state.isLoading, false)
  assert.equal(state.loadError, 'Could not load quota: Quota service is unavailable')
  assert.equal(state.providerViews[0].rows[0].value, '42% left', 'a failure must not erase the panel')
  assert.equal(state.summaryText, 'Lowest 42% left')

  state.retryQuota()
  await flush()
  assert.equal(http.calls.at(-1).url, '/api/quota?refresh=true', 'Retry must repeat the request that failed')
  assert.equal(state.loadError, 'Could not load quota: Quota service is unavailable')

  http.set(async () => new Response(JSON.stringify(livePayload()), { status: 200 }))
  state.retryQuota()
  await flush()
  assert.equal(state.loadError, null)
  assert.equal(state.isLoading, false)
})

test('a first load that fails reports an English error and offers the same retry', async t => {
  const http = fetchHarness(t)
  http.set(async () => { throw new TypeError('Failed to fetch') })
  const { state } = mountMenu(t)
  open(state)
  await flush()
  assert.equal(state.loadError, 'Could not load quota: Failed to fetch')
  assert.equal(state.summaryText, 'Load failed')
  assert.deepEqual(state.providerViews, [])

  http.set(async () => new Response(JSON.stringify(livePayload()), { status: 200 }))
  state.retryQuota()
  await flush()
  assert.equal(http.calls.at(-1).url, '/api/quota')
  assert.equal(state.loadError, null)
  assert.equal(state.providerViews.length, 2)
})

test('a response that arrives after the panel is gone cannot write to it', async t => {
  const http = fetchHarness(t)
  let release
  http.set(() => new Promise(resolve => { release = resolve }))
  const { state, unmount } = mountMenu(t)
  open(state)
  assert.equal(state.isLoading, true)

  unmount()
  release(new Response(JSON.stringify(fullPayload), { status: 200 }))
  await flush()

  assert.equal(state.quota, null)
  assert.equal(state.loadError, null)
  assert.deepEqual(state.providerViews, [])
})

test('the rendered panel shows every limit honestly, with no invented zero', async t => {
  // The component reads the real clock, so this payload is timed from it.
  const live = {
    providers: [
      {
        ...codexProvider,
        as_of: new Date(Date.now() - 5 * MINUTE - 10_000).toISOString(),
        limits: [{ ...codexProvider.limits[0], resets_at: new Date(Date.now() + 73 * HOUR).toISOString() }],
      },
      { ...masoProvider, as_of: new Date(Date.now() - 47 * HOUR - MINUTE).toISOString(), stale: true, enforcing: false },
    ],
  }
  fetchHarness(t, live)
  const { state, tree } = mountMenu(t, { withTemplate: true })
  open(state)
  await flush()
  await vue.nextTick()

  const text = renderedText(tree)
  for (const expected of [
    'Codex', 'MASO', 'Weekly limit', '42% left', '58% used', 'Resets in 3d',
    'gpt-6-astra', '86% left', 'es1_lime', 'No limit', 'es1_slate', 'No data yet',
    'Plan: pro', 'Updated 5m ago', 'Updated 47h ago',
  ]) {
    assert.ok(text.includes(expected), `the panel should show "${expected}"`)
  }
  assert.ok(text.some(entry => entry.includes('62500.0000000000') && entry.includes('unit not reported')))
  assert.ok(text.some(entry => /out of date/.test(entry)), 'a stale reading must be called out on screen')
  assert.ok(text.some(entry => /does not refuse requests/.test(entry)))
  assert.ok(!text.some(entry => entry.includes('0%')), 'no row may render a fabricated 0%')
  assert.ok(!text.includes('Loading quota...'))
  assert.ok(!text.includes('No provider reported quota.'))
})

test('the rendered panel offers a readable failure and a retry control', async t => {
  const http = fetchHarness(t)
  http.set(async () => new Response(JSON.stringify({ detail: 'Quota service is unavailable' }), { status: 503 }))
  const { state, tree } = mountMenu(t, { withTemplate: true })
  assert.ok(renderedText(tree).includes('Not loaded'))

  open(state)
  await flush()
  await vue.nextTick()
  const text = renderedText(tree)
  assert.ok(text.includes('Could not load quota: Quota service is unavailable'))
  assert.ok(text.includes('Retry'))
  assert.ok(text.includes('Load failed'))
})

test('the rendered panel tells the user a refresh produced no newer reading', async t => {
  // Reproduces the reported confusion: a two hour old Codex reading that
  // Refresh cannot move.
  const frozen = new Date(Date.now() - 2 * HOUR).toISOString()
  const payload = { providers: [
    { ...codexProvider, as_of: frozen },
    { ...masoProvider, stale: false, as_of: new Date(Date.now() - MINUTE).toISOString() },
  ] }
  const http = fetchHarness(t, payload)
  const { state, tree } = mountMenu(t, { withTemplate: true })
  open(state)
  await flush()
  await vue.nextTick()

  const beforeRefresh = renderedText(tree)
  assert.ok(beforeRefresh.includes('Updated 2h ago'))
  assert.ok(beforeRefresh.some(entry => /last known values, not a live reading/.test(entry)),
    'an hours-old reading must already be marked as not live')
  assert.ok(!beforeRefresh.some(entry => /same reading/.test(entry)))

  http.set(async () => new Response(JSON.stringify({ providers: [
    { ...codexProvider, as_of: frozen },
    { ...masoProvider, stale: false, as_of: new Date().toISOString() },
  ] }), { status: 200 }))
  state.refreshQuota()
  await flush()
  await vue.nextTick()

  const afterRefresh = renderedText(tree)
  assert.ok(afterRefresh.some(entry => entry === 'Refresh returned the same reading. Codex only '
    + 'publishes usage after a conversation turn finishes, so this will not change until one completes.'),
  'the panel must explain why Refresh changed nothing')
  assert.equal(
    afterRefresh.filter(entry => /same reading/.test(entry)).length,
    1,
    'only the provider that stood still may be marked',
  )
})

test('the rendered panel keeps an unconfigured provider quiet', async t => {
  fetchHarness(t, { providers: [aged(codexProvider, MINUTE), notConfigured()] })
  const { state, tree } = mountMenu(t, { withTemplate: true })
  open(state)
  await flush()
  await vue.nextTick()

  const text = renderedText(tree)
  const classes = renderedClasses(tree)
  assert.ok(text.includes('Not configured'))
  assert.ok(text.includes('This Hub has no MASO service configured.'))
  assert.ok(classes.includes('quota-badge--not_configured'))
  assert.ok(classes.includes('quota-note--neutral'))
  assert.ok(!classes.includes('quota-note--error'), 'no alarm styling for a deployment fact')
  assert.ok(!classes.includes('quota-badge--unavailable'))
  assert.ok(!text.some(entry => /did not report any quota/.test(entry)))
  assert.ok(!text.includes('Reading time unknown'), 'no missing-reading noise for a provider that has none')
  assert.equal(state.summaryText, 'Lowest 42% left', 'the collapsed entry must not look broken')
})

test('the rendered panel keeps a genuinely failed provider loud', async t => {
  fetchHarness(t, { providers: [aged(codexProvider, MINUTE), provider({
    provider: 'maso',
    available: false,
    state: 'unavailable',
    message: 'MASO service did not respond.',
    as_of: null,
  })] })
  const { state, tree } = mountMenu(t, { withTemplate: true })
  open(state)
  await flush()
  await vue.nextTick()

  const classes = renderedClasses(tree)
  assert.ok(renderedText(tree).includes('Unavailable'))
  assert.ok(renderedText(tree).includes('MASO service did not respond.'))
  assert.ok(classes.includes('quota-badge--unavailable'))
  assert.ok(classes.includes('quota-note--error'), 'a real outage keeps its alarm styling')
  assert.ok(renderedText(tree).includes('Reading time unknown'),
    'a provider that was tried and failed still reports that it has no timestamp')
  assert.ok(!classes.includes('quota-note--neutral'))
  assert.ok(!classes.includes('quota-badge--not_configured'))
  assert.equal(state.summaryText, 'Lowest 42% left · 1 provider unavailable')
})

test('the quota entry is mounted in the extensions menu', () => {
  const actions = readSource('components/AppExtensionActions.vue')
  assert.match(actions, /import QuotaMenu from '@\/components\/QuotaMenu\.vue'/)
  assert.match(actions, /<QuotaMenu\s*\/>/)
})

test('every quota string the user can read is English', () => {
  for (const [name, text] of [['QuotaMenu.vue', menuSource], ['quotaDisplay.ts', displaySource]]) {
    assert.doesNotMatch(text, /[\u3400-\u9fff\u3040-\u30ff]/, `${name} must not ship non-English copy`)
  }
})
