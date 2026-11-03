"""Read-only remaining-quota panel for every provider Hub can observe.

A passive display surface: it reports what Codex, MASO, and Antigravity last
published and why a reading is missing, so a single unreadable provider
degrades to an explained entry instead of an error status.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..auth.dependencies import get_current_user
from ..models import QuotaResponse, User
from ..services.provider_quota import QuotaProviderName, read_provider_quota

router = APIRouter(prefix="/api/quota", tags=["quota"])


@router.get("", response_model=QuotaResponse)
async def get_quota(
    refresh: bool = Query(False, description="Bypass the cached reading."),
    provider: Optional[QuotaProviderName] = Query(None, description="Read only this provider."),
    current_user: User = Depends(get_current_user),
) -> QuotaResponse:
    """Return requested quota without waiting on unrelated providers."""
    return await read_provider_quota(refresh=refresh, provider=provider)
