import assert from 'node:assert/strict'
import test from 'node:test'
import { computed, ref } from 'vue'
import { loadChatUtility } from './chatPresentationTestHarness.mjs'

const {
  presentChatModel,
  isChatModelActive,
  isChatModelEffortActive,
  defaultModelReasoningEffortLabel,
  filterChatModels,
  effortLabel,
} = await loadChatUtility('chatModelPresentation')

const model = {
  id: 'model', label: 'Model Label', provider_model_id: 'model-base', default_reasoning_effort: 'low',
  supported_reasoning_efforts: [
    { id: 'low', provider_model_id: 'model-low' },
    { id: 'xhigh', provider_model_id: 'model-thinking' },
  ],
}
const auto = { id: 'auto', label: 'Auto', supported_reasoning_efforts: [] }
const caps = (extra = {}) => ({
  available_models: [model], current_model: null, current_reasoning_effort: null, ...extra,
})
const tab = (agent_type, env = {}) => ({ agent_type, env })

test('model and effort environment keys remain provider-specific', () => {
  for (const [provider, modelKey, effortKey] of [
    ['claude', 'ANTHROPIC_MODEL', null],
    ['codex', 'CODEX_MODEL', 'CODEX_REASONING_EFFORT'],
    ['traex', 'CODEX_MODEL', 'CODEX_REASONING_EFFORT'],
    ['cursor', 'CURSOR_MODEL', null],
    ['maso', 'MASO_MODEL', null],
    ['shell', null, null],
  ]) {
    const result = presentChatModel(tab(provider), null)
    assert.equal(result.modelEnvVar, modelKey)
    assert.equal(result.reasoningEffortEnvVar, effortKey)
  }
  assert.equal(presentChatModel(null, null).combinedModelLabel, 'default')
})

test('effective provider model matches catalog ids, provider ids and effort aliases', () => {
  for (const id of ['model', 'model-base', 'model-thinking']) {
    const result = presentChatModel(tab('codex'), caps({ current_model: id }))
    assert.equal(result.currentModel, '')
    assert.equal(result.currentModelOption, model)
    assert.equal(result.combinedModelLabel, 'Model Label · Low')
    assert.equal(isChatModelActive(model, result), true)
  }
})

test('explicit model wins over capabilities and custom models keep their exact label', () => {
  const result = presentChatModel(tab('claude', { ANTHROPIC_MODEL: 'custom-model' }), caps({ current_model: 'model' }))
  assert.equal(result.currentModel, 'custom-model')
  assert.equal(result.currentModelOption, null)
  assert.equal(result.combinedModelLabel, 'custom-model')
  assert.equal(isChatModelActive(model, result), false)
})

test('Auto row and empty env retain original active-selection and label behavior', () => {
  const options = [auto, model]
  for (const value of ['', 'auto']) {
    const result = presentChatModel(tab('cursor', { CURSOR_MODEL: value }), caps({ available_models: options }))
    assert.equal(isChatModelActive(auto, result), true)
    assert.equal(result.combinedModelLabel, value || 'Auto')
  }
  const effective = presentChatModel(tab('cursor'), caps({ available_models: options, current_model: 'model' }))
  assert.equal(effective.currentModelOption, model)
  assert.equal(isChatModelActive(auto, effective), true)
  assert.equal(isChatModelActive(model, effective), true)
})

test('Cursor effort aliases are explicit; its base model and effective default do not select an effort', () => {
  const explicit = presentChatModel(tab('cursor', { CURSOR_MODEL: 'model-thinking' }), caps())
  assert.equal(explicit.currentReasoningEffort, 'xhigh')
  assert.equal(explicit.combinedModelLabel, 'Model Label · Extra High')
  assert.equal(isChatModelEffortActive(model, 'xhigh', explicit), true)
  assert.equal(isChatModelEffortActive(auto, 'xhigh', explicit), false)
  for (const value of ['', 'model-base']) {
    const result = presentChatModel(tab('cursor', { CURSOR_MODEL: value }), caps({ current_model: 'model-thinking' }))
    assert.equal(result.currentReasoningEffort, '')
  }
})

test('MASO infers effort from the effective model even without an explicit model env', () => {
  for (const value of ['', 'auto', 'model-thinking']) {
    const result = presentChatModel(tab('maso', { MASO_MODEL: value }), caps({ current_model: 'model-thinking' }))
    assert.equal(result.currentReasoningEffort, 'xhigh')
    assert.equal(result.combinedModelLabel, 'Model Label · Extra High')
  }
})

test('Codex and TraeX reject unsupported effort but preserve unknown custom model effort', () => {
  for (const provider of ['codex', 'traex']) {
    const result = presentChatModel(tab(provider, {
      CODEX_MODEL: 'model', CODEX_REASONING_EFFORT: 'unsupported',
    }), caps({ current_reasoning_effort: 'xhigh' }))
    assert.equal(result.currentReasoningEffort, '')
    assert.equal(result.combinedModelLabel, 'Model Label · Extra High')
    const unknown = presentChatModel(tab(provider, {
      CODEX_MODEL: 'custom', CODEX_REASONING_EFFORT: 'unsupported',
    }), caps())
    assert.equal(unknown.currentReasoningEffort, 'unsupported')
  }
})

test('TraeX legacy effort is fallback only and an explicit empty effort suppresses it', () => {
  const env = { CODEX_MODEL: 'model', TRAEX_REASONING_EFFORT: 'xhigh' }
  assert.equal(presentChatModel(tab('traex', env), caps()).currentReasoningEffort, 'xhigh')
  assert.equal(presentChatModel(tab('codex', env), caps()).currentReasoningEffort, '')
  for (const effort of ['', 'low']) {
    assert.equal(presentChatModel(tab('traex', { ...env, CODEX_REASONING_EFFORT: effort }), caps()).currentReasoningEffort, effort)
  }
})

test('effort labels preserve empty, provider-default and custom-id semantics', () => {
  assert.equal(defaultModelReasoningEffortLabel(model, null), 'Low')
  assert.equal(defaultModelReasoningEffortLabel(model, ''), 'Default')
  assert.equal(defaultModelReasoningEffortLabel(model, 'xhigh'), 'Extra High')
  assert.equal(effortLabel('very_high-effort'), 'Very High Effort')
  assert.equal(effortLabel(undefined), 'Default')
})

test('search matches only id or label case-insensitively and does not mutate the catalog', () => {
  const options = [auto, model]
  assert.equal(filterChatModels(options, '  '), options)
  assert.deepEqual(filterChatModels(options, ' MODEL '), [model])
  assert.deepEqual(filterChatModels(options, 'label'), [model])
  assert.deepEqual(filterChatModels(options, 'model-thinking'), [])
  assert.deepEqual(options, [auto, model])
})

test('projection follows caller-owned reactive env and capability changes without retaining stale state', () => {
  const currentTab = ref(tab('traex', { CODEX_MODEL: 'model', CODEX_REASONING_EFFORT: 'low' }))
  const capabilities = ref(caps())
  const presentation = computed(() => presentChatModel(currentTab.value, capabilities.value))
  assert.equal(presentation.value.currentReasoningEffort, 'low')
  currentTab.value.env.CODEX_REASONING_EFFORT = 'xhigh'
  assert.equal(presentation.value.currentReasoningEffort, 'xhigh')
  const before = structuredClone({ ...currentTab.value, env: { ...currentTab.value.env } })
  capabilities.value.available_models = []
  assert.equal(presentation.value.currentModelOption, null)
  assert.deepEqual({ ...currentTab.value, env: { ...currentTab.value.env } }, before)
})
