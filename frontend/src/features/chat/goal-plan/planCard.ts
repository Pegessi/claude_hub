/**
 * Plan-card contract for a `plan_first` Goal.
 *
 * The Hub, not the transcript, identifies the planning turn: the Goal snapshot
 * carries `plan_turn_id` and the validated `plan`, so a turn becomes a card by
 * identity instead of by matching the assistant's prose.
 */

import type { ChatGoal, ChatGoalPlan } from '@/types'

/** Exact message the Approve action sends to resume the Goal. */
export const GOAL_PLAN_APPROVAL_MESSAGE = 'Approve the plan. Start implementing it now.'

/** Prefix the Revise action places in the composer. */
export const GOAL_PLAN_REVISE_PREFIX = 'Revise the plan: '

/**
 * How the plan card should present itself for one transcript turn:
 * `null` — this turn is not the planning turn;
 * `preparing` — the planning turn is still running;
 * `review` — the Goal parked awaiting approval, so Approve/Revise apply;
 * `waiting` — the plan exists but the Goal continued without an approval;
 * `decided` — the user already approved or revised this plan.
 */
export type GoalPlanCardMode = 'preparing' | 'review' | 'waiting' | 'decided'

export function goalPlanCardMode(
  goal: ChatGoal | null | undefined,
  turnId: string | null | undefined,
): GoalPlanCardMode | null {
  if (!goal || !goal.plan_turn_id || !turnId) return null
  if (goal.plan_turn_id !== turnId) return null
  if (goal.plan_approved) return 'decided'
  if (goal.status === 'blocked') return 'review'
  return goal.plan ? 'waiting' : 'preparing'
}

/** Stable per-turn memo signature so a Goal snapshot can surface the card. */
export function goalPlanSignature(goal: ChatGoal | null | undefined, turnId: string | null | undefined): string {
  const mode = goalPlanCardMode(goal, turnId)
  if (mode === null) return ''
  return `${mode}|${goal?.plan ? 'plan' : 'none'}|${goal?.plan_warning ? 'warn' : ''}`
}

/** Whether Approve & start is actionable right now, and why not when it is not. */
export function goalPlanApprovalDisabledReason(
  goal: ChatGoal | null | undefined,
  mode: GoalPlanCardMode | null,
  state: { sending: boolean; turnInFlight: boolean },
): string | null {
  if (mode !== 'review') return 'This plan is not awaiting approval'
  if (!goal) return 'The Goal is no longer available'
  if (goal.pending_image_count) return 'Reattach images and recover the Goal before approving'
  if (goal.dispatch_state && goal.dispatch_state !== 'idle') return 'Wait for the current Goal turn to settle'
  if (state.sending) return 'Sending…'
  if (state.turnInFlight) return 'Wait for the current turn to finish'
  return null
}

/** One-line budget summary; the card lists each helper separately. */
export function goalPlanBudgetSummary(plan: ChatGoalPlan | null | undefined): string {
  if (!plan) return ''
  const helpers = plan.budget?.helpers ?? []
  const models = helpers.map(helper => `${helper.provider}/${helper.model}`)
  const count = helpers.length === 0 ? 'no helpers' : `${helpers.length} helper${helpers.length === 1 ? '' : 's'}`
  const parallel = `max ${plan.budget?.max_parallelism ?? 1} in parallel`
  const duration = plan.budget?.duration_estimate
  return [count, models.join(', '), parallel, duration].filter(part => Boolean(part)).join(' · ')
}
