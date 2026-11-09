import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const pane = readFileSync(new URL('../src/components/TerminalPane.vue', import.meta.url), 'utf8')
const structuredPane = readFileSync(new URL('../src/features/chat/StructuredPane.vue', import.meta.url), 'utf8')
const userMessage = readFileSync(new URL('../src/features/chat/components/ChatUserMessage.vue', import.meta.url), 'utf8')
const timeline = readFileSync(new URL('../src/features/chat/components/ChatTimeline.vue', import.meta.url), 'utf8')
const actions = readFileSync(new URL('../src/features/chat/controllers/useChatActions.ts', import.meta.url), 'utf8')
const panel = readFileSync(new URL('../src/components/FeishuBindingPanel.vue', import.meta.url), 'utf8')
const binding = readFileSync(new URL('../src/composables/useFeishuBinding.ts', import.meta.url), 'utf8')
const identity = readFileSync(new URL('../src/utils/feishuIdentity.ts', import.meta.url), 'utf8')
const identityMark = readFileSync(new URL('../src/components/FeishuIdentityMark.vue', import.meta.url), 'utf8')
const brandMark = readFileSync(new URL('../src/components/FeishuBrandMark.vue', import.meta.url), 'utf8')

test('local direct Chat surface mounts one Feishu binding panel beside its session name', () => {
  assert.match(pane, /class="pane-chrome"[\s\S]*class="pane-session-name"[\s\S]*<FeishuBindingPanel/)
  assert.match(pane, /v-if="isChatSession && !isManagedChat"[\s\S]*:tab-id="pane\.tabId"/)
  assert.match(pane, /import FeishuBindingPanel from '@\/components\/FeishuBindingPanel\.vue'/)
  assert.doesNotMatch(structuredPane, /<FeishuBindingPanel/)
  assert.doesNotMatch(structuredPane, /import FeishuBindingPanel/)
})

test('message source is rendered from each durable turn instead of binding state or turn id', () => {
  assert.match(userMessage, /:data-message-origin="turn\.origin"/)
  assert.match(userMessage, /v-if="turn\.origin === 'feishu'"/)
  assert.match(userMessage, /\{\{ messageSourceLabel\(turn\) \}\}/)
  assert.doesNotMatch(userMessage, /turn\.turnId.*(?:startsWith|includes).*feishu/)
})

test('pool binding retains accessible status and explicit pairing controls', () => {
  for (const marker of [':aria-label="`Feishu connection settings · ${statusText}`"', 'data-testid="feishu-binding-trigger"', 'data-testid="feishu-binding-status"', ':data-state="viewState"', 'Generate pairing code', 'Activate pairing', 'Confirm disconnect']) {
    assert.ok(panel.includes(marker), marker)
  }
  assert.ok(panel.includes('href="/api/auth/login"'))
  assert.ok(panel.includes('v-model="confirmWord"'))
  assert.ok(panel.includes('status-dot'))
  assert.doesNotMatch(binding, /owner_open_id/)
})

