"""Provider quota response schemas."""

from typing import List, Optional

from pydantic import BaseModel, Field


class QuotaLimit(BaseModel):
    """One rate-limit window reported by a provider.

    ``scope`` is a stable machine identifier the UI can key on across polls;
    ``label`` is the English display name. States are the provider-neutral
    vocabulary ``within|near|over|no_limit|no_data|unknown``.
    """

    scope: str
    label: str
    state: str
    used_percent: Optional[int] = None
    remaining_percent: Optional[int] = None
    resets_at: Optional[str] = None


class ProviderQuota(BaseModel):
    """One provider's reading, including why it could not be read.

    ``state`` extends the limit vocabulary with two provider-only values:
    ``unavailable`` (the provider belongs to this deployment but could not be
    read) and ``not_configured`` (the provider is absent from this deployment,
    a statement of fact rather than a fault). ``available`` is False for both
    and ``message`` then says which case applies; an actionable English
    explanation for a fault, a neutral one otherwise. ``stale`` marks a reading
    that was parsed successfully but no longer describes the current window.
    """

    provider: str
    available: bool
    state: str
    message: str = ""
    as_of: Optional[str] = None
    stale: bool = False
    limits: List[QuotaLimit] = Field(default_factory=list)
    plan: Optional[str] = None
    credit_balance: Optional[str] = None
    enforcing: Optional[bool] = None


class QuotaResponse(BaseModel):
    """Remaining-quota readings for every provider Hub can observe."""

    providers: List[ProviderQuota] = Field(default_factory=list)
