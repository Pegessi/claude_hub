"""REST API for scheduled tasks.

All endpoints are authenticated and return / accept JSON. Scheduled tasks
fire an action on a cron / interval / one-off basis; see
``claude_hub.models.ScheduledTask`` for the three kinds and their payloads.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth.dependencies import get_current_user
from ..models import (
    ScheduledTask,
    ScheduledTaskCreate,
    ScheduledTaskRunResult,
    ScheduledTaskUpdate,
    User,
)
from ..services.workspace_manager import workspace_manager

router = APIRouter(prefix="/api/scheduled-tasks", tags=["scheduled-tasks"])


@router.get("", response_model=list[ScheduledTask])
async def list_scheduled_tasks(
    current_user: User = Depends(get_current_user),
) -> list[ScheduledTask]:
    """Return all scheduled tasks."""
    return workspace_manager.list_scheduled_tasks()


@router.post("", response_model=ScheduledTask, status_code=status.HTTP_201_CREATED)
async def create_scheduled_task(
    body: ScheduledTaskCreate,
    current_user: User = Depends(get_current_user),
) -> ScheduledTask:
    """Create a new scheduled task."""
    try:
        return workspace_manager.create_scheduled_task(body)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.get("/{task_id}", response_model=ScheduledTask)
async def get_scheduled_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> ScheduledTask:
    """Return a single scheduled task by id."""
    try:
        return workspace_manager.get_scheduled_task(task_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled task '{task_id}' not found",
        ) from None


@router.patch("/{task_id}", response_model=ScheduledTask)
async def update_scheduled_task(
    task_id: str,
    body: ScheduledTaskUpdate,
    current_user: User = Depends(get_current_user),
) -> ScheduledTask:
    """Update fields of an existing scheduled task.

    ``kind`` is immutable; delete and recreate to change it. When any schedule
    field (``run_at`` / ``cron`` / ``interval_seconds``) is supplied the
    next-run time is recomputed.
    """
    try:
        return workspace_manager.update_scheduled_task(task_id, body)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled task '{task_id}' not found",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


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

    This does not change the task's schedule or enable/disable state; it only
    triggers one immediate execution and records the result.
    """
    try:
        task = await workspace_manager.run_scheduled_task(task_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled task '{task_id}' not found",
        ) from None
    return ScheduledTaskRunResult(
        id=task.id,
        last_run_at=task.last_run_at,
        last_status=task.last_status,
        last_error=task.last_error,
    )
