"""Source-Chat-scoped durable work API."""

from fastapi import APIRouter, Depends, HTTPException

from ..auth.dependencies import get_current_user
from ..models import User
from ..models.schemas import ChatWorkCreate, ChatWorkReport, ChatWorkUpdate, ChatWorkView
from ..services.workspace_manager import workspace_manager

router = APIRouter(prefix="/api/tabs/{tab_id}/work", tags=["chat-work"])


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError) and str(exc) == "legacy_chat_work_read_only_use_workspace_tasks":
        return HTTPException(
            status_code=410, detail="legacy_chat_work_read_only_use_workspace_tasks"
        )
    return HTTPException(
        status_code=404 if isinstance(exc, KeyError) else 400,
        detail="Linked work not found" if isinstance(exc, KeyError) else str(exc),
    )


@router.get("", response_model=list[ChatWorkView])
async def list_work(
    tab_id: str, current_user: User = Depends(get_current_user)
) -> list[ChatWorkView]:
    return workspace_manager.list_chat_work(tab_id)


@router.post("", response_model=ChatWorkView, status_code=201)
async def create_work(
    tab_id: str, body: ChatWorkCreate, current_user: User = Depends(get_current_user)
) -> ChatWorkView:
    try:
        return await workspace_manager.create_chat_work(tab_id, body)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _error(exc) from None


@router.get("/{work_id}", response_model=ChatWorkView)
async def get_work(
    tab_id: str, work_id: str, current_user: User = Depends(get_current_user)
) -> ChatWorkView:
    try:
        return workspace_manager.chat_work_view(
            workspace_manager._chat_work_record(tab_id, work_id)
        )
    except KeyError as exc:
        raise _error(exc) from None


@router.patch("/{work_id}", response_model=ChatWorkView)
async def update_work(
    tab_id: str, work_id: str, body: ChatWorkUpdate, current_user: User = Depends(get_current_user)
) -> ChatWorkView:
    try:
        return await workspace_manager.update_chat_work(tab_id, work_id, body)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _error(exc) from None


@router.post("/{work_id}/report", response_model=ChatWorkView)
async def report_work(
    tab_id: str, work_id: str, body: ChatWorkReport, current_user: User = Depends(get_current_user)
) -> ChatWorkView:
    try:
        return await workspace_manager.report_chat_work(tab_id, work_id, body)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise _error(exc) from None
