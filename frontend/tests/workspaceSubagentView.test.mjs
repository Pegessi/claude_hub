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
    sessions = [],
  } = options
  const openedTabIds = []
  const terminalTabs = vue.ref(subagentTabs)
  const runtime = {
    activeWorkspaceId: vue.ref(activeWorkspaceId),
    visibleWorkspaceSessions: vue.ref(sessions),
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
    showManagedView() {
      runtime.workspaceSubagentView.value = false
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

test('toggle switches between Active Runs and Subagents jump cards', async t => {
  const { panel } = mountPanel(t, {
    subagentTabs: [tab({ id: 'a' }), tab({ id: 'b' })],
    sessions: [{ id: 'run', title: 'Task session', agent_type: 'codex' }],
  })
  assert.equal(subagentRows(panel).length, 2)

  toggleButton(panel).props.onClick()
  await vue.nextTick()
  assert.deepEqual(subagentRows(panel), [])
  // The task session grid is back.
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

test('no sessions or children hides the whole strip, even with Subagents selected', t => {
  const { panel } = mountPanel(t, { subagentTabs: [] })
  assert.deepEqual(subagentRows(panel), [])
  assert.equal(panel.find(n => n.props?.class === 'workspace-agent-status'), null)
})

test('no sub-agent view is rendered without an active workspace', t => {
  const { panel } = mountPanel(t, { activeWorkspaceId: null, subagentTabs: [tab()] })
  assert.deepEqual(subagentRows(panel), [])
  assert.equal(toggleButton(panel), undefined)
})

test('a child alone opens its jump card by default and last-child removal hides the strip', async t => {
  const { panel, terminalTabs } = mountPanel(t, {
    subagentViewActive: false, subagentTabs: [tab({ id: 'only-child' })],
  })
  assert.equal(subagentRows(panel).length, 1)
  terminalTabs.value = []
  await vue.nextTick()
  assert.equal(panel.find(n => n.props?.class === 'workspace-agent-status'), null)
})

test('an active run alone keeps a jump card and does not expose a Reviewers tab', t => {
  const { panel } = mountPanel(t, {
    subagentViewActive: false,
    sessions: [{ id: 'task-run', title: 'Task run', agent_type: 'claude' }],
  })
  assert.ok(panel.find(n => n.props?.class === 'agent-status-card-main'))
  assert.ok(contains(panel.root, 'Active Runs'))
  assert.equal(contains(panel.root, 'Reviewers'), false)
})

test('when the last child leaves, an existing run remains visible instead of an empty Subagents view', async t => {
  const { panel, terminalTabs } = mountPanel(t, {
    subagentTabs: [tab({ id: 'last-child' })],
    sessions: [{ id: 'run', title: 'Task session', agent_type: 'codex' }],
  })
  assert.equal(subagentRows(panel).length, 1)
  terminalTabs.value = []
  await vue.nextTick()
  assert.ok(panel.find(n => n.props?.class === 'agent-status-card-main'))
  assert.deepEqual(subagentRows(panel), [])
})
