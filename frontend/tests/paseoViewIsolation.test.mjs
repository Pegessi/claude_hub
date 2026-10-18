import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const terminalPane = readFileSync(
  new URL('../src/components/TerminalPane.vue', import.meta.url),
  'utf8',
)
const terminalInputRing = readFileSync(
  new URL('../src/features/terminal/state/terminalInputRing.ts', import.meta.url),
  'utf8',
)
const structuredPane = readFileSync(
  new URL('../src/features/chat/StructuredPane.vue', import.meta.url),
  'utf8',
)
const chatTimelineView = readFileSync(
  new URL('../src/features/chat/components/ChatTimeline.vue', import.meta.url),
  'utf8',
)
const chatTurn = readFileSync(
  new URL('../src/features/chat/components/ChatTurn.vue', import.meta.url),
  'utf8',
)
const chatPendingTurn = readFileSync(
  new URL('../src/features/chat/components/ChatPendingTurn.vue', import.meta.url),
  'utf8',
)
const chatComposer = readFileSync(
  new URL('../src/features/chat/components/ChatComposer.vue', import.meta.url),
  'utf8',
)
const chatModePicker = readFileSync(
  new URL('../src/features/chat/components/ChatModePicker.vue', import.meta.url),
  'utf8',
)
const chatAttachments = readFileSync(
  new URL('../src/features/chat/controllers/useChatAttachments.ts', import.meta.url),
  'utf8',
)
const chatDelivery = readFileSync(
  new URL('../src/features/chat/controllers/useChatDelivery.ts', import.meta.url),
  'utf8',
)
const chatSettings = readFileSync(
  new URL('../src/features/chat/controllers/useChatSettings.ts', import.meta.url),
  'utf8',
)
const chatTimeline = readFileSync(
  new URL('../src/features/chat/controllers/useChatTimeline.ts', import.meta.url),
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
const useTabStatus = readFileSync(
  new URL('../src/composables/useTabStatus.ts', import.meta.url),
  'utf8',
)

test('Chat sessions never mount a hidden raw terminal fallback', () => {
  assert.match(terminalPane, /v-if="pane\.tabId && !isChatSession"/)
  assert.match(terminalPane, /v-if="pane\.tabId && isChatSession"/)
  assert.doesNotMatch(terminalPane, /hasStructuredSource/)
  assert.doesNotMatch(structuredPane, /fallback-to-raw/)
})

test('Chat and Terminal are fixed session surfaces, not a per-pane view toggle', () => {
  assert.match(terminalPane, /class="pane-structured"/)
  assert.match(terminalPane, /const isChatSession = computed/)
  assert.match(terminalPane, /paneTab\.value\?\.session_kind === 'chat'/)
  assert.match(terminalPane, /v-if="pane\.tabId && isChatSession"/)
  // No per-pane info header: the TabBar already identifies the active tab, so a
  // header would just duplicate it and cost a row of space.
  assert.doesNotMatch(terminalPane, /pane-header pane-session-header/)
  assert.doesNotMatch(terminalPane, /pane-view-switch/)
  assert.doesNotMatch(terminalPane, /type ViewMode/)
  assert.doesNotMatch(terminalPane, />\s*Paseo\s*<\/button>/)
})

test('new-session launcher requires an explicit Chat or Terminal surface', () => {
  assert.match(sessionCreator, /Create New Session/)
  assert.match(sessionCreator, /Session Type/)
  assert.match(sessionCreator, /form\.session_kind === 'chat'/)
  assert.match(sessionCreator, /form\.session_kind === 'terminal'/)
  assert.match(sessionCreator, /session_kind:\s*form\.session_kind/)
  assert.match(sessionCreator, /:allow-terminal="form\.session_kind === 'terminal'"/)
  assert.match(sessionCreator, />\s*Chat\s*<\/button>/)
  assert.match(sessionCreator, /Chat Provider/)
  assert.match(sessionCreator, /Chat session/)
  assert.match(sessionCreator, /:disabled="form\.session_kind === 'chat'"/)
  assert.match(sessionCreator, /Chat always runs on this Hub host/)
  assert.match(sessionCreator, /Remote SSH is Terminal-only/)
  assert.match(chatTimelineView, /aria-label="Chat conversation"/)
  assert.match(chatTimelineView, /start this chat/)
  assert.match(chatTurn, /Waiting for response…/)
  assert.match(chatPendingTurn, /Waiting for model activity…/)
  assert.match(chatAttachments, /This chat does not support image attachments/)
  // Switch Env / restart UI is shared between the TabBar and sidebar via
  // TabActionsMenu, so it lives in that component now.
  assert.match(tabActionsMenu, /The chat provider will restart/)
  assert.match(tabActionsMenu, />\s*Restart Provider\s*</)
  assert.match(tabActionsMenu, /Chat is resuming its conversation/)
})

test('Chat tab status is backend-derived, accessible, and independent from pane count', () => {
  assert.match(tabBar, /v-if="tab\.session_kind === 'chat' \|\| tab\.is_active"/)
  assert.match(tabBar, /:data-status="getTabStatus\(tab\)"/)
  assert.match(tabBar, /:aria-label="getTabStatusLabel\(tab\)"/)
  assert.match(tabBar, /:title="getTabStatusLabel\(tab\)"/)
  // status logic is shared via useTabStatus (no duplicated fallback in TabBar)
  assert.match(tabBar, /const \{ getTabStatus, getTabStatusLabel \} = useTabStatus\(agentStatuses\)/)
  assert.match(tabBar, /class="pane-indicator"/)
})

test('active Terminal tabs retain their runtime status indicator and idle fallback', () => {
  assert.match(tabBar, /v-if="tab\.session_kind === 'chat' \|\| tab\.is_active"/)
  // The fallback logic lives in the shared useTabStatus composable.
  assert.match(useTabStatus, /tab\.is_active \? 'idle' : 'offline'/)
})

test('direct Chat mode menu renders backend capabilities while managed Task mode stays hidden', () => {
  assert.match(chatComposer, /v-if="!isManagedChat && modeOptions\.length > 0"/)
  assert.match(chatModePicker, /v-for="option in modeOptions"/)
  assert.match(chatModePicker, /:disabled="modeInteractionLocked \|\| isUpdatingMode"/)
  assert.match(chatModePicker, /aria-haspopup="menu"/)
  assert.match(chatModePicker, /role="menuitemradio"/)
  assert.match(chatSettings, /await deps\.setMode\(modeId\)/)
  assert.match(chatModePicker, /@media \(max-width: 640px\)[\s\S]*?\.composer-mode-trigger \{[\s\S]*?min-height: 44px/)
  assert.doesNotMatch(chatSettings, /agentType.*(?:default|plan)/i)
})

test('SAB terminal input decodes a non-shared copy before draining the record', () => {
  assert.match(terminalInputRing, /var decodedBytes = new Uint8Array\(bytes\.length\);/)
  assert.match(terminalInputRing, /decodedBytes\.set\(bytes\);/)
  assert.match(terminalInputRing, /return decoder\.decode\(decodedBytes\);/)
})

test('direct Paseo sends atomically with a stable client turn id', () => {
  assert.match(chatDelivery, /const pendingDirectTurns = ref<PendingTurn\[\]>\(\[\]\)/)
  assert.match(chatDelivery, /client_turn_id: clientTurnId/)
  assert.match(chatDelivery, /crypto\.randomUUID/)
  assert.match(chatDelivery, /turn\.turnId !== clientTurnId/)
  assert.doesNotMatch(chatDelivery, /Sent to terminal/)
  assert.doesNotMatch(chatDelivery, /sendTerminalText/)
})

test('an acknowledged turn shows a waiting state until provider activity arrives', () => {
  assert.match(chatTimeline, /awaitingAgentActivity: !turn\.completed/)
  assert.match(chatTurn, /Waiting for response…/)
  assert.match(chatTurn, /agent-waiting-pulse/)
})

test('structured Retry asks the backend to replace a failed provider transport', () => {
  assert.match(structuredPane, /retry: retryStream/)
  assert.match(structuredPane, /retryStream\(streamSourceId\.value, streamSource\.value\)/)
  assert.doesNotMatch(structuredPane, /function retry\(\) \{\s*startStream\(\)/)
})

test('Paseo follows dynamic timeline height but preserves deliberate history reading', () => {
  assert.match(chatTimeline, /new ResizeObserver/)
  assert.match(chatTimeline, /isTimelineNearBottom\(el\)/)
  assert.match(chatTimeline, /detachFromTail\(\)/)
  assert.match(chatTimeline, /requestLatestAnchor\(true\)/)
  assert.match(structuredPane, /structured-jump-latest/)
})
