import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const app = read('../src/App.vue')
const actions = read('../src/components/AppExtensionActions.vue')
const dialog = read('../src/components/FeishuBotSettingsDialog.vue')
const panel = read('../src/components/FeishuBindingPanel.vue')
const api = read('../src/utils/feishuBotConfig.ts')

test('global Extensions retains one app-level Bot pool dialog', () => {
  assert.ok(actions.includes('appStore.openFeishuBotSettings()'))
  assert.match(app, /<FeishuBotSettingsDialog[\s\S]*v-if="feishuBotSettingsVisible"/)
  assert.ok(dialog.includes('Feishu Bot pool'))
  assert.ok(dialog.includes('data-testid="feishu-bot-settings-dialog"'))
})
test('pool configuration and pairing do not restore removed routes or browser secret storage', () => {
  assert.doesNotMatch(`${dialog}\n${panel}\n${api}`, /\/api\/feishu\/bot\/(?:config|bind\/start|binding|pairings)(?:[/'"`]|$)/)
  assert.doesNotMatch(`${dialog}\n${panel}`, /localStorage|sessionStorage/)
  assert.ok(panel.includes('v-model="confirmWord"'))
})
