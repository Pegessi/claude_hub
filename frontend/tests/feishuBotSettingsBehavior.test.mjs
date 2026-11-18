import assert from 'node:assert/strict'
import test from 'node:test'
import { activeBinding, bot, deferred, findByTestId, flush, harness, mountSettings, pool, response } from './feishuBotUiTestHarness.mjs'

const emptyCreate = { name: '', app_id: '', app_secret: '' }
const input = { name: 'Created', app_id: 'app-c', app_secret: 'create-secret' }
const reads = h => h.calls.filter(call => (call.init.method ?? 'GET') === 'GET').length

test('confirmed create clears its credentials and keeps the success message after selection', async t => {
  const h = harness(t)
  h.setTransport(async (_url, init) => init.method === 'POST'
    ? response(pool(2, [...h.server().bots, bot('c', { name: 'Created' })], 'c'))
    : response(h.server()))
  const state = await mountSettings(t, h)
  state.beginCreate()
  Object.assign(state.create, input)
  await state.createBot()
  assert.deepEqual({ ...state.create }, emptyCreate)
  assert.equal(state.selectedId, 'c')
  assert.equal(state.success, 'Bot added.')
})
test('busy selection and reload are ignored; close does not restore fields on a late successful response', async t => {
  const h = harness(t), waiting = deferred()
  const saved = pool(2, [bot('a', { name: 'Saved', revision: 2 }), bot('b')], 'a')
  h.setTransport(async (_url, init) => init.method === 'PATCH' ? waiting.promise : response(h.server()))
  t.after(async () => { waiting.resolve(response(saved)); await flush() })
  const state = await mountSettings(t, h)
  state.select(h.store.botById('a'))
  state.editName = 'Saved'
  state.appSecret = 'replacement-secret'
  Object.assign(state.create, input)
  const writing = state.saveMetadata()
  assert.equal(state.busy, true)
  const beforeReads = reads(h)
  state.select(h.store.botById('b'))
  state.beginCreate()
  await state.refresh()
  assert.equal(state.selectedId, 'a')
  assert.equal(reads(h), beforeReads)
  state.close()
  assert.equal(state.appSecret, '')
  assert.deepEqual({ ...state.create }, emptyCreate)
  waiting.resolve(response(saved))
  await writing
  assert.equal(h.store.botById('a').name, 'Saved')
  assert.equal(state.success, null)
  assert.equal(state.appSecret, '')
  assert.deepEqual({ ...state.create }, emptyCreate)
})
test('a shared update cannot silently advance a metadata draft revision', async t => {
  const h = harness(t)
  const state = await mountSettings(t, h)
  state.select(h.store.botById('a'))
  state.editName = 'My edit'
  state.appSecret = 'must-clear-on-conflict'
  const changed = pool(2, [bot('a', { name: 'Changed elsewhere', enabled: false, revision: 2 }), bot('b')])
  h.store.applyPool(changed)
  h.setServer(changed)
  h.setTransport(async (_url, init) => init.method === 'PATCH'
    ? response({ detail: 'bot_revision_conflict' }, 409) : response(h.server()))
  await state.saveMetadata()
  const writes = h.calls.filter(call => call.init.method === 'PATCH')
  assert.equal(writes.length, 1)
  assert.equal(JSON.parse(writes[0].init.body).expected_revision, 1)
  assert.equal(state.editName, 'Changed elsewhere')
  assert.equal(state.editEnabled, false)
  assert.equal(state.selectedRevision, 2)
  assert.equal(state.appSecret, '')
  assert.match(state.error, /changed|Reload/i)
})

test('secret replacement and delete confirmation also use the selected draft revision', async t => {
  const h = harness(t)
  const state = await mountSettings(t, h)
  state.select(h.store.botById('a'))
  state.appSecret = 'secret'
  const changed = pool(2, [bot('a', { revision: 2 }), bot('b')])
  h.store.applyPool(changed); h.setServer(changed)
  h.setTransport(async (_url, init) => init.method
    ? response({ detail: 'bot_revision_conflict' }, 409) : response(h.server()))
  await state.saveSecrets()
  const put = h.calls.find(call => call.init.method === 'PUT')
  assert.equal(JSON.parse(put.init.body).expected_revision, 1)
  assert.equal(state.selectedRevision, 2)
  state.confirmDelete = true
  const changedAgain = pool(3, [bot('a', { revision: 3 }), bot('b')])
  h.store.applyPool(changedAgain); h.setServer(changedAgain)
  await state.removeBot()
  const deletion = h.calls.find(call => call.init.method === 'DELETE')
  assert.equal(JSON.parse(deletion.init.body).expected_revision, 2)
  assert.equal(state.confirmDelete, false)
  assert.equal(state.selectedRevision, 3)
})

test('bot_operation_busy keeps the entered credentials and does not re-read the pool', async t => {
  const h = harness(t)
  const state = await mountSettings(t, h)
  state.select(h.store.botById('a'))
  state.appSecret = 'keep-secret'
  h.setTransport(async (_url, init) => init.method === 'PUT'
    ? response({ detail: 'bot_operation_busy' }, 503) : response(h.server()))
  const beforeReads = reads(h)
  await state.saveSecrets()
  assert.equal(state.appSecret, 'keep-secret')
  assert.equal(state.busy, false)
  assert.equal(reads(h), beforeReads)
  assert.match(state.error, /progress|wait/i)
})

