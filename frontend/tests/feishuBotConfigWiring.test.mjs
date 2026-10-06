import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const app = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const actions = readFileSync(new URL('../src/components/AppExtensionActions.vue', import.meta.url), 'utf8')
const dialog = readFileSync(new URL('../src/components/FeishuBotSettingsDialog.vue', import.meta.url), 'utf8')
const bindingPanel = readFileSync(new URL('../src/components/FeishuBindingPanel.vue', import.meta.url), 'utf8')
const store = readFileSync(new URL('../src/stores/appStore.ts', import.meta.url), 'utf8')
const api = readFileSync(new URL('../src/utils/feishuBotConfig.ts', import.meta.url), 'utf8')

test('global Extensions action opens one app-level instance Bot dialog', () => {
  assert.ok(actions.includes('Feishu Bot settings'))
  assert.ok(actions.includes('appStore.openFeishuBotSettings()'))
  assert.match(app, /<FeishuBotSettingsDialog[\s\S]*v-if="feishuBotSettingsVisible"/)
  assert.match(store, /const feishuBotSettingsVisible = ref\(false\)/)
  assert.doesNotMatch(store, /(?:localStorage|sessionStorage).*feishuBotSettings/)
})

test('instance credentials stay separate from current Chat binding', () => {
  assert.ok(dialog.includes('does not connect the current Chat'))
  assert.doesNotMatch(bindingPanel, /App Secret|Verification Token|Encrypt Key/)
  assert.ok(dialog.includes('v-if="!status.can_manage"'))
  assert.ok(dialog.includes('Contact an administrator'))
  assert.match(api, /status\.can_manage[\s\S]*requestJson<FeishuBotAdminConfig>/)
})

test('secrets are password-only local refs and are cleared on success, refresh, and close', () => {
  for (const model of ['appSecret', 'verificationToken', 'encryptKey']) {
    assert.match(dialog, new RegExp(`v-model="${model}"[\\s\\S]{0,100}type="password"`))
  }
  assert.match(dialog, /function clearSecrets\(\)[\s\S]*appSecret\.value = ''[\s\S]*verificationToken\.value = ''[\s\S]*encryptKey\.value = ''/)
  assert.match(dialog, /applyAdminConfig\(next\)[\s\S]*clearSecrets\(\)/)
  assert.match(dialog, /function close\(\)[\s\S]*clearSecrets\(\)/)
  assert.doesNotMatch(dialog + api, /localStorage|sessionStorage/)
})

test('environment configuration is read-only and mutations require editable status', () => {
  assert.ok(dialog.includes('Managed by environment variables (read-only)'))
  assert.ok(dialog.includes("v-else-if=\"status.source === 'invalid_environment'\""))
  assert.ok(dialog.includes('v-if="status.editable"'))
  assert.match(dialog, /if \(!status\.value\?\.editable[\s\S]*return/)
})

test('App ID changes and deactivation require explicit confirmation with history boundaries', () => {
  assert.ok(dialog.includes("cause.code === 'app_id_change_confirmation_required'"))
  assert.ok(dialog.includes('@click="submitConfig(true)"'))
  assert.ok(dialog.includes('Changing the App ID revokes existing bindings and binding codes'))
  assert.ok(dialog.includes('Deactivating stops new Bot deliveries'))
  assert.ok(dialog.includes('Chat history and message source labels remain'))
  assert.ok(dialog.includes('Already-sent network requests cannot be recalled'))
})

test('revision conflicts clear secrets and reload instead of retrying a mutation', () => {
  assert.match(dialog, /cause\.code === 'config_revision_conflict'[\s\S]*reloadAfterConflict\(cause\.message\)/)
  assert.match(dialog, /async function reloadAfterConflict[\s\S]*clearSecrets\(\)[\s\S]*await refresh\(\)/)
  assert.doesNotMatch(dialog, /config_revision_conflict[\s\S]{0,180}submitConfig\(/)
})

test('failed refresh clears stale editable state', () => {
  assert.match(dialog, /catch \(cause\)[\s\S]*status\.value = null[\s\S]*adminConfig\.value = null/)
})

test('status wording does not claim live webhook or Agent connectivity', () => {
  assert.ok(dialog.includes("status.configured ? 'Configured' : 'Not configured'"))
  assert.ok(dialog.includes('It does not verify webhook delivery, Feishu event permissions, or a real Agent round trip.'))
  assert.ok(dialog.includes('Requires an instance public URL.'))
  assert.ok(dialog.includes('Configure the event callback in Feishu before testing real messages.'))
  assert.doesNotMatch(dialog, />\s*(Connected|Online|Active)\s*</)
})


test('clipboard failure is separate from request reload and busy operations remain closable', () => {
  assert.match(dialog, /const clipboardError = ref<string \| null>\(null\)/)
  assert.match(dialog, /async function copyEventUrl[\s\S]*clipboardError\.value = 'Could not copy the webhook URL/)
  assert.match(dialog, />\s*Reload configuration\s*<\/button>/)
  assert.ok(dialog.includes('Closing this dialog does not cancel an operation already submitted to the server'))
  assert.match(dialog, /type="button"\s+@click="close"\s*>\s*Close\s*<\/button>/)
})

test('only uncertain mutation outcomes reload; busy keeps the current form for retry', () => {
  assert.match(dialog, /function shouldReloadAfterMutation[\s\S]*cause.status === 500[\s\S]*public_url_invalid/)
  assert.doesNotMatch(dialog, /function shouldReloadAfterMutation[\s\S]{0,500}config_operation_busy/)
  assert.match(dialog, /else if \([\s\S]*shouldReloadAfterMutation\(cause\)[\s\S]*reloadAfterConflict\(cause\.message\)/)
  assert.ok(api.includes("'config_operation_busy'"))
  assert.ok(api.includes("'bot_config_unavailable'"))
})