test('the Feishu status trigger matches the pane chrome control geometry', () => {
  assert.match(panel, /\.feishu-binding\s*\{[\s\S]*position:\s*relative;[\s\S]*--pane-chrome-control-size, 24px/)
  assert.match(panel, /border-radius: 999px/)
  assert.match(panel, /background: var\(--ch-color-surface-raised\)/)
  assert.match(panel, /box-shadow: 0 1px 4px var\(--ch-shadow-color-soft\)/)
  assert.match(panel, /\.feishu-binding \{[\s\S]*height: var\(--pane-chrome-control-size, 24px\)/)
})

test('the Feishu entry reuses the monochrome bird mark and scopes Bot avatars to Bot identity', () => {
  assert.match(panel, /<FeishuBrandMark[\s\S]{0,160}class="trigger-icon"/)
  assert.match(panel, /<FeishuBrandMark[\s\S]{0,160}class="panel-mark"/)
  assert.equal((panel.match(/<FeishuBrandMark/g) || []).length, 2)
  assert.doesNotMatch(panel, /<svg[\s\S]{0,160}class="trigger-icon"/)
  assert.match(brandMark, /data-feishu-brand-mark="bird"/)
  assert.equal((brandMark.match(/<path/g) || []).length, 3)
  assert.match(brandMark, /fill:\s*none/)
  assert.match(brandMark, /stroke:\s*currentColor/)
  assert.match(panel, /data-testid="feishu-binding-identity"/)
  assert.match(panel, /viewState.value !== 'bound-current'/)
  assert.ok(panel.includes('currentBot.value?.feishu_avatar_url'))
  assert.match(panel, /trustedFeishuAvatarUrl/)
  assert.ok(identity.includes("parsed.protocol === 'https:'"))
  assert.doesNotMatch(identity, /data:image\/png|base64/)
  assert.doesNotMatch(panel, />飞<|>飞\s*<\/span>/)
  assert.match(panel, /class="connected-card__icon"[\s\S]{0,240}data-testid="feishu-binding-identity"/)
  assert.ok(identityMark.includes('failedIdentityKey'))
  assert.match(identityMark, /crossorigin="anonymous"/)
  assert.match(panel, /return `\$\{props\.tabId\}\\n\$\{botId\}\\n\$\{avatarUrl\}`/)
  assert.ok(identityMark.includes('failedIdentityKey.value !== props.identityKey'))
  assert.ok(binding.includes("currentBot.value?.feishu_name||currentBot.value?.name||'Bot'"))
})

test('Feishu message provenance resolves a bound Bot avatar before the outline brand mark', () => {
  assert.ok(userMessage.includes('resolvedFeishuSourceAvatarUrl(turn)'))
  assert.ok(actions.includes('bot.app_id === turn.feishu?.appId'))
  assert.ok(timeline.includes('feishuBotPoolStore.pool?.pool_revision'))
  assert.match(userMessage, /<FeishuIdentityMark[\s\S]{0,160}conversation-message-source__mark/)
  assert.ok(userMessage.includes(':identity-key="feishuSourceIdentityKey(turn)"'))
  assert.doesNotMatch(userMessage, /conversation-message-source__mark[\s\S]{0,120}>\s*飞/)
})

const eventValues = readFileSync(new URL('../src/features/chat/timeline/eventValues.ts', import.meta.url), 'utf8')

test('the unbound panel offers the private pairing flow and a group path side by side', () => {
  for (const marker of [
    'data-testid="feishu-binding-path-private"',
    'data-testid="feishu-binding-path-group"',
    'data-testid="feishu-binding-group-select"',
    'data-testid="feishu-binding-bind-group"',
    'data-testid="feishu-binding-load-groups"',
    'data-testid="feishu-binding-reload-groups"',
    'aria-label="Group"',
    'Anyone in this group can talk to this Chat.',
    'Bind this group',
  ]) {
    assert.ok(panel.includes(marker), marker)
  }
  // Both ways in stay real buttons with a pressed state, so they are keyboard
  // reachable and announce which path is active.
  for (const path of ['private', 'group']) {
    assert.match(panel, new RegExp(
      `<button\\s+type="button"[\\s\\S]{0,200}:aria-pressed="connectMode === '${path}'"` +
      `[\\s\\S]{0,120}@click="connectMode = '${path}'"`,
    ), path)
  }
  assert.ok(panel.includes('Generate pairing code'), 'the existing pairing action remains')
  // The group list is read only when the group path (or a group binding) needs it.
  assert.match(panel, /ensureGroups\(\)/)
  assert.doesNotMatch(panel, /setInterval/)
})

test('a stale group selection cannot survive a Bot switch or a reloaded list', () => {
  // The chosen chat id belongs to one Bot: it must be dropped when the Bot
  // changes or the option disappears, or "Bind this group" submits a chat the
  // user never picked in that Bot's list.
  assert.match(panel, /watch\(selectedBotId, \(\) => \{ selectedChatId\.value = null \}\)/)
  assert.ok(panel.includes('!value.some(chat => chat.chat_id === selectedChatId.value)'))
  assert.ok(panel.includes("selectedChatId.value = null"))
})

test('the topic hint follows the server-resolved form, not just chat_mode', () => {
  assert.ok(panel.includes("chat.is_topic_mode ?? chat.chat_mode === 'topic'"))
  assert.ok(panel.includes("boundGroup.value?.is_topic_mode"))
})

test('the bound panel names a private chat or a group, with the topic hint', () => {
  assert.match(panel, /data-testid="feishu-binding-target"[\s\S]{0,80}\{\{ boundTargetLabel \}\}/)
  assert.match(panel, /binding\.value\?\.chat_type === 'group'/)
  assert.ok(panel.includes('Private chat'))
  assert.ok(panel.includes('Group · '))
  assert.ok(panel.includes('binding.value?.chat_name'))
  assert.match(panel, /v-if="boundIsTopic"[\s\S]{0,180}data-testid="feishu-binding-topic-hint"/)
  assert.ok(panel.includes('Replies stay inside the group topic.'))
  assert.ok(panel.includes("chat.chat_mode === 'topic'"))
  assert.ok(panel.includes('New private messages enter this Chat.'))
  assert.match(binding, /groups,groupsLoaded,groupsLoading,groupsError/)
  assert.ok(binding.includes('listChats'))
  assert.ok(binding.includes('requestGroupBind'))
})

test('a group message badge appends a compact scope suffix and leaves p2p alone', () => {
  assert.match(userMessage, /\{\{ messageSourceLabel\(turn\) \}\}/)
  assert.match(userMessage, /v-if="messageSourceScope\(turn\)"[\s\S]{0,180}data-testid="conversation-message-scope"/)
  assert.ok(userMessage.includes('· {{ messageSourceScope(turn) }}'))
  assert.ok(eventValues.includes("feishu.chatType !== 'group'"))
  assert.ok(eventValues.includes("threadId ? 'group topic' : 'group'"))
})
