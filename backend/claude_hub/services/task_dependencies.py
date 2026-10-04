"""Task prerequisites, independent of the supervision tree and session affinity."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..models.schemas import WorkspaceTask, WorkspaceTaskStatus


class TaskHasDependentsError(ValueError):
    """Deleting a prerequisite would discard another task's durable contract."""


def validate_task_dependencies(
    tasks: Mapping[str, WorkspaceTask],
    workspace_id: str,
    task_id: str,
    dependency_ids: Sequence[str],
) -> list[str]:
    """Validate the reachable DAG without mutating it; preserve stable input order."""
    dependencies = list(dict.fromkeys(dependency_ids))
    active = {task_id}
    visited: set[str] = set()
    stack = [(item, False) for item in reversed(dependencies)]
    while stack:
        current_id, exiting = stack.pop()
        if exiting:
            active.remove(current_id)
            visited.add(current_id)
            continue
        if current_id in active:
            raise ValueError("Task dependency cycle detected")
        if current_id in visited:
            continue
        current = tasks.get(current_id)
        if current is None:
            raise ValueError(f"Task dependency not found: {current_id}")
        if current.workspace_id != workspace_id:
            raise ValueError(f"Task dependency belongs to a different workspace: {current_id}")
        active.add(current_id)
        stack.append((current_id, True))
        stack.extend((item, False) for item in reversed(current.depends_on_task_ids))
    return dependencies


def task_dependency_blockers(tasks: Mapping[str, WorkspaceTask], task: WorkspaceTask) -> list[str]:
    """Return actionable blockers, failing closed for damaged persisted edges."""
    if not task.depends_on_task_ids:
        return []
    try:
        validate_task_dependencies(tasks, task.workspace_id, task.id, task.depends_on_task_ids)
    except ValueError as exc:
        return [str(exc)]
    return [
        f"{dependency_id} ({tasks[dependency_id].status.value}; requires done)"
        for dependency_id in task.depends_on_task_ids
        if tasks[dependency_id].status != WorkspaceTaskStatus.DONE
    ]


def require_task_dependencies(tasks: Mapping[str, WorkspaceTask], task: WorkspaceTask) -> None:
    blockers = task_dependency_blockers(tasks, task)
    if blockers:
        raise ValueError("Task dependencies are not satisfied: " + "; ".join(blockers))
