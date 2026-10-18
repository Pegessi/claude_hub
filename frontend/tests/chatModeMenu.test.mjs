import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const read = path => readFileSync(new URL(`../src/features/chat/${path}`, import.meta.url), 'utf8')
const pane = read('StructuredPane.vue')
const composer = read('components/ChatComposer.vue')
const modePicker = read('components/ChatModePicker.vue')
const modelPicker = read('components/ChatModelPicker.vue')
const settings = read('controllers/useChatSettings.ts')
const composerController = read('controllers/useChatComposer.ts')

test('mode picker lives beside the add menu and opens an upward menu', () => {
  const composerRow = composer.match(/<div class="composer-row">[\s\S]*?class="composer-hints"/)
  assert.ok(composerRow, 'composer row must contain its toolbar with the mode picker')
  assert.match(composerRow[0], /class="composer-tools"/)
  assert.match(composerRow[0], /<ComposerAddMenu[\s\S]*?<ChatModePicker/)
  assert.match(modePicker, /class="composer-mode-trigger"/)
  assert.match(modePicker, /aria-haspopup="menu"/)
  assert.match(modePicker, /:aria-expanded="isModeMenuOpen"/)
  assert.match(modePicker, /\{\{ currentModeLabel \}\}/)
  assert.match(modePicker, /class="composer-mode-chevron"/)
  const menuRule = modePicker.match(/\.composer-mode-menu\s*\{[\s\S]*?\}/)
  assert.ok(menuRule, 'mode menu CSS must exist')
  assert.match(menuRule[0], /bottom:\s*calc\(100% \+ \d+px\)/)
  assert.match(menuRule[0], /max-width:\s*min\(/)
})

test('Stop and Send/Queue share the toolbar line while the model label truncates', () => {
  assert.match(composer, /\.composer-tools \{[^}]*flex: 1 1 0;[^}]*min-width: 0;/)
  assert.match(composer, /<ChatModelPicker/)
  assert.match(modelPicker, /class="composer-mode-picker composer-model-picker"/)
  assert.match(modelPicker, /\.composer-mode-picker\.composer-model-picker \{[^}]*flex-shrink: 1;[^}]*min-width: 0;/)
  assert.match(modelPicker, /\.composer-model-picker \.composer-mode-trigger \{\s*max-width: 100%;/)
  assert.match(composer, /\.composer-send-btn,\s*\.composer-stop-btn \{\s*height: 34px;/)
})

test('mode menu exposes radio semantics and capability-driven options', () => {
  assert.match(modePicker, /role="menu"/)
  assert.match(modePicker, /v-for="option in modeOptions"/)
  assert.match(modePicker, /role="menuitemradio"/)
  assert.match(modePicker, /:aria-checked="currentModeId === option\.id"/)
  assert.match(modePicker, /@click="selectMode\(option\.id\)"/)
  assert.doesNotMatch(modePicker, /agentType.*(?:default|plan)/i)
})

test('mode picker closes on Escape and outside pointer input', () => {
  assert.match(composerController, /event\.key === 'Escape' && deps\.isModeMenuOpen\.value/)
  assert.match(settings, /modePickerEl\.value\?\.contains\(event\.target as Node\)/)
  assert.match(pane, /document\.addEventListener\('pointerdown', settings\.handleModeOutsidePointer\)/)
  assert.match(pane, /document\.removeEventListener\('pointerdown', settings\.handleModeOutsidePointer\)/)
})

test('mode trigger locks for the whole turn and mobile targets remain reachable', () => {
  assert.match(modePicker, /:disabled="modeInteractionLocked \|\| isUpdatingMode"/)
  assert.match(settings, /if \(deps\.modeInteractionLocked\.value \|\| isUpdatingMode\.value/)
  assert.match(modePicker, /@media \(max-width: 640px\)[\s\S]*?\.composer-mode-trigger\s*\{[\s\S]*?min-height:\s*44px/)
  assert.match(modePicker, /@media \(max-width: 640px\)[\s\S]*?\.composer-mode-menu-item\s*\{[\s\S]*?min-height:\s*44px/)
})

test('model picker presents model and thinking effort as a unified two-level menu', () => {
  assert.match(modelPicker, /aria-label="Model and thinking effort"/)
  assert.match(modelPicker, /ref="modelTriggerEl"[\s\S]*?aria-haspopup="dialog"[\s\S]*?:aria-expanded="isModelMenuOpen"/)
  assert.match(modelPicker, /class="composer-mode-menu composer-model-menu"[\s\S]*?role="dialog"/)
  assert.match(modelPicker, /class="composer-mode-list composer-model-list"[\s\S]*?role="listbox"/)
  assert.match(modelPicker, /role="option"/)
  assert.match(modelPicker, /class="composer-model-columns"/)
  assert.match(modelPicker, /v-for="model in filteredModelOptions"/)
  assert.match(modelPicker, /v-for="effort in selectedMenuModel\.supported_reasoning_efforts"/)
  assert.match(modelPicker, /@click="selectModelAndEffort\(selectedMenuModel, effort\.id\)"/)
  assert.match(settings, /computed\(\(\) => presentChatModel\(deps\.currentTab\.value, deps\.capabilities\.value\)\)/)
  assert.match(settings, /return isChatModelEffortActive\(model, effort, modelPresentation\.value\)/)
  assert.match(settings, /modelPresentation\.value\.modelEnvVar/)
  assert.match(settings, /modelPresentation\.value\.reasoningEffortEnvVar/)
  assert.match(settings, /filteredModelOptions\.value\.find\(model => model\.id === selectedMenuModelId\.value\)/)
  assert.match(settings, /deps\.capabilities\.value\?\.current_reasoning_effort/)
  assert.match(modelPicker, /defaultReasoningEffortLabel\(selectedMenuModel\)/)
  assert.match(settings, /defaultModelReasoningEffortLabel\(model, deps\.capabilities\.value\?\.current_reasoning_effort\)/)
  assert.match(modelPicker, /:disabled="modeInteractionLocked \|\| isUpdatingModel \|\| isUpdatingReasoningEffort"/)
  assert.match(settings, /await deps\.terminalStore\.switchEnv[\s\S]*?await deps\.refreshCapabilities\(\)/)
})
