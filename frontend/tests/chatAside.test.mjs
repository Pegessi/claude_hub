import assert from 'node:assert/strict'
import test from 'node:test'
import { loadChatUtility } from './chatPresentationTestHarness.mjs'

const { asideClientTurnId, asideFailure, asideUnavailableReason, buildAsideSendRequest, canOfferAside } =
  await loadChatUtility('chatAside')

const gate = overrides => asideUnavailableReason({
  isManagedChat: false,
  question: 'why is pytest failing?',
  attachmentCount: 0,
  ordinal: 3,
  busy: false,
  ...overrides,
})

test('the aside question becomes the forked tab\'s own first turn', () => {
  const request = buildAsideSendRequest('fork-tab', '  why is pytest failing?  ', 'turn-abc')
  assert.equal(request.path, '/api/workspaces/tabs/fork-tab/stream/send')
  assert.deepEqual(request.body, {
    client_turn_id: 'turn-abc',
    text: 'why is pytest failing?',
    // The fork has no turn in flight: delivering it as a steer would claim to
    // cancel something that does not exist.
    delivery: 'normal',
    // Images are staged per pane; an aside question carries none.
    attachments: [],
  })
})

test('tab ids are encoded into the request path', () => {
  const request = buildAsideSendRequest('tab/with space', 'question', 'id')
  assert.equal(request.path, '/api/workspaces/tabs/tab%2Fwith%20space/stream/send')
})

test('every reason the aside action cannot run is explicit', () => {
  assert.equal(gate({}), null)
  assert.equal(gate({ isManagedChat: true }), 'Asking aside needs a local Chat')
  assert.equal(gate({ busy: true }), 'Asking aside…')
  assert.equal(gate({ ordinal: null }), 'Send a message first, then ask aside')
  assert.equal(gate({ attachmentCount: 1 }), 'Ask aside sends text only — remove attachments first')
  assert.equal(gate({ question: '   ' }), 'Type a question to ask aside')
})

test('the action is hidden where the backend would refuse to fork', () => {
  assert.equal(canOfferAside(false), true)
  assert.equal(canOfferAside(true), false, 'Managed Chat tabs are not forkable')
})

test('generated aside turn ids are stable strings, never empty', () => {
  const first = asideClientTurnId()
  const second = asideClientTurnId()
  assert.equal(typeof first, 'string')
  assert.ok(first.trim().length > 0)
  assert.notEqual(first, second)
})

test('a send failure after the fork puts the reader back where the question is', () => {
  // Forking switched the pane to the new tab; the draft only exists in the
  // composer of the source Chat, so staying on the empty fork hides it.
  const failure = asideFailure('source-tab', 'fork-tab', 'fork-tab', 'Session is still starting (409)')
  assert.equal(failure.restoreTabId, 'source-tab')
  assert.equal(
    failure.message,
    'Ask aside failed: Session is still starting (409). Your question is still in this Chat.',
  )
})

test('a failure before the fork leaves the pane alone', () => {
  const failure = asideFailure('source-tab', null, 'source-tab', 'Could not fork this Chat')
  assert.equal(failure.restoreTabId, null, 'nothing moved, so nothing is put back')
  assert.equal(failure.message, 'Ask aside failed: Could not fork this Chat')
})

test('a failure never overrides a tab the user chose in the meantime', () => {
  const failure = asideFailure('source-tab', 'fork-tab', 'other-tab', 'HTTP 503')
  assert.equal(failure.restoreTabId, null, 'an explicit switch wins over the recovery')
  assert.equal(
    failure.message,
    'Ask aside failed: HTTP 503. Your question is still in the Chat you asked from.',
    'the draft is named where it lives, not "this Chat" which is now a third tab',
  )
  assert.doesNotMatch(failure.message, /this Chat/)
})
