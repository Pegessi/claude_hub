"""Read-only MASO readiness from the Hub host, before a Chat is created."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.dependencies import get_current_user
from ..models import User
from ..services.agent_stream.maso_http import (
    MasoServiceUnavailable,
    check_maso_service,
    maso_base_url,
)

router = APIRouter(prefix="/api/maso", tags=["maso"])


class MasoPreflightRequest(BaseModel):
    base_url: Optional[str] = None


class MasoPreflightResponse(BaseModel):
    available: bool
    base_url: str
    message: str
    model_count: int = 0


@router.post("/preflight", response_model=MasoPreflightResponse)
async def preflight_maso(
    body: MasoPreflightRequest,
    current_user: User = Depends(get_current_user),
) -> MasoPreflightResponse:
    try:
        base_url = maso_base_url({"MASO_BASE_URL": body.base_url} if body.base_url else {})
        models, _default = await check_maso_service({"MASO_BASE_URL": base_url})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except MasoServiceUnavailable as exc:
        return MasoPreflightResponse(available=False, base_url=base_url, message=str(exc))
    return MasoPreflightResponse(
        available=True,
        base_url=base_url,
        message="MASO service is ready.",
        model_count=len(models),
    )
