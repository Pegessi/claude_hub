import type { ChatGoal, ChatGoalStatus } from '@/types'

const TERMINAL_STATUSES = new Set<ChatGoalStatus>(['complete', 'cancelled', 'failed'])

export function isGoalTerminal(status: ChatGoalStatus): boolean {
  return TERMINAL_STATUSES.has(status)
}

export function goalBlocksPlan(goal: ChatGoal | null | undefined): boolean {
  return goal?.status === 'active' || Boolean(goal && goal.dispatch_state && goal.dispatch_state !== 'idle')
}

export function goalPlanLockReason(goal: ChatGoal | null | undefined): string | null {
  return goalBlocksPlan(goal)
    ? 'Pause or finish the active Goal before switching to Plan mode.'
    : null
}

const TRAILING_GOAL_PROTOCOL_RE = new RegExp(
  String.raw`(?:\s*<goal-checkpoint>(?:(?!<goal-checkpoint)[\s\S])*?</goal-checkpoint>)?`
    + String.raw`\s*<goal-status\s+state=["'][a-z_]+["']\s*>(?:(?!<goal-status)[\s\S])*?</goal-status>\s*$`,
  'i',
)

/**
 * Hide a trailing Goal control envelope that the backend did not sanitize
 * (history recorded before every turn in a Goal Chat was sanitized).
 */
export function stripTrailingGoalProtocol(text: string): string {
  if (!/<\/goal-status>/i.test(text)) return text
  return text.replace(TRAILING_GOAL_PROTOCOL_RE, '')
}

export function goalStatusLabel(status: ChatGoalStatus): string {
  return {
    active: 'Active',
    paused: 'Paused',
    blocked: 'Blocked',
    complete: 'Complete',
    cancelled: 'Cancelled',
    failed: 'Failed',
  }[status]
}
