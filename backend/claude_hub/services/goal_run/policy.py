"""Lifecycle guards for Chat Goals."""

from __future__ import annotations

from ...models.goal_run import TERMINAL_GOAL_STATUSES, GoalDispatchState, GoalRun, GoalRunStatus
from ..goal_signals import MISSING_GOAL_SIGNAL_MESSAGE


class GoalPolicyError(ValueError):
    pass


def ensure_unfinished(goal: GoalRun) -> None:
    if goal.status in TERMINAL_GOAL_STATUSES:
        raise GoalPolicyError(f"goal is already {goal.status.value}")


def can_complete_protocol_failure(goal: GoalRun) -> bool:
    """Recognize only the legacy protocol failure, not an execution failure."""
    return (
        goal.execution_owner == "hub_managed"
        and goal.status == GoalRunStatus.FAILED
        and goal.status_message == MISSING_GOAL_SIGNAL_MESSAGE
        and goal.dispatch_state == GoalDispatchState.IDLE
        and goal.current_turn_id is None
        and goal.pending_step_id is None
        and not goal.strict_review_in_progress
    )
