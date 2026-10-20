import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/utils/chatGoalObservation.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2020 },
})
const { latestNativeGoalNotification, goalObservationVersion, goalTurnCompletionReflected, nativeGoalNotificationReflected } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`,
)

const goal = overrides => ({
  id: 'goal-1', status: 'active', updated_at: '2026-10-20T00:00:00Z', turns_completed: 1,
  execution_owner: 'provider_native', provider_thread_id: 'thread-1',
  provider_goal_created_at: 100, provider_updated_at: 200, provider_status: 'active',
  ...overrides,
})
const event = (sequence, method = 'thread/goal/updated', payload = {}) => ({
  type: 'status', session_id: 'session-1', tab_id: 'tab-1', stream_sequence: sequence,
  payload: {
    provider_notification: method,
    goal: { threadId: 'thread-1', goal: { createdAt: 100, updatedAt: 200, status: 'active' } },
    ...payload,
  },
})

test('native Goal notifications refresh once per newly committed suffix', () => {
  const before = [event(0)]
  const next = event(1, 'thread/goal/cleared')
  assert.equal(latestNativeGoalNotification(before, before), null)
  assert.equal(latestNativeGoalNotification(before, [...before, next]), next)
  assert.equal(latestNativeGoalNotification([], [event(0), next]), next)
  assert.equal(latestNativeGoalNotification(before, []), null)
})

test('stream replacement and clearing notifications are recognized without a turn boundary', () => {
  const previous = [event(0)]
  const replacement = { ...event(0, 'goal/cleared'), session_id: 'session-2' }
  assert.equal(latestNativeGoalNotification(previous, [replacement]), replacement)
  assert.equal(latestNativeGoalNotification([], [event(1, 'goal/updated')])?.stream_sequence, 1)
})

test('child Goal events, text deltas, and unrelated status notifications do not refresh', () => {
  for (const value of [
    event(1, 'thread/goal/updated', { subagent_thread: 'child' }),
    event(1, 'thread/status/changed'),
    { ...event(1), type: 'text_delta' },
  ]) assert.equal(latestNativeGoalNotification([], [value]), null)
})

test('observation version includes provider owner, generation and state', () => {
  const initial = goalObservationVersion(goal())
  for (const field of [
    { execution_owner: 'hub_managed' }, { provider_thread_id: 'thread-2' },
    { provider_goal_created_at: 101 }, { provider_updated_at: 201 },
    { provider_status: 'paused' }, { current_turn_id: 'new-turn' },
  ]) assert.notEqual(goalObservationVersion(goal(field)), initial)
  assert.equal(goalObservationVersion(null), 'none')
})

test('native completion uses provider version advancement without requiring a Hub checkpoint', () => {
  const initial = goal()
  const version = goalObservationVersion(initial)
  assert.equal(goalTurnCompletionReflected(initial, 'completed-turn', version), false)
  assert.equal(goalTurnCompletionReflected(goal({ provider_updated_at: 201 }), 'completed-turn', version), true)
  assert.equal(goalTurnCompletionReflected(goal({ current_turn_id: 'next-native-turn' }), 'completed-turn', version), true)
  assert.equal(goalTurnCompletionReflected(goal({ status: 'complete' }), 'completed-turn', version), true)
})

test('Hub completion still requires its completed turn or checkpoint identity', () => {
  const initial = goal({ execution_owner: 'hub_managed' })
  const version = goalObservationVersion(initial)
  assert.equal(goalTurnCompletionReflected({ ...initial, updated_at: 'later' }, 'turn-1', version), false)
  assert.equal(goalTurnCompletionReflected({ ...initial, completed_turn_ids: ['turn-1'] }, 'turn-1', version), true)
  assert.equal(goalTurnCompletionReflected({ ...initial, checkpoint: { turn_id: 'turn-1' } }, 'turn-1', version), true)
  assert.equal(goalTurnCompletionReflected(null, 'turn-1', version), true)
})

test('updated notifications do not accept stale absent or Hub snapshots', () => {
  const notice = event(1)
  assert.equal(nativeGoalNotificationReflected(null, notice, 'none'), false)
  assert.equal(nativeGoalNotificationReflected(goal({ execution_owner: 'hub_managed' }), notice, ''), false)
  assert.equal(nativeGoalNotificationReflected(goal({ provider_thread_id: 'other' }), notice, ''), false)
  assert.equal(nativeGoalNotificationReflected(goal({ provider_updated_at: 199 }), notice, ''), false)
  assert.equal(nativeGoalNotificationReflected(goal({ provider_status: 'paused' }), notice, ''), false)
  assert.equal(nativeGoalNotificationReflected(goal(), notice, goalObservationVersion(goal())), true)
})

test('newer provider snapshots supersede old notifications without rolling the UI back', () => {
  const notice = event(1)
  assert.equal(nativeGoalNotificationReflected(goal({ provider_updated_at: 201, provider_status: 'paused' }), notice, ''), true)
  assert.equal(nativeGoalNotificationReflected(goal({ provider_goal_created_at: 101 }), notice, ''), true)
  assert.equal(nativeGoalNotificationReflected(goal({ provider_goal_created_at: 99 }), notice, ''), false)
})

test('clear waits for authoritative absence rather than hiding an existing Goal', () => {
  const notice = event(1, 'thread/goal/cleared', { goal: { threadId: 'thread-1' } })
  assert.equal(nativeGoalNotificationReflected(goal(), notice, ''), false)
  assert.equal(nativeGoalNotificationReflected(null, notice, ''), true)
})
