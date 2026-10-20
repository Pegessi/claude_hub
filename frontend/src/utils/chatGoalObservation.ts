import type { AgentStreamEvent, ChatGoal } from '@/types'

const NATIVE_GOAL_NOTIFICATIONS = new Set([
  'thread/goal/updated', 'thread/goal/cleared', 'goal/updated', 'goal/cleared',
])

/** A native Goal may change without a turn completing, including while paused. */
export function latestNativeGoalNotification(
  previous: readonly AgentStreamEvent[],
  latest: readonly AgentStreamEvent[],
): AgentStreamEvent | null {
  const prior = previous[previous.length - 1]
  const corresponding = latest[previous.length - 1]
  const isAppend = previous.length <= latest.length && (!prior || (
    corresponding?.session_id === prior.session_id &&
    corresponding?.tab_id === prior.tab_id &&
    corresponding?.stream_sequence === prior.stream_sequence &&
    corresponding?.type === prior.type
  ))
  const start = isAppend ? previous.length : 0
  for (let index = latest.length - 1; index >= start; index -= 1) {
    const event = latest[index]
    if (event.type !== 'status' || event.payload.subagent_thread) continue
    if (NATIVE_GOAL_NOTIFICATIONS.has(String(event.payload.provider_notification))) return event
  }
  return null
}

export function goalObservationVersion(goal: ChatGoal | null): string {
  if (!goal) return 'none'
  return [
    goal.id,
    goal.updated_at,
    goal.status,
    goal.turns_completed,
    goal.checkpoint?.turn_id ?? '',
    goal.current_turn_id ?? '',
    goal.execution_owner ?? 'hub_managed',
    goal.provider_thread_id ?? '',
    goal.provider_goal_created_at ?? '',
    goal.provider_updated_at ?? '',
    goal.provider_status ?? '',
  ].join('\u0000')
}

export function goalTurnCompletionReflected(
  goal: ChatGoal | null,
  turnId: string | null,
  baselineVersion: string,
): boolean {
  if (!goal || goal.status !== 'active') return true
  if (turnId && (goal.completed_turn_ids?.includes(turnId) || goal.checkpoint?.turn_id === turnId)) return true
  if (goal.execution_owner === 'provider_native') return goalObservationVersion(goal) !== baselineVersion
  return turnId ? false : goalObservationVersion(goal) !== baselineVersion
}

/** Native notifications are invalidation hints; display only the API snapshot. */
export function nativeGoalNotificationReflected(
  goal: ChatGoal | null,
  event: AgentStreamEvent,
  baselineVersion: string,
): boolean {
  const method = String(event.payload.provider_notification)
  if (method.endsWith('/cleared')) return goal === null
  if (!goal || goal.execution_owner !== 'provider_native') return false
  const envelope = event.payload.goal
  if (!envelope || typeof envelope !== 'object') return goalObservationVersion(goal) !== baselineVersion
  const observed = envelope as Record<string, unknown>
  if (typeof observed.threadId === 'string' && observed.threadId !== goal.provider_thread_id) return false
  const value = observed.goal
  if (!value || typeof value !== 'object') return goalObservationVersion(goal) !== baselineVersion
  const native = value as Record<string, unknown>
  if (typeof native.createdAt === 'number' && native.createdAt !== goal.provider_goal_created_at) {
    return goal.provider_goal_created_at != null && goal.provider_goal_created_at > native.createdAt
  }
  if (typeof native.updatedAt === 'number' && goal.provider_updated_at != null) {
    return goal.provider_updated_at > native.updatedAt || (
      goal.provider_updated_at === native.updatedAt && goal.provider_status === native.status
    )
  }
  return goalObservationVersion(goal) !== baselineVersion
}
