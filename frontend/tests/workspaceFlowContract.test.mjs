import assert from 'node:assert/strict'
import test from 'node:test'
import { loadWorkspaceFeature, vue } from './workspaceFeatureTestHarness.mjs'

const { useWorkspaceTaskActions } = await loadWorkspaceFeature('composables/useWorkspaceTaskActions.ts')

function ownScope(t, setup) {
  const scope = vue.effectScope()
  t.after(() => scope.stop())
  return scope.run(setup)
}

function taskActions(t, updateTask) {
  return ownScope(t, () => useWorkspaceTaskActions({
    selectedTaskId: vue.ref(null), selectedTask: vue.ref(null),
    runPending: (_key, action) => action(), taskActionKey: (action, id) => `${action}:${id}`,
    workspaceStore: { updateTask }, terminalStore: {},
    expandedArtifactKey: vue.ref(null), closeMarkdownPreviewModal: () => {},
    canEditTask: () => true, canUseManagedTaskActions: () => true, selectedSession: vue.ref(null),
  }))
}

test('task edits round-trip optional contract fields and normalize newline acceptance items', async t => {
  const calls = []
  const actions = taskActions(t, async (...args) => calls.push(args))
  actions.openEditTaskModal({
    id: 'contract', title: 'Task', prompt: 'Implement',
    goal: 'Ship the feature', done_when: ['Test passes', 'UI works'], out_of_scope: ['Deployment'],
  })
  assert.equal(actions.editTaskForm.goal, 'Ship the feature')
  assert.equal(actions.editTaskForm.done_when, 'Test passes\nUI works')
  assert.equal(actions.editTaskForm.out_of_scope, 'Deployment')
  actions.editTaskForm.goal = '  Revised goal  '
  actions.editTaskForm.done_when = '  Build passes  \r\n\r\n  Regression passes \n'
  actions.editTaskForm.out_of_scope = '  No deployment  \n'
  await actions.handleUpdateTask()
  assert.equal(calls[0][0], 'contract')
  assert.equal(calls[0][1].goal, 'Revised goal')
  assert.deepEqual(calls[0][1].done_when, ['Build passes', 'Regression passes'])
  assert.deepEqual(calls[0][1].out_of_scope, ['No deployment'])
  assert.equal('session_id' in calls[0][1], false)
  assert.equal('clear_context' in calls[0][1], false)
  assert.equal('goal_packet' in calls[0][1], false)
  assert.equal(actions.showEditTaskModal.value, false)
  assert.equal(actions.editTaskForm.goal, '')
  assert.equal(actions.editTaskForm.done_when, '')
  assert.equal(actions.editTaskForm.out_of_scope, '')
})

test('legacy tasks open with empty optional fields and failed saves retain the draft', async t => {
  const actions = taskActions(t, async () => { throw new Error('save failed') })
  actions.openEditTaskModal({ id: 'legacy', title: 'Legacy', prompt: 'Original prompt' })
  assert.equal(actions.editTaskForm.goal, '')
  assert.equal(actions.editTaskForm.done_when, '')
  assert.equal(actions.editTaskForm.out_of_scope, '')
  actions.editTaskForm.done_when = 'Must survive a failed save'
  await assert.rejects(actions.handleUpdateTask(), /save failed/)
  assert.equal(actions.editingTaskId.value, 'legacy')
  assert.equal(actions.showEditTaskModal.value, true)
  assert.equal(actions.editTaskForm.done_when, 'Must survive a failed save')
})

test('legacy goal_packet tasks prefill contract fields and title-only edits omit contract mutations', async t => {
  const calls = []
  const actions = taskActions(t, async (...args) => calls.push(args))
  actions.openEditTaskModal({
    id: 'legacy-gp',
    title: 'Old title',
    prompt: 'Keep packet',
    goal_packet: {
      status: 'approved',
      objective: 'Legacy packet objective',
      acceptance_criteria: ['Criterion A'],
      out_of_scope: ['Out B'],
      validation_plan: ['pytest'],
    },
  })
  assert.equal(actions.editTaskForm.goal, 'Legacy packet objective')
  assert.equal(actions.editTaskForm.done_when, 'Criterion A')
  assert.equal(actions.editTaskForm.out_of_scope, 'Out B')
  actions.editTaskForm.title = 'Renamed title'
  await actions.handleUpdateTask()
  assert.equal(calls[0][1].title, 'Renamed title')
  assert.equal('goal' in calls[0][1], false)
  assert.equal('done_when' in calls[0][1], false)
  assert.equal('out_of_scope' in calls[0][1], false)
})

test('Start dispatches the task without a resident seat or context-reset override', async t => {
  const calls = []
  const refreshed = []
  const actions = ownScope(t, () => useWorkspaceTaskActions({
    selectedTaskId: vue.ref(null), selectedTask: vue.ref(null),
    runPending: (_key, action) => action(), taskActionKey: (action, id) => `${action}:${id}`,
    workspaceStore: { startTask: async (...args) => calls.push(args) },
    terminalStore: { fetchTabs: async args => refreshed.push(args) },
    expandedArtifactKey: vue.ref(null), closeMarkdownPreviewModal: () => {},
    canEditTask: () => true, canUseManagedTaskActions: () => true, selectedSession: vue.ref(null),
  }))
  await actions.startTask({ id: 'isolated', session_id: 'old-seat', clear_context: true })
  assert.deepEqual(calls, [['isolated']])
  assert.deepEqual(refreshed, [{ force: true }])
  assert.equal('startOptionsFor' in actions, false)
})

test('Related task can be cleared with the update API empty-string sentinel', async t => {
  const calls = []
  const actions = taskActions(t, async (...args) => calls.push(args))
  actions.openEditTaskModal({ id: 'related', title: 'Task', prompt: 'Continue', related_task_id: 'previous' })
  assert.equal(actions.editTaskForm.related_task_id, 'previous')
  actions.editTaskForm.related_task_id = ''
  await actions.handleUpdateTask()
  assert.equal(calls[0][1].related_task_id, '')
})
