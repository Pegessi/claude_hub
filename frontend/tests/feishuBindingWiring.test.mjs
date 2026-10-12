import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const pane = readFileSync(new URL('../src/components/TerminalPane.vue', import.meta.url), 'utf8')
const structuredPane = readFileSync(new URL('../src/components/StructuredPane.vue', import.meta.url), 'utf8')
const panel = readFileSync(new URL('../src/components/FeishuBindingPanel.vue', import.meta.url), 'utf8')
const binding = readFileSync(new URL('../src/composables/useFeishuBinding.ts', import.meta.url), 'utf8')
const identity = readFileSync(new URL('../src/utils/feishuIdentity.ts', import.meta.url), 'utf8')
const identityMark = readFileSync(new URL('../src/components/FeishuIdentityMark.vue', import.meta.url), 'utf8')
const brandMark = readFileSync(new URL('../src/components/FeishuBrandMark.vue', import.meta.url), 'utf8')

test('local direct Chat surface mounts one Feishu binding panel beside its session name', () => {
  assert.match(pane, /class="pane-chrome"[\s\S]*class="pane-session-name"[\s\S]*<FeishuBindingPanel/)
  assert.match(pane, /v-if="isChatSession"[\s\S]*:tab-id="pane\.tabId"/)
  assert.match(pane, /import FeishuBindingPanel from '@\/components\/FeishuBindingPanel\.vue'/)
  assert.doesNotMatch(structuredPane, /<FeishuBindingPanel/)
  assert.doesNotMatch(structuredPane, /import FeishuBindingPanel/)
})

test('message source is rendered from each durable turn instead of binding state or turn id', () => {
  assert.match(structuredPane, /:data-message-origin="turn\.origin"/)
  assert.match(structuredPane, /v-if="turn\.origin === 'feishu'"/)
  assert.match(structuredPane, /\{\{ messageSourceLabel\(turn\) \}\}/)
  assert.doesNotMatch(structuredPane, /turn\.turnId.*(?:startsWith|includes).*feishu/)
})

test('pool binding retains accessible status and explicit pairing controls', () => {
  for (const marker of [':aria-label="`Feishu connection settings · ${statusText}`"', 'data-testid="feishu-binding-trigger"', 'data-testid="feishu-binding-status"', ':data-state="viewState"', 'Generate pairing code', 'Activate pairing', 'Confirm disconnect']) {
    assert.ok(panel.includes(marker), marker)
  }
  assert.ok(panel.includes('href="/api/auth/login"'))
  assert.doesNotMatch(binding, /owner_open_id/)
})

test('the Feishu status trigger matches the pane chrome control geometry', () => {
  assert.match(panel, /--pane-chrome-control-size, 24px/)
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
  assert.ok(structuredPane.includes('resolvedFeishuSourceAvatarUrl(turn)'))
  assert.ok(structuredPane.includes('bot.app_id === turn.feishu?.appId'))
  assert.ok(structuredPane.includes('feishuBotPoolStore.pool?.pool_revision'))
  assert.match(structuredPane, /<FeishuIdentityMark[\s\S]{0,160}conversation-message-source__mark/)
  assert.ok(structuredPane.includes(':identity-key="feishuSourceIdentityKey(turn)"'))
  assert.doesNotMatch(structuredPane, /conversation-message-source__mark[\s\S]{0,120}>\s*飞/)
})
