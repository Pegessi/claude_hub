import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, ref } from 'vue'
import { loadChatFeature } from './chatPresentationTestHarness.mjs'

const structuredPane = readFileSync(
  new URL('../src/features/chat/components/ChatTurn.vue', import.meta.url),
  'utf8',
)
const userMessage = readFileSync(
  new URL('../src/features/chat/components/ChatUserMessage.vue', import.meta.url),
  'utf8',
)
const { useChatActions } = await loadChatFeature('controllers/useChatActions')

// ---------------------------------------------------------------------------
// Edit-resend guard: while a turn is running (turnInFlight), the hover edit
// button must be disabled with an explanatory tooltip, and startEdit must
// refuse to open the editor. The backend guard (409) is the authoritative
// fallback; this keeps the UX in sync and avoids a pointless round-trip.
// ---------------------------------------------------------------------------

test('edit button is offered only for turns the reader wrote', () => {
  // The button used to appear on any turn with a text summary and a turn id,
  // including Goal continuations whose provider text is a wrapper the reader
  // never typed -- every Resend on those returned 409. The authored-origin
  // rule now decides visibility, and the disablement reason decides enabledness.
  const btnMatch = structuredPane.match(
    /<button\s+v-if="canEditTurn\(turn\)"[\s\S]*?@click="startEdit\(turn\)"/,
  )
  assert.ok(btnMatch, 'edit-resend hover button must exist')
  const btn = btnMatch[0]

  assert.match(btn, /v-if="canEditTurn\(turn\)"/, 'visibility must come from the authored-origin rule')
  assert.match(
    btn,
    /:disabled="editReasonFor\(turn\) !== null"/,
    'the button is disabled exactly when the rule states a reason',
  )
})

test('edit button explains why it is disabled via a tooltip', () => {
  const btnMatch = structuredPane.match(
    /class="edit-resend-hover-btn"[\s\S]*?@click="startEdit\(turn\)"/,
  )
  assert.ok(btnMatch, 'edit-resend hover button must exist')
  const btn = btnMatch[0]

  assert.match(btn, /:title="editReasonFor\(turn\) \|\| 'Edit message'"/, 'the tooltip is the rule\'s reason')
})

// ---------------------------------------------------------------------------
// Placement and disabled affordance. The guards above keep the button from
// doing the wrong thing; these keep it visible in the right place and honest
// about when it is inert.
// ---------------------------------------------------------------------------

test('the action row is in normal flow, not pinned over the message', () => {
  // Absolutely positioned it sat behind the bubble — which is itself
  // positioned — so the buttons could not be clicked at all.
  const rule = structuredPane.match(/\.turn-actions \{[\s\S]*?\n\}/)
  assert.ok(rule, 'the .turn-actions rule must exist')
  assert.doesNotMatch(
    rule[0],
    /position: absolute/,
    'a pinned row lands behind the bubble and swallows clicks',
  )
})

test('the action row reserves room for the buttons and the time', () => {
  const rule = structuredPane.match(/\.turn-actions \{[\s\S]*?\n\}/)
  assert.ok(rule)
  assert.match(rule[0], /display: flex/, 'the row lays its contents out')
  assert.match(rule[0], /gap:/, 'actions and time need a gap so they do not crowd')
  assert.match(rule[0], /min-height:/, 'the row keeps its height so revealing it does not shift the thread')
})

test('the message row carries the edit action and the time', () => {
  const row = structuredPane.match(/class="turn-actions turn-actions--message"[\s\S]*?<\/div>/)
  assert.ok(row, 'the message actions row must exist')
  assert.match(row[0], /class="edit-resend-hover-btn"/, 'the edit action belongs to the message it edits')
  assert.match(row[0], /class="turn-time"/, 'the time sits beside it')
  assert.match(row[0], /messageClockLabel\(turn\)/, 'the time is the message\'s own')
})

test('the turn closes with its own actions row', () => {
  const row = structuredPane.match(/class="turn-actions turn-actions--turn"[\s\S]*?<\/div>/)
  assert.ok(row, 'the turn actions row must exist')
  assert.match(row[0], /class="turn-fork-button"/, 'fork applies to the whole turn')
  assert.match(row[0], /class="turn-time"/, 'the time sits beside it')
  assert.match(row[0], /turnClockLabel\(turn\)/, 'the turn row reports the answer\'s own time')
})

