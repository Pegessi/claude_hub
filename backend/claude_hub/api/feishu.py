"""Feishu card-action result endpoints (Scenario A bridge).

These endpoints back the ``claude-hub feishu`` CLI's wait/collect loop:

* ``POST /api/feishu/cards/register`` -- the CLI registers an opaque token as
  pending right before it pushes a card to a human.
* ``GET  /api/feishu/cards/result/{token}`` -- the CLI polls for the human's
  decision (status ``pending`` until answered, then ``resolved``).
* ``POST /api/feishu/cards/result`` -- the long-connection bot posts the human's
  decision (from a ``card.action.trigger`` callback), keyed by the same token.

Access is gated by :func:`get_current_user`, which already bypasses auth for
loopback / local-network callers. Both the CLI and the co-located bot run on the
same host as the backend, so in the default single-host deployment the opaque,
unguessable token is the capability and local-only access is the perimeter. When
auth is enabled, remote callers must additionally present a valid session.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth.dependencies import get_current_user
from ..models import User
from ..services.feishu_card_results import card_result_store

router = APIRouter(prefix="/api/feishu", tags=["feishu"])


class CardRegisterRequest(BaseModel):
    token: str = Field(..., min_length=8, description="Opaque correlation token.")
    chat_id: Optional[str] = None
    kind: Optional[str] = None


class CardSubmitRequest(BaseModel):
    token: str = Field(..., min_length=8, description="Opaque correlation token.")
    action: Optional[str] = Field(None, description="Decision key, e.g. approve/reject/submit.")
    form: Dict[str, Any] = Field(default_factory=dict, description="Form field values, if any.")
    operator_id: Optional[str] = Field(None, description="Feishu open_id of the operator.")


class CardActionResult(BaseModel):
    token: str
    status: str
    action: Optional[str] = None
    form: Dict[str, Any] = Field(default_factory=dict)
    operator_id: Optional[str] = None
    chat_id: Optional[str] = None
    kind: Optional[str] = None
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None


@router.post("/cards/register", response_model=CardActionResult)
async def register_card(
    payload: CardRegisterRequest,
    current_user: User = Depends(get_current_user),
) -> CardActionResult:
    """Register a token as pending before a card is pushed to a human."""
    card_result_store.register(payload.token, chat_id=payload.chat_id, kind=payload.kind)
    return CardActionResult(**card_result_store.get(payload.token))


@router.post("/cards/result", response_model=CardActionResult)
async def submit_card_result(
    payload: CardSubmitRequest,
    current_user: User = Depends(get_current_user),
) -> CardActionResult:
    """Record a human's card decision (posted by the bot callback)."""
    stored = card_result_store.submit(
        payload.token,
        action=payload.action,
        form=payload.form,
        operator_id=payload.operator_id,
    )
    if not stored:
        # Unknown/expired token, or a decision already landed for it.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="token is unknown, expired, or already resolved",
        )
    return CardActionResult(**card_result_store.get(payload.token))


@router.get("/cards/result/{token}", response_model=CardActionResult)
async def get_card_result(
    token: str,
    current_user: User = Depends(get_current_user),
) -> CardActionResult:
    """Return the current status/decision for a token (CLI polls this)."""
    return CardActionResult(**card_result_store.get(token))
