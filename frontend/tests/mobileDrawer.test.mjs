import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// Structural regression guards for the mobile session drawer. Rather than
// mount the component (the project's node:test setup has no Vue Test Utils /
// jsdom), read the SFC source and assert the properties that the three
// review rounds established — so a future edit that re-introduces one of
// the caught bugs fails here.
const drawer = readFileSync(
  new URL('../src/components/MobileSessionDrawer.vue', import.meta.url),
  'utf8',
)
const sidebar = readFileSync(
  new URL('../src/components/ChatSidebar.vue', import.meta.url),
  'utf8',
)
const app = readFileSync(
  new URL('../src/App.vue', import.meta.url),
  'utf8',
)

test('backdrop uses @click.self so panel interactions do not close the drawer', () => {
  // Round-1 blocker: the panel was a DOM child of a backdrop bound to bare
  // @click, so every click inside the panel bubbled up and closed it.
  assert.match(
    drawer,
    /class="msd-backdrop"\s+@click\.self="emit\('close'\)"/,
  )
})

test('backdrop and panel are siblings so the slide leave animation fires', () => {
  // Round-2 N1: with the panel nested inside the backdrop, the outer v-if
  // removed the whole subtree on close and the inner slide leave never ran.
  const fadeIdx = drawer.indexOf('<Transition name="msd-fade">')
  const slideIdx = drawer.indexOf('<Transition name="msd-slide">')
  assert.ok(fadeIdx !== -1 && slideIdx !== -1 && fadeIdx < slideIdx)
  // The backdrop is self-closed before the panel begins (not nested).
  const backdropClose = drawer.indexOf(
    '/>',
    drawer.indexOf('class="msd-backdrop"'),
  )
  assert.ok(
    backdropClose !== -1 && backdropClose < drawer.indexOf('class="msd-panel"'),
  )
})

test('empty-state message is gated on having no groups', () => {
  // Round-1 major: the empty div was unconditional and rendered under the
  // list even when groups were present. Same bug existed in ChatSidebar.
  assert.match(
    drawer,
    /v-if="filteredGroups\.length === 0"\s+class="msd-empty"/,
  )
  assert.match(
    sidebar,
    /v-if="filteredGroups\.length === 0"\s+class="chat-sidebar__empty"/,
  )
})

test('group header controls its collapsible region via aria-controls', () => {
  assert.match(drawer, /:aria-controls="'msd-group-' \+ group\.cwd"/)
  assert.match(drawer, /:id="'msd-group-' \+ group\.cwd"/)
})

test('filter text resets each time the drawer opens', () => {
  // Round-2 N4: the drawer is always mounted, so filterText persisted
  // between opens and a stale filter could hide sessions.
  assert.match(
    drawer,
    /watch\(\s*\(\) => props\.open,[\s\S]*?filterText\.value = ''/,
  )
})

test('archived tabs are fetched on mount so the count is accurate', () => {
  // Round-2 M1: fetchArchivedTabs ran only on deep-link / archive ops, so a
  // normal load showed "Archived (0)" even when archived sessions existed.
  assert.match(
    app,
    /await store\.fetchTabs\(\)\s*\n\s*void store\.fetchArchivedTabs\(\)/,
  )
})

test('drawer opens the archive panel above itself and closes', () => {
  assert.match(
    app,
    /@open-archive="archivePanelOpen = true; store\.mobileDrawerOpen = false"/,
  )
  // z-index: backdrop 849 < panel 850 < archive panel 900.
  assert.match(drawer, /\.msd-backdrop \{[\s\S]*?z-index: 849/)
  assert.match(drawer, /\.msd-panel \{[\s\S]*?z-index: 850/)
})

test('interactive rows and buttons meet the 44px touch-target guideline', () => {
  assert.match(drawer, /\.msd-item \{[\s\S]*?min-height: 44px/)
  assert.match(drawer, /\.msd-group-header \{[\s\S]*?min-height: 44px/)
  assert.match(drawer, /\.msd-archived-link \{[\s\S]*?min-height: 44px/)
  assert.match(drawer, /\.msd-close \{[\s\S]*?min-height: 44px/)
})
