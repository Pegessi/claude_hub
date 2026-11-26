import asyncio

from ..runtime_isolation import resolve_runtime_home
from .controller import (
    CancelCallback,
    DispatchCallback,
    GoalRunController,
)
from .native import NativeGoalResolver
from .policy import GoalPolicyError
from .prompts import build_continuation_prompt
from .store import GoalRunStore
from .strict_review import (
    StrictReviewerCallback,
    StrictReviewerTarget,
    StrictReviewOutcome,
)

GOAL_SNAPSHOT_PATH = resolve_runtime_home() / "goal_runs.json"
goal_run_store = GoalRunStore(GOAL_SNAPSHOT_PATH)
goal_run_controller = GoalRunController(goal_run_store)
_tab_admission_locks: dict[str, asyncio.Lock] = {}


def get_goal_admission_lock(tab_id: str) -> asyncio.Lock:
    """Serialize direct Chat sends, mode changes, and Goal lifecycle admission."""
    return _tab_admission_locks.setdefault(tab_id, asyncio.Lock())


def configure_goal_dispatch(
    callback: DispatchCallback | None,
    *,
    cancel: CancelCallback | None = None,
    provider_resolver: NativeGoalResolver | None = None,
    strict_reviewer: StrictReviewerCallback | None = None,
) -> None:
    goal_run_controller.set_dispatch_callback(callback)
    goal_run_controller.set_cancel_callback(cancel)
    goal_run_controller.native.resolver = provider_resolver
    goal_run_controller.set_strict_reviewer_callback(strict_reviewer)


def get_goal_manager() -> GoalRunController:
    return goal_run_controller


__all__ = [
    "GOAL_SNAPSHOT_PATH",
    "CancelCallback",
    "GoalPolicyError",
    "GoalRunController",
    "GoalRunStore",
    "StrictReviewerCallback",
    "StrictReviewerTarget",
    "StrictReviewOutcome",
    "configure_goal_dispatch",
    "build_continuation_prompt",
    "get_goal_manager",
    "get_goal_admission_lock",
    "goal_run_controller",
]
