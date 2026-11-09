import assert from 'node:assert/strict'
import test from 'node:test'
import { loadChatUtility } from './chatPresentationTestHarness.mjs'

const { asideClientTurnId, asideFailure, asideForkOrdinal, asideUnavailableReason, buildAsideSendRequest, canOfferAside } =
  await loadChatUtility('chatAside')

const gate = overrides => asideUnavailableReason({
  isManagedChat: false,
  question: 'why is pytest failing?',
  attachmentCount: 0,
  ordinal: 3,
  hasTurns: true,
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
  assert.equal(gate({ ordinal: null, hasTurns: false }), 'Send a message first, then ask aside')
  assert.equal(
    gate({ ordinal: null, hasTurns: true }),
    'Ask aside after this turn finishes',
    'a first turn still running is "not yet", not "nothing to ask about"',
  )
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

test('a restored failure says the reader is back in the source Chat', () => {
  // Forking switched the pane to the new tab; the draft only exists in the
  // composer of the source Chat, so staying on the empty fork hides it.
  const failure = asideFailure({
    sourceTabId: 'source-tab',
    sourceTabName: 'refactor ui',
    forkedTabId: 'fork-tab',
    activeTabId: 'fork-tab',
    detail: 'Session is still starting (409)',
  })
  assert.equal(failure.restoreTabId, 'source-tab')
  assert.equal(
    failure.message,
    'Ask aside failed: Session is still starting (409). Back in refactor ui — your question is still in the composer.',
  )
})

test('a failure before the fork leaves the pane alone', () => {
  const failure = asideFailure({
    sourceTabId: 'source-tab',
    sourceTabName: 'refactor ui',
    forkedTabId: null,
    activeTabId: 'source-tab',
    detail: 'Could not fork this Chat',
  })
  assert.equal(failure.restoreTabId, null, 'nothing moved, so nothing is put back')
  assert.equal(failure.message, 'Ask aside failed: Could not fork this Chat')
})

test('a failure the reader has already moved past names where the question still is', () => {
  // Nothing was restored, so "here" would be false: the sentence has to name
  // the Chat that holds the draft instead.
  const failure = asideFailure({
    sourceTabId: 'source-tab',
    sourceTabName: 'refactor ui',
    forkedTabId: 'fork-tab',
    activeTabId: 'other-tab',
    detail: 'HTTP 503',
  })
  assert.equal(failure.restoreTabId, null, 'an explicit switch wins over the recovery')
  assert.equal(
    failure.message,
    'Ask aside failed: HTTP 503. Your question is still in the composer of refactor ui.',
  )
  assert.doesNotMatch(failure.message, /this Chat|Back in/, 'the reader was not moved back')
})

test('an unknown Chat name still reads as a sentence in both branches', () => {
  const restored = asideFailure({
    sourceTabId: 'source-tab', sourceTabName: '  ', forkedTabId: 'fork-tab',
    activeTabId: 'fork-tab', detail: 'HTTP 409',
  })
  assert.match(restored.message, /Back in the Chat you asked this in — /)
  const moved = asideFailure({
    sourceTabId: 'source-tab', sourceTabName: '', forkedTabId: 'fork-tab',
    activeTabId: 'other-tab', detail: 'HTTP 409',
  })
  assert.match(moved.message, /composer of the Chat you asked this in\./)
})

test('the fork cuts at the last completed turn, never inside a running one', () => {
  // A fork that copies an unfinished turn inherits tool cards and question
  // cards its fresh provider session can never complete.
  assert.equal(asideForkOrdinal([]), null)
  assert.equal(asideForkOrdinal([{ completed: false }]), null, 'a first turn in flight has nothing to cut at')
  assert.equal(asideForkOrdinal([{ completed: true }, { completed: false }]), 0)
  assert.equal(asideForkOrdinal([{ completed: true }, { completed: true }]), 1)
  assert.equal(
    asideForkOrdinal([{ completed: true }, { completed: false }, { completed: true }, { completed: false }]),
    2,
    'the newest completed turn wins even when a newer one is running',
  )
})
