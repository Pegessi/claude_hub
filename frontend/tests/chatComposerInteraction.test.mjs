import test from 'node:test'
import assert from 'node:assert/strict'

import {
  autoresizeComposerTextarea,
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

test('resolveComposerEnterAction inserts a newline on mobile even with a modifier held', () => {
  // An external keyboard's ⌘/Ctrl+Enter must not steer/send on mobile — the
  // Send button is the only send path there.
  assert.equal(resolveComposerEnterAction({
    isComposing: false,
    shiftKey: false,
    metaKey: true,
    ctrlKey: false,
    altKey: false,
    turnInFlight: true,
    hasDraft: true,
    isMobile: true,
  }), 'newline')
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
