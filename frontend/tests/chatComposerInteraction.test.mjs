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

test('autoresizeComposerTextarea caps height and enables scroll', () => {
  const el = {
    style: {},
    scrollHeight: 400,
  }
  autoresizeComposerTextarea(el, 240)
  assert.equal(el.style.height, '240px')
  assert.equal(el.style.overflowY, 'auto')
})
