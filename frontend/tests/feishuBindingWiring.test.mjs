import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const pane = readFileSync(new URL('../src/components/TerminalPane.vue', import.meta.url), 'utf8')
const structuredPane = readFileSync(new URL('../src/components/StructuredPane.vue', import.meta.url), 'utf8')
const panel = readFileSync(new URL('../src/components/FeishuBindingPanel.vue', import.meta.url), 'utf8')
const binding = readFileSync(new URL('../src/composables/useFeishuBinding.ts', import.meta.url), 'utf8')

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
