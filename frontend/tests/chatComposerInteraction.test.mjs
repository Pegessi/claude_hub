import test from 'node:test'
import assert from 'node:assert/strict'

import {
  autoresizeComposerTextarea,
  createComposerDraftId,
  resolveComposerEnterAction,
} from '../src/utils/chatComposerInteraction.ts'

test('resolveComposerEnterAction ignores IME composition', () => {
  assert.equal(resolveComposerEnterAction({
    isComposing: true,
    shiftKey: false,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    turnInFlight: false,
    hasDraft: true,
  }), 'ignore')
})

test('resolveComposerEnterAction queues during an in-flight turn', () => {
  assert.equal(resolveComposerEnterAction({
    isComposing: false,
    shiftKey: false,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    turnInFlight: true,
    hasDraft: true,
  }), 'queue')
})

test('resolveComposerEnterAction steers with meta+enter during in-flight turn', () => {
  assert.equal(resolveComposerEnterAction({
    isComposing: false,
    shiftKey: false,
    metaKey: true,
    ctrlKey: false,
    altKey: false,
    turnInFlight: true,
    hasDraft: true,
  }), 'steer')
})

test('resolveComposerEnterAction sends immediately when idle', () => {
  assert.equal(resolveComposerEnterAction({
    isComposing: false,
    shiftKey: false,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    turnInFlight: false,
    hasDraft: true,
  }), 'send')
})

test('resolveComposerEnterAction inserts a newline on mobile when idle', () => {
  // Mobile soft keyboards have no modifier keys: Enter must insert a newline
  // (sending is only via the Send button), never send/queue/steer.
  assert.equal(resolveComposerEnterAction({
    isComposing: false,
    shiftKey: false,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    turnInFlight: false,
    hasDraft: true,
    isMobile: true,
  }), 'newline')
})

test('resolveComposerEnterAction inserts a newline on mobile during an in-flight turn', () => {
  // On desktop this would queue; on mobile Enter is always a newline.
  assert.equal(resolveComposerEnterAction({
    isComposing: false,
    shiftKey: false,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    turnInFlight: true,
    hasDraft: true,
    isMobile: true,
  }), 'newline')
})

test('resolveComposerEnterAction allows an external keyboard to steer on mobile', () => {
  assert.equal(resolveComposerEnterAction({
    isComposing: false,
    shiftKey: false,
    metaKey: true,
    ctrlKey: false,
    altKey: false,
    turnInFlight: true,
    hasDraft: true,
    isMobile: true,
  }), 'steer')
})

test('resolveComposerEnterAction still ignores IME composition on mobile', () => {
  // isComposing takes precedence over the mobile newline rule so CJK input
  // is not interrupted.
  assert.equal(resolveComposerEnterAction({
    isComposing: true,
    shiftKey: false,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    turnInFlight: false,
    hasDraft: true,
    isMobile: true,
  }), 'ignore')
})

test('resolveComposerEnterAction keeps desktop behavior when isMobile is false', () => {
  assert.equal(resolveComposerEnterAction({
    isComposing: false,
    shiftKey: false,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    turnInFlight: false,
    hasDraft: true,
    isMobile: false,
  }), 'send')
})

test('autoresizeComposerTextarea caps height and enables scroll', () => {
  const el = {
    style: {},
    scrollHeight: 400,
  }
  autoresizeComposerTextarea(el, 240)
  assert.equal(el.style.height, '240px')
  assert.equal(el.style.overflowY, 'auto')
})

for (const modifier of ['metaKey', 'ctrlKey']) {
  test(`${modifier}+Enter steers the first queued item with an empty composer`, () => {
    const context = { isComposing: false, shiftKey: false, metaKey: false, ctrlKey: false, altKey: false, turnInFlight: true, hasDraft: false, hasQueuedDraft: true, [modifier]: true }
    assert.equal(resolveComposerEnterAction(context), 'steer-queued')
    assert.equal(resolveComposerEnterAction({ ...context, isComposing: true }), 'ignore')
    assert.equal(resolveComposerEnterAction({ ...context, hasQueuedDraft: false }), 'ignore')
    assert.equal(resolveComposerEnterAction({ ...context, hasDraft: true }), 'steer')
  })
}

test('draft IDs remain unique on a plain HTTP origin without randomUUID', t => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value: {} })
  t.after(() => Object.defineProperty(globalThis, 'crypto', descriptor))
  assert.match(createComposerDraftId(), /^draft-/)
  assert.notEqual(createComposerDraftId(), createComposerDraftId())
})
