"""Read-only remaining-quota panel for every provider Hub can observe.

A passive display surface: it reports what Codex and MASO last published and
why a reading is missing, so a single unreadable provider degrades to an
explained entry instead of an error status.
"""

from fastapi import APIRouter, Depends, Query

from ..auth.dependencies import get_current_user
from ..models import QuotaResponse, User
from ..services.provider_quota import read_provider_quota

router = APIRouter(prefix="/api/quota", tags=["quota"])


@router.get("", response_model=QuotaResponse)
async def get_quota(
    refresh: bool = Query(False, description="Bypass the cached reading."),
    current_user: User = Depends(get_current_user),
) -> QuotaResponse:
    """Return each provider's remaining quota, Codex first, then MASO."""
    return await read_provider_quota(refresh=refresh)
