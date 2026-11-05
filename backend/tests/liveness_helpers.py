"""Test-driver for the same separate refresh and liveness phases as production."""

from claude_hub.models import WorkspaceTaskStatus
from claude_hub.services.workspace_manager import WorkspaceManager


async def liveness_round(
    manager: WorkspaceManager, workspace_id: str | None = None, *, refresh: bool = True
) -> int:
    if refresh:
        await manager._refresh_session_statuses(workspace_id)
    before = {t.id for t in manager.tasks.values() if t.status == WorkspaceTaskStatus.FAILED}
    for task in list(manager.tasks.values()):
        if workspace_id is None or task.workspace_id == workspace_id:
            await manager._check_task_liveness(task.id)
    return len(
        {t.id for t in manager.tasks.values() if t.status == WorkspaceTaskStatus.FAILED} - before
    )
