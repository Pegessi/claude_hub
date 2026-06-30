// Pure decision logic for the workspace task "Done" / human-acceptance gate.
//
// These helpers are deliberately store-free: they take the task plus its
// already-resolved latest report and latest review report, so they can be unit
// tested in isolation. `AgentWorkspaceView.vue` wraps them with thin functions
// that pull those reports from the workspace store.

import type { AgentReport, WorkspaceTask } from '@/types'

/**
 * The agent reported it is done: the latest report for the task is a plain
 * `completed`.
 *
 * `ready_for_review` is intentionally excluded — it signals the agent is
 * explicitly asking for AI review, so the task should wait for a verdict
 * rather than be human-accepted directly.
 */
export function hasReportedCompletion(latestReport: AgentReport | null): boolean {
  return latestReport?.state === 'completed'
}

/**
 * A final-acceptance signal means the task has moved past the (optional)
 * pre-implementation Goal Packet approval phase and reached the point where a
 * human may accept it:
 *
 *  - `human_acceptance_requested_at` — set after a final reviewer PASS or an
 *    auto-skipped low-risk review (the plan-approval verdict path deliberately
 *    leaves this null via human_acceptance_for_passed=False).
 *  - `review_skipped_at` — review was explicitly skipped.
 *  - latest review report `review_passed` — a final reviewer verdict.
 *  - latest report `completed` — a simple task the agent finished without ever
 *    requesting review, so no verdict is produced.
 */
export function hasFinalAcceptanceSignal(
  task: WorkspaceTask,
  latestReport: AgentReport | null,
  latestReviewReport: AgentReport | null,
): boolean {
  return (
    Boolean(task.human_acceptance_requested_at) ||
    Boolean(task.review_skipped_at) ||
    latestReviewReport?.state === 'review_passed' ||
    hasReportedCompletion(latestReport)
  )
}

/**
 * Whether the task is waiting for a human to accept it.
 *
 * The Goal Packet `pending_review` / `rejected` short-circuit is a
 * pre-implementation plan-approval gate: it must hide Done while the packet is
 * still being approved, but it must NOT strand a task that has already reached
 * final acceptance. Autonomous tasks in particular never transition their
 * packet to `approved`, so a stale `pending_review` would otherwise hide Done
 * forever even after a final `review_passed`. We therefore only honour the
 * packet gate when there is no final-acceptance signal yet.
 */
export function awaitingHumanAcceptance(
  task: WorkspaceTask,
  latestReport: AgentReport | null,
  latestReviewReport: AgentReport | null,
): boolean {
  if (task.status !== 'review') return false

  const finalAcceptanceSignal = hasFinalAcceptanceSignal(
    task,
    latestReport,
    latestReviewReport,
  )

  // Pre-implementation Goal Packet approval still pending/rejected and no final
  // acceptance signal yet: keep Done hidden until the plan is approved.
  if (
    !finalAcceptanceSignal &&
    (task.goal_packet?.status === 'pending_review' ||
      task.goal_packet?.status === 'rejected')
  ) {
    return false
  }

  return finalAcceptanceSignal
}

/**
 * A blocking final review verdict suppresses Done regardless of any acceptance
 * signal or Goal Packet status.
 */
export function hasBlockingReviewResult(latestReviewReport: AgentReport | null): boolean {
  return (
    latestReviewReport?.state === 'review_failed' ||
    latestReviewReport?.state === 'review_needs_input'
  )
}

/** Whether the human "Done" control should be available for this task. */
export function canMarkDoneTask(
  task: WorkspaceTask,
  latestReport: AgentReport | null,
  latestReviewReport: AgentReport | null,
): boolean {
  return (
    awaitingHumanAcceptance(task, latestReport, latestReviewReport) &&
    !hasBlockingReviewResult(latestReviewReport)
  )
}
