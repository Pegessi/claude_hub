import assert from 'node:assert/strict'
import test from 'node:test'
import { ref } from 'vue'
import { createSidebarLoader } from './chatSidebarTestHarness.mjs'

// The real composable, not a stub: the sidebar and the TabBar both read this
// file, so its pending-question mapping is the surface under test.
const load = createSidebarLoader()
const { useTabStatus } = await load('composables/useTabStatus.ts')
const { statusesEqual } = await load('stores/terminal/helpers.ts')

const chatTab = { id: 'tab-1', name: 'mr 3829', session_kind: 'chat', is_active: false }

function status(overrides = {}) {
  return {
    tab_id: 'tab-1',
    tab_name: 'mr 3829',
    agent_type: 'cursor',
    status: 'working',
    status_text: 'Working',
    detail: 'Responding',
    tmux_session: 'claude-hub-tab-1',
    sampled_at: '2026-11-11T15:28:45Z',
    pending_question: false,
    activity: 'Responding',
    activity_since: '2026-11-11T15:28:21Z',
    last_reply_excerpt: null,
    last_reply_at: null,
    ...overrides,
  }
}

test('a turn blocked on a question presents as finished and waiting, not working', () => {
  const agentStatuses = ref([status({ pending_question: true })])
  const { getTabStatus, getTabStatusLabel, isAwaitingAnswer } = useTabStatus(agentStatuses)

  // The provider still holds the turn open, so the poll reports "working"; a
  // card is nevertheless waiting for the user, which is what the row must say.
  assert.equal(agentStatuses.value[0].status, 'working')
  assert.equal(getTabStatus(chatTab), 'idle')
  assert.equal(getTabStatusLabel(chatTab), 'Waiting for your answer')
  assert.equal(isAwaitingAnswer(chatTab), true)
})

test('the same turn reports working again once the card is answered', () => {
  const agentStatuses = ref([status({ pending_question: true })])
  const { getTabStatus, getTabStatusLabel, isAwaitingAnswer } = useTabStatus(agentStatuses)
  assert.equal(getTabStatus(chatTab), 'idle')

  agentStatuses.value = [status({ pending_question: false })]
  assert.equal(getTabStatus(chatTab), 'working')
  assert.equal(getTabStatusLabel(chatTab), 'Working')
  assert.equal(isAwaitingAnswer(chatTab), false)
})

test('a detail still rides on labels that are not a pending question', () => {
  const agentStatuses = ref([status({ pending_question: false, status_text: 'Running pytest -q' })])
  const { getTabStatusLabel } = useTabStatus(agentStatuses)
  assert.equal(getTabStatusLabel(chatTab), 'Working — Running pytest -q')
})

test('a card behind a failed provider keeps the failure and names the card', () => {
  // `pending_question` survives on an attention/offline snapshot (the card's
  // epoch is unchanged and only retirement clears it), but an answer can be
  // refused there: a follow-up card goes stale when its turn ends unsuccessfully
  // and a follow-up answer is refused while turn_in_flight. So the failure keeps
  // its own state and the label says a question waits too.
  const agentStatuses = ref([
    status({ status: 'attention', status_text: 'Needs attention', pending_question: true }),
  ])
  const { getTabStatus, getTabStatusLabel, isAwaitingAnswer } = useTabStatus(agentStatuses)
  assert.equal(getTabStatus(chatTab), 'attention')
  assert.equal(getTabStatusLabel(chatTab), 'Needs attention — a question is waiting')
  assert.equal(isAwaitingAnswer(chatTab), false)

  agentStatuses.value = [
    status({ status: 'offline', status_text: 'Offline', pending_question: true }),
  ]
  assert.equal(getTabStatus(chatTab), 'offline')
  assert.equal(getTabStatusLabel(chatTab), 'Offline — a question is waiting')
  assert.equal(isAwaitingAnswer(chatTab), false)

  // Without the card the failed provider reads exactly as before.
  agentStatuses.value = [status({ status: 'attention', status_text: 'Needs attention' })]
  assert.equal(getTabStatusLabel(chatTab), 'Needs attention')
})

test('a dead provider without a card stays offline', () => {
  const agentStatuses = ref([status({ status: 'offline', status_text: 'Offline' })])
  const { getTabStatus, getTabStatusLabel, isAwaitingAnswer } = useTabStatus(agentStatuses)
  assert.equal(getTabStatus(chatTab), 'offline')
  assert.equal(getTabStatusLabel(chatTab), 'Offline')
  assert.equal(isAwaitingAnswer(chatTab), false)
})

test('an already-idle turn with a waiting card reads the same sentence', () => {
  // Cursor asks out-of-band and can finish the turn; the card then waits on an
  // idle session, which is still "waiting for your answer", not "Idle".
  const agentStatuses = ref([
    status({ status: 'idle', status_text: 'Idle', detail: 'Ready', pending_question: true }),
  ])
  const { getTabStatus, getTabStatusLabel, isAwaitingAnswer } = useTabStatus(agentStatuses)
  assert.equal(getTabStatus(chatTab), 'idle')
  assert.equal(getTabStatusLabel(chatTab), 'Waiting for your answer')
  assert.equal(isAwaitingAnswer(chatTab), true)
})

test('the pending-question mapping leaves ordinary states alone', () => {
  const idle = ref([status({ status: 'idle', status_text: 'Idle', pending_question: false })])
  const idleStatus = useTabStatus(idle)
  assert.equal(idleStatus.getTabStatus(chatTab), 'idle')
  assert.equal(idleStatus.getTabStatusLabel(chatTab), 'Idle')

  const attention = ref([status({ status: 'attention', status_text: 'Needs attention' })])
  const attentionStatus = useTabStatus(attention)
  assert.equal(attentionStatus.getTabStatus(chatTab), 'attention')
  assert.equal(attentionStatus.getTabStatusLabel(chatTab), 'Needs attention')
})

test('a Chat with no reported status keeps its offline fallback', () => {
  const agentStatuses = ref([])
  const { getTabStatus, getTabStatusLabel, isAwaitingAnswer } = useTabStatus(agentStatuses)
  assert.equal(getTabStatus(chatTab), 'offline')
  assert.equal(getTabStatusLabel(chatTab), 'Offline')
  assert.equal(isAwaitingAnswer(chatTab), false)
})

test('a poll that only opens or resolves a question is not dropped as unchanged', () => {
  // The store only assigns a poll when `statusesEqual` says it differs. A
  // question opening mid-turn changes none of the provider's own fields, so
  // leaving it out of the comparison made the row stale for a whole turn.
  const base = status()
  const asked = status({ pending_question: true })
  assert.equal(statusesEqual([base], [asked]), false)

  const busy = status({ activity: 'Asking you', activity_since: '2026-11-11T15:31:00Z' })
  assert.equal(statusesEqual([base], [busy]), false)

  const replied = status({ last_reply_excerpt: 'Done.', last_reply_at: '2026-11-11T15:32:00Z' })
  assert.equal(statusesEqual([base], [replied]), false)

  // An identical payload still counts as unchanged, so the watch effect is not
  // woken by every poll.
  assert.equal(statusesEqual([base], [status()]), true)
})
