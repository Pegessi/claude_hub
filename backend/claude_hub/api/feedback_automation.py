"""Small controls for bounded background feedback and explicit Chat evidence."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth.dependencies import get_current_user
from ..models import ExecutionTarget, SessionKind, User
from ..services import ttyd_manager, workspace_manager
from ..services.feedback_automation import (
    ChatCorrection,
    ChatCorrectionCreate,
    FeedbackAutomationSettings,
    FeedbackAutomationStore,
)
from ..services.workspace_identity import validate_local_agent_cwd_for_workspace
from ..services.workspace_manager import _now

router = APIRouter(prefix="/api/workspaces", tags=["feedback"])


def _workspace(workspace_id: str) -> Any:
    workspace = workspace_manager.workspaces.get(workspace_id)
    if workspace is None:
        raise HTTPException(404, "Workspace not found")
    return workspace


def _store() -> FeedbackAutomationStore:
    return FeedbackAutomationStore(workspace_manager._feedback_store().state_root)


def _chat_tab(tab_id: str) -> Any:
    tab = ttyd_manager.get_tab(tab_id)
    if tab is None or tab.session_kind != SessionKind.CHAT:
        raise HTTPException(404, "Chat tab not found")
    return tab


@router.get("/{workspace_id}/feedback/automation")
async def feedback_status(
    workspace_id: str, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    _workspace(workspace_id)
    return workspace_manager.feedback_automation_status(workspace_id)


@router.put("/{workspace_id}/feedback/automation")
async def feedback_configure(
    workspace_id: str,
    payload: FeedbackAutomationSettings,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _workspace(workspace_id)
    return workspace_manager.configure_feedback_automation(workspace_id, payload)


@router.get("/{workspace_id}/feedback/context")
async def feedback_context(
    workspace_id: str,
    query: str = Query(default="", max_length=2048),
    limit: int = Query(default=5, ge=1, le=10),
    current_user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    _workspace(workspace_id)
    return workspace_manager._feedback_store().lesson_context_payload(
        workspace_id, query, limit=limit
    )


@router.get("/tabs/{tab_id}/feedback/sources")
async def feedback_sources(
    tab_id: str,
    limit: int = Query(default=10, ge=1, le=20),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _chat_tab(tab_id)
    try:
        sources = _store().sources(tab_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"sources": sources, "scope": "recent_user_messages_only", "max_scan_bytes": 262144}


@router.post("/{workspace_id}/feedback/corrections", response_model=ChatCorrection)
async def feedback_capture_correction(
    workspace_id: str,
    payload: ChatCorrectionCreate,
    current_user: User = Depends(get_current_user),
) -> ChatCorrection:
    workspace = _workspace(workspace_id)
    tab = _chat_tab(payload.tab_id)
    try:
        if workspace.target != tab.target:
            raise ValueError("Chat and workspace execution targets differ")
        if workspace.target == ExecutionTarget.LOCAL:
            # Do not infer a cwd for unrelated/legacy tabs; source association
            # needs a verified boundary, including linked Git worktrees.
            if not tab.cwd:
                raise ValueError("Chat tab requires an explicit cwd for workspace feedback")
            validate_local_agent_cwd_for_workspace(workspace.path, tab.cwd)
        elif (
            workspace.remote_profile_id != tab.remote_profile_id
            or workspace.remote_cwd != tab.remote_cwd
        ):
            raise ValueError("Chat and workspace remote identities differ")
        workspace_manager.feedback_automation_status(workspace_id)
        return _store().capture(workspace_id, payload, _now())
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
