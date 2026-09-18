import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const app = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const terminalPane = readFileSync(
  new URL('../src/components/TerminalPane.vue', import.meta.url),
  'utf8',
)
const useViewport = readFileSync(
  new URL('../src/composables/useViewport.ts', import.meta.url),
  'utf8',
)
const chatSidebar = readFileSync(
  new URL('../src/components/ChatSidebar.vue', import.meta.url),
  'utf8',
)
const useTabStatus = readFileSync(
  new URL('../src/composables/useTabStatus.ts', import.meta.url),
  'utf8',
)
const tabBar = readFileSync(
  new URL('../src/components/TabBar.vue', import.meta.url),
  'utf8',
)
const tabActionsMenu = readFileSync(
  new URL('../src/components/TabActionsMenu.vue', import.meta.url),
  'utf8',
)
const useCwdHistory = readFileSync(
  new URL('../src/composables/useCwdHistory.ts', import.meta.url),
  'utf8',
)

test('desktop TabBar is merged into the app-mode-bar, terminal-mode only', () => {
  // Desktop TabBar renders inside the app-mode-bar and is terminal-mode only
  // (not shown in workspace mode, where it would have no effect).
  assert.match(app, /<TabBar v-if="!isMobile && mode === 'terminal'" \/>/)
  // Mobile TabBar stays in the terminal-main-column (unchanged).
  assert.match(app, /<TabBar v-if="isMobile" \/>/)
  // useViewport backs the desktop/mobile switch.
  assert.match(app, /const \{ isMobile \} = useViewport\(\)/)
})

test('a status light in the sidebar shows working, idle, attention, and offline', () => {
  assert.match(chatSidebar, /class="chat-sidebar__item-status"/)
  // the sidebar uses the shared useTabStatus composable (no duplicated logic)
  assert.match(chatSidebar, /const \{ getTabStatus, getTabStatusLabel \} = useTabStatus\(agentStatuses\)/)
  // the status logic lives in the composable
  assert.match(useTabStatus, /const tabStatusById = computed/)
  assert.match(useTabStatus, /function getTabStatus\(tab: TerminalTab\)/)
  assert.match(useTabStatus, /function getTabStatusLabel\(tab: TerminalTab\)/)
  // the per-state colors — aligned with the TabBar indicator (working is
  // warning yellow, attention is attention red, not the old blue accent)
  assert.match(chatSidebar, /\.chat-sidebar__item-status\[data-status='working'\]\s*\{[^}]*--ch-color-warning/)
  assert.match(chatSidebar, /\.chat-sidebar__item-status\[data-status='idle'\]/)
  assert.match(chatSidebar, /\.chat-sidebar__item-status\[data-status='attention'\]\s*\{[^}]*--ch-color-attention/)
  assert.match(chatSidebar, /\.chat-sidebar__item-status\[data-status='offline'\]/)
})

test('an unread completed turn shows a ping ripple on the status light', () => {
  // the status light carries an unread flag
  assert.match(chatSidebar, /:data-unread="tab\.is_unread \? 'true' : 'false'"/)
  // the ping ripple: an accent ring expanding outward from the dot
  assert.match(chatSidebar, /\.chat-sidebar__item-status\[data-unread='true'\]::after/)
  assert.match(chatSidebar, /@keyframes sidebar-unread-ping/)
  // the ripple must be border-box: the global * reset does not reach
  // pseudo-elements, so a content-box border would offset the ring down-right
  assert.match(
    chatSidebar,
    /\.chat-sidebar__item-status\[data-unread='true'\]::after\s*\{[^}]*box-sizing:\s*border-box/s
  )
})

test('a session-name pill sits in the top-right corner of each pane', () => {
  assert.match(terminalPane, /class="pane-session-name"/)
  assert.match(terminalPane, /const tabName = computed/)
  // solid background + shadow so content does not show through
  assert.match(terminalPane, /background: var\(--ch-color-surface-raised\)/)
  assert.match(terminalPane, /box-shadow: 0 1px 4px var\(--ch-shadow-color-soft\)/)
})

test('useViewport is a shared singleton with one resize listener', () => {
  assert.match(useViewport, /let initialized = false/)
  assert.match(useViewport, /window\.addEventListener\('resize', sync\)/)
})

test('session actions live in a shared TabActionsMenu used by both surfaces', () => {
  // the shared menu offers the same actions the TabBar used to own
  for (const label of ['Rename', 'Duplicate', 'Archive', 'Copy Link', 'Switch Env / Model…']) {
    assert.match(tabActionsMenu, new RegExp(label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  }
  // both the TabBar and the sidebar mount it
  assert.match(tabBar, /<TabActionsMenu/)
  assert.match(chatSidebar, /<TabActionsMenu/)
  // the sidebar gets the sidebar variant for its row-embedded trigger
  assert.match(chatSidebar, /variant="sidebar"/)
  // the fixed-position panel re-measures on every open and while scrolling,
  // so it cannot render at stale coordinates after the list scrolls
  assert.match(tabActionsMenu, /void open\.value/)
  assert.match(tabActionsMenu, /addEventListener\('scroll', reposition, true\)/)
  // Switch Env is offered for TraeX only on chat sessions (backend rejects it
  // for terminal-kind TraeX tabs)
  assert.match(tabActionsMenu, /tab\.agent_type === 'traex' && tab\.session_kind === 'chat'/)
  // the Switch Env modal cannot initiate the underlying tab's drag
  assert.match(tabActionsMenu, /@dragstart\.stop/)
  // the Env Preset Manager mounts lazily (one menu exists per session row)
  assert.match(tabActionsMenu, /v-if="showSwitchEnvManager"/)
})

test('cwd history stays in sync across windows and falls back only when storage throws', () => {
  // other windows' writes invalidate every instance via storage events
  assert.match(useCwdHistory, /addEventListener\('storage'/)
  // the in-memory map is consulted only after localStorage itself failed
  assert.match(useCwdHistory, /if \(storageBroken\)/)
})

test('sidebar rows keep a valid nested-control structure', () => {
  // the row is a plain container; the primary action is a real inner button,
  // so the ⋯ menu button is a sibling (no button-inside-role=button)
  assert.match(chatSidebar, /class="chat-sidebar__item-main"/)
  assert.doesNotMatch(chatSidebar, /role="button"/)
})

test('a failed session creation does not enter cwd history', () => {
  // createTab returns undefined on failure; only a truthy result records the
  // cwd and closes the dialog
  assert.match(tabBar, /const created = await store\.createTab\(/)
  assert.match(tabBar, /if \(!created\) return/)
  assert.match(tabBar, /addCwd\(cwd\)/)
})

test('the create-session dialog offers recent working directories and solo default-on', () => {
  // cwd field is wired to a history datalist
  assert.match(tabBar, /list="tab-cwd-history"/)
  assert.match(tabBar, /<datalist id="tab-cwd-history">/)
  assert.match(tabBar, /v-for="cwd in recentCwds"/)
  // history is persisted per launch scope (local / remote:<profile>)
  assert.match(useCwdHistory, /claude-hub:cwd-history:/)
  assert.match(useCwdHistory, /localStorage\.(getItem|setItem)/)
  // new sessions default to solo mode: form init plus both form-reset points
  assert.match(tabBar, /solo_mode:\s*true/)
  const soloResets = tabBar.match(/form\.solo_mode = true/g) || []
  assert.ok(soloResets.length >= 2, 'solo_mode reset to true after close and create')
})

