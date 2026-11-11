import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const app = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const appShellStyle = readFileSync(
  new URL('../src/features/app-shell/styles/shell.css', import.meta.url),
  'utf8',
)
const terminalPane = readFileSync(
  new URL('../src/components/TerminalPane.vue', import.meta.url),
  'utf8',
)
const useViewport = readFileSync(
  new URL('../src/composables/useViewport.ts', import.meta.url),
  'utf8',
)
const chatSidebar = readFileSync(
  new URL('../src/features/chat/sidebar/ChatSidebar.vue', import.meta.url),
  'utf8',
)
const chatSidebarGroup = readFileSync(
  new URL('../src/features/chat/sidebar/ChatSidebarGroup.vue', import.meta.url),
  'utf8',
)
const chatSidebarRow = readFileSync(
  new URL('../src/features/chat/sidebar/ChatSidebarRow.vue', import.meta.url),
  'utf8',
)
const useTabStatus = readFileSync(
  new URL('../src/composables/useTabStatus.ts', import.meta.url),
  'utf8',
)
const tabBar = readFileSync(
  new URL('../src/features/terminal/components/TabBar.vue', import.meta.url),
  'utf8',
)
const sessionCreator = readFileSync(
  new URL('../src/features/terminal/components/SessionCreator.vue', import.meta.url),
  'utf8',
)
const tabActionsMenu = readFileSync(
  new URL('../src/features/terminal/components/TabActionsMenu.vue', import.meta.url),
  'utf8',
)
const agentInboxPanel = readFileSync(
  new URL('../src/features/agent-status/AgentInboxPanel.vue', import.meta.url),
  'utf8',
)
const useCwdHistory = readFileSync(
  new URL('../src/composables/useCwdHistory.ts', import.meta.url),
  'utf8',
)
const viteConfig = readFileSync(
  new URL('../vite.config.ts', import.meta.url),
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
  assert.match(chatSidebarRow, /class="chat-sidebar__item-status"/)
  // the sidebar uses the shared useTabStatus composable (no duplicated logic)
  assert.match(chatSidebar, /const \{ getTabStatus, getTabStatusLabel \} = useTabStatus\(agentStatuses\)/)
  // the status logic lives in the composable
  assert.match(useTabStatus, /const tabStatusById = computed/)
  assert.match(useTabStatus, /function getTabStatus\(tab: TerminalTab\)/)
  assert.match(useTabStatus, /function getTabStatusLabel\(tab: TerminalTab\)/)
  // the per-state colors — aligned with the TabBar indicator (working is
  // warning yellow, attention is attention red, not the old blue accent)
  assert.match(chatSidebarRow, /\.chat-sidebar__item-status\[data-status='working'\]\s*\{[^}]*--ch-color-warning/)
  assert.match(chatSidebarRow, /\.chat-sidebar__item-status\[data-status='idle'\]/)
  assert.match(chatSidebarRow, /\.chat-sidebar__item-status\[data-status='attention'\]\s*\{[^}]*--ch-color-attention/)
  assert.match(chatSidebarRow, /\.chat-sidebar__item-status\[data-status='offline'\]/)
})

test('an unread completed turn shows a compact completion badge and tight unread pulse', () => {
  // the status light carries an unread flag
  assert.match(chatSidebarRow, /:data-unread="tab\.is_unread \? 'true' : 'false'"/)
  // Completion is legible from a checkmark and persistent outline; animation
  // only reinforces that the result is still unread.
  assert.match(chatSidebarRow, /:aria-label="statusLabel"/)
  assert.match(chatSidebar, /:status-label="getTabStatusAriaLabel\(tab\)"/)
  assert.match(chatSidebar, /function getTabStatusAriaLabel\(tab: TerminalTab\)/)
  assert.match(chatSidebar, /return 'Completed, unread'/)
  assert.match(chatSidebarRow, /\.chat-sidebar__item-status\[data-status='idle'\]\[data-unread='true'\]\s*\{[^}]*width:\s*8px;[^}]*height:\s*8px;/s)
  assert.match(chatSidebarRow, /\.chat-sidebar__item-status\[data-status='idle'\]\[data-unread='true'\]::before/)
  assert.match(chatSidebarRow, /content:\s*'✓'/)
  assert.match(chatSidebarRow, /color:\s*var\(--ch-color-sidebar-bg\)/)
  assert.match(chatSidebarRow, /font-size:\s*6px/)
  assert.match(chatSidebarRow, /background:\s*var\(--ch-color-success-strong\)/)
  assert.match(chatSidebarRow, /border:\s*1px solid var\(--ch-color-success\)/)
  assert.match(chatSidebarRow, /0 0 8px color-mix\(in srgb, var\(--ch-color-success\) 42%, transparent\)/)
  assert.match(chatSidebarRow, /\.chat-sidebar__item-status\[data-status='idle'\]\[data-unread='true'\]::after/)
  assert.match(chatSidebarRow, /@keyframes sidebar-complete-unread-pulse/)
  assert.match(chatSidebarRow, /inset:\s*-3px/)
  assert.doesNotMatch(chatSidebarRow, /@keyframes sidebar-unread-ping/)
  assert.doesNotMatch(chatSidebarRow, /border:\s*1\.5px solid var\(--ch-color-accent\)/)
  assert.doesNotMatch(chatSidebarRow, /0 0 10px var\(--ch-color-success-bg\)/)
  assert.match(chatSidebarRow, /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.chat-sidebar__item-status\[data-status='idle'\]\[data-unread='true'\]::after[\s\S]*?animation: none;[\s\S]*?opacity:\s*0\.55;/)
})