test('selecting per_channel mode on an unbound Bot submits mode in PATCH and updates the Bot', async t => {
  const h = harness(t)
  const updated = pool(2, [bot('a', { mode: 'per_channel', revision: 2 }), bot('b')], 'a')
  h.setTransport(async (_url, init) => init.method === 'PATCH'
    ? response(updated) : response(h.server()))
  const state = await mountSettings(t, h)
  state.select(h.store.botById('a'))
  assert.equal(state.editMode, 'single')
  assert.equal(state.modeSwitchDisabled, false)
  assert.equal(state.modeSwitchDisabledReason, null)
  state.selectMode('per_channel')
  assert.equal(state.editMode, 'per_channel')
  assert.equal(await state.saveMetadata(), true)
  const patch = h.calls.find(call => call.init.method === 'PATCH')
  assert.deepEqual(JSON.parse(patch.init.body), {
    name: 'Bot a',
    enabled: true,
    mode: 'per_channel',
    expected_revision: 1,
  })
  assert.equal(h.store.botById('a').mode, 'per_channel')
  assert.equal(state.editMode, 'per_channel')
  assert.equal(state.success, 'Bot details saved.')
})

test('mode switching is disabled and explained when the Bot has active channels, and a 409 conflict reconciles with an explanation', async t => {
  const h = harness(t)
  const boundBot = bot('b', {
    mode: 'single',
    bindings: [activeBinding('tab-active', { bot_id: 'b', app_id: 'app-b' })],
  })
  const initial = pool(1, [bot('a'), boundBot])
  h.store.applyPool(initial)
  h.setServer(initial)
  const state = await mountSettings(t, h)

  state.select(h.store.botById('b'))
  assert.equal(state.modeSwitchDisabled, true)
  assert.match(state.modeSwitchDisabledReason, /no active channels|Disconnect/i)
  state.selectMode('per_channel')
  assert.equal(state.editMode, 'single', 'disabled mode control cannot switch mode')

  // Now select unbound Bot a, switch mode locally, and simulate a 409 because a
  // channel became active on the server before Save was processed.
  state.select(h.store.botById('a'))
  state.selectMode('per_channel')
  const occupiedServer = pool(2, [
    bot('a', { mode: 'single', revision: 2, bindings: [activeBinding('tab-1')] }),
    boundBot,
  ])
  h.setServer(occupiedServer)
  h.setTransport(async (_url, init) => init.method === 'PATCH'
    ? response({ detail: 'bot_has_active_bindings' }, 409)
    : response(h.server()))
  assert.equal(await state.saveMetadata(), false)
  assert.match(state.error, /active.*channel|Disconnect.*before switching/i)
  assert.equal(state.editMode, 'single')
  assert.equal(state.selectedRevision, 2)
  assert.equal(state.modeSwitchDisabled, true)
})

test('renaming or toggling enabled on a Bot with active channels omits mode from PATCH', async t => {
  const h = harness(t)
  const active = activeBinding('tab-1')
  const initial = pool(1, [bot('a', { mode: 'per_channel', bindings: [active] }), bot('b')])
  const updated = pool(2, [bot('a', { name: 'Renamed Bot', mode: 'per_channel', revision: 2, bindings: [active] }), bot('b')], 'a')
  h.store.applyPool(initial)
  h.setServer(initial)
  h.setTransport(async (_url, init) => init.method === 'PATCH'
    ? response(updated) : response(h.server()))
  const state = await mountSettings(t, h)
  state.select(h.store.botById('a'))
  assert.equal(state.modeSwitchDisabled, true)
  state.editName = 'Renamed Bot'
  assert.equal(await state.saveMetadata(), true)
  const patch = h.calls.find(call => call.init.method === 'PATCH')
  const payload = JSON.parse(patch.init.body)
  assert.deepEqual(payload, {
    name: 'Renamed Bot',
    enabled: true,
    expected_revision: 1,
  })
  assert.equal('mode' in payload, false)
  assert.equal(state.success, 'Bot details saved.')
})

test('Personal /ai Assistant panel loads status, updates provider/model/reasoning_effort, and imports ~/.lark-cli token', async t => {
  const h = harness(t)
  const aiStatus = (overrides = {}) => ({
    config: {
      enabled: false,
      prefix: '/ai',
      owner_open_id: 'ou_owner_1',
      app_id: 'cli_app_1',
      provider: 'claude',
      model: '[redacted]',
      reasoning_effort: '',
      cwd: '',
      watch_chats: ['oc_chat_1'],
      poll_interval_seconds: 15,
      max_active_chats: 10,
      max_context_messages: 20,
      ack_emoji: 'OnIt',
      done_emoji: 'DONE',
      footer: '> 🤖 AI 代答，仅供参考',
      reply_in_thread: true,
      ...overrides,
    },
    token_status: {
      has_token: true,
      open_id: 'ou_owner_1',
      user_name: 'Owner',
      app_id: 'cli_app_1',
      source: 'lark_cli',
      expires_at: 9999999999,
      token_expired: false,
      granted_scopes: ['im:message', 'im:message.send_as_user'],
      required_send_scope: 'im:message.send_as_user',
      can_send_as_user: true,
      can_react_as_user: true,
      can_read_messages: true,
      missing_required_scopes: [],
      missing_recommended_scopes: [],
      auth_login_command: 'lark-cli auth login --scope "im:message.send_as_user"',
      remediation_steps: [],
    },
    poller_status: {
      running: false,
      poll_count: 0,
      last_poll_at: null,
      last_error: null,
      commands_detected: 0,
      commands_succeeded: 0,
      commands_failed: 0,
      skipped_count: 0,
      watermarks_count: 0,
    },
    capabilities: {
      providers: ['claude', 'codex', 'traex', 'maso', 'cursor', 'antigravity'],
      reasoning_efforts: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
      provider_reasoning_efforts: {
        claude: [],
        codex: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'],
        maso: ['none', 'low', 'medium', 'high', 'xhigh', 'max'],
      },
      provider_models: {
        claude: ['[redacted]'],
        codex: ['gpt-5.4', 'o3'],
        maso: ['[redacted]_reasoning_high'],
      },
    },
    imported: true,
  })
  h.setTransport(async (url, init = {}) => {
    if (String(url).includes('/api/feishu/bot/ai-command/import-token')) {
      return response(aiStatus({ enabled: true }))
    }
    if (String(url).includes('/api/feishu/bot/ai-command')) {
      if (init.method === 'PATCH') {
        const body = JSON.parse(init.body)
        return response(aiStatus(body))
      }
      return response(aiStatus())
    }
    return response(h.server())
  })
  const state = await mountSettings(t, h)
  await state.openAiCommand()
  await flush()
  assert.equal(state.aiCommandActive, true)
  assert.equal(state.aiCommandLoaded, true)
  assert.equal(state.aiEdit.provider, 'claude')
  assert.equal(state.aiWatchChatsText, 'oc_chat_1')

  state.onAiProviderChange('codex')
  await flush()
  assert.equal(state.aiEdit.provider, 'codex')
  assert.equal(state.aiEdit.model, 'gpt-5.4')
  assert.equal(state.aiEdit.reasoning_effort, 'high')
  state.aiEdit.enabled = true
  state.aiEdit.reasoning_effort = 'xhigh'
  state.aiWatchChatsText = 'oc_chat_1, oc_chat_2'

  assert.equal(await state.saveAiCommand(), true)
  await flush()
  assert.equal(state.aiEdit.provider, 'codex')
  assert.equal(state.aiEdit.model, 'gpt-5.4')
  assert.equal(state.aiEdit.reasoning_effort, 'xhigh')
  assert.deepEqual([...state.aiEdit.watch_chats], ['oc_chat_1', 'oc_chat_2'])
  assert.equal(state.success, '/ai command settings saved.')

  assert.equal(await state.importAiToken(), true)
  await flush()
  assert.match(state.success, /Imported user token/)
})

