import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const app = read('../src/App.vue')
const actions = read('../src/components/AppExtensionActions.vue')
const dialog = read('../src/features/feishu-bot/FeishuBotSettingsDialog.vue')

test('global Extensions retains one app-level Bot pool dialog', () => {
  assert.ok(actions.includes('appStore.openFeishuBotSettings()'))
  assert.match(app, /<FeishuBotSettingsDialog[\s\S]*v-if="feishuBotSettingsVisible"/)
  assert.ok(dialog.includes('Feishu Bots'))
  assert.ok(dialog.includes('data-testid="feishu-bot-settings-dialog"'))
})
test('Bot pool dialog retains shared controls and explicit selection and secret status', () => {
  for (const marker of ['ch-btn', 'ch-input', 'bot-sidebar', 'empty-state', 'settings-section']) {
    assert.ok(dialog.includes(marker), marker)
  }
  assert.ok(dialog.includes(':aria-pressed="bot.bot_id === selectedId"'))
  assert.ok(dialog.includes("? 'set' : 'missing'"))
})
