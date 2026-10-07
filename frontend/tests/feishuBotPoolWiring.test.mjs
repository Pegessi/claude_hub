import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
const files=['../src/utils/feishuBotConfig.ts','../src/stores/feishuBotPoolStore.ts','../src/components/FeishuBotSettingsDialog.vue','../src/composables/useFeishuBinding.ts','../src/components/FeishuBindingPanel.vue'].map(path=>readFileSync(new URL(path,import.meta.url),'utf8'))
const source=files.join('\n')
test('new UI contains no removed routes',()=>{
  assert.doesNotMatch(source,/\/api\/feishu\/bot\/(?:config|bind\/start|binding|pairings)(?:[/'"`]|$)/)
})
test('secrets and confirmation are not persisted or logged',()=>{
  const sensitive=files.slice(1).join('\n')
  assert.doesNotMatch(sensitive,/localStorage/)
  assert.doesNotMatch(sensitive,/console\.(?:log|info|warn|error)/)
  assert.doesNotMatch(files[1],/app_secret|verification_token|encrypt_key|confirm_word/)
})
test('settings list defaults to name and occupancy',()=>{
  const dialog=files[2]
  assert.match(dialog,/occupancy\(bot\)/)
  assert.match(dialog,/In use by Chat/)
  assert.match(dialog,/Callback URL|callback/i)
  assert.match(dialog,/Existing active pairing is preserved/)
})
test('destructive operations have no force path',()=>assert.doesNotMatch(source,/\bforce\b/))