test('Personal /ai Assistant blocks save before initial hydration and preserves unsaved draft edits during token import', async t => {
  const h = harness(t)
  const pendingLoad = deferred()
  let failLoad = true
  const persistedConfig = {
    enabled: true,
    prefix: '/ai',
    owner_open_id: '',
    app_id: '',
    provider: 'maso',
    model: '[redacted]_reasoning_high',
    reasoning_effort: 'high',
    cwd: '/srv/persisted',
    watch_chats: ['oc_persisted'],
    poll_interval_seconds: 20,
    max_active_chats: 10,
    max_context_messages: 20,
    ack_emoji: 'OnIt',
    done_emoji: 'DONE',
    footer: '> 🤖 AI 代答，仅供参考',
    reply_in_thread: true,
  }
  const makePayload = (cfgOverrides = {}, imported = false) => ({
    config: { ...persistedConfig, ...cfgOverrides },
    token_status: {
      has_token: true,
      open_id: 'ou_imported',
      user_name: 'Imported Owner',
      app_id: 'cli_imported',
      source: 'lark_cli',
      expires_at: 9999999999,
      token_expired: false,
      granted_scopes: ['im:message', 'im:message.send_as_user'],
      required_send_scope: 'im:message.send_as_user',
      can_send_as_user: true,
      can_react_as_user: true,
      can_read_messages: true,
      missing_required_scopes: [],
      missing_recommended_scopes: [],
      auth_login_command: 'lark-cli auth login --scope "im:message.send_as_user"',
      remediation_steps: [],
    },
    poller_status: {
      running: true,
      poll_count: 1,
      last_poll_at: 1000,
      last_error: null,
      commands_detected: 0,
      commands_succeeded: 0,
      commands_failed: 0,
      skipped_count: 0,
      watermarks_count: 1,
    },
    capabilities: {
      providers: ['claude', 'codex', 'traex', 'maso', 'cursor', 'antigravity'],
      reasoning_efforts: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
      provider_reasoning_efforts: {
        claude: [],
        codex: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'],
        maso: ['none', 'low', 'medium', 'high', 'xhigh', 'max'],
      },
      provider_models: {
        codex: ['gpt-5.4'],
        maso: ['[redacted]_reasoning_high'],
      },
    },
    imported,
  })

  let getCount = 0
  h.setTransport(async (url, init = {}) => {
    if (String(url).includes('/api/feishu/bot/ai-command/import-token')) {
      return response(makePayload({ owner_open_id: 'ou_imported', app_id: 'cli_imported' }, true))
    }
    if (String(url).includes('/api/feishu/bot/ai-command')) {
      if (init.method === 'PATCH') {
        const body = JSON.parse(init.body)
        return response(makePayload(body))
      }
      getCount += 1
      if (getCount === 1) {
        await pendingLoad.promise
        if (failLoad) {
          return response({ detail: 'ai_command_unavailable' }, 500)
        }
      }
      return response(makePayload())
    }
    return response(h.server())
  })

  const state = await mountSettings(t, h)
  const openPromise = state.openAiCommand()
  await flush()
  assert.equal(state.aiCommandLoading, true)
  assert.equal(state.aiCommandLoaded, false)
  // Cannot save while initial GET is still in flight
  assert.equal(await state.saveAiCommand(), false)
  assert.equal(
    h.calls.some(call => call.init.method === 'PATCH' && String(call.url).includes('/ai-command')),
    false,
  )

  // Fail initial GET: save remains blocked until retry succeeds
  pendingLoad.resolve()
  await openPromise
  await flush()
  assert.equal(state.aiCommandLoading, false)
  assert.equal(state.aiCommandLoaded, false)
  assert.ok(state.aiCommandLoadError)
  assert.equal(await state.saveAiCommand(), false)

  // Retry succeeds and hydrates persisted server values
  await state.retryLoadAiCommand()
  await flush()
  assert.equal(state.aiCommandLoaded, true)
  assert.equal(state.aiCommandLoadError, null)
  assert.equal(state.aiEdit.provider, 'maso')
  assert.equal(state.aiEdit.model, '[redacted]_reasoning_high')
  assert.equal(state.aiEdit.cwd, '/srv/persisted')
  assert.equal(state.aiWatchChatsText, 'oc_persisted')

  // Make unsaved draft edits, then import ~/.lark-cli token: draft edits must be preserved
  state.onAiProviderChange('codex')
  state.aiEdit.model = 'o3'
  state.aiEdit.reasoning_effort = 'low'
  state.aiEdit.cwd = '/srv/draft-cwd'
  state.aiWatchChatsText = 'oc_draft_1, oc_draft_2'
  await flush()

  assert.equal(await state.importAiToken(), true)
  await flush()
  assert.equal(state.aiEdit.provider, 'codex')
  assert.equal(state.aiEdit.model, 'o3')
  assert.equal(state.aiEdit.reasoning_effort, 'low')
  assert.equal(state.aiEdit.cwd, '/srv/draft-cwd')
  assert.equal(state.aiWatchChatsText, 'oc_draft_1, oc_draft_2')
  // Untouched blank identity fields populate from the imported token
  assert.equal(state.aiEdit.owner_open_id, 'ou_imported')
  assert.equal(state.aiEdit.app_id, 'cli_imported')
})

