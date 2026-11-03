import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createPinia, getActivePinia, setActivePinia } from 'pinia'
import ts from 'typescript'
import { vue } from './workspaceFeatureTestHarness.mjs'
import { useWorkspaceStore } from './workspaceStoreTestHarness.mjs'

const stateKey = '__workspaceDeleteConfirmTest'
const asModule = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`

function transpile(path) {
  return ts.transpileModule(path, {
    compilerOptions: { module: ts.ModuleKind.ES2020, target: ts.ScriptTarget.ES2020 },
  }).outputText
}

// Compile the real runtime with stubbed stores, keeping usePendingActions and
// workspaceDeletion REAL so the pending guard and confirm wording are tested.
const runtimeSource = await readFile(
  new URL('../src/features/workspace/composables/useWorkspaceRuntime.ts', import.meta.url),
  'utf8'
)
let compiled = transpile(runtimeSource)
const pendingSource = await readFile(
  new URL('../src/composables/usePendingActions.ts', import.meta.url),
  'utf8'
)
const vueUrl = import.meta.resolve('vue')
const piniaUrl = import.meta.resolve('pinia')
const pendingCompiled = transpile(pendingSource).replaceAll(
  `from 'vue'`, `from ${JSON.stringify(vueUrl)}`
)
const deletionCompiled = transpile(await readFile(
  new URL('../src/features/workspace/presentation/workspaceDeletion.ts', import.meta.url),
  'utf8'
))

const replacements = {
  '@/stores/appStore': asModule(`export const useAppStore = () => globalThis.${stateKey}.app`),
  '@/stores/terminalStore': asModule(`export const useTerminalStore = () => globalThis.${stateKey}.terminal`),
  '@/stores/workspaceStore': asModule(`export const useWorkspaceStore = () => globalThis.${stateKey}.workspace`),
  '@/composables/useLaunchEnvPresets': asModule('export const useLaunchEnvPresets = () => ({envPresets: [],getPresetText: () => "",defaultPresetTextForAgent: () => ""})'),
  '@/composables/usePendingActions': asModule(pendingCompiled),
  '@/composables/useTaskCreateFlow': asModule('export const useTaskCreateFlow = () => ({})'),
  '@/utils/draftAttachments': asModule("export const formatAttachmentSize = () => ''"),
  '../presentation/workspaceDeletion': asModule(deletionCompiled),
  vue: vueUrl,
  pinia: piniaUrl,
}
for (const [specifier, replacement] of Object.entries(replacements)) {
  compiled = compiled.replaceAll(`from '${specifier}'`, `from ${JSON.stringify(replacement)}`)
}
const { useWorkspaceRuntime } = await import(asModule(compiled))

const NOW = '2026-11-03T00:00:00Z'
function workspace(id, name) {
  return {
    id, name, path: `/x/${id}`, default_branch: 'main', session_prefix: id,
    target: 'local', created_at: NOW, updated_at: NOW,
  }
}
function busyBoard() {
  return {
    workspace: workspace('ws-1', 'Workspace One'),
    tasks: [{ id: 't1', status: 'review' }],
    sessions: [{ id: 's1', role: 'orchestrator', runtime_status: 'working' }],
    reports: [],
    tasks_pagination: { status_counts: { review: 1 } },
  }
}

test('confirmDeleteWorkspace: cancel, wording, DELETE reuse, pending guard, failure', async (t) => {
  const requests = []
  const confirms = []
  const accept = vue.ref(false)
  const previous = {
    fetch: globalThis.fetch,
    window: globalThis.window,
    localStorage: globalThis.localStorage,
  }
  globalThis.window = {
    confirm: (message) => { confirms.push(message); return accept.value },
  }
  globalThis.localStorage = { getItem: () => 'ws-1', setItem() {}, removeItem() {} }
  const previousPinia = getActivePinia()
  setActivePinia(createPinia())
  const scope = vue.effectScope()
  t.after(() => {
    scope.stop()
    setActivePinia(previousPinia)
    globalThis.fetch = previous.fetch
    globalThis.window = previous.window
    globalThis.localStorage = previous.localStorage
    delete globalThis[stateKey]
  })

  const store = useWorkspaceStore()
  store.workspaces = [workspace('ws-1', 'Workspace One'), workspace('ws-2', 'Workspace Two')]
  store.board = busyBoard()
  globalThis[stateKey] = {
    app: { colorScheme: vue.ref('light') },
    terminal: {
      agentStatuses: vue.ref([]), tabs: vue.ref([]), notifications: vue.ref([]),
    },
    workspace: store,
  }
  const runtime = scope.run(() => useWorkspaceRuntime())

  async function json204() {
    return new Response(null, { status: 204 })
  }
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ method: options.method ?? 'GET', url })
    if (options.method === 'DELETE') return json204()
    return new Response('[]', { status: 200 })
  }

  // Cancel: nothing deleted; active wording names resources qualitatively.
  await runtime.confirmDeleteWorkspace({ id: 'ws-1', name: 'Workspace One' })
  assert.equal(requests.length, 0)
  assert.match(confirms[0], /Any sessions running in this workspace will be stopped\./)
  assert.match(confirms[0], /still has open tasks and live sessions/)

  // Non-active: stop clause always, no board-derived detail.
  await runtime.confirmDeleteWorkspace({ id: 'ws-2', name: 'Workspace Two' })
  assert.equal(requests.length, 0)
  assert.match(confirms[1], /Any sessions running in this workspace will be stopped\./)
  assert.doesNotMatch(confirms[1], /still has/)

  // Accept: reuses DELETE /api/workspaces/{id} and removes the row.
  accept.value = true
  await runtime.confirmDeleteWorkspace({ id: 'ws-2', name: 'Workspace Two' })
  assert.deepEqual(requests, [{ method: 'DELETE', url: '/api/workspaces/ws-2' }])
  assert.deepEqual(store.workspaces.map(item => item.id), ['ws-1'])

  // Reentrant guard: a second call while DELETE is pending sends nothing.
  requests.length = 0
  let resolveDelete = () => {}
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ method: options.method ?? 'GET', url })
    if (options.method === 'DELETE') {
      await new Promise(resolve => { resolveDelete = resolve })
      return new Response(null, { status: 204 })
    }
    return new Response('[]', { status: 200 })
  }
  const first = runtime.confirmDeleteWorkspace({ id: 'ws-1', name: 'Workspace One' })
  const blocked = runtime.confirmDeleteWorkspace({ id: 'ws-1', name: 'Workspace One' })
  await blocked
  resolveDelete()
  await first
  assert.deepEqual(requests.filter(request => request.method === 'DELETE'), [
    { method: 'DELETE', url: '/api/workspaces/ws-1' },
  ])

  // Failed DELETE does not throw out of the glue and keeps the row.
  store.workspaces.push(workspace('ws-1', 'Workspace One'))
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === 'DELETE') {
      return new Response(
        JSON.stringify({ detail: 'boom' }),
        { status: 500, headers: { 'content-type': 'application/json' } }
      )
    }
    return new Response('[]', { status: 200 })
  }
  await assert.doesNotReject(runtime.confirmDeleteWorkspace({ id: 'ws-1', name: 'Workspace One' }))
  assert.ok(store.workspaces.some(item => item.id === 'ws-1'))
  assert.ok(store.notifications.some(n => n.type === 'error' && /boom/.test(n.message)))
})
