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
  assert.ok(dialog.includes('Feishu Bots'))
  assert.ok(dialog.includes('data-testid="feishu-bot-settings-dialog"'))
})
test('Bot UI follows the shared design language and keeps the Chat control top-right', () => {
  for (const marker of ['ch-btn', 'ch-input', 'bot-sidebar', 'empty-state', 'settings-section']) {
    assert.ok(dialog.includes(marker), marker)
  }
  assert.match(panel, /\.feishu-binding\s*\{[\s\S]*position:\s*absolute;[\s\S]*right:\s*14px;/)
  assert.ok(panel.includes('trigger-icon'))
  assert.ok(panel.includes('status-dot'))
  assert.ok(panel.includes('Feishu connection settings · ${statusText}'))
  assert.ok(dialog.includes(':aria-pressed="bot.bot_id === selectedId"'))
  assert.ok(dialog.includes("? 'set' : 'missing'"))
  assert.doesNotMatch(panel, /<span>飞<\/span> Feishu/)
})
test('pool configuration and pairing do not restore removed routes or browser secret storage', () => {
  assert.doesNotMatch(`${dialog}\n${panel}\n${api}`, /\/api\/feishu\/bot\/(?:config|bind\/start|binding|pairings)(?:[/'"`]|$)/)
  assert.doesNotMatch(`${dialog}\n${panel}`, /localStorage|sessionStorage/)
  assert.ok(panel.includes('v-model="confirmWord"'))
})
