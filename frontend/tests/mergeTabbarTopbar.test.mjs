import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const app = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const structuredPane = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
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

test('desktop TabBar is merged into the app-mode-bar, terminal-mode only', () => {
  // Desktop TabBar renders inside the app-mode-bar and is terminal-mode only
  // (not shown in workspace mode, where it would have no effect).
  assert.match(app, /<TabBar v-if="!isMobile && mode === 'terminal'" \/>/)
  // Mobile TabBar stays in the terminal-main-column (unchanged).
  assert.match(app, /<TabBar v-if="isMobile" \/>/)
  // useViewport backs the desktop/mobile switch.
  assert.match(app, /const \{ isMobile \} = useViewport\(\)/)
})

test('a status light next to Send shows working, idle, and error', () => {
  assert.match(structuredPane, /class="composer-status-light"/)
  assert.match(structuredPane, /const chatStatus = computed/)
  // error on a failed connection
  assert.match(structuredPane, /connectionState\.value === 'failed'\) return 'error'/)
  // working covers a turn in flight and the hydrating phase
  assert.match(
    structuredPane,
    /turnInFlight\.value \|\| connectionState\.value === 'hydrating'\) return 'working'/,
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
