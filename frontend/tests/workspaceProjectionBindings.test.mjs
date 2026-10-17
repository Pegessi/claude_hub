import assert from 'node:assert/strict'
import test from 'node:test'
import { loadWorkspaceFeature, mountWorkspaceConsumer, vue } from './workspaceFeatureTestHarness.mjs'

const { useWorkspaceTaskPresentation } = await loadWorkspaceFeature('composables/useWorkspaceTaskPresentation.ts')
const { useWorkspaceEditor } = await loadWorkspaceFeature('composables/useWorkspaceEditor.ts')
const { useWorkspaceLessons } = await loadWorkspaceFeature('composables/useWorkspaceLessons.ts')
const contexts = await loadWorkspaceFeature('context.ts')
const { default: WorkspaceSummary } = await loadWorkspaceFeature('components/WorkspaceSummary.vue')
const epoch = Date.parse('2026-10-17T10:00:00Z')
const at = seconds => new Date(epoch + seconds * 1000).toISOString()

function harness(t) {
  const scope = vue.effectScope()
  t.after(() => scope.stop())
  const tasks = vue.ref([]), selectedTaskId = vue.ref(null), board = vue.ref(null), activeFeedbackLessons = vue.ref([])
  const workspaces = vue.ref([]), activeWorkspaceId = vue.ref(null), elapsedClockMs = vue.ref(epoch + 120_000)
  const selectedTask = vue.computed(() => tasks.value.find(task => task.id === selectedTaskId.value) || null)
  const activeWorkspace = vue.computed(() => workspaces.value.find(workspace => workspace.id === activeWorkspaceId.value) || null)
  const workspaceStore = vue.reactive({
    reports: [],
    reportsForTaskId(id) { return this.reports.filter(report => report.task_id === id) },
    reportsForTask(task) { return this.reportsForTaskId(task.id) },
    sessionForTask() { return null },
    latestReportForTask(task) { return this.reportsForTaskId(task.id).at(-1) || null },
    fetchFeedbackLessons: async () => undefined,
  })
  const inputs = {
    tasks, selectedTaskId, board, activeFeedbackLessons, workspaces, activeWorkspaceId, elapsedClockMs, workspaceStore,
    selectedTask, activeWorkspace, selectedWorkspaceId: vue.ref(''),
    runPending: (_key, callback) => callback(), taskActionKey: (action, id) => `task:${id}:${action}`,
    reviewerTitle: id => id || 'none', defaultPresetTextForAgent: () => '',
  }
  const presentation = scope.run(() => useWorkspaceTaskPresentation(inputs))
  const editor = scope.run(() => useWorkspaceEditor(inputs))
  const lessons = scope.run(() => useWorkspaceLessons(inputs))
  inputs.workspaceForm = editor.workspaceForm
  return { inputs, selectedTask, ...presentation, ...editor, ...lessons }
}

test('Workspace summary consumes reactive domain contexts and invokes the owning lesson action', async (t) => {
  const api = harness(t)
  let fetches = 0
  api.inputs.workspaceStore.fetchFeedbackLessons = async () => { fetches += 1 }
  const workspaceAgents = vue.ref([{ runtime_status: 'working' }])
  api.inputs.activeWorkspaceId.value = 'workspace'
  const mounted = mountWorkspaceConsumer(t, WorkspaceSummary, () => {
    contexts.provideWorkspaceRuntimeContext({
      error: vue.ref(null), dismissWorkspaceErrors: () => undefined,
      activeWorkspaceId: api.inputs.activeWorkspaceId, workspaceAgents,
      reviewerAgents: vue.ref([]), temporaryReviewers: vue.ref([]),
      tasksByStatus: status => api.inputs.tasks.value.filter(task => task.status === status),
      activeFeedbackLessons: api.inputs.activeFeedbackLessons, columns: [], taskCountByStatus: () => 0,
    })
    contexts.provideWorkspaceLessonsContext({ openLessonsModal: api.openLessonsModal })
  })
  const button = mounted.find(node => node.props?.class === 'summary-chip-button')
  assert.ok(button, 'the real consumer renders its lesson action')
  button.props.onClick()
  assert.equal(api.showLessonsModal.value, true)
  assert.equal(fetches, 1)
  api.inputs.activeFeedbackLessons.value = [{ id: 'lesson' }]
  workspaceAgents.value.push({ runtime_status: 'working' })
  await vue.nextTick()
  assert.equal(button.text.trim(), '1 lessons')
  assert.equal(mounted.find(node => node.tag === 'strong').text, '2 working')
})

