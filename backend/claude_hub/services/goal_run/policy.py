"""Pure state transition and budget policy for Chat Goals."""

from __future__ import annotations

from ...models.goal_run import TERMINAL_GOAL_STATUSES, GoalRun, GoalRunStatus


class GoalPolicyError(ValueError):
    pass


def ensure_unfinished(goal: GoalRun) -> None:
    if goal.status in TERMINAL_GOAL_STATUSES:
        raise GoalPolicyError(f"goal is already {goal.status.value}")


def can_continue(goal: GoalRun) -> tuple[bool, str | None]:
    if goal.turns_completed >= goal.max_turns:
        return False, f"maximum turn count ({goal.max_turns}) reached"
    if (
        goal.token_budget is not None
        and goal.token_usage is not None
        and goal.token_usage >= goal.token_budget
    ):
        return False, f"token budget ({goal.token_budget}) reached"
    return True, None