test('Personal /ai Assistant handles Reload during initial GET, re-entry draft preservation, coherent provider tuple conflict, and backend validation detail', async t => {
  const h = harness(t)
  const pendingInitial = deferred()
  const pendingReopen = deferred()
  const baseCfg = {
    enabled: false,
    prefix: '/ai',
    owner_open_id: 'ou_owner',
    app_id: 'cli_app',
    provider: 'codex',
    model: 'gpt-5.4',
    reasoning_effort: 'high',
    cwd: '/srv',
    watch_chats: [],
    poll_interval_seconds: 15,
    max_active_chats: 10,
    max_context_messages: 20,
    ack_emoji: 'OnIt',
    done_emoji: 'DONE',
    footer: '> 🤖 AI 代答，仅供参考',
    reply_in_thread: true,
  }
  let currentCfg = { ...baseCfg }
  const makeStatus = (overrides = {}) => ({
    config: { ...currentCfg, ...overrides },
    capabilities: {
      providers: ['codex', 'claude', 'maso'],
      provider_models: {
        codex: ['gpt-5.4', 'o3'],
        claude: ['[redacted]'],
        maso: ['[redacted]_reasoning_high'],
      },
      reasoning_efforts: ['none', 'low', 'medium', 'high', 'xhigh', 'max'],
      provider_reasoning_efforts: {
        codex: ['low', 'high', 'xhigh'],
        claude: [],
        maso: ['none', 'low', 'medium', 'high', 'xhigh', 'max'],
      },
      model_reasoning_efforts: {
        '[redacted]_reasoning_high': ['none', 'low', 'medium', 'high'],
      },
    },
  })

  let aiGets = 0
  h.setTransport(async (url, init = {}) => {
    if (String(url).endsWith('/ai-command')) {
      if (init.method === 'PATCH') {
        const body = JSON.parse(init.body)
        if (body.provider === 'maso' && body.reasoning_effort === 'xhigh') {
          return response({
            detail: {
              code: 'invalid_ai_command_config',
              message: "MASO model '[redacted]' does not support reasoning_effort='xhigh'; supported efforts: none, low, medium, high",
            },
          }, 400)
        }
        currentCfg = { ...currentCfg, ...body }
        return response(makeStatus())
      }
      aiGets += 1
      if (aiGets === 1) return pendingInitial.promise
      if (aiGets === 3) return pendingReopen.promise
      return response(makeStatus())
    }
    return response(h.server())
  })

  const state = await mountSettings(t, h)
  // 1. R2-07a: Reload during initial GET settles loading cleanly and allows saving
  const opening = state.openAiCommand()
  await flush()
  assert.equal(state.aiCommandLoading, true)
  await state.refresh()
  await flush()
  pendingInitial.resolve(response(makeStatus()))
  await opening
  await flush()
  assert.equal(state.aiCommandLoaded, true)
  assert.equal(state.aiCommandLoading, false)
  assert.equal(findByTestId(state.$root, 'feishu-ai-save-btn')?.props.disabled, false)

  // 2. R2-07b: Re-entering /ai while already loaded preserves draft edits made during GET
  const reopening = state.openAiCommand()
  await flush()
  state.aiEdit.model = 'o3'
  pendingReopen.resolve(response(makeStatus()))
  await reopening
  await flush()
  assert.equal(state.aiEdit.model, 'o3')
  // Reset model to server snapshot before testing effort-only edit
  state.aiEdit.model = 'gpt-5.4'

  // 3. R2-07c: Editing effort then refreshing after external provider change keeps coherent tuple
  state.aiEdit.reasoning_effort = 'low'
  currentCfg = { ...baseCfg, provider: 'claude', model: '[redacted]', reasoning_effort: '' }
  await state.refresh()
  await flush()
  assert.equal(state.aiEdit.provider, 'codex')
  assert.equal(state.aiEdit.model, 'gpt-5.4')
  assert.equal(state.aiEdit.reasoning_effort, 'low')
  assert.equal(findByTestId(state.$root, 'feishu-ai-effort-select')?.props.disabled, false)
  assert.ok(state.aiTupleConflict)
  assert.equal(state.aiTupleConflict.provider, 'claude')
  // User can explicitly adopt server values from the conflict banner
  state.acceptServerAiTuple()
  await flush()
  assert.equal(state.aiTupleConflict, null)
  assert.equal(state.aiEdit.provider, 'claude')
  assert.equal(state.aiEdit.model, '[redacted]')
  assert.equal(state.aiEdit.reasoning_effort, '')

  // 4. Actionable backend validation message is preserved on 400 invalid_ai_command_config
  state.onAiProviderChange('maso')
  await flush()
  // Model-aware effort filtering limits [redacted]_reasoning_high to none/low/medium/high
  assert.deepEqual([...state.aiEffortOptions], ['none', 'low', 'medium', 'high'])
  state.aiEdit.reasoning_effort = 'xhigh'
  assert.equal(await state.saveAiCommand(), false)
  await flush()
  assert.match(state.error, /MASO model '[redacted]' does not support reasoning_effort='xhigh'/)
})

