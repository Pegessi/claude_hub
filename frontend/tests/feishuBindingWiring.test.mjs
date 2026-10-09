import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const pane = readFileSync(new URL('../src/components/StructuredPane.vue', import.meta.url), 'utf8')
const panel = readFileSync(new URL('../src/components/FeishuBindingPanel.vue', import.meta.url), 'utf8')
const binding = readFileSync(new URL('../src/composables/useFeishuBinding.ts', import.meta.url), 'utf8')

test('local direct Chat surface mounts one Feishu binding panel for its concrete tab', () => {
  assert.match(pane, /<FeishuBindingPanel :tab-id="props\.tabId" \/>/)
  assert.match(pane, /import FeishuBindingPanel from '@\/components\/FeishuBindingPanel\.vue'/)
})

test('message source is rendered from each durable turn instead of binding state or turn id', () => {
  assert.match(pane, /:data-message-origin="turn\.origin"/)
  assert.match(pane, /v-if="turn\.origin === 'feishu'"/)
  assert.match(pane, /\{\{ messageSourceLabel\(turn\) \}\}/)
  assert.doesNotMatch(pane, /turn\.turnId.*(?:startsWith|includes).*feishu/)
})

test('pool binding retains accessible status and explicit pairing controls', () => {
  for (const marker of [':aria-label="`Feishu connection settings · ${statusText}`"', 'data-testid="feishu-binding-trigger"', 'data-testid="feishu-binding-status"', ':data-state="viewState"', 'Generate pairing code', 'Activate pairing', 'Confirm disconnect']) {
    assert.ok(panel.includes(marker), marker)
  }
  assert.ok(panel.includes('href="/api/auth/login"'))
  assert.doesNotMatch(binding, /owner_open_id/)
})
