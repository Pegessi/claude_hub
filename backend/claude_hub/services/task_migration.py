"""Pre-unification state.json migration: Task parent inherit + ACK cursors."""

from __future__ import annotations

import logging
from typing import Dict, Iterable, Optional

from ..models import Workspace, WorkspaceTask
from ..models.agent_tree import AgentRun, ExecutorKind
from .task_graph import validate_parent_task

logger = logging.getLogger(__name__)

LEGACY_RESIDENT_CONSUMER_TEMPLATE = "workspace:{workspace_id}:resident"


def legacy_resident_consumer_key(workspace_id: str) -> str:
    """Load/migration-only resident consumer key. Not a runtime mailbox identity."""

    if not workspace_id:
        raise ValueError("Legacy resident consumer key requires a workspace id")
    return LEGACY_RESIDENT_CONSUMER_TEMPLATE.format(workspace_id=workspace_id)


def is_legacy_resident_consumer_key(consumer_key: str, workspace_id: str) -> bool:
    """True for deprecated ``workspace:{id}:resident`` keys (load-only compat)."""

    return consumer_key == legacy_resident_consumer_key(workspace_id)


def linked_run_for_task(
    task: WorkspaceTask,
    runs: Dict[str, AgentRun],
) -> Optional[AgentRun]:
    """Link by ``Task.agent_run_id``, else unique same-workspace ``context_ref``."""

    if task.agent_run_id:
        run = runs.get(task.agent_run_id)
        if run is None or run.workspace_id != task.workspace_id:
            return None
        return run
    matches = [
        run
        for run in runs.values()
        if run.workspace_id == task.workspace_id and run.context_ref == task.id
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def task_for_run(
    run: AgentRun,
    tasks: Dict[str, WorkspaceTask],
    workspace_id: str,
) -> Optional[WorkspaceTask]:
    """Resolve a Task for a run. Ambiguous links fail closed."""

    linked = [
        task
        for task in tasks.values()
        if task.workspace_id == workspace_id and task.agent_run_id == run.id
    ]
    if len(linked) == 1:
        return linked[0]
    if len(linked) > 1:
        return None
    if not run.context_ref:
        return None
    hinted = [
        task
        for task in tasks.values()
        if task.workspace_id == workspace_id
        and task.agent_run_id is None
        and task.id == run.context_ref
    ]
    if len(hinted) == 1:
        return hinted[0]
    return None


def migrate_pre_unification_graph(
    *,
    tasks: Dict[str, WorkspaceTask],
    runs: Dict[str, AgentRun],
    workspace: Workspace,
    missing_parent_ids: Iterable[str],
    missing_ack_ids: Iterable[str] = (),
    missing_resident_ack: bool = False,
    legacy_resident_ack: int = 0,
) -> Workspace:
    """Inherit missing parents from AgentRun and lift ACK cursors.

    Never mutates ``runs``. Only Tasks whose raw JSON omitted
    ``parent_task_id`` may inherit. Explicit ``null`` stays a root.
    ``related_task_id`` is not consulted.

    ACK lift is one-shot for pre-unification JSON: a Task whose raw blob
    omitted ``consumer_ack_sequence`` may take ``max(0, run.ack_sequence)``.
    Once the Task-owned cursor is persisted, later loads must not re-lift
    from leftover AgentRun.ack_sequence.

    Resident lift applies only when the workspace index JSON omitted
    ``resident_ack_sequence``. An explicit ``resident_ack_sequence`` (including
    ``0``) is authoritative and must not be overwritten from
    ``RESIDENT_ROOT.ack_sequence`` on load.
    """

    workspace_id = workspace.id
    missing = {task_id for task_id in missing_parent_ids}
    for task in list(tasks.values()):
        if task.workspace_id != workspace_id or task.id not in missing:
            continue
        run = linked_run_for_task(task, runs)
        if run is None or not run.parent_id:
            continue
        parent_run = runs.get(run.parent_id)
        if parent_run is None or parent_run.workspace_id != workspace_id:
            logger.warning(
                "Skipping inherited parent task_id=%s: parent run missing or cross-workspace",
                task.id,
            )
            continue
        if parent_run.executor_kind == ExecutorKind.RESIDENT_ROOT:
            continue
        parent_task = task_for_run(parent_run, tasks, workspace_id)
        if parent_task is None or parent_task.id == task.id:
            logger.warning(
                "Skipping inherited parent task_id=%s: ambiguous or self parent",
                task.id,
            )
            continue
        tasks[task.id] = task.model_copy(update={"parent_task_id": parent_task.id})

    for task in list(tasks.values()):
        if task.workspace_id != workspace_id:
            continue
        validate_parent_task(tasks, workspace_id, task.id, task.parent_task_id)

    for run in runs.values():
        if run.workspace_id != workspace_id:
            continue
        if run.executor_kind != ExecutorKind.MANAGED_TASK or not run.context_ref:
            continue
        if any(
            task.workspace_id == workspace_id and task.agent_run_id == run.id
            for task in tasks.values()
        ):
            continue
        hinted = tasks.get(run.context_ref)
        if hinted is None or hinted.workspace_id != workspace_id:
            continue
        if hinted.agent_run_id is not None:
            continue
        tasks[hinted.id] = hinted.model_copy(update={"agent_run_id": run.id})

    missing_acks = {task_id for task_id in missing_ack_ids}
    lifted_resident_ack = int(legacy_resident_ack)
    for run in runs.values():
        if run.workspace_id != workspace_id:
            continue
        if run.executor_kind == ExecutorKind.RESIDENT_ROOT:
            if missing_resident_ack:
                lifted_resident_ack = max(lifted_resident_ack, int(run.ack_sequence))
            continue
        if run.executor_kind != ExecutorKind.MANAGED_TASK:
            continue
        linked_task = task_for_run(run, tasks, workspace_id)
        if linked_task is None or linked_task.id not in missing_acks:
            continue
        live = tasks[linked_task.id]
        tasks[linked_task.id] = live.model_copy(
            update={
                "consumer_ack_sequence": max(int(live.consumer_ack_sequence), int(run.ack_sequence))
            }
        )

    if lifted_resident_ack > 0:
        from .task_graph import top_level_tasks

        for root in top_level_tasks(tasks.values(), workspace_id):
            lifted = max(int(root.consumer_ack_sequence), lifted_resident_ack)
            if lifted != root.consumer_ack_sequence:
                tasks[root.id] = root.model_copy(update={"consumer_ack_sequence": lifted})

    return workspace