test('Personal /ai navigation retires pending load and conflict adoption respects busy lock', async t => {
  const h = harness(t)
  const pendingLoad = deferred()
  const pendingSave = deferred()
  let currentCfg = {
    enabled: false,
    prefix: '/ai',
    owner_open_id: 'ou_owner',
    app_id: 'cli_app',
    provider: 'codex',
    model: 'gpt-5.4',
    reasoning_effort: 'high',
    cwd: '/srv',
    watch_chats: [],
    poll_interval_seconds: 15,
    max_active_chats: 10,
    max_context_messages: 20,
    ack_emoji: 'OnIt',
    done_emoji: 'DONE',
    footer: 'footer',
    reply_in_thread: true,
  }
  const makeStatus = () => ({
    config: { ...currentCfg },
    capabilities: {
      providers: ['codex', 'claude'],
      provider_models: { codex: ['gpt-5.4'], claude: ['[redacted]'] },
      reasoning_efforts: ['low', 'high'],
      provider_reasoning_efforts: { codex: ['low', 'high'], claude: [] },
    },
  })
  let getCount = 0
  let submittedBody = null
  h.setTransport(async (url, init = {}) => {
    if (String(url).endsWith('/ai-command')) {
      if (init.method === 'PATCH') {
        submittedBody = JSON.parse(init.body)
        return pendingSave.promise
      }
      getCount += 1
      return getCount === 1 ? pendingLoad.promise : response(makeStatus())
    }
    return response(h.server())
  })

  const state = await mountSettings(t, h)
  // R3-05a: Navigating away to a Bot during initial /ai GET retires aiCommandLoading immediately
  const opening = state.openAiCommand()
  await flush()
  assert.equal(state.aiCommandLoading, true)
  state.select(h.store.botById('b'))
  await flush()
  assert.equal(state.selectedId, 'b')
  assert.equal(state.aiCommandActive, false)
  assert.equal(state.aiCommandLoading, false)
  pendingLoad.resolve(response(makeStatus()))
  await opening
  await flush()
  assert.equal(state.aiCommandLoading, false)
  assert.equal(state.aiCommandLoaded, false)

  // R3-05b: Use server values button is disabled and guarded while Save is in flight
  await state.openAiCommand()
  await flush()
  assert.equal(state.aiCommandLoaded, true)
  state.aiEdit.reasoning_effort = 'low'
  currentCfg = { ...currentCfg, provider: 'claude', model: '[redacted]', reasoning_effort: '' }
  await state.refresh()
  await flush()
  assert.ok(state.aiTupleConflict)

  const saving = state.saveAiCommand()
  await flush()
  assert.equal(state.busy, true)
  const acceptBtn = findByTestId(state.$root, 'feishu-ai-accept-server-tuple-btn')
  assert.equal(Boolean(acceptBtn?.props.disabled), true)
  state.acceptServerAiTuple()
  await flush()
  // Guarded while busy: conflict remains until settled or user clicks after Save finishes
  assert.equal(state.aiEdit.provider, 'codex')
  assert.ok(state.aiTupleConflict)

  currentCfg = { ...currentCfg, ...submittedBody }
  pendingSave.resolve(response(makeStatus()))
  await saving
  await flush()
  assert.equal(state.busy, false)
  assert.equal(state.aiEdit.provider, 'codex')
  assert.equal(state.aiEdit.reasoning_effort, 'low')
})

