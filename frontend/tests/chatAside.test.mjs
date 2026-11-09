import assert from 'node:assert/strict'
import test from 'node:test'
import { loadChatUtility } from './chatPresentationTestHarness.mjs'

const { asideClientTurnId, asideUnavailableReason, buildAsideSendRequest, canOfferAside } =
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
