import assert from 'node:assert/strict'
import test from 'node:test'
import { loadChatUtility } from './chatPresentationTestHarness.mjs'

const { canOfferEdit, editUnavailableReason, editResendBody, removedTurnsNotice, storedCopyWarning } =
  await loadChatUtility('chatEditResend')

test('the editor is hidden for turns the Hub wrote itself', () => {
  // A Goal continuation stores "Continue active Goal" while the provider
  // received a wrapped instruction: there is no reader text to rewrite, and
  // the backend refuses it. Offering the form could only 409.
  for (const providerOrigin of ['goal', 'workspace', 'restart_continuation', 'question_answer', 'scheduled_task']) {
    assert.equal(
      canOfferEdit({ supportsEditResend: true, providerOrigin, hasTurnId: true }),
      false,
      `${providerOrigin} turns are not the reader's message`,
    )
  }
})

test('the editor is offered for reader turns, including legacy ones', () => {
  for (const providerOrigin of ['web', 'feishu', 'hub_message', null]) {
    assert.equal(
      canOfferEdit({ supportsEditResend: true, providerOrigin, hasTurnId: true }),
      true,
      `${providerOrigin ?? 'legacy'} turns are the reader's own`,
    )
  }
})

test('the editor is hidden where the Chat cannot rewrite history at all', () => {
  assert.equal(canOfferEdit({ supportsEditResend: false, providerOrigin: 'web', hasTurnId: true }), false)
  assert.equal(
    canOfferEdit({ supportsEditResend: true, providerOrigin: 'web', hasTurnId: false }),
    false,
    'a turn without an id has no truncation point',
  )
})

test('every reason the editor cannot open is stated, and a Hub turn states its own', () => {
  const gate = overrides => editUnavailableReason({
    supportsEditResend: true,
    providerOrigin: 'web',
    hasTurnId: true,
    unfinished: false,
    goalReason: null,
    preparingAttachments: false,
    ...overrides,
  })

  assert.equal(gate({}), null)
  assert.equal(gate({ supportsEditResend: false }), 'Editing past turns is unavailable in this Chat')
  assert.equal(gate({ hasTurnId: false }), 'This turn has no id to edit')
  assert.equal(
    gate({ providerOrigin: 'goal' }),
    'This turn was written by the Goal/Hub, not by you',
    'the refusal names the owner instead of blaming the reader',
  )
  assert.equal(gate({ unfinished: true }), 'A turn is currently running')
  assert.equal(gate({ goalReason: 'Finish or clear the Goal first' }), 'Finish or clear the Goal first')
  assert.equal(gate({ preparingAttachments: true }), 'Wait for the image to finish preparing')
})

test('a partial stored copy warns before the reader resends it', () => {
  // Redaction truncates at 4000 characters and masks secret-looking tokens,
  // so the prefill is not the message that was sent. Saying so is the only
  // honest option; the resend sends exactly what the editor holds.
  assert.equal(storedCopyWarning(false), null)
  const warning = storedCopyWarning(true)
  assert.match(warning, /partial copy/)
  assert.match(warning, /exactly the text below/)
})

test('the rewrite reports the turns it removed, and stays quiet when none', () => {
  assert.equal(removedTurnsNotice(0), null, 'nothing removed is not news')
  assert.equal(removedTurnsNotice(undefined), null)
  assert.equal(removedTurnsNotice(null), null)
  assert.equal(removedTurnsNotice(1), 'Message resent · 1 later turn was removed')
  assert.equal(removedTurnsNotice(4), 'Message resent · 4 later turns were removed')
})

test('the body carries only the images added while editing', () => {
  // The backend re-sends the original turn's own images; sending them again
  // here would duplicate them in the new turn.
  const body = editResendBody({
    text: 'edited text',
    clientTurnId: 'turn-new',
    turnId: 'turn-old',
    attachments: [{ filename: 'clipboard.png', mime_type: 'image/png', data_url: 'data:image/png;base64,AA' }],
  })

  assert.deepEqual(body, {
    text: 'edited text',
    client_turn_id: 'turn-new',
    turn_id: 'turn-old',
    attachments: [{ filename: 'clipboard.png', mime_type: 'image/png', data_url: 'data:image/png;base64,AA', preview_data_url: undefined }],
  })
})

test('a bounded preview travels with the added image', () => {
  const body = editResendBody({
    text: 'edited text',
    clientTurnId: 'turn-new',
    turnId: 'turn-old',
    attachments: [{
      filename: 'clipboard.jpg',
      mime_type: 'image/jpeg',
      data_url: 'data:image/jpeg;base64,ORIGINAL',
      preview_data_url: 'data:image/jpeg;base64,PREVIEW',
    }],
  })

  assert.equal(body.attachments[0].preview_data_url, 'data:image/jpeg;base64,PREVIEW')
  assert.equal(body.attachments[0].preview_is_original, undefined, 'a distinct preview is sent once')
})

test('a preview that is the original is flagged, never uploaded twice', () => {
  // Copying the draft verbatim would send the same base64 twice and count it
  // twice against the request cap, so a set the editor accepted could be
  // rejected with HTTP 400. The shared wire form flags it instead.
  const body = editResendBody({
    text: 'edited text',
    clientTurnId: 'turn-new',
    turnId: 'turn-old',
    attachments: [{
      filename: 'clipboard.png',
      mime_type: 'image/png',
      data_url: 'data:image/png;base64,SAME',
      preview_data_url: 'data:image/png;base64,SAME',
    }],
  })

  assert.deepEqual(body.attachments[0], {
    filename: 'clipboard.png',
    mime_type: 'image/png',
    data_url: 'data:image/png;base64,SAME',
    preview_is_original: true,
  })
})
