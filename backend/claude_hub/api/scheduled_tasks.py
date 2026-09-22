"""REST API for scheduled tasks.

All endpoints are authenticated and return / accept JSON. Scheduled tasks
fire an action on a cron / interval / one-off basis; see
``claude_hub.models.ScheduledTask`` for the supported kinds and their payloads.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth.dependencies import get_current_user
from ..models import (
    ScheduledTaskCreate,
    ScheduledTaskRun,
    ScheduledTaskRunResult,
    ScheduledTaskUpdate,
    ScheduledTaskView,
    User,
)
from ..services.workspace_manager import workspace_manager

router = APIRouter(prefix="/api/scheduled-tasks", tags=["scheduled-tasks"])


@router.get("", response_model=list[ScheduledTaskView])
async def list_scheduled_tasks(
    current_user: User = Depends(get_current_user),
) -> list[ScheduledTaskView]:
    """Return all scheduled tasks with live run/backlog counts."""
    return [
        workspace_manager.scheduled_task_view(task)
        for task in workspace_manager.list_scheduled_tasks()
    ]


@router.post("", response_model=ScheduledTaskView, status_code=status.HTTP_201_CREATED)
async def create_scheduled_task(
    body: ScheduledTaskCreate,
    current_user: User = Depends(get_current_user),
) -> ScheduledTaskView:
    """Create a new scheduled task."""
    try:
        task = workspace_manager.create_scheduled_task(body)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return workspace_manager.scheduled_task_view(task)


@router.get("/{task_id}", response_model=ScheduledTaskView)
async def get_scheduled_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> ScheduledTaskView:
    """Return a single scheduled task by id."""
    try:
        task = workspace_manager.get_scheduled_task(task_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled task '{task_id}' not found",
        ) from None
    return workspace_manager.scheduled_task_view(task)


@router.get("/{task_id}/runs", response_model=list[ScheduledTaskRun])
async def list_scheduled_task_runs(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> list[ScheduledTaskRun]:
    """List durable runs for a scheduled Chat automation."""
    try:
        return workspace_manager.list_scheduled_task_runs(task_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled task '{task_id}' not found",
        ) from None


@router.post("/runs/{run_id}/cancel", response_model=ScheduledTaskRun)
async def cancel_scheduled_task_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
) -> ScheduledTaskRun:
    """Cancel one wedged or queued scheduled Chat run.

    Use this to release a run stuck in queued/waiting/dispatching/running
    without a backend restart. The provider-side turn is not interrupted
    (use Chat Stop for that); the run is marked cancelled and the tab's queue
    drains to the next occurrence.
    """
    try:
        return await workspace_manager.cancel_scheduled_task_run(run_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled task run '{run_id}' not found",
        ) from None


@router.post("/{task_id}/runs/clear")
async def clear_scheduled_task_backlog(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> dict[str, int]:
    """Cancel every queued/waiting occurrence of a task.

    A currently dispatching/running occurrence is left to finish. Returns the
    number of runs cancelled.
    """
    try:
        count = await workspace_manager.cancel_pending_scheduled_task_runs(task_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled task '{task_id}' not found",
        ) from None
    return {"cancelled": count}


@router.patch("/{task_id}", response_model=ScheduledTaskView)
async def update_scheduled_task(
    task_id: str,
    body: ScheduledTaskUpdate,
    current_user: User = Depends(get_current_user),
) -> ScheduledTaskView:
    """Update fields of an existing scheduled task.

    ``kind`` is immutable; delete and recreate to change it. When any schedule
    field (``run_at`` / ``cron`` / ``interval_seconds``) is supplied the
    next-run time is recomputed.
    """
    try:
        task = workspace_manager.update_scheduled_task(task_id, body)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled task '{task_id}' not found",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return workspace_manager.scheduled_task_view(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scheduled_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a scheduled task."""
    if not workspace_manager.delete_scheduled_task(task_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled task '{task_id}' not found",
        )


@router.post("/{task_id}/run", response_model=ScheduledTaskRunResult)
async def run_scheduled_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> ScheduledTaskRunResult:
    """Fire a scheduled task immediately (manual run-now).

    Stamps and advances the schedule just like a tick fire: a one-shot task is
    disabled after firing and a recurring task's ``next_run_at`` is recomputed.
    Returns 400 if the task is disabled and 500 if the fire side-effect failed
    (the task's ``last_status`` / ``last_error`` are still persisted either way).
    """
    try:
        task = await workspace_manager.run_scheduled_task(task_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled task '{task_id}' not found",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from None
    return ScheduledTaskRunResult(
        id=task.id,
        last_run_id=task.last_run_id,
        last_run_at=task.last_run_at,
        last_status=task.last_status,
        last_error=task.last_error,
    )
