import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const root = new URL('../', import.meta.url)
const readSrc = path => readFileSync(new URL(`src/${path}`, root), 'utf8')

const jumpCard = readSrc('features/chat/components/ChatSubagentJumpCard.vue')
const structuredPane = readSrc('features/chat/StructuredPane.vue')
const controller = readSrc('features/chat/controllers/useParentSubagents.ts')
const panel = readSrc('features/agent-status/AgentStatusFloatingPanel.vue')
const workspaceStatus = readSrc('features/workspace/components/WorkspaceAgentStatus.vue')
const workspaceRuntime = readSrc('features/workspace/composables/useWorkspaceRuntime.ts')
const terminalTypes = readSrc('types/terminal.ts')

test('TerminalTab carries the Hub subagent linkage fields', () => {
  assert.match(terminalTypes, /parent_tab_id\?: string \| null/)
  assert.match(terminalTypes, /subagent_id\?: string \| null/)
})

test('parent Chat mounts the jump card fed by the parent-subagent controller', () => {
  assert.match(structuredPane, /<ChatSubagentJumpCard/)
  assert.match(structuredPane, /:chips="parentSubagentChips"/)
  assert.match(structuredPane, /@jump="jumpToSubagent"/)
  assert.match(structuredPane, /useParentSubagents\(toRef\(props, 'tabId'\)\)/)
})

test('controller polls the durable subagent list and supports archived restore', () => {
  assert.match(controller, /\$\{API_BASE\}\/subagents\?parent_tab_id=/)
  assert.match(controller, /include_closed=true/)
  assert.match(controller, /subagentPollIntervalMs\(\)/)
  assert.match(controller, /terminalStore\.unarchiveTab\(chip\.tabId\)/)
  assert.match(controller, /appStore\.setMode\('terminal'\)/)
})

test('jump card renders one keyed row per chip with status and jump handler', () => {
  assert.match(jumpCard, /v-for="chip in chips"/)
  assert.match(jumpCard, /:key="chip\.key"/)
  assert.match(jumpCard, /:data-state="chip\.state"/)
  assert.match(jumpCard, /\{\{ chip\.name \}\}/)
  assert.match(jumpCard, /\{\{ chip\.shortId \}\}/)
  assert.match(jumpCard, /\{\{ chip\.stateLabel \}\}/)
  assert.match(jumpCard, /\{\{ chip\.summary \}\}/)
  assert.match(jumpCard, /:disabled="!chip\.canJump"/)
  assert.match(jumpCard, /@click="emitJump\(chip\)"/)
  // Archived children advertise the restore affordance.
  assert.match(jumpCard, /chip\.archived/)
})

test('Terminal Workspace Agents panel marks sub-agent rows', () => {
  assert.match(panel, /v-if="row\.tab\.parent_tab_id"/)
  assert.match(panel, /class="agent-sub-tag"/)
})

test('Workspace mode has a Subagents view of attributed role-less child chats', () => {
  // View/state lives in the runtime composable and reaches the panel via context.
  assert.match(workspaceRuntime, /tab\.workspace_id === wsId/)
  assert.match(workspaceRuntime, /Boolean\(tab\.parent_tab_id\)/)
  assert.match(workspaceRuntime, /tab\.session_kind === 'chat'/)
  assert.match(workspaceStatus, /data-testid="workspace-subagents-toggle"/)
  assert.match(workspaceStatus, /workspaceSubagentTabs/)
  assert.match(workspaceStatus, /data-testid="workspace-subagent-list"/)
  assert.match(workspaceStatus, /@click="openSubagentTab\(tab\.id\)"/)
})
