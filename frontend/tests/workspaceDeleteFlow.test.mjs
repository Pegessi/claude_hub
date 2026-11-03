import assert from 'node:assert/strict'
import test from 'node:test'
import { createPinia, getActivePinia, setActivePinia } from 'pinia'
import { useWorkspaceStore } from './workspaceStoreTestHarness.mjs'

const NOW = '2026-11-03T00:00:00Z'

function workspace(id, name) {
  return {
    id,
    name,
    path: `/fixture/${id}`,
    default_branch: 'main',
    session_prefix: id,
    target: 'local',
    created_at: NOW,
    updated_at: NOW,
  }
}

function boardPayload(ws) {
  return {
    workspace: ws,
    tasks: [],
    sessions: [],
    reports: [],
    markdown_documents: [],
    snapshot_path: null,
    tasks_pagination: { total_count: 0, has_more: false, next_cursor: null, status_counts: {} },
  }
}

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null
    },
    setItem(key, value) {
      values.set(String(key), String(value))
    },
    removeItem(key) {
      values.delete(key)
    },
    clear() {
      values.clear()
    },
    key(index) {
      return [...values.keys()][index] ?? null
    },
    get length() {
      return values.size
    },
  }
}

function replaceGlobal(name, value) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, name)
  Object.defineProperty(globalThis, name, {
    configurable: true,
    writable: true,
    value,
  })
  return () => {
    if (previous) Object.defineProperty(globalThis, name, previous)
    else delete globalThis[name]
  }
}

async function withStore(fetchImpl, run, activeWorkspaceId = 'ws-a') {
  const storage = memoryStorage({
    claude_hub_active_workspace_id: activeWorkspaceId,
  })
  const restoreFetch = replaceGlobal('fetch', fetchImpl)
  const restoreStorage = replaceGlobal('localStorage', storage)
  const restoreWindow = replaceGlobal('window', { setTimeout })
  const previousPinia = getActivePinia()
  setActivePinia(createPinia())
  try {
    const store = useWorkspaceStore()
    store.workspaces = [workspace('ws-a', 'Workspace A'), workspace('ws-b', 'Workspace B')]
    return await run(store, storage)
  } finally {
    setActivePinia(previousPinia)
    restoreWindow()
    restoreStorage()
    restoreFetch()
  }
}

test('deleting the active workspace removes the row and falls back to the next workspace', async () => {
  const requests = []
  await withStore(
    async (url, options = {}) => {
      requests.push({ method: options.method ?? 'GET', url })
      if (options.method === 'DELETE') return new Response(null, { status: 204 })
      if (url === '/api/workspaces/ws-b/board' || url.startsWith('/api/workspaces/ws-b/board?')) {
        return jsonResponse(boardPayload(workspace('ws-b', 'Workspace B')))
      }
      if (url === '/api/workspaces/ws-b/lessons' || url.startsWith('/api/workspaces/ws-b/lessons?')) {
        return jsonResponse([])
      }
      throw new Error(`unexpected request ${options.method ?? 'GET'} ${url}`)
    },
    async (store, storage) => {
      assert.equal(store.activeWorkspaceId, 'ws-a')
      await assert.doesNotReject(store.deleteWorkspace('ws-a'))
      assert.deepEqual(store.workspaces.map(item => item.id), ['ws-b'])
      assert.equal(store.activeWorkspaceId, 'ws-b')
      assert.equal(storage.getItem('claude_hub_active_workspace_id'), 'ws-b')
      assert.equal(store.board.workspace.id, 'ws-b')
    },
  )
  assert.deepEqual(requests, [
    { method: 'DELETE', url: '/api/workspaces/ws-a' },
    { method: 'GET', url: '/api/workspaces/ws-b/board?tasks_limit=15' },
    { method: 'GET', url: '/api/workspaces/ws-b/lessons?limit=50' },
  ])
})

test('deleting a non-active workspace keeps the selection and does not reload the board', async () => {
  const requests = []
  await withStore(
    async (url, options = {}) => {
      requests.push({ method: options.method ?? 'GET', url })
      if (options.method === 'DELETE') return new Response(null, { status: 204 })
      throw new Error(`unexpected request ${options.method ?? 'GET'} ${url}`)
    },
    async (store) => {
      store.board = boardPayload(workspace('ws-a', 'Workspace A'))
      await assert.doesNotReject(store.deleteWorkspace('ws-b'))
      assert.deepEqual(store.workspaces.map(item => item.id), ['ws-a'])
      assert.equal(store.activeWorkspaceId, 'ws-a')
      assert.equal(store.board.workspace.id, 'ws-a')
    },
  )
  assert.deepEqual(requests, [{ method: 'DELETE', url: '/api/workspaces/ws-b' }])
})

test('deleting the last workspace clears the active selection, storage and board', async () => {
  await withStore(
    async (url, options = {}) => {
      if (options.method === 'DELETE') return new Response(null, { status: 204 })
      throw new Error(`unexpected request ${options.method ?? 'GET'} ${url}`)
    },
    async (store, storage) => {
      await assert.doesNotReject(store.deleteWorkspace('ws-b'))
      await assert.doesNotReject(store.deleteWorkspace('ws-a'))
      assert.deepEqual(store.workspaces, [])
      assert.equal(store.activeWorkspaceId, null)
      assert.equal(storage.getItem('claude_hub_active_workspace_id'), null)
      assert.equal(store.board, null)
    },
  )
})

test('a failed delete keeps the row, surfaces an error notification and rethrows', async () => {
  await withStore(
    async (url, options = {}) => {
      if (options.method === 'DELETE') {
        return jsonResponse({ detail: 'Workspace not found' }, 404)
      }
      throw new Error(`unexpected request ${options.method ?? 'GET'} ${url}`)
    },
    async (store) => {
      const before = store.workspaces.map(item => item.id)
      await assert.rejects(store.deleteWorkspace('ws-b'), /Workspace not found/)
      assert.deepEqual(store.workspaces.map(item => item.id), before)
      assert.equal(store.activeWorkspaceId, 'ws-a')
      assert.ok(
        store.notifications.some(
          notification => notification.type === 'error'
            && /Workspace not found/.test(notification.message)
        ),
        'expected the store error notification'
      )
    },
  )
})