test('Maso sidebar and main planes use distinct semantic background roles', () => {
  assert.match(chatSidebar, /\.chat-sidebar\s*\{[^}]*background: var\(--ch-color-sidebar-bg\);/s)
  assert.match(appShellStyle, /\.terminal-main-column\s*\{[^}]*background: var\(--ch-color-main-bg\);/s)
  assert.match(terminalPane, /\.terminal-pane\s*\{[^}]*background-color: var\(--ch-color-main-bg\);/s)
})

test('Maso sidebar text puts workspaces first and dims unselected sessions', () => {
  assert.match(chatSidebarGroup, /\n\.chat-sidebar__group-header \{[^}]*color: var\(--ch-color-text\);/)
  assert.match(chatSidebarRow, /\n\.chat-sidebar__item \{[^}]*color: var\(--ch-color-text-muted\);/)
  assert.match(chatSidebarRow, /\n\.chat-sidebar__item:hover \{[^}]*color: var\(--ch-color-text\);/)
  assert.match(chatSidebarRow, /\n\.chat-sidebar__item\.active \{[^}]*color: var\(--ch-color-text-selected\);/)
  assert.match(chatSidebarRow, /\n\.chat-sidebar__item\.active \.chat-sidebar__item-name \{[^}]*font-weight: 600;/)
})

test('the terminal status list is one Needs-you inbox, not two count pills', () => {
  // The two permanent "Status N" / "Agents N" pills are gone: they counted
  // every tab, so the number was never zero and never meant anything.
  assert.doesNotMatch(tabBar, /AgentStatusFloatingPanel/)
  assert.match(tabBar, /<AgentInboxPanel \/>/)
  // One entry, and its badge counts only rows that need the user.
  // The entry names the demand, not a mailbox: "Inbox" is the composer's
  // message tray, a different surface entirely.
  assert.match(agentInboxPanel, /class="inbox-trigger-label"[\s\S]{0,80}?Needs you/, 'the entry is labelled Needs you')
  // On a phone the label is dropped for space; the hidden label must not take
  // the accessible name with it.
  assert.match(agentInboxPanel, /v-if="!isMobile"[\s\S]{0,80}?inbox-trigger-label/, 'the label is wide-only')
  assert.match(agentInboxPanel, /:aria-label="triggerAriaLabel"/, 'the compact entry keeps a spoken name')
  assert.match(agentInboxPanel, /class="inbox-trigger-tip"/, 'and a hover tip on a narrow mouse-capable window')
  assert.match(agentInboxPanel, /v-if="needsCount > 0"/)
  assert.match(agentInboxPanel, /needsYouCount\(/)
  // Manual versus managed is a filter inside the panel, not two triggers.
  assert.equal((agentInboxPanel.match(/class="inbox-trigger"/g) || []).length, 1)
})

test('preview Pin API can be isolated without desynchronizing live Hub APIs', () => {
  assert.match(viteConfig, /VITE_SIDEBAR_PREFERENCES_API_TARGET/)
  const preferencesProxy = viteConfig.indexOf("'/api/tabs/sidebar-preferences'")
  const apiProxy = viteConfig.indexOf("'/api'")
  assert.ok(preferencesProxy >= 0 && preferencesProxy < apiProxy)
  assert.match(viteConfig, /'\/api\/tabs\/sidebar-preferences': \{[\s\S]*?target: sidebarPreferencesApiTarget/)
})

test('a session-name pill sits in the top-right corner of each pane', () => {
  assert.match(terminalPane, /class="pane-session-name"/)
  assert.match(terminalPane, /const tabName = computed/)
  assert.match(terminalPane, /--pane-chrome-control-size: 24px/)
  assert.match(terminalPane, /height: var\(--pane-chrome-control-size\)/)
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
  for (const label of ['Rename', 'Duplicate', 'Archive', 'Copy Link', 'Switch Env / Model…', 'Delete']) {
    assert.match(tabActionsMenu, new RegExp(label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  }
  assert.doesNotMatch(tabActionsMenu, /Move up|Move down/)
  assert.match(tabActionsMenu, /Delete session\?/)
  assert.match(tabActionsMenu, /store\.deleteTab\(props\.tab\.id\)/)
  // both the TabBar and the sidebar mount it
  assert.match(tabBar, /<TabActionsMenu/)
  assert.match(chatSidebarRow, /<TabActionsMenu/)
  // the sidebar gets the sidebar variant for its row-embedded trigger
  assert.match(chatSidebarRow, /variant="sidebar"/)
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
  // Keep these wiring checks until both paths have direct behavior coverage.
  assert.match(useCwdHistory, /addEventListener\('storage'/)
  assert.match(useCwdHistory, /if \(storageBroken\)/)
})

test('a failed session creation does not enter cwd history', () => {
  // createTab returns undefined on failure; only a truthy result records the
  // cwd and closes the dialog
  assert.match(sessionCreator, /const created = await store\.createTab\(/)
  assert.match(sessionCreator, /if \(!created\) return/)
  assert.match(sessionCreator, /addCwd\(cwd\)/)
})

test('new sessions default to solo mode after close and creation', () => {
  // Keep the reset contract here until it has a direct behavior test.
  assert.match(sessionCreator, /solo_mode:\s*true/)
  const soloResets = sessionCreator.match(/form\.solo_mode = true/g) || []
  assert.ok(soloResets.length >= 2, 'solo_mode reset to true after close and create')
})
