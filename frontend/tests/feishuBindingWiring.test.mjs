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

test('binding UI exposes stable accessible controls and observable states', () => {
  assert.ok(panel.includes('aria-label="Feishu connection settings"'))
  assert.ok(panel.includes('data-testid="feishu-binding-trigger"'))
  assert.ok(panel.includes('role="dialog"'))
  assert.ok(panel.includes('aria-label="Feishu connection"'))
  assert.ok(panel.includes('data-testid="feishu-binding-status"'))
  assert.ok(panel.includes(':data-state="viewState"'))
  assert.ok(panel.includes('Connected to this Chat'))
  assert.ok(panel.includes('Connected to another Chat'))
  assert.ok(panel.includes('Not connected'))
})

test('binding flow offers code copy, rebinding, and confirmed cleanup controls', () => {
  assert.ok(panel.includes('Generate binding code'))
  assert.ok(panel.includes('aria-label="Feishu binding code"'))
  assert.ok(panel.includes('data-testid="feishu-binding-code"'))
  assert.ok(panel.includes('aria-label="Copy binding code"'))
  assert.ok(panel.includes('Connect this Chat'))
  assert.ok(panel.includes('Disconnect Feishu'))
  assert.ok(panel.includes('Confirm disconnect'))
  assert.ok(panel.includes('Cancel disconnect'))
})

test('binding requests use strict same-origin browser auth and fixed backend routes', () => {
  assert.match(binding, /fetch\('\/api\/feishu\/bot\/binding', \{[\s\S]*credentials: 'same-origin'/)
  assert.match(binding, /fetch\('\/api\/feishu\/bot\/bind\/start', \{[\s\S]*method: 'POST'/)
  assert.match(binding, /body: JSON\.stringify\(\{ tab_id: tabId\.value \}\)/)
  assert.match(binding, /fetch\('\/api\/feishu\/bot\/binding', \{[\s\S]*method: 'DELETE'/)
  assert.doesNotMatch(binding, /JSON\.stringify\(\{[^}]*?(workspace_id|cwd|shell|session_id)/)
})

test('401 clears stale state and offers the existing Feishu login entry', () => {
  assert.match(binding, /response\.status === 401[\s\S]*requireLogin\(\)/)
  assert.match(binding, /function requireLogin\(\)[\s\S]*binding\.value = null[\s\S]*pendingCode\.value = null[\s\S]*needsLogin\.value = true/)
  assert.ok(panel.includes('href="/api/auth/login"'))
  assert.ok(panel.includes('Sign in with Feishu'))
})

test('pending code polling is bounded by activation and stops on deactivation', () => {
  assert.ok(binding.includes('pollTimer = setTimeout'))
  assert.ok(binding.includes('if (!pollingEnabled || !pendingCode.value || codeExpired())'))
  assert.ok(panel.includes('onActivated(() =>'))
  assert.ok(panel.includes('onDeactivated(pause)'))
  assert.ok(panel.includes('onUnmounted(() =>'))
})
