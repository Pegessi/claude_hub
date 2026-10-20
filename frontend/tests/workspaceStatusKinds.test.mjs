import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { createPinia, defineStore, setActivePinia } from 'pinia'
import ts from 'typescript'
import { loadWorkspaceFeature, mountWorkspaceConsumer, vue } from './workspaceFeatureTestHarness.mjs'
import { useWorkspaceStore } from './workspaceStoreTestHarness.mjs'

const contexts = await loadWorkspaceFeature('context.ts')
const { default: Panel } = await loadWorkspaceFeature('components/WorkspaceAgentStatus.vue')
const source = await readFile(new URL('../src/features/workspace/composables/useWorkspaceRuntime.ts', import.meta.url), 'utf8')
const asModule = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const stateKey = '__workspaceStatusKindsTest'
let compiled = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022,
} }).outputText
const replacements = {
  '@/stores/appStore': asModule(`export const useAppStore = () => globalThis.${stateKey}.app`),
  '@/stores/terminalStore': asModule(`export const useTerminalStore = () => globalThis.${stateKey}.terminal`),
  '@/stores/workspaceStore': asModule(`export const useWorkspaceStore = () => globalThis.${stateKey}.workspace`),
  '@/composables/useLaunchEnvPresets': asModule(`export const useLaunchEnvPresets = () => ({envPresets: [],getPresetText: () => '',defaultPresetTextForAgent: () => ''})`),
  '@/composables/usePendingActions': asModule(`export const usePendingActions = () => ({isPending: () => false,runPending: (_k, cb) => cb()})`),
  '@/composables/useTaskCreateFlow': asModule('export const useTaskCreateFlow = () => ({})'),
  '@/utils/draftAttachments': asModule("export const formatAttachmentSize = () => ''"),
  vue: import.meta.resolve('vue'),
  pinia: import.meta.resolve('pinia'),
}
for (const [specifier, replacement] of Object.entries(replacements)) {
  compiled = compiled.replaceAll(`from '${specifier}'`, `from ${JSON.stringify(replacement)}`)
}
const { useWorkspaceRuntime } = await import(asModule(compiled))

test('real runtime and Agents/Reviewers panel show native and terminal busy/idle without parsing transcript text', async (t) => {
  const previousStorage = globalThis.localStorage
  globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }
  t.after(() => { globalThis.localStorage = previousStorage })
  setActivePinia(createPinia())
  const app = defineStore('status-app', () => ({ colorScheme: vue.ref('dark') }))()
  const terminal = defineStore('status-terminal', () => ({
    agentStatuses: vue.ref([]), tabs: vue.ref([]), notifications: vue.ref([]),
  }))()
  const workspace = useWorkspaceStore()
  globalThis[stateKey] = { app, terminal, workspace }
  t.after(() => { delete globalThis[stateKey] })
  const scope = vue.effectScope()
  t.after(() => scope.stop())
  const runtime = scope.run(() => useWorkspaceRuntime())
  workspace.activeWorkspaceId = 'kind-ws'
  const base = { target: 'local', title: 'Status kind', status: 'idle', runtime_status: 'idle', queued_count: 0 }
  const sessions = [
    { ...base, id: 'native-agent', tab_id: 'native-agent-tab', role: 'orchestrator', session_kind: 'chat', agent_type: 'claude' },
    { ...base, id: 'native-codex-agent', tab_id: 'native-codex-agent-tab', role: 'orchestrator', session_kind: 'chat', agent_type: 'codex' },
    { ...base, id: 'native-claude-reviewer', tab_id: 'native-claude-reviewer-tab', role: 'reviewer', session_kind: 'chat', agent_type: 'claude' },
    { ...base, id: 'terminal-claude-reviewer', tab_id: 'terminal-claude-reviewer-tab', role: 'reviewer', session_kind: 'terminal', agent_type: 'claude' },
    { ...base, id: 'native-reviewer', tab_id: 'native-reviewer-tab', role: 'reviewer', session_kind: 'chat', agent_type: 'codex' },
    { ...base, id: 'terminal-reviewer', tab_id: 'terminal-reviewer-tab', role: 'reviewer', session_kind: 'terminal', agent_type: 'codex' },
  ]
  workspace.board = { workspace: { id: 'kind-ws' }, sessions, tasks: [], reports: [] }
  const panel = mountWorkspaceConsumer(t, Panel, () => {
    contexts.provideWorkspaceRuntimeContext(runtime)
    contexts.provideWorkspaceEditorContext({ isResidentAgent: () => false })
    contexts.provideWorkspaceAgentManagerContext({})
  })
  // Use distinct titles to locate the rendered card without coupling to nesting.
  sessions.forEach(s => { s.title = s.id })
  function contains(node, text) {
    return node.text?.trim() === text || (node.children || []).some(child => contains(child, text))
  }
  function findIn(node, className) {
    if (node.props?.class === className) return node
    for (const child of node.children || []) {
      const found = findIn(child, className)
      if (found) return found
    }
    return null
  }
  for (const view of ['agents', 'reviewers']) {
    runtime.workspaceSessionView.value = view
    for (const state of ['working', 'idle', 'working']) {
      // Intentionally stale board runtime: the panel must prefer current tab API status.
      workspace.board = { ...workspace.board, sessions: sessions.map(s => ({ ...s, runtime_status: state === 'working' ? 'idle' : 'working' })) }
      terminal.agentStatuses = sessions.map(s => ({ tab_id: s.tab_id, status: state, status_text: state === 'working' ? 'Working' : 'Idle', detail: `${s.session_kind} ${state}` }))
      await vue.nextTick()
      for (const session of sessions.filter(s => (view === 'reviewers') === (s.role === 'reviewer'))) {
        const rendered = panel.find(n => n.props?.class === 'agent-status-card' && contains(n, session.id))
        assert.ok(rendered, `${view} card ${session.id}`)
        assert.equal(findIn(rendered, 'agent-status-dot').props['data-status'], state)
        const badge = findIn(rendered, 'agent-status-pill')
        assert.equal(badge.props['data-status'], state)
        assert.ok(contains(badge, state === 'working' ? 'Working' : 'Idle'))
        assert.ok(contains(rendered, `${session.session_kind} ${state}`))
      }
    }
  }
  // Missing tab poll falls back to managed session runtime for both kinds/roles.
  terminal.agentStatuses = []
  await vue.nextTick()
  for (const session of workspace.board.sessions) assert.equal(runtime.agentRuntimeStatus(session), session.runtime_status)
})
