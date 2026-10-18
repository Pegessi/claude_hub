import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { ref } from 'vue'
import { loadChatFeature } from './chatPresentationTestHarness.mjs'

const timeline = readFileSync(
  new URL('../src/features/chat/components/ChatTimeline.vue', import.meta.url),
  'utf8',
)
const approvalCard = readFileSync(
  new URL('../src/features/chat/components/ChatApprovalCard.vue', import.meta.url),
  'utf8',
)
const { useChatComposer } = await loadChatFeature('controllers/useChatComposer')

function makeComposer() {
  const isSending = ref(false)
  const controller = useChatComposer({
    attachments: ref([]),
    closeImageLightbox() {},
    closeModeMenu() {},
    closeModelMenu() {},
    composerError: ref(null),
    draftMessage: ref(''),
    draftQueue: ref([]),
    enqueueDraft() {},
    goalComposerReason: ref(null),
    goalInputBlocked: ref(false),
    goalSteerOnly: ref(false),
    imageLightboxUrl: ref(null),
    isManagedChat: ref(false),
    isModeMenuOpen: ref(false),
    isModelMenuOpen: ref(false),
    isPaneActivated: ref(true),
    isSending,
    lightboxEl: ref(null),
    async sendQueuedDraft() {},
    async submit() { return true },
    turnInFlight: ref(false),
    composerTurnInFlight: ref(false),
  })
  return { controller, isSending }
}

const approval = {
  key: 'approval-a',
  callId: 'call-a',
  kind: 'ask_question',
  title: 'Pick one',
  questions: [{
    id: 'q1',
    prompt: 'Pick one',
    allowMultiple: false,
    options: [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }],
  }],
}

test('turn v-memo deps include the approval interaction signature', () => {
  const memoMatch = timeline.match(/v-memo="\[([^\]]*)\]"/)
  assert.ok(memoMatch, 'a v-memo directive must exist on the turn loop')
  assert.match(memoMatch[1], /turnApprovalSignature\(turn\)/)
})

test('turnApprovalSignature tracks answers, resolved state, and sending', () => {
  const { controller, isSending } = makeComposer()
  const turn = { approvals: [approval] }
  const initial = controller.turnApprovalSignature(turn)

  controller.toggleQuestionOption('approval-a', 'q1', 'a', false)
  const selected = controller.turnApprovalSignature(turn)
  controller.markResolved('approval-a')
  const resolved = controller.turnApprovalSignature(turn)
  isSending.value = true
  const sending = controller.turnApprovalSignature(turn)

  assert.notEqual(selected, initial)
  assert.notEqual(resolved, selected)
  assert.match(sending, /\|sending$/)
  assert.equal(controller.turnApprovalSignature({ approvals: [] }), '')
})

test('approval card options are wired to the composable selection helpers', () => {
  assert.match(approvalCard, /isQuestionOptionSelected\(\s*part\.approval\.key/)
  assert.match(
    approvalCard,
    /toggleQuestionOption\(\s*part\.approval\.key,\s*question\.id,\s*option\.id,\s*question\.allowMultiple,?\s*\)/,
  )
  assert.match(approvalCard, /canSubmitQuestion\(part\.approval\)/)
})
