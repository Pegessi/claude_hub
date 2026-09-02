import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.dependencies import get_current_user
from ..models import (
    SwitchEnvRequest,
    TerminalAgentStatus,
    TerminalTab,
    TerminalTabCreate,
    TerminalTabUpdate,
    User,
)
from ..services import ttyd_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tabs", tags=["tabs"])


class TabOrderUpdate(BaseModel):
    tab_ids: List[str]


@router.get("", response_model=List[TerminalTab])
async def list_tabs(current_user: User = Depends(get_current_user)) -> List[TerminalTab]:
    """List all terminal tabs."""
    return ttyd_manager.list_tabs()


@router.get("/status", response_model=List[TerminalAgentStatus])
async def list_tab_statuses(
    current_user: User = Depends(get_current_user),
) -> List[TerminalAgentStatus]:
    """List best-effort runtime statuses for terminal agents."""
    return await ttyd_manager.list_tab_agent_statuses()


@router.post("", response_model=TerminalTab, status_code=201)
async def create_tab(
    tab: TerminalTabCreate,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Create a new terminal tab."""
    logger.info(
        f"Received create_tab request: name={tab.name}, solo_mode={tab.solo_mode}, shell={tab.shell}, cwd={tab.cwd}, agent_type={tab.agent_type}, session_kind={tab.session_kind}, target={tab.target}, remote_profile_id={tab.remote_profile_id}, agent_session_id={tab.agent_session_id}, user={current_user.email}"
    )
    return await ttyd_manager.create_tab(
        name=tab.name,
        shell=tab.shell,
        cwd=tab.cwd,
        solo_mode=tab.solo_mode,
        agent_type=tab.agent_type,
        session_kind=tab.session_kind,
        chat_mode=tab.chat_mode,
        target=tab.target,
        remote_profile_id=tab.remote_profile_id,
        remote_cwd=tab.remote_cwd,
        remote_reconnect=tab.remote_reconnect,
        env=tab.env,
        agent_session_id=tab.agent_session_id,
    )


@router.put("/order")
async def update_tab_order(
    order: TabOrderUpdate,
    current_user: User = Depends(get_current_user),
) -> dict[str, bool]:
    """Update the order of tabs."""
    logger.info(f"Updating tab order: {order.tab_ids}, user={current_user.email}")
    ttyd_manager.set_tab_order(order.tab_ids)
    return {"success": True}


@router.post("/{tab_id}/duplicate", response_model=TerminalTab, status_code=201)
async def duplicate_tab(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Duplicate a terminal tab, preserving launch configuration like solo mode."""
    logger.info(f"Duplicating tab {tab_id}, user={current_user.email}")
    tab = await ttyd_manager.duplicate_tab(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    return tab


@router.post("/{tab_id}/switch-env", response_model=TerminalTab)
async def switch_tab_env(
    tab_id: str,
    req: SwitchEnvRequest,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Hot-swap environment variables and/or solo mode for a live local Claude/Codex tab.

    Rewrites the launch wrapper (and settings.json for Claude) and uses
    ``tmux respawn-pane -k`` to relaunch the agent with resume flags so
    conversation history is preserved (``--resume`` for Claude,
    ``codex resume --last`` for Codex).
    Returns 400 for non-Claude/Codex, remote, or stopped tabs.
    """
    logger.info(
        "switch_env for tab %s: %d env vars, solo_mode=%s, user=%s",
        tab_id,
        len(req.env),
        req.solo_mode,
        current_user.email,
    )
    try:
        tab = await ttyd_manager.switch_env(tab_id, req.env, solo_mode=req.solo_mode)
    except KeyError:
        raise HTTPException(status_code=404, detail="Tab not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return tab


@router.get("/{tab_id}", response_model=TerminalTab)
async def get_tab(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Get a terminal tab by ID."""
    tab = ttyd_manager.get_tab(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    return tab


@router.put("/{tab_id}", response_model=TerminalTab)
async def update_tab(
    tab_id: str,
    tab_update: TerminalTabUpdate,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Update a terminal tab."""
    tab = await ttyd_manager.update_tab(
        tab_id,
        tab_update.name,
        tab_update.shell,
        tab_update.cwd,
        tab_update.solo_mode,
        tab_update.agent_type,
        tab_update.target,
        tab_update.remote_profile_id,
        tab_update.remote_cwd,
        tab_update.remote_reconnect,
        tab_update.env,
    )
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    return tab


@router.delete("/{tab_id}", status_code=204)
async def delete_tab(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a terminal tab."""
    success = await ttyd_manager.delete_tab(tab_id)
    if not success:
        raise HTTPException(status_code=404, detail="Tab not found")
