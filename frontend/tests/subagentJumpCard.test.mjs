import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'

const root = new URL('../', import.meta.url)
const readSrc = path => readFileSync(new URL(`src/${path}`, root), 'utf8')

const rows = readSrc('features/chat/components/ChatSubagentRow.vue')
const list = readSrc('features/chat/components/ChatSubagentList.vue')
const dockPill = readSrc('features/chat/components/ChatSubagentDockPill.vue')
const mobileBar = readSrc('features/chat/components/ChatSubagentMobileBar.vue')
const hubLink = readSrc('features/chat/components/ChatHubSubagentLink.vue')
const toolGroup = readSrc('features/chat/components/ChatToolGroup.vue')
const terminalPane = readSrc('components/TerminalPane.vue')
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

test('old bottom jump card is gone', () => {
  assert.equal(existsSync(new URL('src/features/chat/components/ChatSubagentJumpCard.vue', root)), false)
  assert.doesNotMatch(structuredPane, /ChatSubagentJumpCard/)
})

test('TerminalPane owns the single poller and shares it with the pill and Chat body', () => {
  assert.equal((terminalPane.match(/useParentSubagents\(/g) ?? []).length, 1)
  assert.match(terminalPane, /provide\(subagentDockKey, subagentDock\)/)
  assert.match(terminalPane, /<ChatSubagentDockPill[\s\S]*?v-if="isChatSession && !isMobile"/)
  assert.doesNotMatch(structuredPane, /useParentSubagents/)
  assert.match(structuredPane, /useSubagentDock\(\)/)
})

test('mobile shows only a running-only bar above the composer', () => {
  assert.match(structuredPane, /<ChatSubagentMobileBar[\s\S]*?v-if="isMobile && subagentDock"/)
  assert.ok(structuredPane.indexOf('<ChatSubagentMobileBar') < structuredPane.indexOf('<ChatComposer'))
  assert.match(mobileBar, /v-if="counts\.running > 0"/)
  assert.match(mobileBar, /aria-modal="true"/)
  assert.match(mobileBar, /@keydown\.esc\.stop="closeSheet"/)
})

test('dock pill is an accessible popover with Esc and outside-click dismissal', () => {
  assert.match(dockPill, /counts\.value\.running > 0\s*\? `\$\{counts\.value\.running\} running`\s*: `\$\{counts\.value\.history\} done`/)
  assert.match(dockPill, /v-if="counts\.total > 0"/)
  assert.match(dockPill, /:aria-haspopup="railFits \? undefined : 'dialog'"/)
  assert.match(dockPill, /:aria-expanded="railFits \? railMode : open"/)
  assert.match(dockPill, /@keydown\.esc="onEscape"/)
  // Esc is only consumed while the popover is open.
  assert.match(dockPill, /if \(!open\.value\) return\s+event\.stopPropagation\(\)/)
  assert.match(dockPill, /:aria-controls="railMode \? railId : \(open \? popoverId : undefined\)"/)
  assert.match(dockPill, /addEventListener\('pointerdown'/)
  assert.match(dockPill, /prefers-reduced-motion: reduce/)
})

test('wide panes rest the list in the right gutter; narrow panes fold it', () => {
  assert.match(dockPill, /const gutter = computed\(\(\) => \(paneWidth\.value - CHAT_COLUMN_PX\) \/ 2/)
  assert.match(dockPill, /const railMode = computed\(\(\) => railFits\.value && !userCollapsed\.value\)/)
  assert.match(dockPill, /v-if="open && !railFits"/)
  assert.match(dockPill, /new ResizeObserver/)
  assert.match(dockPill, /\.subagent-dock \{\s+position: relative;\s+display: flex;/)
})

test('list shows Active first and a collapsible Earlier with restore wording', () => {
  assert.ok(list.indexOf('Active') < list.indexOf('Earlier'))
  assert.match(list, /:aria-expanded="earlierOpen"/)
  assert.match(list, /chip\.group === 'active'/)
  assert.match(rows, /Restore & open/)
  assert.match(rows, /:data-state="chip\.state"/)
  assert.match(rows, /\{\{ chip\.summary \}\}/)
  assert.match(rows, /:disabled="!chip\.canJump"/)
})

test('inline Hub agent run calls link to a matching sub-agent only', () => {
  assert.match(toolGroup, /<ChatHubSubagentLink/)
  assert.match(hubLink, /hubSubagentIdFromTool\(props\.argsText, props\.resultText\)/)
  assert.match(hubLink, /dock\?\.jump\(chip\)/)
})

test('controller polls the durable subagent list and supports archived restore', () => {
  assert.match(controller, /\$\{API_BASE\}\/subagents\?parent_tab_id=/)
  assert.match(controller, /include_closed=true/)
  assert.match(controller, /subagentPollIntervalMs\(\)/)
  assert.match(controller, /terminalStore\.unarchiveTab\(chip\.tabId\)/)
  assert.match(controller, /appStore\.setMode\('terminal'\)/)
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
