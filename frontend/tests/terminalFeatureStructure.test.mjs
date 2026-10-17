import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const read = relativePath => readFileSync(new URL(relativePath, import.meta.url), 'utf8')

const facades = new Map([
  ['TabBar', '../src/components/TabBar.vue'],
  ['TerminalView', '../src/components/TerminalView.vue'],
  ['TabActionsMenu', '../src/components/TabActionsMenu.vue'],
])

const productionFiles = [
  '../src/App.vue',
  ...facades.values(),
  '../src/features/terminal/components/TabBar.vue',
  '../src/features/terminal/components/TerminalView.vue',
  '../src/features/terminal/components/TabActionsMenu.vue',
  '../src/features/terminal/components/SessionCreator.vue',
  '../src/features/terminal/components/CloseTabDialog.vue',
  '../src/features/terminal/composables/useTerminalView.ts',
  '../src/features/terminal/state/terminalInputRing.ts',
  '../src/features/terminal/state/terminalIframeScript.ts',
  '../src/features/terminal/state/terminalIframeSource.ts',
  '../src/features/terminal/state/terminalTheme.ts',
  '../src/features/terminal/styles/tab-bar.css',
  '../src/features/terminal/styles/tab-actions-menu.css',
  '../src/features/terminal/styles/session-creator.css',
  '../src/features/terminal/styles/terminal-view.css',
  '../src/features/app-shell/styles/theme.css',
  '../src/features/app-shell/styles/controls.css',
  '../src/features/app-shell/styles/shell.css',
]

test('legacy terminal component entrypoints are pure default re-exports', () => {
  for (const [name, path] of facades) {
    const source = read(path)
    assert.match(source, new RegExp(`export \\{ default \\} from '@/features/terminal/components/${name}\\.vue'`))
    assert.doesNotMatch(source, /onMounted|onUnmounted|watch\s*\(/)
  }
})

test('terminal feature files respect the production line budget', () => {
  for (const path of productionFiles) {
    const lineCount = read(path).split('\n').length - 1
    assert.ok(lineCount <= 800, `${path} has ${lineCount} lines (limit 800)`)
  }
})

test('terminal view delegates cohesive iframe policies without changing its template contract', () => {
  const view = read('../src/features/terminal/components/TerminalView.vue')
  const controller = read('../src/features/terminal/composables/useTerminalView.ts')
  assert.match(view, /v-for="cachedTabId in cachedTabIds"/)
  assert.match(view, /useTerminalView\(props\)/)
  assert.match(controller, /buildTerminalIframeHandlerScript\(props\.agentType\)/)
  assert.match(controller, /clearAllTimers\(\)/)
  assert.match(controller, /terminalStore\.settlePaneReconnect\(tabId, 'success'\)/)
  assert.match(controller, /terminalStore\.settlePaneReconnect\(tabId, 'error'\)/)
})
