// Regression: a persistent app-server (TraeX) re-emitted a nested code-mode
// item/completed hours after its turn ended, stamped with no turn id. The
// reducer used to open a fresh never-completed legacy turn for it, which made
// isChatModeLocked() return true forever — the composer could not send and
// Stop found nothing to cancel (the "native chat 永久卡死" wedge).
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { Buffer } from 'node:buffer'
import test from 'node:test'

import ts from 'typescript'

const read = name => readFile(new URL(`../src/utils/${name}`, import.meta.url), 'utf8')
const transpileOptions = {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
}
const [timelineSrc, lifeSrc, durationSrc, questionSrc, subagentSrc, agentImageSrc] = await Promise.all([
  read('agentStreamTimeline.ts'),
  read('chatTurnLifecycle.ts'),
  read('duration.ts'),
  read('chatQuestionResponse.ts'),
  read('subagentTool.ts'),
  read('agentImage.ts'),
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
    created_at: `2026-01-01T00:00:${String(seq).padStart(2, '0')}Z`,
    redacted: false,
    ...overrides,
  }
}

// The exact production sequence: turn with an exec tool completes, then a
// duplicate item/completed arrives with no turn id, then a goal/updated
// notification with turnId=null.
function wedgeEvents() {
  const callId = 'code-mode-nested:29:call_x:exec-1'
  return [
    makeEvent(0, 'turn_started', { summary: 'do work' }, { turn_id: 'turn-1' }),
    makeEvent(1, 'tool_call_started', { tool_call_id: callId, name: 'exec_command' }, { turn_id: 'turn-1' }),
    makeEvent(2, 'tool_call_completed', { tool_call_id: callId, status: 'completed' }, { turn_id: 'turn-1' }),
    makeEvent(3, 'turn_completed', { status: 'completed' }, { turn_id: 'turn-1' }),
    // Hours later — provider replay / background execution, no Hub turn.
    makeEvent(4, 'tool_call_completed', { tool_call_id: callId, status: 'completed' }, { turn_id: null }),
    makeEvent(5, 'status', { provider_notification: 'thread/goal/updated', goal: { turnId: null } }, { turn_id: null }),
  ]
}

test('duplicate null-turn tool replay after completion does not mint a legacy turn', () => {
  const turns = groupEventsIntoTurns(wedgeEvents())
  assert.equal(turns.length, 1)
  assert.equal(turns[0].turnId, 'turn-1')
  assert.equal(turns[0].completed, true)
  assert.equal(isChatModeLocked(false, turns), false)
})

test('incremental reducer applies the same drop when the replay arrives live', () => {
  const reducer = new IncrementalTimelineReducer()
  const events = wedgeEvents()
  reducer.reduce(events.slice(0, 4))
  const turns = reducer.reduce(events)
  assert.equal(turns.length, 1)
  assert.equal(turns[0].completed, true)
  assert.equal(isChatModeLocked(false, turns), false)
})

test('goal/updated (turnId=null) alone never locks the composer', () => {
  const turns = groupEventsIntoTurns([
    makeEvent(0, 'turn_started', { summary: 'q' }, { turn_id: 't1' }),
    makeEvent(1, 'turn_completed', { status: 'completed' }, { turn_id: 't1' }),
    makeEvent(2, 'status', { provider_notification: 'thread/goal/updated', goal: { turnId: null } }),
  ])
  assert.equal(turns.length, 1)
  assert.equal(turns[0].completed, true)
  assert.equal(isChatModeLocked(false, turns), false)
})

test('an unrelated null-turn completion (unknown tool id) is still rendered', () => {
  // Only exact identity replays are suppressed; a genuinely novel orphan row
  // must not be silently swallowed (defense in depth is on the backend).
  const turns = groupEventsIntoTurns([
    makeEvent(0, 'turn_started', { summary: 'q' }, { turn_id: 't1' }),
    makeEvent(1, 'turn_completed', { status: 'completed' }, { turn_id: 't1' }),
    makeEvent(2, 'tool_call_completed', { tool_call_id: 'brand-new-id', status: 'completed' }, { turn_id: null }),
  ])
  assert.equal(turns.length, 2)
  assert.equal(turns[1].turnId, null)
  assert.equal(turns[1].tools.length, 1)
})

test('a live legacy turn still receives null-turn tool records', () => {
  // Provider/transcript paths can emit interior records before any
  // turn_started; those must keep flowing into the open legacy turn.
  const turns = groupEventsIntoTurns([
    makeEvent(0, 'tool_call_started', { tool_call_id: 'k1', name: 'Bash' }, { turn_id: null }),
    makeEvent(1, 'tool_call_completed', { tool_call_id: 'k1', status: 'completed' }, { turn_id: null }),
  ])
  assert.equal(turns.length, 1)
  assert.equal(turns[0].turnId, null)
  assert.equal(turns[0].completed, false)
  assert.equal(turns[0].tools.length, 1)
})
