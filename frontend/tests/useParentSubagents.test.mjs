import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import * as vue from 'vue'

const dataUrl = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const transpile = path =>
  ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
  }).outputText

const vueUrl = await import.meta.resolve('vue')
// Lifetime hooks only run inside a component; the tests drive refresh()/jump()
// directly, so no-op the hooks while keeping real reactivity.
const vueStubUrl = dataUrl(`
export { computed, ref, watch } from ${JSON.stringify(vueUrl)};
export const onMounted = () => {};
export const onUnmounted = () => {};
export const onActivated = () => {};
export const onDeactivated = () => {};
`)
const helpersStubUrl = dataUrl(`export const API_BASE = '/api';`)
const terminalStoreStubUrl = dataUrl(`
export function useTerminalStore() {
  return globalThis.__subagentHarness.terminalStore;
}
`)
const appStoreStubUrl = dataUrl(`
export function useAppStore() {
  return globalThis.__subagentHarness.appStore;
}
`)
let presentationUrl = dataUrl(transpile('../src/features/chat/presentation/subagentJump.ts'))
let composableCode = transpile('../src/features/chat/controllers/useParentSubagents.ts')
for (const [specifier, url] of [
  ['vue', vueStubUrl],
  ['@/stores/terminal/helpers', helpersStubUrl],
  ['@/stores/terminalStore', terminalStoreStubUrl],
  ['@/stores/appStore', appStoreStubUrl],
  ['../presentation/subagentJump', presentationUrl],
]) {
  composableCode = composableCode.replaceAll(`'${specifier}'`, JSON.stringify(url))
}
const composableUrl = dataUrl(composableCode)
const { useParentSubagents } = await import(composableUrl)

function summaryRow(overrides = {}) {
  return {
    id: 'sa-0123456789ab',
    tab_id: 'child-tab',
    parent_tab_id: 'parent-tab',
    agent_type: 'codex',
    name: 'codex subagent: task',
    keep: false,
    closed: false,
    close_reason: null,
    created_at: '2026-11-03T00:00:00Z',
    turn_count: 1,
    last_turn_id: 'turn-1',
    last_status: 'running',
    last_summary: null,
    ...overrides,
  }
}

function makeHarness() {
  const calls = []
  // Pinia unwraps refs on the store instance; reactive() mirrors that.
  const terminalStore = vue.reactive({
    tabs: [],
    archivedTabs: [],
    agentStatuses: [],
    async fetchTabs() {
      calls.push('fetchTabs')
    },
    setActiveTab(id) {
      calls.push(['setActiveTab', id])
    },
    async unarchiveTab(id) {
      calls.push(['unarchiveTab', id])
      return true
    },
  })
  const appStore = vue.reactive({
    mode: 'terminal',
    setMode(mode) {
      calls.push(['setMode', mode])
    },
  })
  globalThis.__subagentHarness = { terminalStore, appStore, calls }
  return { terminalStore, appStore, calls }
}

function stubFetch(routes) {
  const requests = []
  globalThis.fetch = async url => {
    requests.push(String(url))
    for (const route of routes) {
      if (String(url).startsWith(route.prefix)) {
        return {
          ok: route.ok !== false,
          status: route.status ?? 200,
          json: async () => route.rows,
        }
      }
    }
    return { ok: false, status: 404, json: async () => [] }
  }
  return requests
}

test('refresh lists closed children for the parent and builds live chips', async () => {
  const harness = makeHarness()
  const requests = stubFetch([
    { prefix: '/api/subagents', rows: [summaryRow()] },
  ])
  const controller = useParentSubagents(vue.ref('parent-tab'))

  await controller.refresh()
  assert.equal(requests.length, 1)
  const url = new URL(requests[0], 'http://local')
  assert.equal(url.pathname, '/api/subagents')
  assert.equal(url.searchParams.get('parent_tab_id'), 'parent-tab')
  assert.equal(url.searchParams.get('include_closed'), 'true')
  assert.equal(controller.chips.value.length, 1)
  assert.equal(controller.chips.value[0].state, 'running')
  assert.equal(controller.chips.value[0].canJump, true)

  // Once the live tab list arrives, the chip still maps from store state.
  harness.terminalStore.tabs = [
    { id: 'child-tab', session_kind: 'chat', archived: false },
  ]
  assert.equal(controller.chips.value[0].state, 'running')
})

