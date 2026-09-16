import type { ChatGoal, ChatGoalStatus, ChatGoalUsageQuality } from '@/types'

const TERMINAL_STATUSES = new Set<ChatGoalStatus>(['complete', 'cancelled', 'failed'])

export function isGoalTerminal(status: ChatGoalStatus): boolean {
  return TERMINAL_STATUSES.has(status)
}

export function goalBlocksPlan(goal: ChatGoal | null | undefined): boolean {
  return goal?.status === 'active'
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
    budget_limited: 'Budget limited',
    complete: 'Complete',
    cancelled: 'Cancelled',
    failed: 'Failed',
  }[status]
}

export function goalUsageLabel(
  usage: number | null,
  budget: number | null,
  quality: ChatGoalUsageQuality,
): string {
  if (quality === 'unavailable' || usage === null) {
    return budget === null ? 'Usage unavailable' : `Usage unavailable · ${budget.toLocaleString()} token budget`
  }
  const qualifier = quality === 'estimated' ? 'estimated' : 'exact'
  const amount = usage.toLocaleString()
  return budget === null
    ? `${amount} tokens (${qualifier})`
    : `${amount} / ${budget.toLocaleString()} tokens (${qualifier})`
}
