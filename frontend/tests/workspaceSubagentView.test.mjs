import assert from 'node:assert/strict'
import test from 'node:test'
import { vue } from './workspaceFeatureTestHarness.mjs'
import { loadWorkspaceFeature, mountWorkspaceConsumer } from './workspaceFeatureTestHarness.mjs'

// Direct behavior coverage for InfoWall d2627d2f AC7: the workspace-mode
// Subagents view lists only role-less Hub child Chats attributed to the
// ACTIVE workspace (filtering lives in useWorkspaceRuntime, which the panel
// consumes via context), maps each row to its tab id for jump, and renders a
// correct empty state. The terminal tab store is the only possible source —
// board sessions never contain these tabs.

const contexts = await loadWorkspaceFeature('context.ts')
const { default: Panel } = await loadWorkspaceFeature('components/WorkspaceAgentStatus.vue')

function tab(overrides = {}) {
  return {
    id: 'tab',
    name: 'child',
    agent_type: 'codex',
    session_kind: 'chat',
    workspace_id: 'ws-1',
    parent_tab_id: 'parent-tab',
    archived: false,
    is_active: true,
    ...overrides,
  }
}

function mountPanel(t, options = {}) {
  const {
    activeWorkspaceId = 'ws-1',
    subagentTabs = [],
    subagentViewActive = true,
  } = options
  const openedTabIds = []
  const terminalTabs = vue.ref(subagentTabs)
  const runtime = {
    activeWorkspaceId: vue.ref(activeWorkspaceId),
    workspaceSessionView: vue.ref('agents'),
    workspaceAgents: vue.ref([]),
    reviewerSessions: vue.ref([]),
    visibleWorkspaceSessions: vue.ref([]),
    visibleWorkspaceSessionsEmptyText: vue.ref('No agents in this workspace.'),
    isPending: () => false,
    refreshAgentStatuses: () => {},
    sessionActionKey: () => 'noop',
    openSession: () => {},
    agentRuntimeStatus: () => 'idle',
    agentRoleLabel: () => 'Agent',
    agentRuntimeDetail: () => '',
    agentTaskLabel: () => '',
    agentRuntimeText: () => 'Idle',
    canSwitchAgentEnv: () => false,
    canDeleteAgent: () => false,
    agentDeleteTitle: () => '',
    agentActionKey: () => '',
    deleteAgent: () => {},
    // The store-derived filter under test, mirrored from useWorkspaceRuntime.
    workspaceSubagentTabs: vue.computed(() => {
      const wsId = activeWorkspaceId
      return terminalTabs.value.filter(
        row =>
          row.workspace_id === wsId &&
          Boolean(row.parent_tab_id) &&
          !row.archived &&
          row.session_kind === 'chat',
      )
    }),
    workspaceSubagentView: vue.ref(subagentViewActive),
    showManagedView(view) {
      runtime.workspaceSubagentView.value = false
      runtime.workspaceSessionView.value = view
    },
    toggleWorkspaceSubagentView() {
      runtime.workspaceSubagentView.value = !runtime.workspaceSubagentView.value
    },
    openSubagentTab(tabId) {
      openedTabIds.push(tabId)
    },
    subagentRuntimeStatus: row => row.runtimeStatus ?? 'idle',
    subagentStatusText: row => row.statusText ?? 'Idle',
  }
  const panel = mountWorkspaceConsumer(t, Panel, () => {
    contexts.provideWorkspaceRuntimeContext(runtime)
    contexts.provideWorkspaceEditorContext({ isResidentAgent: () => false })
    contexts.provideWorkspaceAgentManagerContext({})
  })
  return { panel, runtime, terminalTabs, openedTabIds }
}

function findAll(node, predicate, out = []) {
  if (predicate(node)) out.push(node)
  for (const child of node.children || []) findAll(child, predicate, out)
  return out
}

function contains(node, text) {
  return node.text?.trim() === text || (node.children || []).some(child => contains(child, text))
}

function subagentRows(panel) {
  return findAll(panel.root, node => node.props?.class === 'workspace-subagent-row')
}

function toggleButton(panel) {
  return findAll(panel.root, node => node.props?.['data-testid'] === 'workspace-subagents-toggle')[0]
}

test('Subagents view lists only attributed live child chats of the active workspace', t => {
  const { panel } = mountPanel(t, {
    subagentTabs: [
      tab({
        id: 'child-a',
        name: 'codex subagent: parser',
        runtimeStatus: 'working',
        statusText: 'Working',
      }),
      // Another workspace: excluded.
      tab({ id: 'child-b', workspace_id: 'ws-2' }),
      // Attributed to ws-1 but not a sub-agent link (e.g. a managed session tab): excluded.
      tab({ id: 'child-c', parent_tab_id: null }),
      // Archived child: excluded from the live view.
      tab({ id: 'child-d', archived: true }),
    ],
  })

  const rows = subagentRows(panel)
  assert.equal(rows.length, 1)
  assert.ok(contains(rows[0], 'codex subagent: parser'))
  assert.ok(contains(rows[0], 'Sub-agent'))
  assert.ok(contains(rows[0], 'Working'))
  const dot = findAll(
    rows[0],
    node => node.props?.class === 'agent-status-dot',
  )[0]
  assert.equal(dot.props['data-status'], 'working')
})

test('clicking a sub-agent row jumps to its tab id', t => {
  const { panel, openedTabIds } = mountPanel(t, {
    subagentTabs: [tab({ id: 'jump-target', name: 'jump me' })],
  })
  const row = subagentRows(panel)[0]
  row.props.onClick()
  assert.deepEqual(openedTabIds, ['jump-target'])
})

test('toggle hides the list for the managed Agents grid and returns with the count', async t => {
  const { panel } = mountPanel(t, {
    subagentTabs: [tab({ id: 'a' }), tab({ id: 'b' })],
  })
  assert.equal(subagentRows(panel).length, 2)

  toggleButton(panel).props.onClick()
  await vue.nextTick()
  assert.deepEqual(subagentRows(panel), [])
  // Managed grid is back; its empty-state placeholder exists.
  assert.ok(
    findAll(panel.root, node => node.props?.class === 'agent-status-grid').length >= 1,
  )

  toggleButton(panel).props.onClick()
  await vue.nextTick()
  assert.equal(subagentRows(panel).length, 2)
  const button = toggleButton(panel)
  assert.ok(contains(button, 'Subagents'))
  assert.ok(contains(button, '2'))
})

test('empty sub-agent view shows the dedicated empty state', t => {
  const { panel } = mountPanel(t, { subagentTabs: [] })
  assert.deepEqual(subagentRows(panel), [])
  assert.ok(contains(panel.root, 'No sub-agents in this workspace.'))
})

test('no sub-agent view is rendered without an active workspace', t => {
  const { panel } = mountPanel(t, { activeWorkspaceId: null, subagentTabs: [tab()] })
  assert.deepEqual(subagentRows(panel), [])
  assert.equal(toggleButton(panel), undefined)
})
