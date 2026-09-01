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

test('Agent sessions never mount a hidden raw terminal fallback', () => {
  assert.match(terminalPane, /v-if="pane\.tabId && !isAgentSession"/)
  assert.match(terminalPane, /v-if="pane\.tabId && isAgentSession"/)
  assert.doesNotMatch(terminalPane, /hasStructuredSource/)
  assert.doesNotMatch(structuredPane, /fallback-to-raw/)
})

test('Agent and Terminal are fixed session surfaces, not a per-pane view toggle', () => {
  assert.match(terminalPane, /class="pane-header pane-session-header"/)
  assert.match(terminalPane, /class="pane-structured"/)
  assert.match(terminalPane, /const isAgentSession = computed/)
  assert.match(terminalPane, /paneTab\.value\?\.session_kind === 'agent'/)
  assert.match(terminalPane, /Agent · native structured/)
  assert.match(terminalPane, /Terminal · native TUI/)
  assert.match(terminalPane, /const providerLabel = computed/)
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

test('direct Paseo sends atomically with a stable client turn id', () => {
  assert.match(structuredPane, /const pendingDirectTurns = ref<PendingTurn\[\]>\(\[\]\)/)
  assert.match(structuredPane, /client_turn_id: clientTurnId/)
  assert.match(structuredPane, /crypto\.randomUUID/)
  assert.match(structuredPane, /turn\.turnId !== clientTurnId/)
  assert.doesNotMatch(structuredPane, /Sent to terminal/)
  assert.doesNotMatch(structuredPane, /sendTerminalText/)
})

test('an acknowledged turn shows a waiting state until provider activity arrives', () => {
  assert.match(structuredPane, /awaitingAgentActivity: !turn\.completed/)
  assert.match(structuredPane, /Waiting for agent response…/)
  assert.match(structuredPane, /agent-waiting-pulse/)
})

test('structured Retry asks the backend to replace a failed provider transport', () => {
  assert.match(structuredPane, /retry: retryStream/)
  assert.match(structuredPane, /retryStream\(props\.tabId, 'terminal-tab'\)/)
  assert.doesNotMatch(structuredPane, /function retry\(\) \{\s*startStream\(\)/)
})

test('Paseo follows dynamic timeline height but preserves deliberate history reading', () => {
  assert.match(structuredPane, /new ResizeObserver/)
  assert.match(structuredPane, /isFollowingLatest\.value = isTimelineNearBottom\(el\)/)
  assert.match(structuredPane, /requestLatestAnchor\(true\)/)
  assert.match(structuredPane, /structured-jump-latest/)
})