test('Workspace report projections follow task selection, report updates, language, and clock', async (t) => {
  const api = await harness(t)
  const { inputs } = api
  inputs.tasks.value = [
    { id: 'first', title: 'first', status: 'working', created_at: at(0), started_at: at(30) },
    { id: 'second', title: 'second', status: 'working', created_at: at(60) },
  ]
  inputs.selectedTaskId.value = 'first'
  inputs.workspaceStore.reports = [
    { id: 'one', task_id: 'first', state: 'completed', message: 'legacy', message_en: 'English', message_zh: '中文', validation: 'tests', created_at: at(60) },
    { id: 'two', task_id: 'second', state: 'working', message: 'second', created_at: at(90) },
  ]
  assert.equal(api.latestSelectedReportAgeLabel.value, '1m ago')
  assert.equal(api.isLatestSelectedReport(api.selectedReports.value[0]), true)
  assert.equal(api.hasBilingualReport.value, true)
  assert.equal(api.reportMessageForLang(api.selectedReports.value[0]), 'English')
  api.reportLang.value = 'zh'
  assert.equal(api.reportMessagePreview(api.selectedReports.value[0]), '中文')
  assert.equal(api.taskTotalElapsedLabel(api.selectedTask.value), '2m')
  assert.equal(api.taskWorkingElapsedLabel(api.selectedTask.value), '1m')
  inputs.elapsedClockMs.value += 60_000
  assert.equal(api.latestSelectedReportAgeLabel.value, '2m ago')
  assert.equal(api.selectedProgressTimeline.value.at(-1).timestampMs, epoch + 180_000)
  inputs.selectedTaskId.value = 'second'
  assert.equal(api.isLatestSelectedReport(api.selectedReports.value[0]), true)
  assert.equal(api.hasBilingualReport.value, false)
  assert.equal(api.reportElapsedLabel(api.selectedReports.value[0], 0), 'creation +30s')
  inputs.workspaceStore.reports.push({ id: 'three', task_id: 'second', state: 'working', message: 'new', created_at: at(150) })
  assert.equal(api.isLatestSelectedReport(api.selectedReports.value[1]), true)
  assert.equal(api.reportElapsedTitle(api.selectedReports.value[1], 1), 'Elapsed since previous report: 1m')
  inputs.selectedTaskId.value = null
  assert.deepEqual(api.selectedProgressTimeline.value, [])
  assert.equal(api.latestSelectedReportAgeLabel.value, 'none')
})

test('Workspace feedback and Markdown projections react to current store arrays', async (t) => {
  const api = await harness(t)
  const { inputs } = api
  inputs.tasks.value = [{ id: 'task', title: 'Alpha', prompt: '' }]
  inputs.selectedTaskId.value = 'task'
  assert.equal(api.feedbackLessonMatchSummary.value, 'No active lessons indexed')
  inputs.activeFeedbackLessons.value = [{ id: 'lesson', summary: 'alpha' }]
  assert.equal(api.feedbackLessonMatchSummary.value, '1 current tasks match active lessons')
  inputs.tasks.value[0].title = 'Beta'
  assert.equal(api.feedbackLessonMatchSummary.value, 'No current task matches')
  inputs.board.value = { markdown_documents: [
    { id: 'direct', task_id: 'task', path: 'spec.md' },
    { id: 'report-doc', report_id: 'report', path: 'result.md' },
  ] }
  assert.deepEqual(api.selectedMarkdownDocuments.value.map(doc => doc.id), ['direct'])
  inputs.workspaceStore.reports.push({ id: 'report', task_id: 'task' })
  assert.deepEqual(api.selectedMarkdownDocuments.value.map(doc => doc.id), ['direct', 'report-doc'])
})

