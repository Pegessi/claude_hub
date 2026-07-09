import hashlib
import json
import logging
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Request, Response
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


# sampled_at is regenerated on every call (the in-process cache is 0.75s TTL,
# shorter than the 5s poll interval) and churns even when every user-visible
# field is stable. Stripping it from the ETag payload lets an idle poll resolve
# to 304 instead of re-shipping the status list every 5s. last_changed_at is
# content-meaningful (it flips when the status actually transitions) and is
# kept so real state changes rotate the hash.
_VOLATILE_STATUS_FIELDS = ("sampled_at",)


def _tab_status_etag(statuses: List[TerminalAgentStatus]) -> str:
    """Compute a stable, order-independent ETag over tab-status content.

    Mirrors the shape of ``workspaces._board_etag``: dump through pydantic,
    drop volatile per-tick fields, sort the list by tab_id to neutralize
    iteration-order jitter, canonicalize with ``sort_keys``/tight separators,
    sha256, truncate to 32 hex chars, wrap in double quotes per RFC 7232.
    """
    payload = [s.model_dump(mode="json") for s in statuses]
    for item in payload:
        for field in _VOLATILE_STATUS_FIELDS:
            item.pop(field, None)
    payload.sort(key=lambda item: item.get("tab_id") or "")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f'"{digest}"'


class TabOrderUpdate(BaseModel):
    tab_ids: List[str]


@router.get("", response_model=List[TerminalTab])
async def list_tabs(current_user: User = Depends(get_current_user)) -> List[TerminalTab]:
    """List all terminal tabs."""
    return ttyd_manager.list_tabs()


@router.get("/status", response_model=List[TerminalAgentStatus])
async def list_tab_statuses(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> Any:
    """List best-effort runtime statuses for terminal agents.

    Emits a content-based ETag; a 5s poll whose ``If-None-Match`` still matches
    gets a bodyless 304 instead of re-serializing the status array over loopback.
    """
    statuses = await ttyd_manager.list_tab_agent_statuses()

    etag = _tab_status_etag(statuses)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and etag in {tag.strip() for tag in if_none_match.split(",")}:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

    return statuses


@router.post("", response_model=TerminalTab, status_code=201)
async def create_tab(
    tab: TerminalTabCreate,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Create a new terminal tab."""
    logger.info(
        f"Received create_tab request: name={tab.name}, solo_mode={tab.solo_mode}, shell={tab.shell}, cwd={tab.cwd}, agent_type={tab.agent_type}, target={tab.target}, remote_profile_id={tab.remote_profile_id}, user={current_user.email}"
    )
    return await ttyd_manager.create_tab(
        tab.name,
        tab.shell,
        tab.cwd,
        tab.solo_mode,
        tab.agent_type,
        tab.target,
        tab.remote_profile_id,
        tab.remote_cwd,
        tab.remote_reconnect,
        env=tab.env,
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
    """Hot-swap environment variables and/or solo mode for a live local Claude tab.

    Rewrites the launch wrapper/settings and uses ``tmux respawn-pane -k`` to
    relaunch Claude with ``--resume`` so conversation history is preserved.
    Returns 400 for non-Claude, remote, or stopped tabs.
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