test('Personal /ai Assistant truthfully renders user mode, bot fallback with distinct user/bot identities, and unavailable fallback guidance', async t => {
  const h = harness(t)
  const baseCfg = {
    enabled: false,
    prefix: '/ai',
    owner_open_id: 'ou_owner_fb',
    app_id: 'cli_bot_fb',
    provider: 'claude',
    model: '[redacted]',
    reasoning_effort: '',
    cwd: '/srv',
    watch_chats: ['oc_chat_fb'],
    poll_interval_seconds: 15,
    max_active_chats: 10,
    max_context_messages: 20,
    ack_emoji: 'OnIt',
    done_emoji: 'DONE',
    footer: '> 🤖 AI 代答，仅供参考',
    reply_in_thread: true,
  }
  let tokenStatus = {
    has_token: true,
    open_id: 'ou_owner_fb',
    user_name: '[redacted]',
    app_id: 'cli_user_app',
    source: 'lark_cli',
    expires_at: 9999999999,
    token_expired: false,
    granted_scopes: ['im:message', 'im:message.reactions:write_only'],
    required_send_scope: 'im:message.send_as_user',
    can_send: false,
    can_send_as_user: false,
    has_send_as_user_scope: false,
    can_send_via_bot: false,
    fallback_bot_app_id: null,
    send_mode: 'none',
    can_react_as_user: true,
    can_read_messages: true,
    missing_required_scopes: ['im:message.send_as_user'],
    missing_recommended_scopes: [],
    auth_login_command: 'lark-cli auth login --scope "im:message im:message.reactions:write_only"',
    remediation_steps: [
      '1. Configure app secret for `cli_user_app` (or enable `im:message.send_as_user` in Feishu Developer Console for `cli_user_app`).',
    ],
  }
  h.setTransport(async url => {
    if (String(url).includes('/api/feishu/bot/ai-command')) {
      return response({
        config: { ...baseCfg },
        token_status: { ...tokenStatus },
        poller_status: {
          running: false,
          poll_count: 1,
          last_poll_at: 1000,
          last_error: null,
          commands_detected: 0,
          commands_succeeded: 0,
          commands_failed: 0,
          skipped_count: 0,
          watermarks_count: 0,
        },
        capabilities: {
          providers: ['claude', 'codex'],
          provider_models: { claude: ['[redacted]'], codex: ['gpt-5.4'] },
          reasoning_efforts: ['low', 'high'],
          provider_reasoning_efforts: { claude: [], codex: ['low', 'high'] },
        },
      })
    }
    return response(h.server())
  })

  const state = await mountSettings(t, h)
  await state.openAiCommand()
  await flush()

  // 1. Unavailable fallback state: failed card, Missing send_as_user chip, remediation guidance + login command visible
  assert.equal(state.aiTokenCanSend, false)
  assert.equal(state.aiTokenChipLabel, 'Missing send_as_user')
  assert.equal(
    state.aiTokenSummary,
    '[redacted] (ou_owner_fb) · Missing required scope: im:message.send_as_user (and no bot fallback configured)',
  )
  assert.equal(
    state.aiTokenGuidance,
    '1. Configure app secret for `cli_user_app` (or enable `im:message.send_as_user` in Feishu Developer Console for `cli_user_app`).',
  )
  assert.equal(state.aiSidebarMeta, 'Personal account reply')
  assert.equal(findByTestId(state.$root, 'feishu-ai-token-card')?.props['data-state'], 'failed')
  assert.ok(findByTestId(state.$root, 'feishu-ai-token-guidance'))
  assert.ok(findByTestId(state.$root, 'feishu-ai-token-login-cmd'))

  // 2. Bot fallback state with distinct user open_id and fallback_bot_app_id
  tokenStatus = {
    ...tokenStatus,
    can_send: true,
    can_send_as_user: false,
    has_send_as_user_scope: false,
    can_send_via_bot: true,
    fallback_bot_app_id: 'cli_bot_fallback_99',
    send_mode: 'bot_fallback',
    remediation_steps: [
      'Ready via bot fallback (`cli_bot_fallback_99`): user token reads context and reacts as ou_owner_fb.',
    ],
  }
  await state.refresh()
  await flush()
  assert.equal(state.aiTokenCanSend, true)
  assert.equal(state.aiTokenChipLabel, 'Bot fallback (cli_bot_fallback_99)')
  assert.equal(
    state.aiTokenSummary,
    'Ready (bot fallback: cli_bot_fallback_99): [redacted] (ou_owner_fb) · im:message.send_as_user not granted',
  )
  assert.equal(state.aiSidebarMeta, 'Bot fallback (cli_bot_fallback_99)')
  assert.equal(state.aiTokenGuidance, '')
  assert.equal(findByTestId(state.$root, 'feishu-ai-token-card')?.props['data-state'], 'connected')
  assert.equal(findByTestId(state.$root, 'feishu-ai-token-guidance'), null)
  assert.equal(findByTestId(state.$root, 'feishu-ai-token-login-cmd'), null)

  // 3. Direct user scope state (im:message.send_as_user granted)
  tokenStatus = {
    ...tokenStatus,
    granted_scopes: ['im:message', 'im:message.send_as_user'],
    can_send: true,
    can_send_as_user: true,
    has_send_as_user_scope: true,
    can_send_via_bot: true,
    send_mode: 'user',
    missing_required_scopes: [],
  }
  await state.refresh()
  await flush()
  assert.equal(state.aiTokenCanSend, true)
  assert.equal(state.aiTokenChipLabel, 'Scope ready')
  assert.equal(
    state.aiTokenSummary,
    'Ready: [redacted] (ou_owner_fb) · im:message.send_as_user granted',
  )
  assert.equal(state.aiSidebarMeta, 'Personal account reply')
  assert.equal(findByTestId(state.$root, 'feishu-ai-token-card')?.props['data-state'], 'connected')
})

