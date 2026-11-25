import assert from 'node:assert/strict'
import test from 'node:test'
import { createPinia, setActivePinia } from 'pinia'

const storage = new Map()
globalThis.localStorage = {
  getItem: key => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: key => storage.delete(key),
  clear: () => storage.clear(),
}
globalThis.window = {
  addEventListener() {},
  removeEventListener() {},
  setTimeout: () => 1,
  clearTimeout() {},
}
globalThis.document = {
  visibilityState: 'visible',
  addEventListener() {},
  removeEventListener() {},
}

const { managedChatSurfaceModuleUrl, useTerminalStore } = await import(
  './terminalStoreTestHarness.mjs'
)
const {
  isManagedChatTab,
  resolveTabDeletePath,
  resolveTabUnarchivePath,
} = await import(managedChatSurfaceModuleUrl)

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  }
}

function makeTab(overrides = {}) {
  return {
    id: 'tab-1',
    name: 'Test Tab',
    cwd: '/repo',
    env: 'default',
    status: 'active',
    session_kind: 'chat',
    created_at: '2026-11-25T10:00:00Z',
    is_unread: false,
    ...overrides,
  }
}

test('managedChatSurface identifies managed Chat tabs vs manual, subagent, and terminal tabs', () => {
  const manualChat = makeTab({ id: 'manual-1' })
  const hubSubagent = makeTab({
    id: 'sub-1',
    workspace_id: 'ws-1',
    parent_tab_id: 'manual-1',
    subagent_id: 'sa-1',
  })
  const managedTerminal = makeTab({
    id: 'term-1',
    session_kind: 'terminal',
    workspace_id: 'ws-1',
    managed_session_id: 'ws-agent-term',
    workspace_role: 'agent',
  })
  const managedChat = makeTab({
    id: 'managed-1',
    workspace_id: 'ws-1',
    managed_session_id: 'ws-agent-1',
    workspace_role: 'orchestrator',
  })
  const incompleteManagedChat = makeTab({
    id: 'managed-bad',
    workspace_role: 'orchestrator',
  })

  assert.equal(isManagedChatTab(manualChat), false)
  assert.equal(isManagedChatTab(hubSubagent), false)
  assert.equal(isManagedChatTab(managedTerminal), false)
  assert.equal(isManagedChatTab(managedChat), true)
  assert.equal(isManagedChatTab(incompleteManagedChat), true)

  assert.equal(resolveTabUnarchivePath('/api', 'manual-1', manualChat), '/api/tabs/manual-1/unarchive')
  assert.equal(resolveTabUnarchivePath('/api', 'sub-1', hubSubagent), '/api/tabs/sub-1/unarchive')
  assert.equal(resolveTabUnarchivePath('/api', 'term-1', managedTerminal), '/api/tabs/term-1/unarchive')
  assert.equal(
    resolveTabUnarchivePath('/api', 'managed-1', managedChat),
    '/api/workspaces/ws-1/sessions/ws-agent-1/unarchive',
  )
  assert.throws(
    () => resolveTabUnarchivePath('/api', 'managed-bad', incompleteManagedChat),
    /missing its Workspace session identity/,
  )

  assert.equal(resolveTabDeletePath('/api', 'manual-1', manualChat), '/api/tabs/manual-1')
  assert.equal(resolveTabDeletePath('/api', 'sub-1', hubSubagent), '/api/tabs/sub-1')
  assert.equal(
    resolveTabDeletePath('/api', 'managed-1', managedChat),
    '/api/workspaces/sessions/ws-agent-1',
  )
  assert.throws(
    () => resolveTabDeletePath('/api', 'managed-bad', incompleteManagedChat),
    /missing its managed session identity/,
  )
})

test('terminalStore separates archivedManualTabs from archivedManagedTabs', () => {
  storage.clear()
  setActivePinia(createPinia())
  const store = useTerminalStore()
  const manual = makeTab({ id: 'arch-manual', archived: true })
  const subagent = makeTab({
    id: 'arch-sub',
    archived: true,
    workspace_id: 'ws-1',
    parent_tab_id: 'parent-1',
  })
  const managed = makeTab({
    id: 'arch-managed',
    archived: true,
    workspace_id: 'ws-1',
    managed_session_id: 'ws-agent-1',
    workspace_role: 'orchestrator',
  })
  store.archivedTabs = [manual, subagent, managed]

  assert.deepEqual(
    store.archivedManualTabs.map(t => t.id),
    ['arch-manual', 'arch-sub'],
  )
  assert.deepEqual(
    store.archivedManagedTabs.map(t => t.id),
    ['arch-managed'],
  )
})

