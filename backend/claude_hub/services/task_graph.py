"""Task Graph helpers: parent/root/path, cycle guards, consumer keys.

Slice 1 of the Task-centric mailbox. WorkspaceTask is the work node;
ManagedSession IDs are assignment metadata only and never key an ACK cursor.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..models import WorkspaceTask

logger = logging.getLogger(__name__)

TASK_CONSUMER_PREFIX = "task:"


class TaskHasDescendantsError(ValueError):
    """Raised when delete is attempted on a Task that still has child Tasks."""


def task_has_descendants(
    tasks: Dict[str, WorkspaceTask],
    workspace_id: str,
    task: WorkspaceTask,
) -> bool:
    """Return True if any direct or indirect child Task exists under ``task``."""

    prefix = f"{task.path}/"
    return any(
        other.workspace_id == workspace_id
        and other.id != task.id
        and other.path.startswith(prefix)
        for other in tasks.values()
    )


def compat_run_id_for_task(task: WorkspaceTask) -> str:
    """Stable compat projection id: linked run.id or the Task id itself."""

    return task.agent_run_id or task.id


def compat_path_for_task(tasks: Dict[str, WorkspaceTask], task: WorkspaceTask) -> str:
    """Walk ``parent_task_id`` and map every ancestor through the compat id."""

    parts = [compat_run_id_for_task(task)]
    parent_id = task.parent_task_id
    seen = {task.id}
    while parent_id:
        if parent_id in seen:
            raise ValueError(f"Task parent cycle involving {task.id}")
        parent = tasks.get(parent_id)
        if parent is None or parent.workspace_id != task.workspace_id:
            raise KeyError(parent_id)
        parts.append(compat_run_id_for_task(parent))
        seen.add(parent.id)
        parent_id = parent.parent_task_id
    return "/".join(reversed(parts))


def make_task_consumer_key(task_id: str) -> str:
    """Durable consumer key for a parent Task waiting on its subtree."""

    if not task_id:
        raise ValueError("Task consumer key requires a task id")
    return f"{TASK_CONSUMER_PREFIX}{task_id}"


def parent_task_consumer_key(task: WorkspaceTask) -> str:
    """Consumer identity for this Task when it supervises children."""

    return make_task_consumer_key(task.id)


def task_inbox_consumer_key(task: WorkspaceTask) -> str:
    """Events addressed to this Task (DISPATCHED / FOLLOWUP / ABORT)."""

    return make_task_consumer_key(task.id)


def task_supervisor_consumer_key(task: WorkspaceTask) -> str:
    """Events this Task sends to its supervisor (STARTED / REPORT / verdicts).

    Root tasks (no ``parent_task_id``) address their own Task consumer —
    there is no implicit Resident / workspace-level supervisor.
    """

    if task.parent_task_id:
        return make_task_consumer_key(task.parent_task_id)
    return make_task_consumer_key(task.id)


def validate_parent_task(
    tasks: Dict[str, WorkspaceTask],
    workspace_id: str,
    child_id: str,
    parent_task_id: Optional[str],
) -> None:
    """Reject missing, cross-workspace, self, or cyclic parents."""

    if parent_task_id is None:
        return
    if parent_task_id == child_id:
        raise ValueError("A task cannot be its own parent")
    parent = tasks.get(parent_task_id)
    if parent is None:
        raise KeyError(parent_task_id)
    if parent.workspace_id != workspace_id:
        raise ValueError("Parent task belongs to a different workspace")

    seen = {child_id}
    node_id: Optional[str] = parent_task_id
    while node_id is not None:
        if node_id in seen:
            raise ValueError(f"Task parent cycle involving {child_id}")
        node = tasks.get(node_id)
        if node is None:
            raise KeyError(node_id)
        if node.workspace_id != workspace_id:
            raise ValueError("Parent task belongs to a different workspace")
        seen.add(node_id)
        node_id = node.parent_task_id


def resolve_task_tree_fields(
    tasks: Dict[str, WorkspaceTask],
    workspace_id: str,
    task_id: str,
    parent_task_id: Optional[str],
) -> Tuple[Optional[str], str, str]:
    """Return ``(parent_task_id, root_task_id, path)`` after validating parent."""

    if not parent_task_id:
        return None, task_id, task_id
    validate_parent_task(tasks, workspace_id, task_id, parent_task_id)
    ancestors: List[str] = []
    node_id: Optional[str] = parent_task_id
    while node_id is not None:
        node = tasks[node_id]
        ancestors.append(node_id)
        node_id = node.parent_task_id
    root_id = ancestors[-1]
    path = "/".join([*reversed(ancestors), task_id])
    return parent_task_id, root_id, path


def top_level_tasks(
    tasks: Iterable[WorkspaceTask],
    workspace_id: str,
) -> List[WorkspaceTask]:
    return [
        task for task in tasks if task.workspace_id == workspace_id and task.parent_task_id is None
    ]


def tasks_in_subtree(
    tasks: Iterable[WorkspaceTask],
    workspace_id: str,
    root: WorkspaceTask,
) -> List[WorkspaceTask]:
    prefix = f"{root.path}/"
    return [
        task
        for task in tasks
        if task.workspace_id == workspace_id
        and (task.id == root.id or task.path.startswith(prefix))
    ]


def reparent_task(
    tasks: Dict[str, WorkspaceTask],
    task: WorkspaceTask,
    new_parent_id: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    """Stage parent/root/path updates for ``task`` and every descendant.

    Pure: reads ``tasks`` and returns ``{task_id: field_updates}`` without
    mutating the live map. The caller must apply the current task plus
    descendants together and persist once.
    """

    parent_id, root_id, new_path = resolve_task_tree_fields(
        tasks, task.workspace_id, task.id, new_parent_id
    )
    old_path = task.path or task.id
    staged: Dict[str, Dict[str, Any]] = {
        task.id: {
            "parent_task_id": parent_id,
            "root_task_id": root_id,
            "path": new_path,
        }
    }
    for other in tasks.values():
        if other.id == task.id or other.workspace_id != task.workspace_id:
            continue
        if other.path == old_path or other.path.startswith(f"{old_path}/"):
            suffix = other.path[len(old_path) :]
            staged[other.id] = {
                "path": f"{new_path}{suffix}",
                "root_task_id": root_id,
            }
    return staged


def materialize_loaded_task_graph(
    tasks: Dict[str, WorkspaceTask],
    workspace_id: str,
) -> None:
    """Fill root/path on load. Illegal persisted parents are detached to root."""

    for task in list(tasks.values()):
        if task.workspace_id != workspace_id:
            continue
        try:
            parent_id, root_id, path = resolve_task_tree_fields(
                tasks, workspace_id, task.id, task.parent_task_id
            )
        except ValueError as exc:
            message = str(exc).lower()
            if "cycle" in message or "own parent" in message:
                raise
            logger.warning(
                "Detaching illegal task parent on load task_id=%s parent_task_id=%s: %s",
                task.id,
                task.parent_task_id,
                exc,
            )
            parent_id, root_id, path = None, task.id, task.id
        except KeyError as exc:
            logger.warning(
                "Detaching illegal task parent on load task_id=%s parent_task_id=%s: %s",
                task.id,
                task.parent_task_id,
                exc,
            )
            parent_id, root_id, path = None, task.id, task.id
        if task.parent_task_id != parent_id or task.root_task_id != root_id or task.path != path:
            tasks[task.id] = task.model_copy(
                update={
                    "parent_task_id": parent_id,
                    "root_task_id": root_id,
                    "path": path,
                }
            )
