import assert from 'node:assert/strict'
import test from 'node:test'
import { computed, ref } from 'vue'
import { loadChatFeature } from './chatPresentationTestHarness.mjs'

const { useChatComposer } = await loadChatFeature('controllers/useChatComposer')
const { useChatDelivery } = await loadChatFeature('controllers/useChatDelivery')

const approval = (callId = 'call-current') => ({
  key: 'card-current',
  callId,
  kind: 'ask_question',
  title: 'Pick one',
  questions: [{
    id: 'q1', prompt: 'Pick one', allowMultiple: false,
    options: [{ id: 'red', label: 'Red' }, { id: 'blue', label: 'Blue' }],
  }],
})

function makeComposer({ managed = true, inFlight = true } = {}) {
  const calls = []
  let queued = 0
  const deps = {
    attachments: ref([]),
    closeImageLightbox() {},
    closeModeMenu() {},
    closeModelMenu() {},
    composerError: ref(null),
    draftMessage: ref('durable follow-up'),
    draftQueue: ref([]),
    enqueueDraft() { queued += 1 },
    goalComposerReason: ref(null),
    goalInputBlocked: ref(false),
    goalSteerOnly: ref(false),
    imageLightboxUrl: ref(null),
    isManagedChat: ref(managed),
    isModeMenuOpen: ref(false),
    isModelMenuOpen: ref(false),
    isPaneActivated: ref(true),
    isSending: ref(false),
    lightboxEl: ref(null),
    async sendQueuedDraft() {},
    async submit(...args) { calls.push(args); return true },
    turnInFlight: ref(inFlight),
  }
  return { controller: useChatComposer(deps), deps, calls, queued: () => queued }
}

test('managed question submission carries exact card identity as a normal delivery', async () => {
  const { controller, calls } = makeComposer()
  const card = approval()
  controller.toggleQuestionOption(card.key, 'q1', 'red', false)

  await controller.submitQuestionResponse({ turnId: 'turn-current' }, card)

  assert.equal(calls.length, 1)
  assert.equal(calls[0][0], 'normal')
  assert.equal(calls[0][2], 'user')
  assert.deepEqual(calls[0][3], { turnId: 'turn-current', callId: 'call-current' })
  assert.equal(controller.isApprovalResolved(card), true)
})

test('question submission fails closed when provider identity is incomplete', async () => {
  const { controller, deps, calls } = makeComposer()
  const card = approval('')
  controller.toggleQuestionOption(card.key, 'q1', 'red', false)

  await controller.submitQuestionResponse({ turnId: 'turn-current' }, card)

  assert.equal(calls.length, 0)
  assert.match(deps.composerError.value, /missing its current request identity/)
})

test('managed busy Enter and Ctrl+Enter durably submit instead of using local queue', () => {
  const state = makeComposer()
  let prevented = 0
  const event = ctrlKey => ({
    isComposing: false,
    shiftKey: false,
    metaKey: false,
    ctrlKey,
    altKey: false,
    preventDefault() { prevented += 1 },
  })

  state.controller.handleComposerEnter(event(false))
  state.controller.handleComposerEnter(event(true))
  assert.deepEqual(state.calls.map(call => call[0]), ['normal', 'normal'])
  assert.equal(state.queued(), 0)
  assert.equal(prevented, 2)

  state.deps.isManagedChat.value = false
  state.controller.handleComposerEnter(event(false))
  state.controller.handleComposerEnter(event(true))
  assert.equal(state.queued(), 1)
  assert.deepEqual(state.calls.map(call => call[0]), ['normal', 'normal', 'steer'])
})

function makeManagedDelivery() {
  let nudges = 0
  let statusRefreshes = 0
  const controller = useChatDelivery({
    attachments: ref([]),
    authoritativeTurns: computed(() => [{ turnId: 'turn-current', completed: false, errors: [] }]),
    connectionState: ref('live'),
    draftQueue: ref([]),
    enqueueDraft() {},
    events: ref([]),
    goal: ref(null),
    goalComposerReason: ref(null),
    goalError: ref(null),
    goalImageRecovery: ref(false),
    goalInputBlocked: ref(false),
    goalSteerOnly: ref(false),
    async hydrateGoal() {},
    isDraftQueuePaused: ref(false),
    isGoalHydrating: ref(false),
    isGoalMutating: ref(false),
    isManagedChat: ref(true),
    isPreparingAttachments: ref(false),
    isUpdatingMode: ref(false),
    isUpdatingModel: ref(false),
    isUpdatingReasoningEffort: ref(false),
    nudge() { nudges += 1 },
    requestLatestAnchor() {},
    streamBasePath: ref('/api/workspaces/sessions/managed-1/stream'),
    syncComposerTextareaHeight() {},
    terminalStore: { async fetchAgentStatuses() { statusRefreshes += 1 } },
  })
  return { controller, nudges: () => nudges, statusRefreshes: () => statusRefreshes }
}

test('stale managed Stop remains a conflict and cannot cancel a newer turn', async t => {
  const state = makeManagedDelivery()
  t.mock.method(globalThis, 'fetch', async () => ({
    ok: false, status: 409, json: async () => ({ detail: 'A newer managed Chat turn is active' }),
  }))

  await state.controller.cancelActiveTurn()

  assert.match(state.controller.composerError.value, /newer managed Chat turn/)
  assert.equal(state.controller.optimisticCancelledTurnId.value, null)
  assert.equal(state.nudges(), 0)
  assert.equal(state.statusRefreshes(), 0)
})

test('already-terminal managed Stop is idempotent and releases local UI optimism', async t => {
  const state = makeManagedDelivery()
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/workspaces/sessions/managed-1/stream/cancel')
    assert.deepEqual(JSON.parse(options.body), { expected_turn_id: 'turn-current' })
    return {
      ok: true, status: 200,
      json: async () => ({ ok: true, cancelled: false, outcome: 'already_terminal' }),
    }
  })

  await state.controller.cancelActiveTurn()
  await Promise.resolve()

  assert.equal(state.controller.composerError.value, null)
  assert.equal(state.controller.optimisticCancelledTurnId.value, 'turn-current')
  assert.equal(state.nudges(), 1)
  assert.equal(state.statusRefreshes(), 1)
})
