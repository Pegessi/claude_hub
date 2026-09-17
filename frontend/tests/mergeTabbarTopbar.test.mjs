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
  // the per-state colors
  assert.match(chatSidebar, /\.chat-sidebar__item-status\[data-status='working'\]/)
  assert.match(chatSidebar, /\.chat-sidebar__item-status\[data-status='idle'\]/)
  assert.match(chatSidebar, /\.chat-sidebar__item-status\[data-status='attention'\]/)
  assert.match(chatSidebar, /\.chat-sidebar__item-status\[data-status='offline'\]/)
})

test('an unread completed turn shows a ping ripple on the status light', () => {
  // the status light carries an unread flag
  assert.match(chatSidebar, /:data-unread="tab\.is_unread \? 'true' : 'false'"/)
  // the ping ripple: an accent ring expanding outward from the dot
  assert.match(chatSidebar, /\.chat-sidebar__item-status\[data-unread='true'\]::after/)
  assert.match(chatSidebar, /@keyframes sidebar-unread-ping/)
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
