from ..runtime_isolation import resolve_runtime_home
from .controller import (
    CancelCallback,
    DispatchCallback,
    GoalRunController,
    build_continuation_prompt,
)
from .policy import GoalPolicyError
from .store import GoalRunStore

GOAL_SNAPSHOT_PATH = resolve_runtime_home() / "goal_runs.json"
goal_run_store = GoalRunStore(GOAL_SNAPSHOT_PATH)
goal_run_controller = GoalRunController(goal_run_store)


def configure_goal_dispatch(
    callback: DispatchCallback | None, *, cancel: CancelCallback | None = None
) -> None:
    goal_run_controller.set_dispatch_callback(callback)
    goal_run_controller.set_cancel_callback(cancel)


def get_goal_manager() -> GoalRunController:
    return goal_run_controller


__all__ = [
    "GOAL_SNAPSHOT_PATH",
    "CancelCallback",
    "GoalPolicyError",
    "GoalRunController",
    "GoalRunStore",
    "configure_goal_dispatch",
    "build_continuation_prompt",
    "get_goal_manager",
    "goal_run_controller",
]
