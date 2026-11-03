import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { loadChatFeature, loadChatUtility } from './chatPresentationTestHarness.mjs'

const { groupEventsIntoTurns } = await loadChatUtility('agentStreamTimeline')
const { messageSourceLabel } = await loadChatFeature('timeline/eventValues')
const { pendingMessageReceipts, messageInboxReadError } = await loadChatFeature('presentation/chatMessageInbox')
const event = metadata => ({ stream_sequence: 1, session_id: 's', tab_id: 't', agent_type: 'codex',
  type: 'turn_started', payload: { summary: 'hello', metadata }, created_at: '2026-10-20T00:00:00Z',
  run_epoch: 1, turn_id: 'turn', message_id: null, call_id: null })

test('hub origin replays with server actor and unverified source hint, without Feishu identity', () => {
  const first = event({ origin: 'hub_message', hub_message: { message_id: 'inbox-1', actor: 'local CLI', source_tab_id: 'source' } })
  const [turn] = groupEventsIntoTurns([first, { ...first, stream_sequence: 2 }])
  assert.equal(turn.origin, 'hub_message')
  assert.deepEqual(turn.hubMessage, { messageId: 'inbox-1', actor: 'local CLI', sourceTabId: 'source' })
  assert.equal(turn.feishu, null)
  assert.equal(messageSourceLabel(turn), 'Hub message · local CLI')
})

test('malformed source hints never become identity; missing legacy origin remains unknown', () => {
  for (const source of [42, {}, []]) {
    const [turn] = groupEventsIntoTurns([event({ origin: 'hub_message', hub_message: { message_id: 'id', actor: 'local CLI', source_tab_id: source } })])
    assert.equal(turn.hubMessage.sourceTabId, null)
  }
  const [invalid] = groupEventsIntoTurns([event({ origin: 'hub_message', hub_message: { message_id: 2, actor: [] } })])
  assert.equal(invalid.hubMessage, null)
  assert.equal(messageSourceLabel(invalid), 'Hub message')
  const [legacy] = groupEventsIntoTurns([event({})])
  assert.equal(legacy.origin, 'unknown')
  assert.equal(legacy.hubMessage, null)
})

test('pending receipt list excludes submitted and terminal turns and bounds malformed input', () => {
  const messages = ['queued', 'waiting', 'dispatching', 'uncertain', 'accepted', 'completed', 'cancelled', 'failed']
    .map(state => ({ message_id: state, state, waiting_reason: 'busy' }))
  assert.deepEqual(pendingMessageReceipts({ messages }).map(row => row.state), ['queued', 'waiting', 'dispatching', 'uncertain'])
  assert.deepEqual(pendingMessageReceipts({ messages: [null, {}, { state: 'queued', message_id: 42 }] }), [])
  assert.equal(pendingMessageReceipts({ messages: Array(200).fill(messages[0]) }).length, 100)
})

test('invalid inbox envelopes fail instead of replacing receipts with an empty list', () => {
  for (const value of [null, [], {}, { messages: null }, { messages: {} }, { messages: 'unavailable' }]) {
    assert.throws(() => pendingMessageReceipts(value), /Invalid message inbox response/)
  }
  assert.deepEqual(pendingMessageReceipts({ messages: [] }), [])
})

test('message status errors distinguish timeout, network, authentication and HTTP failures', () => {
  assert.match(messageInboxReadError(undefined, true), /timed out after 10 seconds/)
  assert.match(messageInboxReadError(200, true), /timed out/)
  assert.match(messageInboxReadError(undefined, false), /network connection/)
  assert.match(messageInboxReadError(401, false), /Sign-in.*HTTP 401/)
  assert.match(messageInboxReadError(403, false), /Access was denied.*HTTP 403/)
  for (const status of [404, 429, 500, 502, 503]) {
    assert.match(messageInboxReadError(status, false), new RegExp(`HTTP ${status}`))
  }
  assert.match(messageInboxReadError(200, false), /unreadable.*response/)
})

test('user bubble separates Hub badge from Feishu avatar and inbox lifecycle clears polling', async () => {
  const bubble = await readFile(new URL('../src/features/chat/components/ChatUserMessage.vue', import.meta.url), 'utf8')
  assert.match(bubble, /turn.origin === 'feishu' \|\| turn.origin === 'hub_message'/)
  assert.match(bubble, /<FeishuIdentityMark\s+v-if="turn.origin === 'feishu'"/)
  const inbox = await readFile(new URL('../src/features/chat/components/ChatMessageInbox.vue', import.meta.url), 'utf8')
  assert.match(inbox, /onDeactivated\(stop\)/)
  assert.match(inbox, /onUnmounted\(stop\)/)
  assert.match(inbox, /identity !== generation/)
})
