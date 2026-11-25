import assert from 'node:assert/strict'
import test from 'node:test'
import { deferred, findByTestId, harness, mountRealtimeSettings, response, settleUi } from './feishuBotUiTestHarness.mjs'

const capabilities = {
  providers: ['maso', 'codex', 'claude'],
  provider_reasoning_efforts: { maso: ['low', 'high'], codex: ['low', 'high', 'xhigh'], claude: [] },
  provider_model_options: {
    maso: [{ id: 'flash-model', label: 'Flash model', provider_model_id: 'flash-model_reasoning_high',
      supported_reasoning_efforts: [
        { id: 'low', provider_model_id: 'flash-model_reasoning_low' },
        { id: 'high', provider_model_id: 'flash-model_reasoning_high' },
      ] }],
    codex: [{ id: 'code-model', label: 'Code model', provider_model_id: 'code-model' }],
    claude: [{ id: 'text-model', label: 'Text model', provider_model_id: 'text-model', supported_reasoning_efforts: [] }],
  },
}
const initial = { enabled: true, provider: 'maso', model: '', reasoning_effort: 'high', workspace_id: '' }
function transport(h, getConfig, saveConfig = patch => ({ ...getConfig(), ...patch })) {
  h.setTransport(async (url, init = {}) => {
    if (url === '/api/workspaces') return response([{ id: 'workspace-a', name: 'Example workspace' }])
    assert.equal(url, '/api/feishu/bot/realtime-events')
    return response({ config: init.method === 'PATCH' ? await saveConfig(JSON.parse(init.body)) : await getConfig() })
  })
}

test('realtime selector saves logical model, effort and workspace without changing its enabled toggle', async t => {
  const h = harness(t)
  let server = { ...initial }, patch
  transport(h, () => server, input => { patch = input; server = { ...server, ...input }; return server })
  const state = await mountRealtimeSettings(t, h, capabilities)
  assert.equal(state.draft.model, '')
  assert.equal(state.draft.reasoning_effort, 'high')
  state.changeModel('flash-model')
  state.draft.reasoning_effort = 'low'
  state.draft.workspace_id = 'workspace-a'
  await state.save()
  assert.deepEqual(patch, { provider: 'maso', model: 'flash-model', reasoning_effort: 'low', workspace_id: 'workspace-a' })
  assert.equal(state.saved, true)
  await state.load()
  assert.equal(state.selectedModel, 'flash-model')
  assert.equal(state.draft.workspace_id, 'workspace-a')
  state.changeProvider('claude')
  state.changeModel('text-model')
  assert.equal(state.draft.reasoning_effort, '')
  await settleUi()
  assert.equal(findByTestId(state.$root, 'feishu-realtime-effort').props.disabled, true)
  state.changeProvider('maso')
  assert.equal(state.draft.model, '')
  assert.equal(state.draft.reasoning_effort, 'high')
})

test('realtime settings preserve a failed-save draft and require readback before another write', async t => {
  const h = harness(t)
  let server = { ...initial }, saves = 0
  transport(h, () => server, input => {
    saves += 1
    server = { ...server, ...input }
    throw new Error('Response lost')
  })
  const state = await mountRealtimeSettings(t, h, capabilities)
  state.changeModel('flash-model')
  await state.save()
  assert.equal(state.draft.model, 'flash-model')
  assert.equal(state.uncertain, true)
  await state.save()
  assert.equal(saves, 1)
  await state.load()
  assert.equal(state.uncertain, false)
  assert.equal(state.draft.model, 'flash-model')
})

test('realtime settings block saving until initial hydration and retain legacy catalog variant selection', async t => {
  const h = harness(t), waiting = deferred()
  transport(h, () => waiting.promise)
  const state = await mountRealtimeSettings(t, h, capabilities)
  assert.equal(state.disabled, true)
  await state.save()
  assert.equal(h.calls.filter(call => call.init.method === 'PATCH').length, 0)
  waiting.resolve({ ...initial, model: 'flash-model_reasoning_high' })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(state.selectedModel, 'flash-model')
  assert.equal(state.disabled, false)
})
