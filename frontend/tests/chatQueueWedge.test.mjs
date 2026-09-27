// Regression for the provider capacity-queue wedge (tab 2feb646a).
//
// While a TraeX turn waits for model capacity the provider emits queue/status
// every ~1s ("Too many current requests. Your queue position is N."), all with
// ONE stable message id, and may emit a recoverable "Reconnecting… 1/5" notice.
// Required behavior:
//   - the queue snapshots coalesce into a single in-place status part (no
//     transcript flood) and the turn stays NOT completed, so
//     isChatModeLocked() stays true and Stop remains mounted;
//   - a recoverable reconnect notice arrives (post-backend-fix) as a coalesced
//     STATUS, never as a terminal error, so it must not release the lock;
//   - a genuine ERROR is still terminal.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { Buffer } from 'node:buffer'
import test from 'node:test'

import ts from 'typescript'

const read = name => readFile(new URL(`../src/utils/${name}`, import.meta.url), 'utf8')
const transpileOptions = {
  compilerOptions: { module: ts.ModuleKind.ES2020, target: ts.ScriptTarget.ES2020 },
}
const [timelineSrc, durationSrc, questionSrc, subagentSrc, agentImageSrc, lifeSrc] = await Promise.all([
  read('agentStreamTimeline.ts'),
  read('duration.ts'),
  read('chatQuestionResponse.ts'),
  read('subagentTool.ts'),
  read('agentImage.ts'),
  read('chatTurnLifecycle.ts'),
])
const bundle = [durationSrc, questionSrc, subagentSrc, agentImageSrc, timelineSrc]
  .map(src => ts.transpileModule(
    src.replace(/^import .* from '@\/utils\/.*$/gm, ''),
    transpileOptions,
  ).outputText)
  .join('\n')
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(bundle).toString('base64')}`
)
const life = await import(
  `data:text/javascript;base64,${Buffer.from(ts.transpileModule(lifeSrc, transpileOptions).outputText).toString('base64')}`
)
const { groupEventsIntoTurns, IncrementalTimelineReducer } = mod
const { isChatModeLocked } = life

function makeEvent(seq, type, payload = {}, overrides = {}) {
  return {
    stream_sequence: seq,
    session_id: 's1',
    tab_id: 't1',
    agent_type: 'traex',
    type,
    payload,
    message_id: overrides.message_id,
    created_at: `2026-09-27T02:${String(Math.floor(seq / 60)).padStart(2, '0')}:${String(seq % 60).padStart(2, '0')}Z`,
    redacted: false,
    ...overrides,
  }
}

const queueStatus = (seq, position) => makeEvent(
  seq,
  'status',
  {
    text: `Too many current requests. Your queue position is ${position}. Please wait for a while.`,
    provider_status: 'queue/status',
    snapshot: true,
  },
  { turn_id: 'turn-1', message_id: 'traex-status:queue/status' },
)
const turnStarted = seq => makeEvent(seq, 'turn_started', { summary: 'work' }, { turn_id: 'turn-1' })

test('thousands of queue snapshots coalesce into one status part and keep the turn locked', () => {
  const count = 3000
  const events = [turnStarted(0)]
  for (let i = 1; i <= count; i += 1) events.push(queueStatus(i, 357 - Math.floor(i / 10)))
  const lastPos = 357 - Math.floor(count / 10)
  const turns = groupEventsIntoTurns(events)
  assert.equal(turns.length, 1)
  const turn = turns[0]
  assert.equal(turn.completed, false)
  const statusParts = turn.parts.filter(part => part.kind === 'status')
  assert.equal(statusParts.length, 1)
  // The single part reflects the LATEST position (in-place replacement).
  assert.ok(statusParts[0].text.includes(`queue position is ${lastPos}.`))
  assert.equal(turn.errors.length, 0)
  // Stop stays mounted / send stays queued: the turn is still in flight.
  assert.equal(isChatModeLocked(false, turns), true)
})

test('incremental reducer coalesces a live queue flood the same way', () => {
  const reducer = new IncrementalTimelineReducer()
  reducer.reduce([turnStarted(0), queueStatus(1, 100)])
  const events = [turnStarted(0)]
  for (let i = 1; i <= 500; i += 1) events.push(queueStatus(i, 100 - Math.floor(i / 50)))
  const turns = reducer.reduce(events)
  const statusParts = turns[0].parts.filter(part => part.kind === 'status')
  assert.equal(statusParts.length, 1)
  assert.equal(isChatModeLocked(false, turns), true)
})

test('a recoverable reconnect STATUS (post-fix shape) does not release the lock', () => {
  // Backend now maps provider "Reconnecting… n/m" error notices to a coalesced
  // STATUS instead of a terminal ERROR.
  const reconnect = makeEvent(
    9001,
    'status',
    { text: 'Reconnecting… 1/5', provider_status: 'provider/reconnecting', snapshot: true },
    { turn_id: 'turn-1', message_id: 'provider-status:reconnect' },
  )
  const turns = groupEventsIntoTurns([turnStarted(0), queueStatus(1, 50), reconnect, queueStatus(2, 49)])
  assert.equal(turns[0].completed, false)
  assert.equal(turns[0].errors.length, 0)
  assert.equal(isChatModeLocked(false, turns), true)
})

test('a genuine ERROR still terminalizes the turn (defense in depth)', () => {
  const fatal = makeEvent(
    3,
    'error',
    { message: 'Internal server error' },
    { turn_id: 'turn-1' },
  )
  const turns = groupEventsIntoTurns([turnStarted(0), queueStatus(1, 5), fatal])
  assert.equal(isChatModeLocked(false, turns), false)
  assert.equal(turns[0].errors.length, 1)
})

test('queue then real generation completes the turn normally', () => {
  const ready = makeEvent(
    2,
    'status',
    { text: 'Model is ready; starting the response.', provider_status: 'queue/status', snapshot: true },
    { turn_id: 'turn-1', message_id: 'traex-status:queue/status' },
  )
  const text = makeEvent(3, 'text_delta', { text: 'done' }, { turn_id: 'turn-1' })
  const completed = makeEvent(4, 'turn_completed', { status: 'completed' }, { turn_id: 'turn-1' })
  const turns = groupEventsIntoTurns([turnStarted(0), queueStatus(1, 1), ready, text, completed])
  assert.equal(turns[0].completed, true)
  assert.equal(turns[0].completionStatus, 'completed')
  assert.equal(isChatModeLocked(false, turns), false)
})
