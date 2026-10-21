import type { ChatGoal, ChatGoalStatus } from '@/types'

const TERMINAL_STATUSES = new Set<ChatGoalStatus>(['complete', 'cancelled', 'failed'])

export function isGoalTerminal(status: ChatGoalStatus): boolean {
  return TERMINAL_STATUSES.has(status)
}

export function goalStopRetryAction(goal: ChatGoal): 'pause' | 'clear' {
  // A native Goal can already be complete while its last turn still needs an
  // interrupt acknowledgement. Retrying that interrupt must not delete it.
  if (goal.execution_owner === 'provider_native' && goal.provider_stop_pending) return 'pause'
  return isGoalTerminal(goal.status) ? 'clear' : 'pause'
}

export function goalBlocksPlan(goal: ChatGoal | null | undefined): boolean {
  return goal?.status === 'active' || Boolean(goal && goal.dispatch_state && goal.dispatch_state !== 'idle')
}

export function goalPlanLockReason(goal: ChatGoal | null | undefined): string | null {
  return goalBlocksPlan(goal)
    ? 'Pause or finish the active Goal before switching to Plan mode.'
    : null
}

export function goalStatusLabel(status: ChatGoalStatus): string {
  return {
    active: 'Active',
    paused: 'Paused',
    blocked: 'Blocked',
    usageLimited: 'Usage limited',
    budgetLimited: 'Budget limited',
    complete: 'Complete',
    cancelled: 'Cancelled',
    failed: 'Failed',
  }[status]
}
