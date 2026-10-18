import assert from 'node:assert/strict'
import test from 'node:test'
import { loadWorkspaceFeature, mountWorkspaceConsumer, vue } from './workspaceFeatureTestHarness.mjs'

const { useWorkspaceTaskActions } = await loadWorkspaceFeature('composables/useWorkspaceTaskActions.ts')
const { useWorkspaceArtifacts } = await loadWorkspaceFeature('composables/useWorkspaceArtifacts.ts')
const { useWorkspaceLifecycle } = await loadWorkspaceFeature('composables/useWorkspaceLifecycle.ts')

function setGlobal(t, name, value) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, name)
  Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
  t.after(() => {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor)
    else delete globalThis[name]
  })
}

function taskOwner(t) {
  const scope = vue.effectScope()
  t.after(() => scope.stop())
  const selectedTaskId = vue.ref('one')
  const tasks = vue.ref([{ id: 'one', workspace_id: 'workspace' }, { id: 'two', workspace_id: 'workspace' }])
  const selectedTask = vue.computed(() => tasks.value.find(task => task.id === selectedTaskId.value) || null)
  const runtime = {
    selectedTask, selectedTaskId, activeWorkspaceId: vue.ref('workspace'),
    runPending: (_key, callback) => callback(), taskActionKey: (action, id) => `task:${id}:${action}`,
    workspaceStore: {}, terminalStore: {},
  }
  const artifacts = scope.run(() => useWorkspaceArtifacts(runtime))
  const actions = scope.run(() => useWorkspaceTaskActions({
    ...runtime, expandedArtifactKey: artifacts.expandedArtifactKey,
    closeMarkdownPreviewModal: artifacts.closeMarkdownPreviewModal,
    canEditTask: () => true, canUseManagedTaskActions: () => true, selectedSession: vue.ref({ id: 'session' }),
  }))
  return { runtime, actions, artifacts }
}

test('task selection and close use the single attachment and artifact owners', (t) => {
  setGlobal(t, 'HTMLElement', class HTMLElement {})
  const revoked = []
  t.mock.method(URL, 'revokeObjectURL', value => revoked.push(value))
  const { runtime, actions, artifacts } = taskOwner(t)
  actions.detailMessage.value = 'draft'
  actions.detailAttachments.value.push({ id: 'image', preview_url: 'blob:one' })
  artifacts.expandedArtifactKey.value = 'artifact'
  actions.selectTask({ target: null }, 'one')
  assert.equal(actions.detailMessage.value, 'draft')
  assert.equal(actions.detailAttachments.value.length, 1)
  actions.selectTask({ target: null }, 'two')
  assert.equal(runtime.selectedTaskId.value, 'two')
  assert.equal(actions.detailMessage.value, '')
  assert.deepEqual(actions.detailAttachments.value, [])
  assert.deepEqual(revoked, ['blob:one'])
  assert.equal(artifacts.expandedArtifactKey.value, null)
  artifacts.markdownPreviewModalPath.value = 'report.md'
  actions.closeTaskDetail()
  assert.equal(runtime.selectedTaskId.value, null)
  assert.equal(artifacts.markdownPreviewModalPath.value, null)
})

test('detail submission uses the selected identity and serializes only owned draft attachments', async (t) => {
  const { runtime, actions } = taskOwner(t)
  let request
  runtime.workspaceStore.sendMessage = async (...args) => { request = args }
  actions.detailMessage.value = '  message  '
  actions.detailAttachments.value.push({ id: 'image', filename: 'image.png', mime_type: 'image/png', data_url: 'data:image/png;base64,eA==', preview_url: 'blob:image' })
  await actions.sendDetailMessage()
  assert.deepEqual(request, ['session', 'message', [{ filename: 'image.png', mime_type: 'image/png', data_url: 'data:image/png;base64,eA==' }]])
  assert.equal(actions.detailMessage.value, '')
  assert.deepEqual(actions.detailAttachments.value, [])
})

test('workspace mounts one polling lifecycle and releases timers listeners and drafts once', async (t) => {
  const events = [], timers = new Map(), calls = []
  let nextTimer = 1
  setGlobal(t, 'document', {
    addEventListener: (...args) => events.push(['add', ...args]),
    removeEventListener: (...args) => events.push(['remove', ...args]),
  })
  setGlobal(t, 'window', {
    setInterval(callback, delay) { const id = nextTimer++; timers.set(id, { callback, delay }); return id },
    clearInterval(id) { timers.delete(id) },
  })
  const selectedTaskId = vue.ref(null), activeWorkspaceId = vue.ref('one')
  const detailAttachments = vue.ref([{ id: 'image', preview_url: 'blob:image' }])
  const inputs = {
    tasks: vue.ref([]), selectedTaskId, activeWorkspaceId, selectedWorkspaceId: vue.ref('one'),
    workspaceSessionView: vue.ref('reviewers'), elapsedClockMs: vue.ref(0), detailAttachments,
    workspaceStore: {
      fetchTaskCapabilities: async id => calls.push(['capabilities', id]),
      fetchWorkspaces: async () => calls.push(['workspaces']),
      clearTaskReports: id => calls.push(['clear', id]),
      fetchTaskReports: async (...args) => calls.push(['reports', ...args]),
    },
    taskCreateFlow: { onWorkspaceChanged: id => calls.push(['workspace', id]), dispose: () => calls.push(['dispose']) },
    terminalStore: { startAgentStatusPolling: () => calls.push(['start']), stopAgentStatusPolling: () => calls.push(['stop']) },
    refreshBoard: () => calls.push(['refresh']), fetchRemoteProfiles: async () => calls.push(['profiles']),
    closeTaskDetail: () => { selectedTaskId.value = null; calls.push(['close']) },
    handleWorkspaceDocumentPointerDown: () => undefined, handleLightboxKeydown: () => undefined,
  }
  const Consumer = { setup() { useWorkspaceLifecycle(inputs); return () => null } }
  const mounted = mountWorkspaceConsumer(t, Consumer, () => undefined)
  await Promise.resolve()
  await Promise.resolve()
  assert.deepEqual([...timers.values()].map(timer => timer.delay), [2500, 30000])
  assert.deepEqual(calls.slice(0, 4), [['capabilities', 'one'], ['profiles'], ['workspaces'], ['start']])
  selectedTaskId.value = 'task'
  await vue.nextTick()
  assert.ok(calls.some(call => call[0] === 'reports' && call[1] === 'one' && call[2] === 'task'))
  activeWorkspaceId.value = 'two'
  await vue.nextTick()
  assert.equal(inputs.selectedWorkspaceId.value, 'two')
  assert.equal(inputs.workspaceSessionView.value, 'agents')
  assert.equal(selectedTaskId.value, null)
  mounted.unmount()
  assert.equal(timers.size, 0)
  assert.deepEqual(detailAttachments.value, [])
  assert.equal(calls.filter(call => call[0] === 'dispose').length, 1)
  assert.equal(calls.filter(call => call[0] === 'stop').length, 1)
  assert.deepEqual(events.filter(event => event[0] === 'add').map(event => event.slice(1)), events.filter(event => event[0] === 'remove').map(event => event.slice(1)))
})