test('inline editing does not balloon the bubble', () => {
  // The form forced ``min-width: 320px`` and drew a bordered dark box inside
  // the blue bubble, so a two-character message opened a wide empty-looking
  // form. The textarea now reads as the message itself.
  const form = userMessage.match(/\.edit-resend-form \{[\s\S]*?\n\}/)
  assert.ok(form, 'the edit form rule must exist')
  assert.doesNotMatch(form[0], /min-width: min\(320px/, 'the form must not force a wide bubble')

  const textarea = userMessage.match(/\.edit-resend-textarea \{[\s\S]*?\n\}/)
  assert.ok(textarea, 'the textarea rule must exist')
  assert.match(textarea[0], /border: none/, 'the textarea must not draw a box inside the bubble')
  assert.match(textarea[0], /background: none/, 'the textarea must not draw its own fill')
})

test('a disabled edit button looks disabled', () => {
  // The button is disabled whenever any turn is running — most of the time in
  // an active chat. Wired but unstyled it looked enabled and swallowed clicks
  // with no feedback at all.
  const rule = structuredPane.match(/\.edit-resend-hover-btn:disabled \{[\s\S]*?\n\}/)
  assert.ok(rule, 'a :disabled rule must exist for the edit button')
  assert.match(rule[0], /opacity:/, 'disabled must be visibly dimmed')
  assert.match(rule[0], /cursor: not-allowed/, 'disabled must not claim to be clickable')
})

/** The controller's dependency surface; every test here only exercises the
 *  edit gate and the resend flow, so unrelated deps stay inert. Overrides are
 *  applied on top, and the editor's own refs are wired to one reset helper so a
 *  test that passes its own refs still sees them cleared. */
function actionsController(overrides = {}) {
  const deps = {
    composerError: ref(null),
    currentModeId: ref('default'),
    feishuBotPoolStore: { bots: [], activeBotForTab: () => null },
    goalComposerLocked: ref(false),
    goalComposerReason: ref(null),
    goalEditReason: ref(null),
    isManagedChat: ref(false),
    modeChangeError: ref(null),
    props: { tabId: 'tab-a', paneId: 'pane-a' },
    resetStream() {},
    async setMode() {},
    async start() {},
    streamBasePath: ref('/api/workspaces/tabs/tab-a/stream'),
    streamSource: computed(() => 'terminal-tab'),
    streamSourceId: ref('tab-a'),
    async submit() { return true },
    terminalStore: { async forkTab() {}, pushNotification() {} },
    turnInFlight: ref(false),
    turns: computed(() => []),
    supportsEditResend: ref(true),
    editAttachmentError: ref(null),
    isPreparingEditAttachments: ref(false),
    handleEditPaste() {},
    removeEditAttachment() {},
    optimisticCancelledTurnId: ref(null),
    ...overrides,
  }
  deps.editAttachments = deps.editAttachments ?? ref([])
  deps.resetEditAttachments = deps.resetEditAttachments ?? (() => {
    deps.editAttachments.value = []
    deps.editAttachmentError.value = null
  })
  return useChatActions(deps)
}

test('startEdit refuses to open the editor while a turn is running', () => {
  const controller = actionsController({ turnInFlight: ref(true) })
  const turn = { key: 'turn-a', userText: 'original', providerOrigin: 'web', turnId: 't-1' }

  controller.startEdit(turn)

  assert.equal(controller.editingTurnKey.value, null)
  assert.equal(controller.editDraft.value, '')
  assert.equal(controller.editError.value, 'A turn is currently running')
})

test('startEdit never opens on a turn the Hub wrote', () => {
  // A Goal continuation's provider text is a wrapper; resending it as the
  // reader's own message is not a rewrite this surface can perform.
  const controller = actionsController()
  const turn = {
    key: 'turn-goal',
    userText: 'Continue active Goal',
    providerOrigin: 'goal',
    turnId: 't-1',
  }

  controller.startEdit(turn)

  assert.equal(controller.editingTurnKey.value, null, 'no editor for a Hub-authored turn')
})

test('startEdit opens on an ordinary reader turn', () => {
  const controller = actionsController()
  const turn = { key: 'turn-a', userText: 'why is pytest failing?', providerOrigin: 'web', turnId: 't-1' }

  controller.startEdit(turn)

  assert.equal(controller.editingTurnKey.value, 'turn-a')
  assert.equal(controller.editDraft.value, 'why is pytest failing?')
  assert.equal(controller.editError.value, null)
})


/** A pasted image, in the editor's staged shape. */
const SEEDED_IMAGE = {
  id: 'att-1',
  filename: 'clipboard.png',
  mime_type: 'image/png',
  data_url: 'data:image/png;base64,AA',
  preview_url: 'data:image/png;base64,AA',
  size_bytes: 1,
}

/** Install a stub ``fetch`` for the resend route and record what it saw. */
function stubResend({ detail, status } = {}) {
  const notifications = []
  const calls = []
  const fetchCalls = []
  const optimisticCancelledTurnId = ref('t-stopped')
  const editAttachments = ref([])
  globalThis.fetch = async (path, init) => {
    fetchCalls.push({ path, init })
    if (detail) {
      return new Response(JSON.stringify({ detail }), { status: status ?? 409 })
    }
    return new Response(JSON.stringify({ ok: true, removed_turn_count: 2 }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  return { notifications, calls, fetchCalls, optimisticCancelledTurnId, editAttachments }
}

test('a successful resend retires the Stop optimism, clears the draft and reports what it removed', async () => {
  // The rewrite truncates the store and restarts the stream. The local Stop
  // optimism must die with the old turn (it names a turn that no longer
  // exists), the staged images belong to the edit that sent them, and the
  // removed-turn count must reach the reader.
  const { notifications, calls, fetchCalls, optimisticCancelledTurnId, editAttachments } = stubResend()
  const controller = actionsController({
    optimisticCancelledTurnId,
    editAttachments,
    terminalStore: { async forkTab() {}, pushNotification: n => notifications.push(n) },
    resetStream() { calls.push('reset') },
    async start() { calls.push('start') },
  })
  const turn = { key: 'turn-a', userText: 'original', providerOrigin: 'web', turnId: 't-1' }

  controller.startEdit(turn)
  editAttachments.value = [{ ...SEEDED_IMAGE }]
  controller.editDraft.value = 'edited text'
  await controller.submitEdit(turn)

  assert.equal(controller.isEditSending.value, false)
  assert.equal(controller.editError.value, null)
  assert.equal(controller.editingTurnKey.value, null, 'the editor closes on success')
  assert.equal(optimisticCancelledTurnId.value, null, 'no phantom Stopped row survives the rewrite')
  assert.deepEqual(editAttachments.value, [], 'staged images belong to the edit that sent them')
  assert.deepEqual(calls, ['reset', 'start'], 'the stream is rehydrated from the truncated store')
  assert.match(notifications.at(-1).message, /2 later turns were removed/)

  assert.equal(fetchCalls.length, 1)
  assert.equal(fetchCalls[0].path, '/api/workspaces/tabs/tab-a/stream/edit-resend')
  const body = JSON.parse(fetchCalls[0].init.body)
  assert.equal(body.turn_id, 't-1')
  assert.equal(body.text, 'edited text')
  assert.deepEqual(body.attachments, [{
    filename: 'clipboard.png',
    mime_type: 'image/png',
    data_url: 'data:image/png;base64,AA',
  }], 'the draft travels in the shared wire form, not copied verbatim')
  globalThis.fetch = undefined
})

test('a failed resend keeps the draft, states the reason and touches nothing else', async () => {
  const { notifications, calls, optimisticCancelledTurnId, editAttachments } = stubResend({
    detail: 'This turn was written by the Goal/Hub, not by you. Only your own messages can be edited.',
  })
  const controller = actionsController({
    optimisticCancelledTurnId,
    editAttachments,
    terminalStore: { async forkTab() {}, pushNotification: n => notifications.push(n) },
    resetStream() { calls.push('reset') },
  })
  const turn = { key: 'turn-a', userText: 'original', providerOrigin: 'web', turnId: 't-1' }

  controller.startEdit(turn)
  editAttachments.value = [{ ...SEEDED_IMAGE }]
  controller.editDraft.value = 'edited text'
  await controller.submitEdit(turn)

  assert.equal(controller.editingTurnKey.value, 'turn-a', 'the editor stays open with the draft')
  assert.match(controller.editError.value, /Only your own messages can be edited/)
  assert.equal(optimisticCancelledTurnId.value, 't-stopped', 'a failed rewrite keeps Stop optimism')
  assert.deepEqual(editAttachments.value.length, 1, 'the staged image is not thrown away')
  assert.deepEqual(calls, [], 'a failed rewrite does not rehydrate the stream')
  globalThis.fetch = undefined
})

test('a resend waits for a pasted image to finish preparing', async () => {
  // Sending before the image is read would drop it from the request while the
  // reader still sees the chip on screen.
  const { notifications, fetchCalls, optimisticCancelledTurnId, editAttachments } = stubResend()
  const controller = actionsController({
    optimisticCancelledTurnId,
    editAttachments,
    isPreparingEditAttachments: ref(true),
    terminalStore: { async forkTab() {}, pushNotification: n => notifications.push(n) },
  })
  const turn = { key: 'turn-a', userText: 'original', providerOrigin: 'web', turnId: 't-1' }

  controller.startEdit(turn)
  controller.editDraft.value = 'edited text'
  await controller.submitEdit(turn)

  assert.equal(controller.editError.value, 'Wait for the image to finish preparing')
  assert.equal(fetchCalls.length, 0, 'nothing is sent while an image is preparing')
  globalThis.fetch = undefined
})
