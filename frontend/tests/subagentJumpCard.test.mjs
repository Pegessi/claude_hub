import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'

const root = new URL('../', import.meta.url)
const readSrc = path => readFileSync(new URL(`src/${path}`, root), 'utf8')

const rows = readSrc('features/chat/components/ChatSubagentRow.vue')
const list = readSrc('features/chat/components/ChatSubagentList.vue')
const dockPill = readSrc('features/chat/components/ChatSubagentDockPill.vue')
const sheet = readSrc('features/chat/components/ChatSubagentSheet.vue')
const hubLink = readSrc('features/chat/components/ChatHubSubagentLink.vue')
const toolGroup = readSrc('features/chat/components/ChatToolGroup.vue')
const terminalPane = readSrc('components/TerminalPane.vue')
const structuredPane = readSrc('features/chat/StructuredPane.vue')
const controller = readSrc('features/chat/controllers/useParentSubagents.ts')
const panel = readSrc('features/agent-status/AgentInboxContent.vue')
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
  assert.match(terminalPane, /<ChatSubagentDockPill[\s\S]*?v-if="isChatSession"/)
  assert.doesNotMatch(terminalPane, /isMobile/)
  assert.doesNotMatch(structuredPane, /useParentSubagents/)
})

test('phones reuse the pane-chrome pill and reveal the list as a bottom sheet', () => {
  // No second control above the composer: the Chat body owns inline links only.
  assert.equal(existsSync(new URL('src/features/chat/components/ChatSubagentMobileBar.vue', root)), false)
  assert.doesNotMatch(structuredPane, /ChatSubagentMobileBar/)
  // The pill keeps its pane-chrome styling at every width; only the reveal
  // differs, and a phone still gets the whole list (running + earlier).
  assert.match(dockPill, /import ChatSubagentSheet from '.\/ChatSubagentSheet\.vue'/)
  assert.equal((dockPill.match(/data-testid="subagent-dock-pill"/g) ?? []).length, 1)
  assert.match(dockPill, /<ChatSubagentSheet[\s\S]*?\/>/)
  assert.match(dockPill, /:id="sheetId"/)
  assert.match(dockPill, /const railFits = computed\(\(\) => !isMobile\.value && gutter\.value >= RAIL_MIN_PX\)/)
  assert.match(dockPill, /height: var\(--pane-chrome-control-size, 24px\)/)
  assert.match(dockPill, /var\(--ch-color-surface-raised\)/)
  assert.match(sheet, /aria-modal="true"/)
  assert.match(sheet, /@keydown\.esc\.stop="emit\('close'\)"/)
  assert.match(sheet, /<ChatSubagentList/)
  assert.match(sheet, /onMounted\(\(\) => \{\s+closeRef\.value\?\.focus\(\)/)
  // Modal hygiene: Tab stays inside and the transcript cannot scroll behind it.
  assert.match(sheet, /@keydown\.tab="onTab"/)
  // Hidden "Earlier" rows must not be counted as trap edges.
  assert.match(sheet, /filter\(button => button\.getClientRects\(\)\.length > 0\)/)
  assert.match(sheet, /document\.body\.style\.overflow = 'hidden'/)
  assert.match(sheet, /onBeforeUnmount\(\(\) => \{\s+document\.body\.style\.overflow = previousBodyOverflow/)
})

test('dock pill is an accessible popover with Esc and outside-click dismissal', () => {
  assert.match(dockPill, /counts\.value\.running > 0\s*\? `\$\{counts\.value\.running\} running`\s*: `\$\{counts\.value\.history\} done`/)
  // Esc must work even when a tap moved focus to <body> (the sheet is teleported,
  // so its own handler never sees that key); outside-pointer guards the popover.
  assert.match(dockPill, /if \(open\.value\) \{\s+document\.addEventListener\('keydown', onDismissKey, true\)/)
  assert.match(dockPill, /document\.addEventListener\('pointerdown', onOutsidePointer, true\)/)
  assert.match(dockPill, /v-if="counts\.total > 0"/)
  assert.match(dockPill, /:aria-haspopup="railFits \? undefined : 'dialog'"/)
  assert.match(dockPill, /:aria-expanded="railFits \? railMode : open"/)
  assert.match(dockPill, /@keydown\.esc="onEscape"/)
  // Esc is only consumed while the popover is open.
  assert.match(dockPill, /if \(!open\.value\) return\s+event\.stopPropagation\(\)/)
  assert.match(dockPill, /:aria-controls="railMode \? railId : \(open \? revealId : undefined\)"/)
  assert.match(dockPill, /const revealId = computed\(\(\) => \(isMobile\.value \? sheetId : popoverId\)\)/)
  assert.match(dockPill, /addEventListener\('pointerdown'/)
  assert.match(dockPill, /prefers-reduced-motion: reduce/)
})

test('wide panes rest the list in the right gutter; narrow panes fold it', () => {
  assert.match(dockPill, /const gutter = computed\(\(\) => \(paneWidth\.value - CHAT_COLUMN_PX\) \/ 2/)
  assert.match(dockPill, /const railMode = computed\(\(\) => railFits\.value && !userCollapsed\.value\)/)
  assert.match(dockPill, /v-if="open && !railFits && !isMobile"/)
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
  // A row is inert only when neither action exists: Hub children jump to their
  // tab, native children open the pane's detail window.
  assert.match(rows, /:disabled="!chip\.canJump && !chip\.canOpen"/)
})

test('native sub-agents join the same dock as Hub children', () => {
  // One control: the chrome merges both sources instead of adding a second pill.
  assert.equal((terminalPane.match(/<ChatSubagentDockPill/g) ?? []).length, 1)
  assert.match(terminalPane, /createNativeSubagentDock\(\)/)
  assert.match(terminalPane, /provide\(nativeSubagentDockKey, nativeSubagentDock\)/)
  assert.match(terminalPane, /:\s*chips="subagentRows"/)
  assert.match(terminalPane, /\.\.\.subagentDock\.chips\.value,\s*\.\.\.nativeSubagentDock\.rows\.value/)
  // The Chat body projects the rows and renders the window, because only it
  // owns the timeline and image owners the window's content needs.
  assert.match(structuredPane, /nativeSubagentRows\(\s*timeline\.turns\.value,\s*nativeRowMemo,\s*nativeChildrenReadable,/)
  // A lazy turn can already carry a stub failed child; only readable children
  // may become rows (the same gate `presentTurnParts` uses).
  assert.match(structuredPane, /return !\(detail && detail\.status !== 'loaded'\)/)
  assert.match(structuredPane, /nativeSubagentDock\?\.publish\(rows\)/)
  // Mounted only while a row is open AND this pane is the active one: the
  // window is teleported to <body>, so a cached (hidden) tab would otherwise
  // keep showing it over the tab the reader switched to.
  assert.match(structuredPane, /<ChatNativeSubagentOverlay v-if="nativeOverlayOpen && isPaneActivated" \/>/)
  assert.match(structuredPane, /nativeOverlayOpen = computed\(\(\) => Boolean\(nativeSubagentDock\?\.openRow\.value\)\)/)
  // A cached pane can be evicted while another tab is active, so only the
  // pane that is on screen may clear the shared dock on unmount.
  assert.match(structuredPane, /onUnmounted\(\(\) => \{[\s\S]*?if \(isPaneActivated\.value\) nativeSubagentDock\?\.publish\(\[\]\)/)
  // Every reveal forwards opens: the gutter rail is the default wide-pane
  // presentation, so it must not be a dead click.
  assert.equal((dockPill.match(/@open="onNativeOpen"/g) ?? []).length, 3)
  // The dock owns focus restoration on dismissal (the row, else this pill);
  // a data-driven row clear must not move focus at all.
  assert.match(dockPill, /nativeDock\?\.open\(chip, source, triggerRef\.value\)/)
  assert.doesNotMatch(dockPill, /watch\(\(\) => nativeDock\?\.openRow\.value/)
  assert.match(readSrc('features/chat/controllers/nativeSubagentDock.ts'),
    /const targets = \[opener, fallback\]/)
  // The pane keeps one dock while KeepAlive caches one timeline per tab:
  // activation republishes this tab's rows, deactivation clears them.
  assert.match(structuredPane, /onActivated\(\(\) => \{[\s\S]*?nativeSubagentDock\?\.publish\(nativeDockRows\.value\)/)
  assert.match(structuredPane, /onDeactivated\(\(\) => \{[\s\S]*?nativeSubagentDock\?\.publish\(\[\]\)/)
  // A cached, deactivated pane keeps flushing its own watchers, so the
  // projection may only publish while this pane is the activated tab.
  assert.match(structuredPane, /if \(isPaneActivated\.value\) nativeSubagentDock\?\.publish\(rows\)/)
})

test('the native window replays the inline thread rows and can jump back', () => {
  const overlay = readSrc('features/chat/components/ChatNativeSubagentOverlay.vue')
  const body = readSrc('features/chat/components/ChatSubthreadBody.vue')
  // Same rows as the transcript: one body component serves both call sites.
  assert.match(overlay, /<ChatSubthreadBody/)
  assert.match(body, /subthreadPartsFor\(thread\)/)
  const subthread = readSrc('features/chat/components/ChatSubthread.vue')
  assert.match(subthread, /<ChatSubthreadBody/)
  assert.equal((body.match(/subthreadPartsFor\(thread\)/g) ?? []).length, 1)
  // Dialog semantics + focus trap + body lock, like the other phone overlays.
  assert.match(overlay, /role="dialog"/)
  assert.match(overlay, /aria-modal="true"/)
  // One document-level handler owns Escape and Tab, because a click on static
  // text drops focus to <body>; a top-layer <dialog> (the image lightbox opened
  // from the window) keeps its own keys.
  assert.match(overlay, /document\.addEventListener\('keydown', onDocumentKey, true\)/)
  assert.match(overlay, /function topLayerOwnsKeys\(\): boolean \{\s+return typeof document !== 'undefined' && document\.querySelector\('dialog\[open\]'\) !== null/)
  // The body itself holds tab stops (markdown links, tool <details>), so the
  // trap cannot count only the footer buttons.
  assert.match(overlay, /'a\[href\], button:not\(\[disabled\]\), summary, \[tabindex\]:not\(\[tabindex="-1"\]\)'/)
  // Focus that escaped is pulled back to an edge instead of walking the page.
  assert.match(overlay, /if \(!root\.contains\(active\)\) \{/)
  assert.match(overlay, /document\.body\.style\.overflow = 'hidden'/)
  assert.match(overlay, /addEventListener\('keydown', onDocumentKey, true\)/)
  // A native child has no tab: the jump goes to the transcript, not a tab.
  assert.match(overlay, /revealSubthread\(target\.turnKey, target\.threadId\)/)
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

test('the agent inbox marks sub-agent rows', () => {
  // Same requirement as the old flat status panel: a child Chat started by
  // another Chat must be recognizable in the list, not just in the Workspace.
  assert.match(panel, /v-if="row\.tab\.parent_tab_id"/)
  assert.match(panel, /class="inbox-row-sub"/)
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
