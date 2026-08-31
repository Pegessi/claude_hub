import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const terminalPane = readFileSync(
  new URL('../src/components/TerminalPane.vue', import.meta.url),
  'utf8',
)
const terminalView = readFileSync(
  new URL('../src/components/TerminalView.vue', import.meta.url),
  'utf8',
)
const structuredPane = readFileSync(
  new URL('../src/components/StructuredPane.vue', import.meta.url),
  'utf8',
)
const tabBar = readFileSync(
  new URL('../src/components/TabBar.vue', import.meta.url),
  'utf8',
)

test('Agent sessions keep their transport mounted behind an opacity boundary', () => {
  // ttyd's active iframe explicitly sets visibility:visible, so visibility on
  // a Vue parent is not a sufficient hiding boundary. This assertion guards
  // the exclusive same-source view contract without needing a browser iframe.
  const hiddenRule = terminalPane.match(/\.pane-terminal\.is-hidden\s*\{([\s\S]*?)\n\}/)
  assert.ok(hiddenRule, 'the mounted Raw wrapper must have a hidden rule')
  assert.match(hiddenRule[1], /opacity:\s*0/)
  assert.match(hiddenRule[1], /pointer-events:\s*none/)
  assert.match(hiddenRule[1], /z-index:\s*0/)
})

test('Agent and Terminal are fixed session surfaces, not a per-pane view toggle', () => {
  assert.match(terminalPane, /class="pane-header pane-session-header"/)
  assert.match(terminalPane, /class="pane-structured"/)
  assert.match(terminalPane, /const isAgentSession = computed/)
  assert.match(terminalPane, /paneTab\.value\?\.session_kind === 'agent'/)
  assert.match(terminalPane, /v-if="pane\.tabId && isAgentSession"/)
  assert.doesNotMatch(terminalPane, /pane-view-switch/)
  assert.doesNotMatch(terminalPane, /type ViewMode/)
  assert.doesNotMatch(terminalPane, />\s*Paseo\s*<\/button>/)
})

test('new-session launcher requires an explicit Agent or Terminal surface', () => {
  assert.match(tabBar, /Create New Session/)
  assert.match(tabBar, /Session Type/)
  assert.match(tabBar, /form\.session_kind === 'agent'/)
  assert.match(tabBar, /form\.session_kind === 'terminal'/)
  assert.match(tabBar, /session_kind:\s*form\.session_kind/)
  assert.match(tabBar, /:allow-terminal="form\.session_kind === 'terminal'"/)
})

test('SAB terminal input decodes a non-shared copy before draining the record', () => {
  assert.match(terminalView, /var decodedBytes = new Uint8Array\(bytes\.length\);/)
  assert.match(terminalView, /decodedBytes\.set\(bytes\);/)
  assert.match(terminalView, /return decoder\.decode\(decodedBytes\);/)
})

test('direct Paseo sends one ordered prompt frame and keeps a pending acknowledgement', () => {
  assert.match(structuredPane, /const pendingDirectTurns = ref<PendingTurn\[\]>\(\[\]\)/)
  assert.match(structuredPane, /Sent to terminal · waiting for agent activity/)
  assert.match(structuredPane, /sendTerminalText/)
  assert.match(structuredPane, /sendText\(`\$\{message\}\\r`, props\.tabId\)/)
  assert.doesNotMatch(structuredPane, /for \(const char of Array\.from\(message\)\)/)
  assert.match(terminalView, /event\.data\.type === 'terminal-text'/)
})

test('Paseo follows dynamic timeline height but preserves deliberate history reading', () => {
  assert.match(structuredPane, /new ResizeObserver/)
  assert.match(structuredPane, /isFollowingLatest\.value = isTimelineNearBottom\(el\)/)
  assert.match(structuredPane, /requestLatestAnchor\(true\)/)
  assert.match(structuredPane, /structured-jump-latest/)
})
