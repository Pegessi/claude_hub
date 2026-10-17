import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'
import ts from 'typescript'
import { workspaceProjectionModuleUrl } from './workspaceProjectionTestHarness.mjs'

const require = createRequire(import.meta.url)
const { parse } = require('vue/compiler-sfc')
const source = await readFile(new URL('../src/components/AgentWorkspaceView.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const script = ts.createSourceFile('AgentWorkspaceView.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const bindings = new Set([
  'activeWorkspace', 'selectedTask', 'selectedReports', 'selectedMarkdownDocuments',
  'feedbackLessonMatchSummary', 'latestSelectedReportAgeLabel', 'selectedProgressTimeline',
  'reportMessagePreview', 'primaryExpandedReportId', 'isLatestSelectedReport',
  'taskTotalElapsedLabel', 'taskWorkingElapsedLabel', 'reportElapsedLabel', 'reportElapsedTitle',
  'REPORT_LANG_STORAGE_KEY', 'reportLang', 'reportMessageForLang', 'hasBilingualReport',
  'enabledPeriodicTaskCount', 'residentLastRunLabel', 'residentRunPending', 'residentNextRunLabel', 'latestResidentReport',
  'buildResidentPayload',
])
const selectedNames = new Set()
const statements = []
for (const statement of script.statements) {
  if (ts.isImportDeclaration(statement) && statement.moduleSpecifier.text.startsWith('@/utils/workspace')) {
    const name = statement.moduleSpecifier.text.split('/').at(-1)
    statements.push(statement.getText(script).replace(statement.moduleSpecifier.text, await workspaceProjectionModuleUrl(name)))
    continue
  }
  const name = ts.isFunctionDeclaration(statement)
    ? statement.name.text
    : ts.isVariableStatement(statement) ? statement.declarationList.declarations[0].name.getText(script) : null
  if (bindings.has(name)) {
    selectedNames.add(name)
    statements.push(statement.getText(script))
  }
}
assert.deepEqual([...selectedNames].sort(), [...bindings].sort(), 'exercise every intended SFC projection binding')

// Load the real env parser without starting the preset composable's storage or fetch lifecycle.
const envSource = await readFile(new URL('../src/composables/useLaunchEnvPresets.ts', import.meta.url), 'utf8')
const envScript = ts.createSourceFile('useLaunchEnvPresets.ts', envSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const envHelpers = envScript.statements.filter(statement =>
  ts.isFunctionDeclaration(statement) && ['stripShellQuotes', 'parseLaunchEnv'].includes(statement.name.text)
)
assert.equal(envHelpers.length, 2)

// Execute the actual SFC projection declarations with Vue refs. Stores and the
// component clock remain external inputs; fetching and lifecycle stay out of this seam.
const runtime = `
import { computed, reactive, ref } from '${import.meta.resolve('vue')}'
const localStorage = undefined
const tasks = ref([]), selectedTaskId = ref(null), board = ref(null), activeFeedbackLessons = ref([])
const workspaces = ref([]), activeWorkspaceId = ref(null), elapsedClockMs = ref(0)
const workspaceForm = reactive({
  resident_agent_enabled: true, resident_agent_paused: false, resident_agent_master_mode: false,
  resident_agent_interval_minutes: 60, resident_agent_directive: '', resident_agent_periodic_tasks: [],
  resident_agent_type: 'claude', resident_agent_solo_mode: true, resident_agent_title: '',
  resident_agent_target: 'local', resident_agent_remote_profile_id: '', resident_agent_cwd: '',
  resident_agent_remote_reconnect: true, resident_env_text: '',
})
const workspaceStore = reactive({
  reports: [],
  reportsForTaskId(id) { return this.reports.filter(report => report.task_id === id) },
})
${envHelpers.map(statement => statement.getText(envScript)).join('\n')}
${statements.join('\n')}
export const inputs = { tasks, selectedTaskId, board, activeFeedbackLessons, workspaces, activeWorkspaceId, elapsedClockMs, workspaceForm, workspaceStore }
export { ${[...bindings].filter(name => name !== 'REPORT_LANG_STORAGE_KEY').join(', ')} }
`
const { outputText } = ts.transpileModule(runtime, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
})
const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
const epoch = Date.parse('2026-10-17T10:00:00Z')
const at = seconds => new Date(epoch + seconds * 1000).toISOString()

async function harness(name) {
  const api = await import(`${url}#${name}`)
  api.inputs.elapsedClockMs.value = epoch + 120_000
  return api
}

test('SFC report projections follow task selection, report updates, language, and clock', async () => {
  const api = await harness('reports')
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
  assert.equal(api.primaryExpandedReportId(), 'one')
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
  assert.equal(api.primaryExpandedReportId(), 'two')
  assert.equal(api.hasBilingualReport.value, false)
  assert.equal(api.reportElapsedLabel(api.selectedReports.value[0], 0), 'creation +30s')
  inputs.workspaceStore.reports.push({ id: 'three', task_id: 'second', state: 'working', message: 'new', created_at: at(150) })
  assert.equal(api.primaryExpandedReportId(), 'three')
  assert.equal(api.reportElapsedTitle(api.selectedReports.value[1], 1), 'Elapsed since previous report: 1m')
  inputs.selectedTaskId.value = null
  assert.deepEqual(api.selectedProgressTimeline.value, [])
  assert.equal(api.latestSelectedReportAgeLabel.value, 'none')
})

test('SFC feedback and Markdown projections react to current store arrays', async () => {
  const api = await harness('feedback-markdown')
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

test('SFC resident payload wrapper parses the current draft env and forwards overrides', async () => {
  const api = await harness('resident-payload')
  const { workspaceForm } = api.inputs
  workspaceForm.resident_env_text = '# ignored\nexport MODEL="model=one"\nLABEL=\'resident worker\''
  workspaceForm.resident_agent_title = '  draft title  '
  workspaceForm.resident_agent_periodic_tasks.push({ id: 'one', text: ' check ', enabled: true })
  const payload = api.buildResidentPayload()
  assert.deepEqual(payload.resident_agent_env, { MODEL: 'model=one', LABEL: 'resident worker' })
  assert.equal(payload.resident_agent_title, 'draft title')
  assert.deepEqual(payload.resident_agent_periodic_tasks, [{ id: 'one', text: 'check', enabled: true }])
  assert.equal(workspaceForm.resident_agent_periodic_tasks[0].text, ' check ')

  workspaceForm.resident_env_text = 'MODEL=model-two'
  assert.deepEqual(api.buildResidentPayload().resident_agent_env, { MODEL: 'model-two' })
  workspaceForm.resident_env_text = '# no custom env\ninvalid line'
  assert.deepEqual(api.buildResidentPayload().resident_agent_env, {})

  const overrides = { resident_agent_enabled: false, resident_agent_title: 'override', resident_agent_env: { EXTRA: 'yes' } }
  const overridden = api.buildResidentPayload(overrides)
  assert.equal(overridden.resident_agent_enabled, false)
  assert.equal(overridden.resident_agent_title, 'override')
  assert.equal(overridden.resident_agent_env, overrides.resident_agent_env)
  assert.equal(workspaceForm.resident_agent_enabled, true)
  assert.equal(workspaceForm.resident_agent_title, '  draft title  ')
})

test('SFC resident projections react to workspace switches, draft edits, and clock ticks', async () => {
  const api = await harness('resident')
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
