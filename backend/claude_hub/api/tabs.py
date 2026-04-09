from fastapi import APIRouter, HTTPException, Depends
from typing import List
from pydantic import BaseModel
import logging

from ..models import TerminalTab, TerminalTabCreate, TerminalTabUpdate, User
from ..services import ttyd_manager
from ..auth.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tabs", tags=["tabs"])


class TabOrderUpdate(BaseModel):
    tab_ids: List[str]


@router.get("", response_model=List[TerminalTab])
async def list_tabs(current_user: User = Depends(get_current_user)):
    """List all terminal tabs."""
    return ttyd_manager.list_tabs()


@router.post("", response_model=TerminalTab, status_code=201)
async def create_tab(
    tab: TerminalTabCreate,
    current_user: User = Depends(get_current_user),
):
    """Create a new terminal tab."""
    logger.info(f"Received create_tab request: name={tab.name}, solo_mode={tab.solo_mode}, shell={tab.shell}, cwd={tab.cwd}, agent_type={tab.agent_type}, user={current_user.email}")
    return await ttyd_manager.create_tab(tab.name, tab.shell, tab.cwd, tab.solo_mode, tab.agent_type)


@router.get("/{tab_id}", response_model=TerminalTab)
async def get_tab(
    tab_id: str,
    current_user: User = Depends(get_current_user),
):
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
):
    """Update a terminal tab."""
    tab = await ttyd_manager.update_tab(tab_id, tab_update.name, tab_update.shell, tab_update.cwd, tab_update.solo_mode, tab_update.agent_type)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    return tab


@router.delete("/{tab_id}", status_code=204)
async def delete_tab(
    tab_id: str,
    current_user: User = Depends(get_current_user),
):
    """Delete a terminal tab."""
    success = await ttyd_manager.delete_tab(tab_id)
    if not success:
        raise HTTPException(status_code=404, detail="Tab not found")


@router.put("/order")
async def update_tab_order(
    order: TabOrderUpdate,
    current_user: User = Depends(get_current_user),
):
    """Update the order of tabs."""
    logger.info(f"Updating tab order: {order.tab_ids}, user={current_user.email}")
    ttyd_manager.set_tab_order(order.tab_ids)
    return {"success": True}
