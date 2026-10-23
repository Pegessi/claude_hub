"""The single writer for Task status.

Every change to ``WorkspaceTask.status`` goes through ``apply_task_update``:
callers still decide *when* and store the result, but the allowed edges live in
one table and each change names its cause. Unexpected edges are logged in
production and raise under ``CLAUDE_HUB_STRICT_TASK_TRANSITIONS=1`` (set for
the test suite), so a new writer cannot silently invent a transition.
"""

import logging
import os
from typing import Any

from ..models import WorkspaceTask, WorkspaceTaskStatus

logger = logging.getLogger(__name__)

STRICT_ENV = "CLAUDE_HUB_STRICT_TASK_TRANSITIONS"

TODO = WorkspaceTaskStatus.TODO
QUEUED = WorkspaceTaskStatus.QUEUED
WORKING = WorkspaceTaskStatus.WORKING
REVIEW = WorkspaceTaskStatus.REVIEW
DONE = WorkspaceTaskStatus.DONE
FAILED = WorkspaceTaskStatus.FAILED

# Causes that may move a Task between any two statuses: an explicit human
# edit, and load-time repair of persisted records.
UNRESTRICTED_CAUSES = frozenset({"manual", "load"})

# (from, to) -> causes that may take that edge.
TRANSITIONS: dict[tuple[WorkspaceTaskStatus, WorkspaceTaskStatus], frozenset[str]] = {
    # Admission and dispatch.
    (TODO, QUEUED): frozenset({"start", "scheduled"}),
    (FAILED, QUEUED): frozenset({"start"}),
    (REVIEW, QUEUED): frozenset({"start", "dispatch_decision"}),
    (QUEUED, QUEUED): frozenset(),
    (QUEUED, WORKING): frozenset({"dispatch"}),
    # Direct dispatch of a never-queued record (internal callers and recovery).
    # Workers may report on a record that was never formally started.
    (TODO, WORKING): frozenset({"initiator_progress", "dispatch", "report"}),
    (TODO, REVIEW): frozenset({"report", "review_request", "review_verdict"}),
    # A scheduled Task whose worker died is redispatched to a fresh worker.
    (WORKING, QUEUED): frozenset({"scheduled"}),
    # Work produces results.
    # A reviewer verdict can land while the worker is still marked working
    # (e.g. a Goal Packet verdict), which parks the round in review.
    (WORKING, REVIEW): frozenset(
        {"report", "review_request", "review_verdict", "reconcile", "prompt_stalled"}
    ),
    (QUEUED, REVIEW): frozenset({"report", "review_request", "reconcile", "prompt_stalled"}),
    (REVIEW, REVIEW): frozenset(),
    (REVIEW, WORKING): frozenset(
        {"continue", "report", "review_verdict", "review_request", "prompt_stalled"}
    ),
    (FAILED, WORKING): frozenset({"continue", "initiator_progress", "report"}),
    (FAILED, REVIEW): frozenset({"report", "review_request", "review_verdict"}),
    (WORKING, WORKING): frozenset(),
    # Completion.
    # An aborted Feedback Reaper summary is closed rather than requeued.
    (REVIEW, DONE): frozenset({"accept", "internal_report", "feedback_reaper", "abort"}),
    (WORKING, DONE): frozenset(
        {"initiator_progress", "internal_report", "feedback_reaper", "abort"}
    ),
    (TODO, DONE): frozenset({"initiator_progress", "internal_report", "feedback_reaper"}),
    (QUEUED, DONE): frozenset({"internal_report", "feedback_reaper", "abort"}),
    # Failure.
    (WORKING, FAILED): frozenset({"report", "initiator_progress", "runtime_failure", "scheduled"}),
    (QUEUED, FAILED): frozenset({"scheduled", "runtime_failure"}),
    (TODO, FAILED): frozenset({"scheduled", "initiator_progress", "report"}),
    (REVIEW, FAILED): frozenset({"report", "runtime_failure"}),
    # Back to an unowned record.
    (QUEUED, TODO): frozenset({"abort", "handoff", "feedback_reaper"}),
    (WORKING, TODO): frozenset({"abort", "handoff", "feedback_reaper"}),
    (REVIEW, TODO): frozenset({"abort", "handoff", "feedback_reaper"}),
    (FAILED, TODO): frozenset({"abort", "handoff", "feedback_reaper"}),
    # Handoff may reopen a closed record for a new executor.
    (DONE, TODO): frozenset({"feedback_reaper", "handoff"}),
}


class TaskTransitionError(RuntimeError):
    """A status change that the transition table does not allow."""


def transition_allowed(
    current: WorkspaceTaskStatus, target: WorkspaceTaskStatus, cause: str
) -> bool:
    if current == target or cause in UNRESTRICTED_CAUSES:
        return True
    return cause in TRANSITIONS.get((current, target), frozenset())


def apply_task_update(task: WorkspaceTask, update: dict[str, Any], *, cause: str) -> WorkspaceTask:
    """Return ``task`` with ``update`` applied, checking any status change."""

    target = update.get("status", task.status)
    if not transition_allowed(task.status, target, cause):
        message = (
            f"Unexpected Task transition {task.status.value}->{target.value} "
            f"cause={cause} task_id={task.id}"
        )
        if os.environ.get(STRICT_ENV) == "1":
            raise TaskTransitionError(message)
        logger.warning(message)
    return task.model_copy(update=update)
