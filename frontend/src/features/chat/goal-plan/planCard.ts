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
 * `preparing` — the planning turn is still running and no plan arrived yet;
 * `review` — the Goal parked awaiting approval, so Approve/Revise apply;
 * `waiting` — the plan exists but the Goal moved on without asking for it;
 * `continuing` — the user's answer was recorded and the Goal turn it dispatched
 * is still running, so the plan is in effect but not yet continued;
 * `answered` — a turn carrying the user's own message was dispatched and the
 * Goal is idle again, which ends this plan's approval request (approval and a
 * change request look the same here, so the card must not claim either).
 */
export type GoalPlanCardMode = 'preparing' | 'review' | 'waiting' | 'continuing' | 'answered'

export function goalPlanCardMode(
  goal: ChatGoal | null | undefined,
  turnId: string | null | undefined,
): GoalPlanCardMode | null {
  if (!goal || !goal.plan_turn_id || !turnId) return null
  if (goal.plan_turn_id !== turnId) return null
  if (goal.plan_answered) {
    // The answer is recorded when its turn is dispatched, so the Goal is only
    // "continued" once that turn has settled; until then it is still running.
    return goal.dispatch_state === 'idle' ? 'answered' : 'continuing'
  }
  if (goal.status === 'blocked') return 'review'
  return goal.plan ? 'waiting' : 'preparing'
}

/** Stable per-turn memo signature so a Goal snapshot can surface the card. */
export function goalPlanSignature(goal: ChatGoal | null | undefined, turnId: string | null | undefined): string {
  const mode = goalPlanCardMode(goal, turnId)
  if (mode === null) return ''
  return `${mode}|${goal?.plan ? 'plan' : 'none'}|${goal?.plan_warning ? 'warn' : ''}`
}

/**
 * Element id for the card's heading.
 *
 * Each transcript turn renders its own card, and a grid pane can show several
 * Chats at once, so the id must be unique per turn: a fixed literal would make
 * the second card's `aria-labelledby` resolve to the first one's heading.
 */
export function goalPlanHeadingId(turnId: string | null | undefined): string {
  const slug = (turnId ?? '').replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '')
  return slug ? `goal-plan-heading-${slug}` : 'goal-plan-heading'
}

/** Whether Approve & start is actionable right now, and why not when it is not. */
export function goalPlanApprovalDisabledReason(
  goal: ChatGoal | null | undefined,
  mode: GoalPlanCardMode | null,
  state: { sending: boolean; turnInFlight: boolean },
): string | null {
  if (mode !== 'review') return 'This plan is not awaiting approval'
  if (!goal) return 'The Goal is no longer available'
  // A Goal can ask for input without a reviewable plan (the agent omitted the
  // block or it was rejected). There is nothing to approve, so the user has to
  // ask for a plan instead of starting work on an empty promise.
  if (!goal.plan) return 'No plan was proposed to approve. Ask for a revised plan instead.'
  if (goal.pending_image_count) return 'Reattach images and recover the Goal before approving'
  if (goal.dispatch_state && goal.dispatch_state !== 'idle') return 'Wait for the current Goal turn to settle'
  if (state.sending) return 'Sending…'
  if (state.turnInFlight) return 'Wait for the current turn to finish'
  return null
}

/** Whether Revise is actionable: it may still fix a request with no plan. */
export function goalPlanReviseDisabledReason(
  goal: ChatGoal | null | undefined,
  mode: GoalPlanCardMode | null,
  state: { sending: boolean; turnInFlight: boolean },
): string | null {
  if (mode !== 'review') return 'This plan is not awaiting approval'
  if (!goal) return 'The Goal is no longer available'
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