test('Workspace resident save actions parse the current draft env and forward lifecycle overrides', async (t) => {
  const api = harness(t)
  const { workspaceForm, workspaceStore } = api.inputs
  api.inputs.workspaces.value = [{
    id: 'workspace', name: 'Test', path: '/workspace', default_branch: 'main', session_prefix: 'test',
    target: 'local', remote_reconnect: true, resident_agent_enabled: false,
  }]
  api.inputs.activeWorkspaceId.value = 'workspace'
  api.openEditWorkspaceModal()
  let payload
  workspaceStore.updateWorkspace = async (_id, next) => { payload = next; return { id: 'workspace' } }
  workspaceForm.resident_env_text = '# ignored\nexport MODEL="model=one"\nLABEL=\'resident worker\''
  workspaceForm.resident_agent_title = '  draft title  '
  workspaceForm.resident_agent_periodic_tasks.push({ id: 'one', text: ' check ', enabled: true })
  await api.handleSubmitWorkspace()
  assert.deepEqual(payload.resident_agent_env, { MODEL: 'model=one', LABEL: 'resident worker' })
  assert.equal(payload.resident_agent_title, 'draft title')
  assert.deepEqual(payload.resident_agent_periodic_tasks, [{ id: 'one', text: 'check', enabled: true }])
  assert.equal(workspaceForm.resident_agent_periodic_tasks[0].text, ' check ')

  workspaceForm.resident_env_text = 'MODEL=model-two'
  await api.handleSubmitWorkspace()
  assert.deepEqual(payload.resident_agent_env, { MODEL: 'model-two' })
  workspaceForm.resident_env_text = '# no custom env\ninvalid line'
  await api.handleSubmitWorkspace()
  assert.deepEqual(payload.resident_agent_env, {})

  assert.equal(workspaceForm.resident_agent_enabled, false)
  await api.handleCreateResident()
  assert.equal(payload.resident_agent_enabled, true)
  assert.equal(workspaceForm.resident_agent_enabled, true)
  assert.equal(workspaceForm.resident_agent_title, '  draft title  ')
})

test('Workspace resident projections react to workspace switches, draft edits, and clock ticks', async (t) => {
  const api = await harness(t)
  const { inputs } = api
  inputs.workspaces.value = [{
    id: 'workspace', resident_agent_enabled: true, resident_agent_session_id: 'resident',
    resident_agent_next_run_at: at(180), resident_agent_last_run_at: at(60),
  }]
  inputs.activeWorkspaceId.value = 'workspace'
  assert.equal(api.residentLastRunLabel.value, '1m ago')
  assert.equal(api.residentNextRunLabel.value, 'in 1m')
  inputs.elapsedClockMs.value += 60_000
  assert.equal(api.residentNextRunLabel.value, 'due now')
  inputs.workspaces.value[0].resident_agent_run_requested_at = at(180)
  assert.equal(api.residentRunPending.value, true)
  assert.equal(api.residentNextRunLabel.value, 'queued')
  inputs.workspaceForm.resident_agent_periodic_tasks.push({ id: 'draft', enabled: true, text: '' })
  assert.equal(api.enabledPeriodicTaskCount.value, 0)
  inputs.workspaceForm.resident_agent_periodic_tasks[0].text = 'check'
  assert.equal(api.enabledPeriodicTaskCount.value, 1)
  inputs.workspaceStore.reports.push({ id: 'report', session_id: 'resident', created_at: at(150) })
  assert.equal(api.latestResidentReport.value.id, 'report')
  inputs.activeWorkspaceId.value = 'other'
  assert.equal(api.latestResidentReport.value, null)
  assert.equal(api.residentNextRunLabel.value, '')
})
