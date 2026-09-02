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

test('Chat sessions never mount a hidden raw terminal fallback', () => {
  assert.match(terminalPane, /v-if="pane\.tabId && !isChatSession"/)
  assert.match(terminalPane, /v-if="pane\.tabId && isChatSession"/)
  assert.doesNotMatch(terminalPane, /hasStructuredSource/)
  assert.doesNotMatch(structuredPane, /fallback-to-raw/)
})

test('Chat and Terminal are fixed session surfaces, not a per-pane view toggle', () => {
  assert.match(terminalPane, /class="pane-header pane-session-header"/)
  assert.match(terminalPane, /class="pane-structured"/)
  assert.match(terminalPane, /const isChatSession = computed/)
  assert.match(terminalPane, /paneTab\.value\?\.session_kind === 'chat'/)
  assert.match(terminalPane, /Chat · native structured/)
  assert.match(terminalPane, /Terminal · native TUI/)
  assert.match(terminalPane, /const providerLabel = computed/)
  assert.match(terminalPane, /v-if="pane\.tabId && isChatSession"/)
  assert.doesNotMatch(terminalPane, /pane-view-switch/)
  assert.doesNotMatch(terminalPane, /type ViewMode/)
  assert.doesNotMatch(terminalPane, />\s*Paseo\s*<\/button>/)
})

test('new-session launcher requires an explicit Chat or Terminal surface', () => {
  assert.match(tabBar, /Create New Session/)
  assert.match(tabBar, /Session Type/)
  assert.match(tabBar, /form\.session_kind === 'chat'/)
  assert.match(tabBar, /form\.session_kind === 'terminal'/)
  assert.match(tabBar, /session_kind:\s*form\.session_kind/)
  assert.match(tabBar, /:allow-terminal="form\.session_kind === 'terminal'"/)
  assert.match(tabBar, />\s*Chat\s*<\/button>/)
  assert.match(tabBar, /Chat Provider/)
  assert.match(tabBar, /Chat session/)
  assert.match(structuredPane, /aria-label="Chat conversation"/)
  assert.match(structuredPane, /start this chat/)
  assert.match(structuredPane, /Waiting for response…/)
  assert.match(structuredPane, /Waiting for model activity…/)
  assert.match(structuredPane, /This chat does not support image attachments/)
  assert.match(tabBar, /The chat provider will restart/)
  assert.match(tabBar, />\s*Restart Provider\s*</)
  assert.match(tabBar, /Chat is resuming its conversation/)
})

test('Chat tab status is backend-derived, accessible, and independent from pane count', () => {
  assert.match(tabBar, /v-if="tab\.session_kind === 'chat' \|\| tab\.is_active"/)
  assert.match(tabBar, /:data-status="getTabStatus\(tab\)"/)
  assert.match(tabBar, /:aria-label="getTabStatusLabel\(tab\)"/)
  assert.match(tabBar, /:title="getTabStatusLabel\(tab\)"/)
  assert.match(tabBar, /tab\.session_kind === 'chat' \? 'offline' : tab\.is_active \? 'idle' : 'offline'/)
  assert.match(tabBar, /tab\.session_kind === 'chat' \? 'Chat' : 'Terminal'/)
  assert.match(tabBar, /class="pane-indicator"/)
})

test('active Terminal tabs retain their runtime status indicator and idle fallback', () => {
  assert.match(tabBar, /v-if="tab\.session_kind === 'chat' \|\| tab\.is_active"/)
  assert.match(tabBar, /tabStatusById\.value\[tab\.id\]\?\.status/)
  assert.match(tabBar, /tab\.is_active \? 'idle' : 'offline'/)
})

test('Chat mode menu renders only backend capabilities and applies to the next turn', () => {
  assert.match(structuredPane, /v-if="modeOptions\.length > 0"/)
  assert.match(structuredPane, /v-for="option in modeOptions"/)
  assert.match(structuredPane, /:disabled="modeInteractionLocked \|\| isUpdatingMode"/)
  assert.match(structuredPane, /aria-haspopup="menu"/)
  assert.match(structuredPane, /role="menuitemradio"/)
  assert.match(structuredPane, /await setMode\(modeId\)/)
  assert.match(structuredPane, /@media \(max-width: 640px\)[\s\S]*?\.composer-mode-trigger \{[\s\S]*?min-height: 44px/)
  assert.doesNotMatch(structuredPane, /agentType.*(?:default|plan)/i)
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
  assert.match(structuredPane, /Waiting for response…/)
  assert.match(structuredPane, /agent-waiting-pulse/)
})

test('structured Retry asks the backend to replace a failed provider transport', () => {
  assert.match(structuredPane, /retry: retryStream/)
  assert.match(structuredPane, /retryStream\(props\.tabId, 'terminal-tab'\)/)
  assert.doesNotMatch(structuredPane, /function retry\(\) \{\s*startStream\(\)/)
})

test('Paseo follows dynamic timeline height but preserves deliberate history reading', () => {
  assert.match(structuredPane, /new ResizeObserver/)
  assert.match(structuredPane, /isTimelineNearBottom\(el\)/)
  assert.match(structuredPane, /detachFromTail\(\)/)
  assert.match(structuredPane, /requestLatestAnchor\(true\)/)
  assert.match(structuredPane, /structured-jump-latest/)
})