test('unarchiveTab routes archived managed Chats to the Workspace unarchive endpoint', async () => {
  storage.clear()
  setActivePinia(createPinia())
  const store = useTerminalStore()
  const managed = makeTab({
    id: 'tab-managed-1',
    archived: true,
    workspace_id: 'ws-123',
    managed_session_id: 'evo-agent-1',
    workspace_role: 'orchestrator',
  })
  store.archivedTabs = [managed]

  const requests = []
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || 'GET'
    requests.push({ url, method })
    if (url === '/api/workspaces/ws-123/sessions/evo-agent-1/unarchive' && method === 'POST') {
      return jsonResponse({ status: 'ok' })
    }
    if (url === '/api/tabs' && method === 'GET') {
      return jsonResponse([{ ...managed, archived: false }])
    }
    if (url === '/api/tabs/archived' && method === 'GET') {
      return jsonResponse([])
    }
    if (url === '/api/tabs/preferences' && method === 'GET') {
      return jsonResponse({ pinned_chat_ids: [] })
    }
    if (url === '/api/tabs/preferences' && method === 'PUT') {
      return jsonResponse({ pinned_chat_ids: [] })
    }
    throw new Error(`Unexpected fetch: ${method} ${url}`)
  }

  const ok = await store.unarchiveTab('tab-managed-1')
  // Wait one tick for background fetchArchivedTabs() refresh.
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(ok, true)
  assert.equal(requests[0].url, '/api/workspaces/ws-123/sessions/evo-agent-1/unarchive')
  assert.equal(requests[0].method, 'POST')
  assert.equal(store.archivedTabs.length, 0)
  assert.equal(store.tabs.length, 1)
})

test('unarchiveTab fails closed when a managed Chat lacks workspace identity', async () => {
  storage.clear()
  setActivePinia(createPinia())
  const store = useTerminalStore()
  store.archivedTabs = [
    makeTab({
      id: 'tab-broken-managed',
      archived: true,
      workspace_role: 'orchestrator',
    }),
  ]

  let fetchCalled = false
  globalThis.fetch = async () => {
    fetchCalled = true
    return jsonResponse({})
  }

  const ok = await store.unarchiveTab('tab-broken-managed')
  assert.equal(ok, false)
  assert.equal(fetchCalled, false, 'must not call /api/tabs/.../unarchive for incomplete managed tab')
  assert.ok(store.notifications.length > 0, 'should notify user of missing Workspace identity')
  assert.match(store.notifications[0].message, /missing its Workspace session identity/)
})

test('deleteTab routes managed Chats to Workspace session deletion and manual Chats to /api/tabs', async () => {
  storage.clear()
  setActivePinia(createPinia())
  const store = useTerminalStore()
  const managed = makeTab({
    id: 'tab-managed-del',
    archived: true,
    workspace_id: 'ws-9',
    managed_session_id: 'ws-sess-9',
    workspace_role: 'agent',
  })
  const manual = makeTab({
    id: 'tab-manual-del',
    archived: true,
  })
  store.archivedTabs = [managed, manual]

  const requests = []
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || 'GET'
    requests.push({ url, method })
    if (method === 'DELETE') {
      return jsonResponse({ status: 'deleted' })
    }
    throw new Error(`Unexpected fetch: ${method} ${url}`)
  }

  assert.equal(await store.deleteTab('tab-managed-del'), true)
  assert.equal(await store.deleteTab('tab-manual-del'), true)
  assert.deepEqual(requests, [
    { url: '/api/workspaces/sessions/ws-sess-9', method: 'DELETE' },
    { url: '/api/tabs/tab-manual-del', method: 'DELETE' },
  ])
})

test('unarchiveTab and deleteTab refresh archivedTabs on cache miss before resolving managed Chat routes', async () => {
  storage.clear()
  setActivePinia(createPinia())
  const store = useTerminalStore()
  // Neither store.tabs nor store.archivedTabs knows about this background-archived managed tab yet.
  store.tabs = []
  store.archivedTabs = []
  const remoteManaged = makeTab({
    id: 'tab-bg-managed',
    archived: true,
    workspace_id: 'ws-bg',
    managed_session_id: 'bg-agent-1',
    workspace_role: 'orchestrator',
  })

  const requests = []
  let restored = false
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || 'GET'
    requests.push({ url, method })
    if (url === '/api/tabs/archived' && method === 'GET') {
      return jsonResponse(restored ? [] : [remoteManaged])
    }
    if (url === '/api/workspaces/ws-bg/sessions/bg-agent-1/unarchive' && method === 'POST') {
      restored = true
      return jsonResponse({ status: 'ok' })
    }
    if (url === '/api/tabs' && method === 'GET') {
      return jsonResponse([{ ...remoteManaged, archived: false }])
    }
    if (url === '/api/tabs/preferences') {
      return jsonResponse({ pinned_chat_ids: [] })
    }
    if (url === '/api/workspaces/sessions/bg-agent-1' && method === 'DELETE') {
      return jsonResponse({ status: 'deleted' })
    }
    throw new Error(`Unexpected fetch: ${method} ${url}`)
  }

  assert.equal(await store.unarchiveTab('tab-bg-managed'), true)
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(requests[0].url, '/api/tabs/archived')
  assert.deepEqual(requests[1], {
    url: '/api/workspaces/ws-bg/sessions/bg-agent-1/unarchive',
    method: 'POST',
  })

  // Now simulate deleteTab cache miss for another archived managed Chat.
  store.tabs = []
  store.archivedTabs = []
  restored = false
  requests.length = 0
  assert.equal(await store.deleteTab('tab-bg-managed'), true)
  assert.deepEqual(requests, [
    { url: '/api/tabs/archived', method: 'GET' },
    { url: '/api/workspaces/sessions/bg-agent-1', method: 'DELETE' },
  ])
})
