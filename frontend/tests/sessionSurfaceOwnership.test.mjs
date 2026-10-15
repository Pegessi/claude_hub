import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const readSource = relativePath => readFileSync(
  new URL(relativePath, import.meta.url),
  'utf8',
)

const tabBar = readSource('../src/components/TabBar.vue')
const workspaceView = readSource('../src/components/AgentWorkspaceView.vue')
const terminalPane = readSource('../src/components/TerminalPane.vue')
const structuredPane = readSource('../src/components/StructuredPane.vue')
const taskDialog = readSource('../src/components/TaskCreateDialog.vue')

test('top-level and Workspace launchers both expose an explicit execution surface', () => {
  assert.match(tabBar, /@click="setSessionKind\('chat'\)"/)
  assert.match(tabBar, /@click="setSessionKind\('terminal'\)"/)
  assert.match(tabBar, /session_kind:\s*form\.session_kind/)

  assert.match(workspaceView, /<label>Execution surface<\/label>/)
  assert.match(workspaceView, /handleAgentSurfaceChange\('chat'\)/)
  assert.match(workspaceView, /session_kind:\s*agentOptionsForm\.session_kind/)
  assert.match(taskDialog, /form\.session_kind === 'chat'/)
  assert.match(taskDialog, /workspaceAgents\.filter\(agent => agent\.session_kind === form\.session_kind\)/)
})

test('Agent Workspace opens the existing managed tab without creating a second transport', () => {
  const openSession = workspaceView.match(
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
  assert.match(structuredPane, /`\$\{streamBasePath\.value\}\/send`/)
  assert.match(structuredPane, /`\$\{streamBasePath\.value\}\/cancel`/)
  assert.match(structuredPane, /`\$\{streamBasePath\.value\}\/attachments\/\$\{encId\}`/)
  assert.match(structuredPane, /Managed Chat identity is not available yet\. Refresh Workspace sessions and try again\./)
  assert.match(structuredPane, /if \(managedIdentityMissing\.value\)/)
})

test('Workspace launcher exposes MASO only for local Chat', () => {
  assert.match(workspaceView, /:allow-maso="agentOptionsForm\.session_kind === 'chat'"/)
  assert.match(workspaceView, /:allow-terminal="agentOptionsForm\.session_kind === 'terminal'"/)
  assert.match(workspaceView, /:disabled="agentOptionsForm\.target === 'remote'"/)
  assert.match(workspaceView, /if \(target === 'remote'\) handleAgentSurfaceChange\('terminal'\)/)
})
