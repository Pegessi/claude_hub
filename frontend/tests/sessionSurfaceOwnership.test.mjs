import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const readSource = relativePath => readFileSync(
  new URL(relativePath, import.meta.url),
  'utf8',
)

const sessionCreator = readSource('../src/features/terminal/components/SessionCreator.vue')
const workspaceAgentDialog = readSource('../src/features/workspace/components/WorkspaceAgentDialog.vue')
const workspaceAgentManager = readSource('../src/features/workspace/composables/useWorkspaceAgentManager.ts')
const workspaceRuntime = readSource('../src/features/workspace/composables/useWorkspaceRuntime.ts')
const terminalPane = readSource('../src/components/TerminalPane.vue')
const structuredPane = readSource('../src/features/chat/StructuredPane.vue')
const chatComposer = readSource('../src/features/chat/components/ChatComposer.vue')
const chatComposerController = readSource('../src/features/chat/controllers/useChatComposer.ts')
const chatDelivery = readSource('../src/features/chat/controllers/useChatDelivery.ts')
const chatImages = readSource('../src/features/chat/controllers/useChatImages.ts')
const taskDialog = readSource('../src/components/TaskCreateDialog.vue')
const agentConfigFields = readSource('../src/components/AgentConfigFields.vue')
const residentAgentDialog = readSource('../src/features/workspace/components/ResidentAgentDialog.vue')
const scheduledTasksPanel = readSource('../src/features/scheduled-tasks/ScheduledTasksPanel.vue')
const agentAvatar = readSource('../src/components/AgentAvatar.vue')

test('Antigravity is opt-in for local Chat surfaces and scheduled tasks, not Resident or Terminal', () => {
  assert.match(agentConfigFields, /v-if="allowAntigravity && !excludeTypes\.includes\('antigravity'\)"/)
  assert.match(agentConfigFields, /allowAntigravity: false/)
  assert.match(sessionCreator, /:allow-antigravity="form\.session_kind === 'chat' && form\.target === 'local'"/)
  assert.match(sessionCreator, /kind === 'terminal' && \['maso', 'antigravity'\]\.includes\(form\.agent_type\)/)
  assert.match(workspaceAgentDialog, /:allow-antigravity="agentOptionsForm\.session_kind === 'chat' && agentOptionsForm\.target === 'local'"/)
  assert.match(scheduledTasksPanel, /<option value="antigravity">\s*antigravity\s*<\/option>/)
  assert.doesNotMatch(residentAgentDialog, /allow-antigravity|value="antigravity"/)
  assert.match(chatComposer, /:show-schedule="!isManagedChat"/)
})

test('Antigravity solo is an explicit choice and its identity is not rendered as Terminal', () => {
  assert.match(agentConfigFields, /'antigravity'\]\s*\.includes\(agentType\)\) \{\s*emit\('update:soloMode', false\)/)
  assert.match(agentConfigFields, /Runs Antigravity with --dangerously-skip-permissions/)
  assert.match(sessionCreator, /\['claude', 'codex', 'traex', 'antigravity'\]\.includes\(form\.agent_type\)/)
  assert.match(agentAvatar, /v-else-if="kind === 'antigravity'"/)
  assert.match(agentAvatar, /raw === 'antigravity'/)
})

test('top-level and Workspace launchers both expose an explicit execution surface', () => {
  assert.match(sessionCreator, /@click="setSessionKind\('chat'\)"/)
  assert.match(sessionCreator, /@click="setSessionKind\('terminal'\)"/)
  assert.match(sessionCreator, /session_kind:\s*form\.session_kind/)

  assert.match(workspaceAgentDialog, /<label>Execution surface<\/label>/)
  assert.match(workspaceAgentDialog, /handleAgentSurfaceChange\('chat'\)/)
  assert.match(workspaceAgentManager, /session_kind:\s*agentOptionsForm\.session_kind/)
  assert.match(taskDialog, /form\.session_kind === 'chat'/)
  assert.match(taskDialog, /workspaceAgents\.filter\(agent => agent\.session_kind === form\.session_kind\)/)
})

test('Agent Workspace opens the existing managed tab without creating a second transport', () => {
  const openSession = workspaceRuntime.match(
    /async function openSession\(session: ManagedSession\)\s*\{[\s\S]*?\n\}/,
  )
  assert.ok(openSession, 'AgentWorkspaceView must expose the managed-session open action')
  assert.match(openSession[0], /appStore\.setMode\('terminal'\)/)
  assert.match(openSession[0], /terminalStore\.setActiveTab\(session\.tab_id\)/)
  assert.doesNotMatch(openSession[0], /createTab/)
})

test('managed Chat mounts StructuredPane with managed session identity', () => {
  assert.match(terminalPane, /paneTab\.value\?\.session_kind === 'chat'/)
  assert.match(terminalPane, /:managed-session-id="isManagedTab \? paneTab\?\.managed_session_id \|\| undefined : undefined"/)
  assert.doesNotMatch(terminalPane, /paneTab\?\.agent_session_id|tab\.agent_session_id/)
  assert.match(terminalPane, /v-if="pane\.tabId && !isChatSession"[\s\S]*?<TerminalView/)
  assert.match(terminalPane, /v-if="pane\.tabId && isChatSession"[\s\S]*?<StructuredPane/)
  assert.match(structuredPane, /workspaces\/sessions\/\$\{encodeURIComponent\(streamSourceId\.value\)\}\/stream/)
  assert.match(chatDelivery, /const base = `\$\{deps\.streamBasePath\.value\}\/send`/)
  assert.match(chatDelivery, /`\$\{deps\.streamBasePath\.value\}\/cancel`/)
  assert.match(chatImages, /`\$\{deps\.streamBasePath\.value\}\/attachments\/\$\{encId\}`/)
  assert.match(structuredPane, /Managed Chat identity is not available yet\. Refresh Workspace sessions and try again\./)
  assert.match(chatComposer, /v-else-if="isManagedChat"[\s\S]*Enter \/ ⌘\/Ctrl\+Enter send[\s\S]*Busy turn follow-ups are durable[\s\S]*Shift\+Enter newline/)
  assert.match(chatComposerController, /if \(deps\.isManagedChat\.value\) \{[\s\S]*deps\.submit\('normal'\)[\s\S]*return[\s\S]*\}[\s\S]*if \(action === 'queue'\)/)
  assert.match(chatComposerController, /deps\.submit\(action === 'steer' \? 'steer' : 'normal'\)/)
})

test('Workspace launcher exposes MASO only for local Chat', () => {
  assert.match(workspaceAgentDialog, /:allow-maso="agentOptionsForm\.session_kind === 'chat'"/)
  assert.match(workspaceAgentDialog, /:allow-terminal="agentOptionsForm\.session_kind === 'terminal'"/)
  assert.match(workspaceAgentDialog, /:disabled="agentOptionsForm\.target === 'remote'"/)
  assert.match(workspaceAgentManager, /if \(target === 'remote'\) handleAgentSurfaceChange\('terminal'\)/)
})
