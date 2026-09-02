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
const workspaceStore = readSource('../src/stores/workspaceStore.ts')

test('only the Terminal top-level launcher offers Chat versus Terminal creation', () => {
  assert.match(tabBar, /@click="setSessionKind\('chat'\)"/)
  assert.match(tabBar, /@click="setSessionKind\('terminal'\)"/)
  assert.match(tabBar, /session_kind:\s*form\.session_kind/)

  assert.doesNotMatch(workspaceView, /session_kind/)
  assert.doesNotMatch(workspaceView, /setSessionKind/)
  assert.doesNotMatch(workspaceView, /StructuredPane/)
})

test('Agent Workspace opens managed sessions through the Terminal top-level surface', () => {
  const openSession = workspaceView.match(
    /async function openSession\(session: ManagedSession\)\s*\{[\s\S]*?\n\}/,
  )
  assert.ok(openSession, 'AgentWorkspaceView must expose the managed-session open action')
  assert.match(openSession[0], /appStore\.setMode\('terminal'\)/)
  assert.match(openSession[0], /terminalStore\.setActiveTab\(session\.tab_id\)/)
  assert.doesNotMatch(openSession[0], /createTab|session_kind|chat/)
})

test('workspace-managed tabs fail closed to TerminalView even if their kind is stale', () => {
  assert.match(
    terminalPane,
    /const isManagedTab = computed\(\(\) => Boolean\(paneTab\.value\?\.workspace_role\)\)/,
  )
  assert.match(
    terminalPane,
    /paneTab\.value\?\.session_kind === 'chat' && !isManagedTab\.value/,
  )
  assert.match(terminalPane, /v-if="pane\.tabId && !isChatSession"[\s\S]*?<TerminalView/)
  assert.match(terminalPane, /v-if="pane\.tabId && isChatSession"[\s\S]*?<StructuredPane/)
  assert.match(terminalPane, /<StructuredPane\s+:tab-id="pane\.tabId"/)
  assert.doesNotMatch(terminalPane, /:session-id=/)
  assert.doesNotMatch(terminalPane, /sessionForTab|managedSession/)
  assert.doesNotMatch(workspaceStore, /sessionForTab/)
})
