"""Lifecycle guards for Chat Goals."""

from __future__ import annotations

from ...models.goal_run import TERMINAL_GOAL_STATUSES, GoalRun, GoalRunStatus


class GoalPolicyError(ValueError):
    pass


def ensure_unfinished(goal: GoalRun) -> None:
    if goal.status in TERMINAL_GOAL_STATUSES:
        raise GoalPolicyError(f"goal is already {goal.status.value}")
