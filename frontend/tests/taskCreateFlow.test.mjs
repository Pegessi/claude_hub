import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { loadWorkspaceFeature, vue } from './workspaceFeatureTestHarness.mjs'

const { useTaskCreateFlow } = await loadWorkspaceFeature('../../composables/useTaskCreateFlow.ts')
const { useWorkspaceStore } = await loadWorkspaceFeature('../../stores/workspaceStore.ts')

function setup(t, target = 'local') {
  const previousStorage = globalThis.localStorage
  const previousSessionStorage = globalThis.sessionStorage
  const items = new Map()
  globalThis.localStorage = {
    getItem: key => items.get(key) ?? null,
    setItem: (key, value) => items.set(key, value),
    removeItem: key => items.delete(key),
  }
  globalThis.sessionStorage = globalThis.localStorage
  t.after(() => {
    globalThis.localStorage = previousStorage
    globalThis.sessionStorage = previousSessionStorage
  })
  setActivePinia(createPinia())
  const workspace = useWorkspaceStore()
  workspace.workspaces = [{ id: 'ws', target }]
  workspace.activeWorkspaceId = 'ws'
  workspace.taskCapabilitiesWorkspaceId = 'ws'
  workspace.taskCapabilitiesStatus = 'supported'
  const calls = []
  workspace.createTask = async (payload, workspaceId) => {
    calls.push({ payload: JSON.parse(JSON.stringify(payload)), workspaceId })
    return { id: 'new-task', ...payload }
  }
  workspace.fetchBoard = async () => {}
  const scope = vue.effectScope()
  const flow = scope.run(() => useTaskCreateFlow())
  t.after(() => { flow.dispose(); scope.stop() })
  flow.open('ws')
  flow.form.title = 'New task'
  flow.form.prompt = 'Implement the contract'
  return { flow, workspace, calls }
}

test('local creation defaults to Chat, carries agent family and omits resident seat fields', async t => {
  const { flow, calls } = setup(t)
  assert.equal(flow.form.session_kind, 'chat')
  assert.equal(flow.form.agent_type, 'codex')
  assert.deepEqual(flow.availableAgentTypes.value, ['codex', 'claude', 'maso', 'cursor', 'antigravity'])
  assert.equal('session_id' in flow.form, false)
  assert.equal('clear_context' in flow.form, false)
  flow.form.agent_type = 'maso'
  flow.form.related_task_id = 'previous-task'
  await flow.submit()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].payload.session_kind, 'chat')
  assert.equal(calls[0].payload.agent_type, 'maso')
  assert.equal(calls[0].payload.related_task_id, 'previous-task')
  assert.equal('session_id' in calls[0].payload, false)
  assert.equal('clear_context' in calls[0].payload, false)
  flow.open('ws')
  assert.equal(flow.form.session_kind, 'chat')
  assert.equal(flow.form.agent_type, 'codex')
})

test('remote creation defaults to Terminal and rejects Chat, local re-open restores Chat', async t => {
  const { flow, workspace, calls } = setup(t, 'remote')
  assert.equal(flow.isRemoteWorkspace.value, true)
  assert.equal(flow.form.session_kind, 'terminal')
  assert.deepEqual(flow.availableAgentTypes.value, ['codex', 'claude', 'cursor'])
  flow.setSessionKind('chat')
  assert.equal(flow.form.session_kind, 'terminal')
  flow.form.agent_type = 'claude'
  await flow.submit()
  assert.equal(calls[0].payload.session_kind, 'terminal')
  assert.equal(calls[0].payload.agent_type, 'claude')
  workspace.workspaces[0].target = 'local'
  flow.open('ws')
  assert.equal(flow.form.session_kind, 'chat')
})

for (const provider of ['maso', 'antigravity']) {
  test(`switching to Terminal or a remote target clears Chat-only ${provider}`, t => {
    const { flow, workspace } = setup(t)
    flow.form.agent_type = provider
    flow.setSessionKind('terminal')
    assert.equal(flow.form.agent_type, 'codex')
    flow.setSessionKind('chat')
    flow.form.agent_type = provider
    workspace.workspaces[0].target = 'remote'
    assert.equal(flow.form.session_kind, 'terminal')
    assert.equal(flow.form.agent_type, 'codex')
  })
}

test('Workspace does not offer unsupported TraeX on either surface', t => {
  const { flow } = setup(t)
  assert.equal(flow.availableAgentTypes.value.includes('traex'), false)
  flow.setSessionKind('terminal')
  assert.equal(flow.availableAgentTypes.value.includes('traex'), false)
})

test('retry preserves the frozen agent family and surface rather than creating a second request', async t => {
  const { flow, workspace, calls } = setup(t)
  workspace.createTask = async payload => {
    calls.push(JSON.parse(JSON.stringify(payload)))
    if (calls.length === 1) throw new Error('uncertain response')
    return { id: 'retry-task', ...payload }
  }
  flow.form.agent_type = 'cursor'
  await flow.submit()
  assert.ok(flow.pendingAttempt.value)
  flow.close()
  flow.open('ws')
  await flow.submit()
  assert.equal(calls.length, 2)
  assert.deepEqual(calls[1], calls[0])
  assert.equal(calls[1].agent_type, 'cursor')
  assert.equal(calls[1].session_kind, 'chat')
})

test('workspace and task UI expose no resident pool controls, while Resident configuration remains', () => {
  const source = path => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')
  const header = source('features/workspace/components/WorkspaceHeader.vue')
  const page = source('features/workspace/WorkspacePage.vue')
  const create = source('components/TaskCreateDialog.vue')
  const edit = source('features/workspace/components/TaskEditDialog.vue')
  const board = source('features/workspace/components/WorkspaceBoard.vue')
  assert.doesNotMatch(header, /Manage Agents|openAgentOptions/)
  assert.match(header, /Per-task isolated sessions with contract-driven blind review\./)
  assert.doesNotMatch(page, /WorkspaceAgentDialog/)
  assert.match(page, /<ResidentAgentDialog \/>/)
  assert.match(page, /<WorkspaceHeader \/>\s*<WorkspaceAgentStatus \/>\s*<WorkspaceTriagePro/)
  for (const ui of [create, edit, board]) {
    assert.doesNotMatch(ui, /Dispatch agent|Clear context|Dispatch options/)
  }
  assert.match(create, /v-model="form\.agent_type"/)
  assert.match(create, /Select only when you need to continue another task’s context/)
  assert.match(board, /Create or select a workspace to add and run tasks\./)
})