test('Personal /ai Model selector renders Provider-linked logical options, maps persisted variants without false dirty state, syncs efforts, and preserves custom models', async t => {
  const h = harness(t)
  let serverCfg = {
    enabled: true,
    prefix: '/ai',
    owner_open_id: 'ou_owner',
    app_id: 'cli_owner',
    provider: 'maso',
    model: '[redacted]_reasoning_high',
    reasoning_effort: 'high',
    cwd: '/srv/ai',
    watch_chats: [],
    poll_interval_seconds: 15,
    max_active_chats: 10,
    max_context_messages: 20,
    ack_emoji: 'OnIt',
    done_emoji: 'DONE',
    footer: '> 🤖 AI 代答，仅供参考',
    reply_in_thread: true,
  }
  const capabilities = {
    providers: ['claude', 'codex', 'traex', 'maso', 'cursor', 'antigravity'],
    reasoning_efforts: ['default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
    provider_reasoning_efforts: {
      claude: [],
      codex: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
      traex: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
      maso: ['default', 'none', 'low', 'medium', 'high', 'xhigh', 'max'],
      cursor: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'],
      antigravity: [],
    },
    provider_models: {
      claude: ['claude-opus-4-8', '[redacted]'],
      codex: ['gpt-5.4', '[redacted]'],
      maso: ['[redacted]_reasoning_high', '[redacted]_reasoning_high'],
      cursor: ['gpt-5.3-codex-high', 'claude-4.5-sonnet'],
      antigravity: ['[redacted]-high'],
    },
    provider_model_options: {
      claude: [
        { id: 'claude-opus-4-8', label: 'claude-opus-4-8', provider_model_id: 'claude-opus-4-8' },
        { id: '[redacted]', label: '[redacted]', provider_model_id: '[redacted]' },
      ],
      codex: [
        {
          id: 'gpt-5.4',
          label: 'gpt-5.4',
          provider_model_id: 'gpt-5.4',
          default_reasoning_effort: 'high',
          supported_reasoning_efforts: [
            { id: 'low', label: 'Low', provider_model_id: 'gpt-5.4' },
            { id: 'high', label: 'High', provider_model_id: 'gpt-5.4' },
            { id: 'xhigh', label: 'Extra High', provider_model_id: 'gpt-5.4' },
          ],
        },
        {
          id: '[redacted]',
          label: '[redacted]',
          provider_model_id: '[redacted]',
          default_reasoning_effort: 'high',
          supported_reasoning_efforts: [
            { id: 'medium', label: 'Medium', provider_model_id: '[redacted]' },
            { id: 'high', label: 'High', provider_model_id: '[redacted]' },
            { id: 'max', label: 'Max', provider_model_id: '[redacted]' },
          ],
        },
      ],
      maso: [
        {
          id: '[redacted]',
          label: '[redacted]',
          provider_model_id: '[redacted]',
          default_reasoning_effort: 'default',
          supported_reasoning_efforts: [
            { id: 'default', label: 'Default', provider_model_id: '[redacted]' },
            { id: 'high', label: 'High', provider_model_id: '[redacted]_reasoning_high' },
            { id: 'xhigh', label: 'Extra High', provider_model_id: '[redacted]_reasoning_xhigh' },
            { id: 'max', label: 'Max', provider_model_id: '[redacted]_reasoning_max' },
          ],
        },
        {
          id: '[redacted]',
          label: '[redacted]',
          provider_model_id: '[redacted]',
          default_reasoning_effort: 'low',
          supported_reasoning_efforts: [
            { id: 'low', label: 'Low', provider_model_id: '[redacted]' },
            { id: 'medium', label: 'Medium', provider_model_id: '[redacted]_reasoning_medium' },
            { id: 'high', label: 'High', provider_model_id: '[redacted]_reasoning_high' },
          ],
        },
      ],
      cursor: [
        {
          id: 'gpt-5.3-codex',
          label: 'gpt-5.3-codex',
          provider_model_id: 'gpt-5.3-codex-high',
          default_reasoning_effort: 'high',
          supported_reasoning_efforts: [
            { id: 'low', label: 'Low', provider_model_id: 'gpt-5.3-codex-low' },
            { id: 'high', label: 'High', provider_model_id: 'gpt-5.3-codex-high' },
            { id: 'xhigh', label: 'Extra High', provider_model_id: 'gpt-5.3-codex-xhigh' },
          ],
        },
        {
          id: 'claude-4.5-sonnet',
          label: 'claude-4.5-sonnet',
          provider_model_id: 'claude-4.5-sonnet',
          supported_reasoning_efforts: [],
        },
      ],
      antigravity: [
        { id: '[redacted]-high', label: '[redacted]-high', provider_model_id: '[redacted]-high' },
      ],
    },
  }

  let savedBody = null
  function normalizeServerAiPatch(prev, patch) {
    const next = { ...prev, ...patch }
    const prov = String(next.provider || 'codex')
    const model = String(next.model ?? '').trim()
    let effort = String(next.reasoning_effort ?? '').trim().toLowerCase()
    if (prov === 'claude' || prov === 'antigravity') {
      if (effort) throw new Error(`Provider ${prov} does not support reasoning_effort`)
      return { ...next, model, reasoning_effort: '' }
    }
    if (prov === 'maso' || prov === 'cursor') {
      if (!model) {
        return { ...next, model: '', reasoning_effort: '' }
      }
      const catalog = capabilities.provider_model_options[prov] || []
      const opt = catalog.find(
        item => item.id === model
          || item.provider_model_id === model
          || item.supported_reasoning_efforts?.some(e => e.provider_model_id === model),
      )
      if (effort && opt?.supported_reasoning_efforts?.length) {
        const variant = opt.supported_reasoning_efforts.find(e => e.id === effort)
        if (!variant) throw new Error(`${prov} model ${opt.id} does not support effort=${effort}`)
        return { ...next, model: variant.provider_model_id || model, reasoning_effort: effort }
      }
      return { ...next, model, reasoning_effort: effort }
    }
    return { ...next, model, reasoning_effort: effort }
  }
  h.setTransport(async (url, init = {}) => {
    if (String(url).endsWith('/ai-command')) {
      if (init.method === 'PATCH') {
        savedBody = JSON.parse(init.body)
        serverCfg = normalizeServerAiPatch(serverCfg, savedBody)
      }
      return response({
        config: { ...serverCfg },
        capabilities,
      })
    }
    return response(h.server())
  })

  const state = await mountSettings(t, h)
  await state.openAiCommand()
  await flush()

  // 1. Model control is a <select>, shows logical labels (not duplicate _reasoning_* rows), and maps persisted variant cleanly
  const modelSelect = findByTestId(state.$root, 'feishu-ai-model-input')
  assert.equal(modelSelect?.tag, 'select')
  assert.deepEqual(
    state.aiModelSelectOptions.map(o => o.label),
    ['[redacted]', '[redacted]'],
  )
  assert.equal(state.aiCustomModelOption, null)
  assert.equal(state.aiSidebarMeta, 'maso · [redacted]')
  assert.deepEqual([...state.aiEffortOptions], ['low', 'medium', 'high'])

  // 2. Hydrating persisted variant did NOT mark server tuple dirty: external server change adopts cleanly without conflict
  serverCfg = { ...serverCfg, model: '[redacted]_reasoning_medium', reasoning_effort: 'medium' }
  await state.refresh()
  await flush()
  assert.equal(state.aiTupleConflict, null)
  assert.equal(state.aiEdit.model, '[redacted]_reasoning_medium')
  assert.equal(state.aiEdit.reasoning_effort, 'medium')

  // 3. Changing effort on MASO updates variant provider_model_id; switching to live-shaped [redacted]
  //    supports mode='default' ('[redacted]') across selection -> PATCH -> reload as well as xhigh/max!
  state.onAiEffortChange('low')
  await flush()
  assert.equal(state.aiEdit.model, '[redacted]')
  assert.equal(state.aiEdit.reasoning_effort, 'low')

  state.onAiModelChange('[redacted]')
  await flush()
  assert.deepEqual([...state.aiEffortOptions], ['default', 'high', 'xhigh', 'max'])
  assert.equal(state.aiEdit.model, '[redacted]')
  assert.equal(state.aiEdit.reasoning_effort, 'default')
  assert.equal(await state.saveAiCommand(), true)
  assert.deepEqual(
    { provider: savedBody.provider, model: savedBody.model, reasoning_effort: savedBody.reasoning_effort },
    { provider: 'maso', model: '[redacted]', reasoning_effort: 'default' },
  )
  await state.openAiCommand()
  await flush()
  assert.equal(state.aiTupleConflict, null)
  assert.equal(state.aiEdit.model, '[redacted]')
  assert.equal(state.aiEdit.reasoning_effort, 'default')

  state.onAiEffortChange('xhigh')
  await flush()
  assert.equal(state.aiEdit.model, '[redacted]_reasoning_xhigh')
  assert.equal(state.aiEdit.reasoning_effort, 'xhigh')

  // 3b. MASO Provider default ('') from [redacted]_reasoning_xhigh + xhigh clears model-bound effort,
  //     disables effort select, saves ('maso', '', ''), and reloads cleanly without conflict or argon fallback
  state.onAiModelChange('')
  await flush()
  assert.equal(state.aiEdit.model, '')
  assert.equal(state.aiEdit.reasoning_effort, '')
  assert.deepEqual([...state.aiEffortOptions], [])
  assert.equal(findByTestId(state.$root, 'feishu-ai-effort-select')?.props.disabled, true)
  assert.equal(await state.saveAiCommand(), true)
  assert.deepEqual(
    { provider: savedBody.provider, model: savedBody.model, reasoning_effort: savedBody.reasoning_effort },
    { provider: 'maso', model: '', reasoning_effort: '' },
  )
  await state.openAiCommand()
  await flush()
  assert.equal(state.aiTupleConflict, null)
  assert.equal(state.aiEdit.provider, 'maso')
  assert.equal(state.aiEdit.model, '')
  assert.equal(state.aiEdit.reasoning_effort, '')
  assert.equal(state.aiSidebarMeta, 'maso · default')

  // Re-selecting [redacted] restores xhigh and unlocks Cursor switch test below
  state.onAiModelChange('[redacted]_reasoning_high')
  state.onAiEffortChange('xhigh')
  await flush()

  // 4. Cursor effort-capable model preserves compatible xhigh effort as gpt-5.3-codex-xhigh; non-effort model and Provider default disable effort select and clear effort
  state.onAiProviderChange('cursor')
  await flush()
  assert.equal(state.aiEdit.model, 'gpt-5.3-codex-xhigh')
  assert.equal(state.aiEdit.reasoning_effort, 'xhigh')
  assert.deepEqual([...state.aiEffortOptions], ['low', 'high', 'xhigh'])
  assert.equal(findByTestId(state.$root, 'feishu-ai-effort-select')?.props.disabled, false)

  // Cursor Provider default ('') -> PATCH -> reload persists ('cursor', '', ''), not gpt-5.3-codex-high
  state.onAiModelChange('')
  await flush()
  assert.equal(state.aiEdit.model, '')
  assert.equal(state.aiEdit.reasoning_effort, '')
  assert.equal(findByTestId(state.$root, 'feishu-ai-effort-select')?.props.disabled, true)
  assert.equal(await state.saveAiCommand(), true)
  assert.deepEqual(
    { provider: savedBody.provider, model: savedBody.model, reasoning_effort: savedBody.reasoning_effort },
    { provider: 'cursor', model: '', reasoning_effort: '' },
  )
  await state.openAiCommand()
  await flush()
  assert.equal(state.aiEdit.model, '')
  assert.equal(state.aiEdit.reasoning_effort, '')

  state.onAiModelChange('claude-4.5-sonnet')
  await flush()
  assert.equal(state.aiEdit.model, 'claude-4.5-sonnet')
  assert.equal(state.aiEdit.reasoning_effort, '')
  assert.deepEqual([...state.aiEffortOptions], [])
  assert.equal(findByTestId(state.$root, 'feishu-ai-effort-select')?.props.disabled, true)

  // 5. Custom/unlisted model ID is preserved via custom/current option and Codex Provider default ('') preserves ENV_KEY effort across save/reload
  serverCfg = { ...serverCfg, provider: 'codex', model: 'my-custom-codex-preview', reasoning_effort: 'high' }
  await state.openAiCommand()
  // Draft was dirty from cursor edit, accept server tuple to inspect custom model hydration
  state.acceptServerAiTuple()
  await flush()
  assert.equal(state.aiEdit.provider, 'codex')
  assert.equal(state.aiEdit.model, 'my-custom-codex-preview')
  assert.deepEqual(state.aiCustomModelOption, {
    value: 'my-custom-codex-preview',
    label: 'my-custom-codex-preview (custom/current)',
  })

  state.onAiModelChange('')
  await flush()
  assert.equal(state.aiEdit.model, '')
  assert.equal(state.aiEdit.reasoning_effort, 'high')
  assert.equal(findByTestId(state.$root, 'feishu-ai-effort-select')?.props.disabled, false)
  assert.equal(await state.saveAiCommand(), true)
  assert.deepEqual(
    { provider: savedBody.provider, model: savedBody.model, reasoning_effort: savedBody.reasoning_effort },
    { provider: 'codex', model: '', reasoning_effort: 'high' },
  )
  await state.openAiCommand()
  await flush()
  assert.equal(state.aiEdit.model, '')
  assert.equal(state.aiEdit.reasoning_effort, 'high')

  state.onAiModelChange('[redacted]')
  state.onAiEffortChange('max')
  await flush()
  assert.equal(await state.saveAiCommand(), true)
  assert.equal(savedBody.provider, 'codex')
  assert.equal(savedBody.model, '[redacted]')
  assert.equal(savedBody.reasoning_effort, 'max')
})
