"""Authenticated controls for the resident demand patrol."""

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError

from ..auth.dependencies import get_current_user
from ..models import User
from ..services.demand_patrol.models import PatrolRunRequest
from ..services.demand_patrol.service import demand_patrol_service

router = APIRouter(prefix="/api/patrol", tags=["patrol"])


@router.get("/status")
async def patrol_status(current_user: User = Depends(get_current_user)) -> dict[str, Any]:
    return await demand_patrol_service.status()


@router.put("/config")
async def patrol_config(
    changes: dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        config = await demand_patrol_service.configure(changes)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_context=False, include_input=False)) from exc
    return config.model_dump(mode="json")


@router.post("/run")
async def patrol_run(
    request: PatrolRunRequest,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    return await demand_patrol_service.run(
        channel=request.channel, dry_run=request.dry_run, notify=request.notify
    )
