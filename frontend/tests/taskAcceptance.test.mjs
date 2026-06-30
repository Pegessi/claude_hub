import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'

const helperSource = await readFile(
  new URL('../src/utils/taskAcceptance.ts', import.meta.url),
  'utf8',
)
// Strip the type-only import (`import type ... from '@/types'`); the runtime
// transpile target has no module resolver and the helpers need no values from it.
const runtimeSource = helperSource.replace(/^import type .*$/m, '')
const { outputText } = ts.transpileModule(runtimeSource, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
})
const helperModule = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
)
const { awaitingHumanAcceptance, canMarkDoneTask, hasBlockingReviewResult } = helperModule

function task(overrides = {}) {
  return {
    status: 'review',
    human_acceptance_requested_at: null,
    review_skipped_at: null,
    goal_packet: null,
    ...overrides,
  }
}

function report(state) {
  return state ? { state } : null
}

test('stale pending_review packet + final review_passed -> Done shown', () => {
  // The live repro: autonomous task whose packet is stuck at pending_review
  // but has reached final acceptance (human_acceptance_requested_at set, latest
  // review report review_passed).
  const t = task({
    goal_packet: { status: 'pending_review' },
    human_acceptance_requested_at: '2026-06-30T12:36:22Z',
  })
  assert.equal(canMarkDoneTask(t, report('completed'), report('review_passed')), true)
})

test('stale pending_review packet + review_passed (no human ts) -> Done shown', () => {
  const t = task({ goal_packet: { status: 'pending_review' } })
  assert.equal(canMarkDoneTask(t, null, report('review_passed')), true)
})

test('pending_review packet + no final acceptance signal -> Done hidden (plan gate preserved)', () => {
  const t = task({ goal_packet: { status: 'pending_review' } })
  assert.equal(awaitingHumanAcceptance(t, null, null), false)
  assert.equal(canMarkDoneTask(t, null, null), false)
})

test('rejected packet + no final acceptance signal -> Done hidden', () => {
  const t = task({ goal_packet: { status: 'rejected' } })
  assert.equal(canMarkDoneTask(t, null, null), false)
})

test('final review_failed verdict suppresses Done even with acceptance signal', () => {
  const t = task({
    goal_packet: { status: 'approved' },
    human_acceptance_requested_at: '2026-06-30T12:36:22Z',
  })
  assert.equal(hasBlockingReviewResult(report('review_failed')), true)
  assert.equal(canMarkDoneTask(t, null, report('review_failed')), false)
})

test('review_needs_input suppresses Done', () => {
  const t = task({ human_acceptance_requested_at: '2026-06-30T12:36:22Z' })
  assert.equal(canMarkDoneTask(t, null, report('review_needs_input')), false)
})

test('happy path: reported completed, no packet -> Done shown', () => {
  const t = task()
  assert.equal(canMarkDoneTask(t, report('completed'), null), true)
})

test('review_skipped_at alone permits Done', () => {
  const t = task({ review_skipped_at: '2026-06-30T12:00:00Z' })
  assert.equal(canMarkDoneTask(t, null, null), true)
})

test('ready_for_review is not a completion signal', () => {
  // ready_for_review asks for AI review; it must not by itself unblock Done.
  const t = task()
  assert.equal(awaitingHumanAcceptance(t, report('ready_for_review'), null), false)
})

test('task not in review status -> Done hidden regardless of signals', () => {
  const t = task({ status: 'working', human_acceptance_requested_at: '2026-06-30T12:00:00Z' })
  assert.equal(awaitingHumanAcceptance(t, report('completed'), report('review_passed')), false)
})