test('failed fetch keeps prior rows and is non-fatal', async () => {
  makeHarness()
  const requests = stubFetch([
    { prefix: '/api/subagents', rows: [summaryRow()] },
  ])
  const controller = useParentSubagents(vue.ref('parent-tab'))
  await controller.refresh()
  assert.equal(controller.chips.value.length, 1)

  globalThis.fetch = async url => {
    requests.push(String(url))
    return { ok: false, status: 500, json: async () => [] }
  }
  await controller.refresh()
  assert.equal(controller.failed.value, true)
  assert.equal(controller.chips.value.length, 1)
})

test('jump to a live tab refreshes and focuses in terminal mode', async () => {
  const harness = makeHarness()
  stubFetch([{ prefix: '/api/subagents', rows: [summaryRow()] }])
  const controller = useParentSubagents(vue.ref('parent-tab'))
  await controller.refresh()
  harness.terminalStore.tabs = [
    { id: 'child-tab', session_kind: 'chat', archived: false },
  ]

  await controller.jump(controller.chips.value[0])
  assert.deepEqual(harness.calls, [
    'fetchTabs',
    ['setMode', 'terminal'],
    ['setActiveTab', 'child-tab'],
  ])
})

test('jump to an archived child restores it first', async () => {
  const harness = makeHarness()
  stubFetch([
    {
      prefix: '/api/subagents',
      rows: [summaryRow({ closed: true, last_status: 'completed' })],
    },
  ])
  const controller = useParentSubagents(vue.ref('parent-tab'))
  await controller.refresh()
  // Archived children are served by the separate archived-tabs store ref.
  harness.terminalStore.archivedTabs = [
    { id: 'child-tab', session_kind: 'chat', archived: true },
  ]

  const chip = controller.chips.value[0]
  assert.equal(chip.archived, true)
  assert.equal(chip.state, 'closed')
  await controller.jump(chip)
  assert.deepEqual(harness.calls, [
    ['unarchiveTab', 'child-tab'],
    ['setMode', 'terminal'],
  ])
})

test('switching parent tab resets and refetches rows', async () => {
  makeHarness()
  const requests = stubFetch([
    { prefix: '/api/subagents', rows: [summaryRow()] },
  ])
  const tabId = vue.ref('parent-tab')
  const controller = useParentSubagents(tabId)
  await controller.refresh()
  assert.equal(controller.chips.value.length, 1)

  tabId.value = 'other-parent'
  await vue.nextTick()
  assert.equal(controller.chips.value.length, 0)
  await Promise.resolve()
  const lastUrl = new URL(requests.at(-1), 'http://local')
  assert.equal(lastUrl.searchParams.get('parent_tab_id'), 'other-parent')
})

test('polling pauses outside terminal mode and refetches on return', async () => {
  const harness = makeHarness()
  const requests = stubFetch([{ prefix: '/api/subagents', rows: [summaryRow()] }])
  const controller = useParentSubagents(vue.ref('parent-tab'))
  await controller.refresh()
  assert.equal(requests.length, 1)

  harness.appStore.mode = 'workspace'
  await vue.nextTick()
  assert.equal(requests.length, 1)

  // Coming back must not wait a whole poll interval for fresh rows.
  harness.appStore.mode = 'terminal'
  await vue.nextTick()
  assert.equal(requests.length, 2)
  // The resumed interval belongs to this controller; leave no timer behind.
  harness.appStore.mode = 'workspace'
  await vue.nextTick()
})
