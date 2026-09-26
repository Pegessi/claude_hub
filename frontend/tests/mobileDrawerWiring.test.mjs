import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// Structural regression guards for the mobile session drawer. The drawer now
// REUSES ChatSidebar (overlay form factor on mobile) instead of shipping a
// duplicate list, so these assert the wiring that keeps groups/pinned/search/
// archive and the edge gesture intact.
const sidebar = readFileSync(
  new URL('../src/components/ChatSidebar.vue', import.meta.url),
  'utf8',
)
const app = readFileSync(
  new URL('../src/App.vue', import.meta.url),
)
const tabBar = readFileSync(
  new URL('../src/components/TabBar.vue', import.meta.url),
  'utf8',
)
const composable = readFileSync(
  new URL('../src/composables/useEdgeSwipeDrawer.ts', import.meta.url),
  'utf8',
)

test('the sidebar is no longer display:none on mobile — it becomes an overlay drawer', () => {
  assert.ok(!/display:\s*none/.test(sidebar.match(/@media \(max-width: 768px\) \{[\s\S]*$/)?.[0] ?? ''))
  assert.match(sidebar, /\.chat-sidebar\.mobile-drawer \{[\s\S]*?position: fixed/)
  // Required width form factor.
  assert.match(sidebar, /width: min\(82vw, 320px\)/)
  // iPhone notch / home indicator adaptation.
  assert.match(sidebar, /env\(safe-area-inset-top/)
  assert.match(sidebar, /env\(safe-area-inset-bottom/)
})

test('the drawer slides in/out via transform and starts hidden when closed', () => {
  assert.match(sidebar, /mobile-drawer--open/)
  assert.match(sidebar, /transform: translateX\(-100%\)/)
  assert.match(sidebar, /mobile-drawer--open[\s\S]*?transform: translateX\(0\)/)
})

test('the pinned group markup from ChatSidebar is reused unchanged in the drawer', () => {
  assert.match(sidebar, /group\.pinned/)
  assert.match(sidebar, /class="chat-sidebar__group-header chat-sidebar__pinned-header"/)
  assert.match(sidebar, /buildChatSidebarGroups\(chatTabs\.value, pinnedChatIds\.value/)
})

test('an invisible left edge band is rendered only on mobile terminal mode', () => {
  assert.match(sidebar, /class="chat-drawer-edge-band"/)
  assert.match(sidebar, /v-if="isMobile && isTerminalMode"/)
})

test('the backdrop closes via @click.self so panel interactions do not dismiss it', () => {
  assert.match(sidebar, /class="chat-drawer-backdrop"[\s\S]*?@click\.self="closeMobileDrawer"/)
})

test('selecting a row in the mobile drawer goes through selectMobileTab (switch + auto-close)', () => {
  assert.match(sidebar, /if \(isMobile\.value\) \{\s*selectMobileTab\(tab\.id\)/)
})

test('the edge gesture uses the pure composable, not inline template geometry', () => {
  assert.match(sidebar, /useEdgeSwipeDrawer\(/)
  // touchmove must be non-passive so horizontal takeover can preventDefault.
  assert.match(composable, /addEventListener\('touchmove', onMove, \{ passive: false \}\)/)
})

test('TabBar keeps an explicit non-gesture drawer entry wired to the store action', () => {
  assert.match(tabBar, /@click="openMobileDrawer"/)
  assert.match(tabBar, /store\.openMobileDrawer\(\)/)
})

test('the duplicate MobileSessionDrawer is no longer mounted in App', () => {
  assert.ok(!/MobileSessionDrawer/.test(app))
})

test('opening archive from the drawer dismisses the drawer first', () => {
  assert.match(sidebar, /function openArchive\(\) \{[\s\S]*?closeMobileDrawer\(\)[\s\S]*?emit\('open-archive'\)/)
})

test('filter text resets whenever the drawer opens', () => {
  assert.match(
    sidebar,
    /watch\(mobileDrawerOpen, isOpen => \{[\s\S]*?if \(isOpen\) \{\s*filterText\.value = ''/,
  )
})
