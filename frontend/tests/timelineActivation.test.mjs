import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const source = await readFile(
  new URL('../src/utils/timelineActivation.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const { createTimelineActivation } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)

test('initial phase is hidden and followOutput is true', () => {
  const a = createTimelineActivation()
  assert.equal(a.phase, 'hidden')
  assert.equal(a.followOutput, true)
})

test('markHistoryReady moves to pinning and forces followOutput true', () => {
  const a = createTimelineActivation()
  a.detachFromTail()
  assert.equal(a.followOutput, false)
  a.markHistoryReady()
  assert.equal(a.phase, 'pinning')
  assert.equal(a.followOutput, true)
})

test('confirmTailPinned moves pinning to revealed', () => {
  const a = createTimelineActivation()
  a.markHistoryReady()
  a.confirmTailPinned()
  assert.equal(a.phase, 'revealed')
})

test('confirmTailPinned is a no-op outside pinning', () => {
  const a = createTimelineActivation()
  a.confirmTailPinned()
  assert.equal(a.phase, 'hidden')
})

test('detachFromTail sets followOutput false', () => {
  const a = createTimelineActivation()
  a.markHistoryReady()
  a.confirmTailPinned()
  a.detachFromTail()
  assert.equal(a.followOutput, false)
})

test('live updates do not rearm follow after detach', () => {
  const a = createTimelineActivation()
  a.markHistoryReady()
  a.confirmTailPinned()
  a.detachFromTail()
  assert.equal(a.shouldFollowLiveUpdate(), false)
  // followOutput must remain false — live updates never rearm it.
  assert.equal(a.followOutput, false)
})

test('resize does not rearm follow after detach', () => {
  const a = createTimelineActivation()
  a.markHistoryReady()
  a.confirmTailPinned()
  a.detachFromTail()
  assert.equal(a.shouldHandleResize(), false)
  assert.equal(a.followOutput, false)
})

test('rearmFollow explicitly re-enables following', () => {
  const a = createTimelineActivation()
  a.detachFromTail()
  a.rearmFollow()
  assert.equal(a.followOutput, true)
})

test('shouldFollowLiveUpdate is false while hidden or pinning', () => {
  const a = createTimelineActivation()
  assert.equal(a.shouldFollowLiveUpdate(), false)
  a.markHistoryReady()
  assert.equal(a.shouldFollowLiveUpdate(), false)
  a.confirmTailPinned()
  assert.equal(a.shouldFollowLiveUpdate(), true)
})

test('reset returns to hidden and followOutput true', () => {
  const a = createTimelineActivation()
  a.markHistoryReady()
  a.confirmTailPinned()
  a.detachFromTail()
  a.reset()
  assert.equal(a.phase, 'hidden')
  assert.equal(a.followOutput, true)
})
